# mini_nanobot：中文 AI 编程助手

这是在 nanobot 基础上重新整理的精简学习版，聚焦代码阅读、文件修改、命令验证、多 agent、会话和项目记忆。界面、提示词、主要注释和学习文档使用中文；标准协议字段和函数名保留英文。

## 在 VS Code 中运行

本机已安装依赖并配置好 DeepSeek。打开本目录，在“运行和调试”中选择 **mini_nanobot：完整前后端**，按 **F5**。

也可以在 VS Code 新建终端，执行：

```bash
.venv/bin/python -m mini_nanobot web
```

使用终端输出的本机登录链接。默认页面地址为 `http://127.0.0.1:8765`；链接中的访问码只用于本机页面登录，不是模型密钥。保持终端开启，Ctrl+C 会停止服务、子任务和正在执行的命令。

前端热更新：

```bash
.venv/bin/python -m mini_nanobot web --dev
```

终端编程：

```bash
.venv/bin/python -m mini_nanobot code -w /你的代码目录
.venv/bin/python -m mini_nanobot code -w /你的代码目录 -m '探索项目，修复一个明确的问题并验证'
```

终端会输出会话 ID。使用 `code --session 会话ID` 续接；交互模式输入“退出”结束。同一数据目录同时运行一个网页服务或 CLI，避免两个进程互相误判任务中断。需要独立运行时可指定 `--home /另一个数据目录`。

## 第一次安装

需要 Python 3.11+、Node.js 18+ 和 npm。推荐使用 uv：

```bash
uv sync --extra dev
cd webui
npm ci
npm run build
cd ..
cp .env.example .env
# 编辑 .env 后执行：
.venv/bin/python -m mini_nanobot init --if-missing
.venv/bin/python -m mini_nanobot web
```

`.env` 的核心配置如下，密钥不要提交到 Git：

```dotenv
MINI_NANOBOT_MODEL=deepseek-flash
MINI_NANOBOT_API_BASE=https://api.deepseek.com
MINI_NANOBOT_API_KEY=你的密钥
```

已有环境变量优先于 `.env`。默认继续读取 `~/.mini_nanobot/config.json` 中的核心配置，旧版渠道等字段不再使用。修改 `.env` 后需要重启；界面会提示模型是否已配置。

## 现在只有这条主线

```text
网页 / 终端
    ↓
agent.py：管理会话和任务
    ↓
runner.py：模型 → 工具 → 结果反馈 → 下一轮
    ├── provider.py：DeepSeek / OpenAI 兼容接口
    ├── tools/：文件、搜索、命令、记忆
    └── subagents.py：探索 / 实现 / 审查
```

| 位置 | 内容 |
| --- | --- |
| `mini_nanobot/cli.py` | 初始化、网页和终端启动入口 |
| `mini_nanobot/server.py` | 本地 HTTP API、认证和事件流 |
| `mini_nanobot/agent.py` | 主任务协调、停止和检查点 |
| `mini_nanobot/runner.py` | ReAct 执行循环 |
| `mini_nanobot/provider.py` | 模型请求、流式响应和重试 |
| `mini_nanobot/context.py` | 中文提示词、预算估算和摘要压缩 |
| `mini_nanobot/session.py` | SQLite 会话、任务事件和项目记忆 |
| `mini_nanobot/subagents.py` | 任务依赖调度、生命周期和并发控制 |
| `mini_nanobot/task_graph.py` | 任务计划校验与结构化交接协议 |
| `mini_nanobot/validation.py` | 可信验收、版本核对与有限修复 |
| `mini_nanobot/workspaces.py` | 独立副本、真实补丁、冲突与发布日志 |
| `mini_nanobot/evaluation.py`、`eval_cases.json` | 固定任务集、模式对比与评测报告 |
| `mini_nanobot/tools/` | 工具接口、显式注册、文件操作和 Shell |
| `mini_nanobot/security.py`、`sandbox.py` | 目录边界和可选系统沙箱 |
| `webui/src/` | 单页中文前端与事件流客户端 |
| `tests/` | 核心行为和权限测试 |

第一次学习从 [架构导读](docs/架构导读.md) 开始，再读 [接口与配置](docs/接口与配置.md)。

## 多 agent 与工具

主 agent 可创建 explorer（探索）、coder（实现）、reviewer（审查）三个角色。探索和审查只能读取、列目录、搜索和读取记忆；实现角色可编辑文件和执行命令。子任务不能继续创建子任务。主 agent 保留直接编程能力，简单任务无需委派。

所有角色共享模型配置，默认最多 4 个子任务同时执行。隔离模式让每个角色使用独立目录，主 agent 应明确 write_paths 文件归属，后端检测冲突并控制发布。`wait=true` 等待当前结果；`wait=false` 允许主任务继续独立工作，在最终答复前会等待并汇总子任务；修改原目录会使正在执行批次的基线过期，并阻断该批发布。`send`、`check`、`cancel` 的任务 ID 作用域是当前这次请求。

第一阶段已加入 **依赖任务图与结构化交接**。主 agent 通过 `subagent(action="plan")` 一次提交任务、前置 ID 和验收要求；后端拒绝循环、重复 ID 和不存在的前置任务。无依赖的任务可并发，等待依赖不占执行名额。前置失败、取消或标记待处理时，下游进入“前置阻断”，不调用模型。

在网页中可以直接发送：

> 请使用任务计划，先让探索 agent 定位问题；完成后由实现 agent 修复并运行针对性验证；最后让审查 agent 读取修改后的代码和前置交接，独立复核。明确每项的验收要求，最终核对实际命令记录和未解决问题。

右侧任务卡片会显示前置任务、验收要求、角色汇报、文件工具修改记录、实际命令退出码与未解决问题。交接自动传给下游。角色自报“完成”与实际执行记录分别保存，不等于机器自动验收；命令产生的文件变化不包含在文件工具修改记录中。具体协议与示例见 [接口与配置](docs/接口与配置.md#多-agent-任务计划与交接)。

保留的文件工具为 `read_file`、`list_dir`、`search`、`write_file`、`edit_file`；还有 `exec`、`read_memory`、`remember` 和 `subagent`。全部在 `tools/registry.py` 显式注册，没有插件扫描。项目技能可写成 `.mini_nanobot/skills/*.md`，按需把简短说明加入上下文。

## 数据与执行边界

精简版使用独立的 `~/.mini_nanobot/mini_sessions.sqlite3`，旧版配置、聊天数据库和工作区数据保持原样，不自动迁移或删除。新页面只显示精简版会话。会话检查点和项目记忆通过 SQLite 事务保存；子任务保留状态、依赖和交接事件，不提供完整子任务历史恢复。

上下文按文本长度估算预算，超过预算时通过模型生成摘要，保留当前任务和完整的工具调用组。估算值不是模型的实际计费 Token。中断后会提示先核实工具是否生效，不承诺“恰好执行一次”，不自动重放未知操作。

文件工具会解析符号链接并拒绝工作目录越界，也会拒绝 `.env` 与 Git 内部文件。Shell 保留基本命令过滤、目录检查、超时和进程组取消，并且不向子进程传递模型密钥。应用层检查不能隔离任意 Python/脚本代码；需要更强隔离时配置 macOS Seatbelt 或 Linux bwrap。沙箱配置后失败即停止，不会自动降级。该项目面向本机学习和开发，只绑定回环地址。

## 多 Agent 效果评测

第一阶段加入 16 项合成代码修复任务，比较单 Agent、串行多 Agent、并行多 Agent；外部验收与角色完成声明分别记录，并保存调用归属、HTTP 重试、耗时与供应商 Token 用量。

可选 `--mode auto` 让主模型先探索，再自行直接完成或委派子任务。选择依据包括修改量、依赖关系、文件归属和交接成本，默认直接处理小型任务；报告记录实际子任务数与委派工具调用。可用 `--mode single --mode auto --repeat 2` 比较自主选择的结果与开销。模式和指标说明见 [评测与追踪](docs/评测与追踪.md)。

`--mode auto_legacy` 在当前执行器上重放旧委派提示，供新旧提示对照使用。补充的业务规则任务集位于 `mini_nanobot/eval_suites/routing_checks.json`，可用 `--suite` 选取，先执行 `--self-check` 离线核对，再运行固定策略和自主选择的对照。

最终复测中，两个补充业务任务的 `auto` 模式均由主 Agent 直接修复并正常通过独立验收（2/2）；固定 `single` 正常完成 1/2，另一项代码已通过但收尾耗尽预算。默认单次输出上限为 8192，模型可看到共享调用剩余额度，截断的工具响应会停止执行。完整结果、失败记录与修复版本区别见 [评测与追踪](docs/评测与追踪.md#修复后复测与最终收口)。小样本不能证明多 Agent 提速或路由最优。

```bash
# 离线核对任务集，不调用模型
.venv/bin/python -m mini_nanobot eval --self-check --output eval_results/self_check
# 使用已配置模型跑三种模式的小样本，会产生调用费用
.venv/bin/python -m mini_nanobot eval --case clamp_and_slug --max-calls 18 --timeout 150 --output eval_results/smoke
```

每次试验使用独立工作目录和数据目录。结果输出到新目录，包含中文报告、JSON 指标、完整追踪和最终代码。Token 未返回时显示未知；小样本不代表总体效果。完整用法、指标口径与限制见 [评测与追踪](docs/评测与追踪.md)，已完成的真实调用见 [第一阶段小样本报告](docs/第一阶段小样本报告.md)。

## 后端验收与有限修复

第二阶段支持从可信配置加载验收命令，将退出码、产物哈希与角色声明分别保存。验收失败后最多两次业务修复；交接格式可限次更正，且不执行更正响应中的工具。所有角色、摘要与修复共享调用和时间预算。网页会显示后端验收、未配置检查或结果过期状态。

```bash
.venv/bin/python -m mini_nanobot eval --case clamp_and_slug --repairs 2 --report-repairs 1 --max-calls 18 --timeout 150 --output eval_results/stage2_smoke
```

普通聊天默认没有验收规则，需要在可信配置中显式加入。配置示例、子任务验收 ID、预算及版本核对见 [验收与有限修复](docs/验收与有限修复.md)，真实运行的发现与限制见 [第二阶段小样本报告](docs/第二阶段小样本报告.md)。

## 独立副本与补丁集成

第三阶段默认让子任务在独立副本中执行。后端采集真实文件差异、传递依赖产物、检测写入越界与补丁冲突，在组合验收和原目录版本核对后发布。失败或取消保留补丁证据；发布异常只回滚本批仍可确认的写入，保留后续外部修改。原仓库不执行提交、重置或索引更新。

```bash
.venv/bin/python -m mini_nanobot eval --case clamp_and_slug --isolate --repairs 2 --report-repairs 1 --max-calls 30 --timeout 180 --output eval_results/stage3_smoke
```

副本不等于系统沙箱；依赖目录、删除策略、发布日志和当前限制见 [隔离与补丁集成](docs/隔离与补丁集成.md)，真实链路验证和后续 Git 边界修复见 [第三阶段小样本报告](docs/第三阶段小样本报告.md)。

## 开发验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check mini_nanobot tests scripts
.venv/bin/python scripts/check_boundaries.py
cd webui
npm test
npm run build
```

## 来源与裁剪范围

来源为 HKUDS/nanobot，基线提交 `3e4068411275fac02fbc109926fffea855a94961`。当前已将编程主线重新组织到 `mini_nanobot/`，保留并适配了上游系统沙箱实现。移除了通用渠道、自动化、远程连接、自更新、多模型 OAuth、文档转换、原生 TUI、MCP、应用商店和复杂前端路由。精简版 API 与旧 WebUI 协议不兼容，网页和后端应一起更新。

源码遵循上游 MIT [LICENSE](LICENSE)，保留 [第三方来源声明](THIRD_PARTY_NOTICES.md)。
