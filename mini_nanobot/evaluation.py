"""离线校验任务集，或对比固定策略与主模型自主委派；结果与模型声明分别记录。"""

import asyncio
import hashlib
import json
import random
import shlex
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from statistics import mean
from typing import Any

from jsonschema import ValidationError, validate

from mini_nanobot import __version__
from mini_nanobot.config import Settings, ValidationRule
from mini_nanobot.context import DELEGATION_POLICY, build_system
from mini_nanobot.events import event_scope, redact, traced_sink
from mini_nanobot.provider import BudgetedProvider, BudgetExceeded, ModelProvider
from mini_nanobot.runner import AgentRunner
from mini_nanobot.security import resolve_path
from mini_nanobot.session import SessionStore
from mini_nanobot.subagents import SubagentManager, TaskQuota
from mini_nanobot.tools.registry import SubagentTool, build_tools
from mini_nanobot.validation import ValidationRunner, validation_loop
from mini_nanobot.workspaces import WorkspaceBundle

MODES = ("single", "serial", "parallel", "auto", "auto_legacy")
# 仅供评测在当前引擎重放旧的委派提示，保留当时的系统段落与工具说明。
LEGACY_DELEGATION_POLICY = "复杂任务可使用 subagent 分配 explorer（探索）、coder（实现）、reviewer（审查）。明确任务、文件范围 write_paths、验收条件。开启隔离时后端在独立副本采集补丁，完成声明不等于合入，必须检查 integration 结果；冲突或验收失败不发布。不要把并行任务指向同一文件。需要结果时使用 wait=true。简单任务可直接完成。"
LEGACY_SUBAGENT_DESCRIPTION = "管理子任务：plan 提交依赖计划；create 创建独立任务；check 查看；send 补充；cancel 取消。计划项含 id、role、task、depends_on、acceptance，实现任务请明确 write_paths 文件或目录范围，可用 validation 引用可信验收 ID。独立副本模式由后端采集真实补丁、检查冲突、组合验收后发布；完成声明不等于已发布。wait 默认 true。"
CASE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "category", "description", "files", "reference_files", "work_items", "validation"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}$"},
        "category": {"enum": ["single_file", "cross_file", "independent", "shared_interface"]},
        "description": {"type": "string", "minLength": 1},
        "files": {"type": "object", "minProperties": 1, "additionalProperties": {"type": "string"}},
        "reference_files": {"type": "object", "minProperties": 1, "additionalProperties": {"type": "string"}},
        "validation": {"type": "string", "minLength": 1},
        "work_items": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "files", "task"],
            "properties": {"id": {"type": "string", "pattern": "^fix_[a-z0-9_]{1,30}$"},
                           "files": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
                           "task": {"type": "string", "minLength": 1}},
        }},
    },
}


@dataclass
class EvalCase:
    id: str
    category: str
    description: str
    files: dict[str, str]
    reference_files: dict[str, str]
    work_items: list[dict[str, Any]]
    validation: str

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))

    @property
    def prompt(self) -> str:
        scope = "\n".join(f"{', '.join(item['files'])}：{item['task']}" for item in self.work_items)
        return f"{self.description}\n文件范围与要求：\n{scope}\n先读后改，按需求修复，不硬编码验收样例。请自行做针对性验证。"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_cases(path: Path | None = None) -> list[EvalCase]:
    source = path.read_text() if path else files("mini_nanobot").joinpath("eval_cases.json").read_text()
    data = json.loads(source)
    try:
        validate(data, {"type": "object", "required": ["version", "cases"], "additionalProperties": False,
                        "properties": {"version": {"const": 1}, "cases": {"type": "array", "minItems": 1, "items": CASE_SCHEMA}}})
    except ValidationError as error:
        raise ValueError(f"任务集格式错误：{error.message}") from error
    seen = set()
    for case in data["cases"]:
        if case["id"] in seen:
            raise ValueError(f"任务 ID 重复：{case['id']}")
        seen.add(case["id"])
        try:
            compile(case["validation"], "<eval-validation>", "exec")
        except SyntaxError as error:
            raise ValueError(f"任务 {case['id']} 的验收脚本存在语法错误。") from error
        if set(case["files"]) != set(case["reference_files"]):
            raise ValueError("初始代码与参考代码必须包含相同的文件。")
        for name in case["files"]:
            # 任务集为可信代码，但文件路径仍必须是普通项目内相对路径。
            if Path(name).is_absolute() or ".." in Path(name).parts or Path(name).as_posix() != name or not name.endswith(".py"):
                raise ValueError(f"任务文件路径无效：{name}")
            resolve_path(Path("/mini-eval-workspace"), name)
        owners: set[str] = set()
        ids: set[str] = set()
        for item in case["work_items"]:
            if item["id"] in ids or not set(item["files"]) <= set(case["files"]):
                raise ValueError("实现任务 ID 重复，或包含未定义文件。")
            ids.add(item["id"])
            if owners.intersection(item["files"]):
                raise ValueError("阶段一并行任务须拥有不同文件；跨文件依赖由最终验收暴露。")
            owners.update(item["files"])
        if owners != set(case["files"]):
            raise ValueError("实现任务必须覆盖全部任务文件。")
    return [EvalCase(**case) for case in data["cases"]]


def seed_workspace(workspace: Path, contents: dict[str, str]) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    for name, content in contents.items():
        target = resolve_path(workspace.resolve(), name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def case_validator(case: EvalCase, workspace: Path, settings: Settings) -> ValidationRunner:
    # 可信验收脚本不放入模型可编辑的工作区。新缓存前缀避免同秒、同大小修改误读旧 pyc。
    script = "import sys\nsys.path.insert(0, '.')\n" + case.validation + "\nprint('MINI_EVAL_ACCEPTED')\n"
    cache = str(workspace / (".mini-eval-cache-" + uuid.uuid4().hex))
    command = shlex.join([sys.executable, "-I", "-B", "-X", "pycache_prefix=" + cache, "-c", script])
    return ValidationRunner(workspace, settings, [ValidationRule("acceptance", command, timeout=15)])


async def check_case(validator: ValidationRunner, emit=None, attempt=1) -> dict:
    result = await validator.run(attempt=attempt, **({"emit": emit} if emit else {}))
    if result["status"] == "passed" and not all("MINI_EVAL_ACCEPTED" in (check.get("output") or "") for check in result["checks"]):
        result = {**result, "status": "failed", "reason": "验收脚本未执行到结束标记。"}
    return result


def acceptance_result(case: EvalCase, validation: dict) -> dict:
    check = (validation.get("checks") or [{}])[-1]
    return {**validation, "passed": validation["status"] == "passed", "exit_code": check.get("exit_code"),
            "timed_out": bool(check.get("timed_out")), "output": check.get("output") or validation.get("reason", ""),
            "validation_hash": digest(case.validation)}


async def validate_workspace(case: EvalCase, workspace: Path, settings: Settings) -> dict[str, Any]:
    return acceptance_result(case, await check_case(case_validator(case, workspace, settings)))


async def self_check(cases: list[EvalCase], output: Path, settings: Settings) -> dict[str, Any]:
    """每项必须原始代码失败、参考实现通过，防止无效任务污染分数。"""
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = []
    for case in cases:
        broken, fixed = output / case.id / "broken", output / case.id / "reference"
        seed_workspace(broken, case.files)
        seed_workspace(fixed, case.reference_files)
        original = await validate_workspace(case, broken, settings)
        reference = await validate_workspace(case, fixed, settings)
        rows.append({"case_id": case.id, "case_hash": case.fingerprint,
                     "valid": not original["passed"] and reference["passed"] and not original["timed_out"],
                     "original": original, "reference": reference})
    report = {"kind": "suite_self_check", "passed": all(row["valid"] for row in rows), "cases": rows}
    write_json(output / "self_check.json", report)
    return report


def make_plan(case: EvalCase, mode: str) -> list[dict[str, Any]]:
    plan = [{"id": "explore", "role": "explorer", "task": case.prompt + "\n只读探索，给各实现任务提供定位依据。",
             "depends_on": [], "acceptance": ["覆盖任务要求，指出文件、问题与实现建议"]}]
    previous = "explore"
    for item in case.work_items:
        plan.append({"id": item["id"], "role": "coder",
                     "task": case.prompt + f"\n你的任务：{item['task']}\n仅修改：{', '.join(item['files'])}。",
                     "depends_on": [previous if mode == "serial" else "explore"],
                     "write_paths": item["files"],
                     "acceptance": ["实现分配的需求，不覆盖其他任务文件", "执行针对性验证并如实汇报"]})
        previous = item["id"]
    plan.append({"id": "review", "role": "reviewer", "task": case.prompt + "\n只读复核修改后的代码、接口一致性与已有证据；如有缺陷使用 needs_followup。",
                 "depends_on": [item["id"] for item in case.work_items],
                 "acceptance": ["独立读取最终代码，指出具体问题或说明无发现", "区分角色声明与实际验证证据"]})
    return plan


def trace_metrics(events: list[dict[str, Any]], logical_calls: int) -> dict[str, Any]:
    requests = {event["request_id"] for event in events if event["type"] == "model" and event["state"] == "running"}
    terminal = {event["request_id"]: event for event in events
                if event["type"] == "model" and event["state"] in {"done", "error", "cancelled"}}
    measured = [event["usage"] for key, event in terminal.items() if key in requests
                and isinstance(event.get("usage"), dict) and event["usage"].get("total_tokens") is not None]
    known = sum(usage["total_tokens"] for usage in measured)
    complete_usage = len(measured) == len(requests) and (bool(requests) or logical_calls == 0)
    return {"model_calls": logical_calls, "http_requests": len(requests),
            "model_errors": sum(event["state"] == "error" for event in terminal.values()),
            "tool_calls": sum(event["type"] == "tool" and event["state"] == "running" for event in events),
            "total_tokens": known if complete_usage else None, "known_total_tokens": known,
            "usage_coverage": len(measured) / len(requests) if requests else (1.0 if not logical_calls else 0.0),
            "subagent_tool_calls": sum(event["type"] == "tool" and event["state"] == "running"
                                        and event.get("name") == "subagent" for event in events),
            "subtasks_created": len({(event.get("workflow_attempt", 1), event["task_id"])
                                     for event in events if event["type"] == "task"}),
            "compaction_calls": sum(event["type"] == "model" and event["state"] == "running"
                                    and event.get("purpose") == "compaction" for event in events)}


async def run_trial(case: EvalCase, mode: str, repeat: int, settings: Settings, output: Path,
                    max_calls: int, timeout: int, provider: ModelProvider | None = None,
                    *, max_repairs: int = 0, report_repairs: int = 0, isolated: bool = False) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("模式须为 single、serial、parallel、auto 或 auto_legacy。")
    directory = output / f"{case.id}__{mode}__r{repeat}"
    directory.mkdir(mode=0o700)
    workspace = directory / "workspace"
    seed_workspace(workspace, case.files)
    config = replace(settings, home=directory / "profile", workspace=workspace)
    store = SessionStore(config.home)
    session = store.create(workspace)
    actual_provider = provider or ModelProvider(config)
    budget = BudgetedProvider(actual_provider, max_calls)
    runner = AgentRunner(budget, config)
    validator = case_validator(case, workspace, config)
    manager = None
    quota = TaskQuota()
    events: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex
    trace_file = (directory / "trace.jsonl").open("x", encoding="utf-8")
    (directory / "trace.jsonl").chmod(0o600)
    history, summary = [], ""
    outcome, status, error = "not_submitted", "error", ""
    validation_seconds = 0.0
    started = time.perf_counter()

    async def emit(event):
        event = redact(event, config.api_key)
        if event["type"] == "integration":
            store.save_integration(session["id"], event["integration"])
        events.append(event)
        if event["type"] in {"model", "tool", "task", "error", "compaction", "validation", "repair", "report_repair", "integration"}:
            event = store.record(session["id"], event)
        trace_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        trace_file.flush()

    emit = traced_sink(emit)

    async def checkpoint(messages, current_summary):
        nonlocal history, summary
        history, summary = messages, current_summary
        store.save(session["id"], history, summary, "running")

    system = build_system(workspace, "")
    if mode == "auto_legacy":
        system = system.replace(DELEGATION_POLICY, LEGACY_DELEGATION_POLICY, 1)
    lock = asyncio.Lock()

    async def execute(attempt, feedback):
        nonlocal manager, history, outcome, status, error
        if manager:
            await manager.close()
        error = ""
        if mode != "single":
            manager = SubagentManager(runner, system,
                lambda role: build_tools(workspace, config, store, lock, role=role), emit,
                asyncio.Semaphore(1 if mode == "serial" else config.max_subagents), report_repairs=report_repairs, quota=quota,
                bundle=WorkspaceBundle(workspace, config.home, lock, allow_deletes=config.allow_subagent_deletes) if isolated else None,
                workspace_tools=lambda role, root, scopes: build_tools(root, config, store, asyncio.Lock(), role,
                                                                     write_paths=scopes, memory_workspace=workspace),
                validator_factory=lambda root: case_validator(case, root, config))
        if mode in {"single", "auto", "auto_legacy"}:
            if not history:
                history = [{"role": "user", "content": case.prompt}]
            if feedback:
                history.append({"role": "user", "content": case.prompt + "\n\n" + feedback})

            async def settle():
                return await manager.notifications(wait=True)

            tools = build_tools(workspace, config, store, lock, role="main" if manager else "coder",
                                subagent=manager.dispatch if manager else None)
            if mode == "auto_legacy":
                tools.tools["subagent"].description = LEGACY_SUBAGENT_DESCRIPTION
            report = await runner.run_reported(system, history, tools, emit, checkpoint,
                report_repairs=report_repairs, inject=manager.notifications if manager else None,
                settle=settle if manager else None)
            outcome = report["outcome"]
            status = "completed" if outcome == "completed" and not report["unresolved"] else "needs_followup"
            if manager and any(item.status != "done" for item in manager.tasks.values()):
                status = "needs_followup"
        else:
            plan = make_plan(case, mode)
            if feedback:
                for item in plan:
                    item["task"] += "\n" + feedback
            write_json(directory / ("plan.json" if attempt == 1 else f"plan_attempt{attempt}.json"), plan)
            await manager.dispatch("plan", tasks=plan)
            await manager.close()
            status = "completed" if all(item.status == "done" for item in manager.tasks.values()) else "needs_followup"
            outcome = "completed" if status == "completed" else "needs_followup"
        if manager:
            write_json(directory / f"handoffs_attempt{attempt}.json", redact([item.handoff() for item in manager.tasks.values()], config.api_key))
            failures = [item.result for item in manager.tasks.values() if item.status == "error"]
            if failures:
                status, error = "error", failures[0]
            if budget.exhausted:
                status, error = "budget_exceeded", "已达到本次试验的共享模型请求预算。"
        return status in {"completed", "needs_followup"}

    async def validate(attempt):
        nonlocal validation_seconds
        validation = await check_case(validator, emit, attempt)
        validation_seconds += validation.get("elapsed_ms", 0) / 1000
        attempts.append({"attempt": attempt, "execution_status": status, "reported_outcome": outcome, "validation": validation})
        store.save_validation(session["id"], validation)
        return validation

    try:
        with event_scope(run_id=run_id, session_id=session["id"], task_id="main", role="main",
                         parent_task_id=None, parent_span_id=None, purpose="generation", workflow_attempt=1):
            async with asyncio.timeout(timeout):
                await validation_loop(execute, validate, emit, max_repairs)
    except BudgetExceeded as exc:
        status, error = "budget_exceeded", str(exc)
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except TimeoutError:
        status, error = "timeout", "试验达到总耗时上限，已取消角色及其命令。"
    except Exception as exc:
        status, error = "error", str(exc)
    finally:
        if manager:
            await manager.close()
            write_json(directory / "handoffs.json", redact([item.handoff() for item in manager.tasks.values()], config.api_key))
        agent_elapsed = round(max(0, time.perf_counter() - started - validation_seconds), 4)
        store.save(session["id"], history, summary, "idle" if status == "completed" else "error")
        trace_file.close()
        if status == "cancelled":
            store.close()
        if provider is None:
            await actual_provider.close()

    # 所有角色退出后独立复核最终产物；中断、预算耗尽也保留其真实代码状态。
    try:
        final_validation = await check_case(validator)
        validation = acceptance_result(case, final_validation)
        store.save_validation(session["id"], final_validation)
    finally:
        store.close()
    result = {"case_id": case.id, "category": case.category, "case_hash": case.fingerprint, "session_id": session["id"],
              "mode": mode, "repeat": repeat, "run_id": run_id, "execution_status": status,
              "reported_outcome": outcome, "error": error, "agent_elapsed_seconds": agent_elapsed,
              "acceptance": validation, "success": status == "completed" and validation["passed"],
              "first_pass_success": bool(attempts and attempts[0]["execution_status"] == "completed" and attempts[0]["validation"]["status"] == "passed"),
              "attempts": attempts, "repairs_used": max(0, len(attempts) - 1),
              "integration": manager.integration if manager and isolated else {"status": "not_enabled", "files": []},
              "false_completion": outcome == "completed" and not validation["passed"],
              "metrics": {**trace_metrics(events, budget.calls),
                          "integration_conflicts": sum(event["type"] == "integration" and event["state"] == "conflict" for event in events),
                          "overlap_conflicts": sum(event["type"] == "integration" and event.get("integration", {}).get("kind") == "overlap" for event in events)},
              "artifact_hashes": final_validation.get("artifacts", {}),
              "artifacts": str(directory.relative_to(output))}
    result = redact(result, config.api_key)
    write_json(directory / "result.json", result)
    return result


def aggregate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for mode in MODES:
        samples = [row for row in results if row["mode"] == mode]
        if not samples:
            continue
        tokens = [row["metrics"]["total_tokens"] for row in samples]
        subtasks = [row["metrics"].get("subtasks_created") for row in samples]
        claims = sum(row["reported_outcome"] == "completed" for row in samples)
        false = sum(row["false_completion"] for row in samples)
        rows.append({"mode": mode, "trials": len(samples),
                     "success_rate": mean(row["success"] for row in samples),
                     "first_pass_success_rate": mean(row.get("first_pass_success", row["success"]) for row in samples),
                     "mean_repairs": mean(row.get("repairs_used", 0) for row in samples),
                     "acceptance_rate": mean(row["acceptance"]["passed"] for row in samples),
                     "false_completion_rate": false / claims if claims else None,
                     "completed_claims": claims, "false_completions": false,
                     "mean_agent_seconds": mean(row["agent_elapsed_seconds"] for row in samples),
                     "mean_model_calls": mean(row["metrics"]["model_calls"] for row in samples),
                     "mean_total_tokens": mean(tokens) if all(value is not None for value in tokens) else None,
                     "delegation_rate": mean(value > 0 for value in subtasks) if all(value is not None for value in subtasks) else None,
                     "mean_subtasks": mean(subtasks) if all(value is not None for value in subtasks) else None,
                     "usage_coverage": mean(row["metrics"]["usage_coverage"] for row in samples),
                     "execution_errors": sum(row["execution_status"] in {"error", "timeout", "budget_exceeded"} for row in samples)})
    return rows


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# 多 Agent 编程评测", "", f"模型：{report['config']['model']}；任务数：{len(report['cases'])}；每模式重复：{report['config']['repeats']}。",
             "所有失败、超时、预算耗尽均计入分母；最终成功率包含配置的有限修复，首轮成功率单独记录。", "",
             "| 模式 | 次数 | 成功率 | 首轮成功率 | 验收通过率 | 错误完成率¹ | 平均秒数 | 平均调用 | 平均 Token | 用量覆盖率 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in report["summary"]:
        tokens = "未知" if row["mean_total_tokens"] is None else f"{row['mean_total_tokens']:.0f}"
        false = "无完成声明" if row["false_completion_rate"] is None else f"{row['false_completion_rate']:.1%}"
        lines.append(f"| {row['mode']} | {row['trials']} | {row['success_rate']:.1%} | {row['first_pass_success_rate']:.1%} | {row['acceptance_rate']:.1%} | {false} | "
                     f"{row['mean_agent_seconds']:.2f} | {row['mean_model_calls']:.1f} | {tokens} | {row['usage_coverage']:.1%} |")
    lines += ["", "¹ 错误完成率 = 自报完成但验收失败的次数 / 自报完成次数。成功率要求执行正常结束且外部验收通过。",
              "Token 缺失时显示未知，JSON 另存已知部分；不是费用或计费估算。", "",
              "串行与并行采用固定探索→实现→审查 DAG。auto 给主模型普通工具和委派工具，由模型自行探索、拆分或直接完成，规划开销计入预算。",
              "auto_legacy 在相同引擎上重放旧委派提示和工具说明，其余设置相同；完整提示保存在 delegation_prompts.json。",
              "隔离模式与修复预算见 manifest；补丁未发布不计为最终代码成功。",
              "单文件任务没有实现阶段并行空间。小样本用于诊断，不能据此推断一般性能。", "", "## 每次试验", "",
              "| 任务 | 模式 | 重复 | 执行状态 | 验收 | 子任务数 | 委派工具调用 | 证据目录 |", "| --- | --- | ---: | --- | --- | ---: | ---: | --- |"]
    for row in report["results"]:
        lines.append(f"| {row['case_id']} | {row['mode']} | {row['repeat']} | {row['execution_status']} | "
                     f"{'通过' if row['acceptance']['passed'] else '失败'} | {row['metrics'].get('subtasks_created', '未知')} | "
                     f"{row['metrics'].get('subagent_tool_calls', '未知')} | [{row['artifacts']}]({row['artifacts']}/result.json) |")
    return "\n".join(lines) + "\n"


async def evaluate(cases: list[EvalCase], modes: list[str], repeats: int, settings: Settings,
                   output: Path, max_calls: int = 30, timeout: int = 180, seed: int = 42,
                   progress=print, *, max_repairs: int = 0, report_repairs: int = 0, isolated: bool = True) -> dict[str, Any]:
    if not modes or len(set(modes)) != len(modes) or not set(modes) <= set(MODES):
        raise ValueError("模式须为不重复的 single、serial、parallel、auto、auto_legacy。")
    if not 1 <= repeats <= 20 or not 1 <= max_calls <= 200 or not 1 <= timeout <= 3600:
        raise ValueError("重复次数须为 1～20，调用预算为 1～200，总超时为 1～3600 秒。")
    if not 0 <= max_repairs <= 2 or not 0 <= report_repairs <= 2:
        raise ValueError("业务修复与格式更正次数须为 0～2。")
    if not cases:
        raise ValueError("请至少选择一个任务。")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    engine = {str(path.relative_to(Path(__file__).parent)): path.read_text()
              for path in sorted(Path(__file__).parent.rglob("*.py"))}
    delegation_prompts = {
        "current": {"policy": DELEGATION_POLICY, "tool_description": SubagentTool.description},
        "legacy": {"policy": LEGACY_DELEGATION_POLICY, "tool_description": LEGACY_SUBAGENT_DESCRIPTION},
    }
    write_json(output / "delegation_prompts.json", delegation_prompts)
    report = {"kind": "live_model_evaluation", "version": __version__,
              "created_at": datetime.now(timezone.utc).isoformat(), "engine_hash": digest(engine),
              "delegation_prompt_hashes": {name: digest(value) for name, value in delegation_prompts.items()},
              "config": {"model": settings.model, "api_base": settings.api_base, "repeats": repeats,
                         "python_version": sys.version.split()[0], "platform": sys.platform,
                         "modes": modes, "max_calls_per_trial": max_calls, "timeout_seconds": timeout,
                         "max_iterations": settings.max_iterations, "context_budget": settings.context_budget,
                         "max_output_tokens": settings.max_output_tokens,
                         "max_parallel_subagents": settings.max_subagents, "stream_usage": settings.stream_usage,
                         "sandbox": settings.sandbox, "schedule_seed": seed,
                         "max_repairs": max_repairs, "report_repairs": report_repairs, "isolated_subagents": isolated},
              "cases": [{"id": case.id, "category": case.category, "hash": case.fingerprint} for case in cases]}
    write_json(output / "manifest.json", report)
    schedule = [(case, mode, repeat) for case in cases for mode in modes for repeat in range(1, repeats + 1)]
    random.Random(seed).shuffle(schedule)
    results = []
    for index, (case, mode, repeat) in enumerate(schedule, 1):
        progress(f"[{index}/{len(schedule)}] {case.id} / {mode} / 第 {repeat} 次")
        result = await run_trial(case, mode, repeat, settings, output, max_calls, timeout,
                                 max_repairs=max_repairs, report_repairs=report_repairs, isolated=isolated)
        results.append(result)
        progress(f"  执行：{result['execution_status']}，验收：{'通过' if result['acceptance']['passed'] else '失败'}")
    report.update(results=results, summary=aggregate(results),
                  summary_by_category={category: aggregate([row for row in results if row["category"] == category])
                                       for category in sorted({case.category for case in cases})})
    write_json(output / "report.json", report)
    (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
    return report
