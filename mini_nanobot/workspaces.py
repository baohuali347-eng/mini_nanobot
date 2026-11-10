"""项目副本与补丁集成；保留用户脏工作区，冲突和验收失败均不发布。"""

import asyncio
import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from mini_nanobot.security import IGNORED, path_in_scope, resolve_path

SKIP = IGNORED | {"eval_results", ".ruff_cache", ".mypy_cache", ".aws", ".ssh", ".codex", ".agents"}


def synchronized(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        with self._lock:
            return function(self, *args, **kwargs)
    return call


class IntegrationConflict(ValueError):
    def __init__(self, paths, reason="补丁冲突", kind="overlap"):
        self.paths = sorted(set(paths))
        self.kind = kind
        super().__init__(reason + "：" + "、".join(self.paths[:12]))


@dataclass(frozen=True)
class FileVersion:
    data: bytes
    mode: int

    def metadata(self):
        return {"sha256": hashlib.sha256(self.data).hexdigest(), "mode": self.mode, "size": len(self.data)}


def tree_hash(tree):
    return hashlib.sha256(json.dumps({name: version.metadata() for name, version in sorted(tree.items())}, sort_keys=True).encode()).hexdigest()


def child_environment():
    return {key: value for key, value in os.environ.items() if key in {
        "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMPDIR",
    }}


def capture(root: Path, *, git_inventory=False):
    """Git 仅用于读取根项目的文件清单；不提交、不改索引，不执行仓库 hook。"""
    root = root.resolve()
    names = None
    if git_inventory and (root / ".git").exists() and shutil.which("git"):
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", str(root),
             "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            env={**child_environment(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True, timeout=15, check=False,
        )
        if result.returncode:
            raise ValueError("读取 Git 文件清单失败，未创建隔离副本。")
        names = sorted(set(os.fsdecode(name) for name in result.stdout.split(b"\0") if name))
    if names is None:
        names = []

        def fail(error):
            raise error

        for directory, directories, files in os.walk(root, followlinks=False, onerror=fail):
            directories[:] = sorted(name for name in directories if name not in SKIP)
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("隔离副本不支持符号链接目录，请缩小代码目录。")
            names.extend(str((Path(directory) / name).relative_to(root)) for name in files)
            if len(names) > 10000:
                raise ValueError("隔离副本超过 10000 个文件。")
    tree, size = {}, 0
    for name in names:
        parts = Path(name).parts
        if any(part in SKIP or part == ".env" or part.startswith(".env.") for part in parts):
            continue
        path = resolve_path(root, name)
        # 不把链接带进副本，也不通过它读取外部文件。
        if (root / name).is_symlink():
            raise ValueError("隔离副本不支持符号链接文件：" + name)
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("隔离清单包含非普通文件：" + name)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o7000:
            raise ValueError("隔离副本不支持特殊权限位：" + name)
        with path.open("rb") as stream:
            data = stream.read(5_000_001)
        size += len(data)
        if len(data) > 5_000_000 or size > 100_000_000 or len(tree) >= 10000:
            raise ValueError("隔离项目过大，请缩小代码目录。")
        tree[name] = FileVersion(data, mode)
    return tree


def materialize(root, tree):
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, version in tree.items():
        path = resolve_path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(version.data)
        path.chmod(version.mode)


async def finish_io(function, *args):
    """取消时等复制线程退出，避免清理后线程继续创建目录。"""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


class WorkspaceBundle:
    def __init__(self, workspace: Path, home: Path, write_lock: asyncio.Lock, *, allow_deletes: bool = False):
        self.workspace = workspace.resolve()
        if home.resolve().is_relative_to(self.workspace):
            raise ValueError("隔离数据目录不能位于代码目录内。")
        parent = home / "workspaces"
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = Path(tempfile.mkdtemp(prefix="run-", dir=parent)).resolve()
        self.write_lock = write_lock
        self.allow_deletes = allow_deletes
        self._lock = threading.RLock()
        self.base = None
        self.patches = {}
        self.baselines = {}
        self.views = {}
        self.promoted = set()
        self.epoch = 0

    def json(self, name, value):
        target = self.directory / name
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".record-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def remember(self, tree):
        objects = self.directory / "objects"
        objects.mkdir(exist_ok=True, mode=0o700)
        for version in tree.values():
            path = objects / version.metadata()["sha256"]
            if not path.exists():
                path.write_bytes(version.data)
                path.chmod(0o600)

    @synchronized
    def ensure_base(self):
        if self.base is None:
            first = capture(self.workspace, git_inventory=True)
            if tree_hash(first) != tree_hash(capture(self.workspace, git_inventory=True)):
                raise IntegrationConflict(["工作目录"], "创建快照时文件发生变化", "stale")
            self.base = first
            self.remember(first)
            self.json("baseline-0.json", {"hash": tree_hash(first), "files": {name: value.metadata() for name, value in first.items()}})

    @synchronized
    def combine(self, ids):
        self.ensure_base()
        tree = dict(self.base)
        for identity, changes in self.patches.items():
            if identity not in ids or identity in self.promoted:
                continue
            conflicts = []
            for name, (before, after) in changes.items():
                current = tree.get(name)
                if current == after:
                    continue
                if current != before:
                    conflicts.append(name)
                elif after is None:
                    tree.pop(name, None)
                else:
                    tree[name] = after
            if conflicts:
                raise IntegrationConflict(conflicts)
        return tree

    @synchronized
    def fork(self, identity, ancestors):
        tree = self.combine(ancestors)
        path = self.directory / ("view-" + identity)
        materialize(path, tree)
        self.baselines[identity], self.views[identity] = tree, path
        return path

    @synchronized
    def collect(self, identity, scopes):
        before = self.baselines[identity]
        if self.views[identity].is_symlink():
            raise ValueError("候选根目录被替换为符号链接，未采集补丁。")
        after = capture(self.views[identity])
        changes = {name: (before.get(name), after.get(name)) for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)}
        denied = [name for name in changes if not path_in_scope(name, scopes)]
        if denied:
            raise IntegrationConflict(denied, "超出分配的写入范围", "scope")
        deleted = [name for name, (_, after) in changes.items() if after is None]
        if deleted and not self.allow_deletes:
            raise IntegrationConflict(deleted, "后端未授权隔离子任务删除原文件", "deletion")
        if len(changes) > 100:
            raise ValueError("单项补丁超过 100 个文件，未集成。")
        self.patches[identity] = changes
        self.remember({version.metadata()["sha256"]: version for pair in changes.values() for version in pair if version is not None})
        metadata, diff = [], []
        for name, (old, new) in changes.items():
            metadata.append({"path": name, "operation": "add" if old is None else "delete" if new is None else "modify",
                             "before": old.metadata() if old else None, "after": new.metadata() if new else None})
            old_text = old.data.decode("utf-8", errors="replace").splitlines(keepends=True) if old else []
            new_text = new.data.decode("utf-8", errors="replace").splitlines(keepends=True) if new else []
            diff.extend(difflib.unified_diff(old_text, new_text, fromfile="a/" + name, tofile="b/" + name))
        patch_path = self.directory / (identity + ".diff")
        patch_path.write_text("".join(diff))
        patch_path.chmod(0o600)
        result = {"status": "collected", "files": metadata, "base_hash": tree_hash(before),
                  "candidate_hash": tree_hash(after), "patch_path": str(patch_path),
                  "manifest_path": str(self.directory / (identity + ".json")),
                  "diff": "".join(diff)[:8000]}
        self.json(identity + ".json", result)
        return result

    @synchronized
    def stage(self, ids):
        tree = self.combine(ids)
        path = self.directory / ("integration-" + str(self.epoch))
        materialize(path, tree)
        return path, tree

    def _replace(self, name, version):
        path = resolve_path(self.workspace, name)
        if version is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mini-integrate-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(version.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, version.mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @synchronized
    def publish(self, tree, ids):
        current = capture(self.workspace, git_inventory=True)
        if tree_hash(current) != tree_hash(self.base):
            changed = [name for name in current.keys() | self.base.keys() if current.get(name) != self.base.get(name)]
            raise IntegrationConflict(changed, "原工作目录在任务执行期间发生变化，未覆盖新修改", "stale")
        changes = {name: (current.get(name), tree.get(name)) for name in sorted(current.keys() | tree.keys()) if current.get(name) != tree.get(name)}
        journal_name = f"journal-{self.epoch}.json"
        journal = {"status": "prepared", "files": list(changes), "before_hash": tree_hash(current), "after_hash": tree_hash(tree), "applied": [], "inflight": None}
        self.json(journal_name, journal)
        try:
            for name, (_, after) in changes.items():
                journal["inflight"] = name
                self.json(journal_name, journal)
                self._replace(name, after)
                journal["applied"].append(name)
                journal["inflight"] = None
                self.json(journal_name, journal)
            if tree_hash(capture(self.workspace, git_inventory=True)) != tree_hash(tree):
                raise ValueError("发布后文件校对不一致。")
            journal["status"] = "committed"
            self.json(journal_name, journal)
        except Exception:
            recovery = []
            possibly_written = journal["applied"] + ([journal["inflight"]] if journal["inflight"] else [])
            for name in reversed(possibly_written):
                before, after = changes[name]
                try:
                    path = resolve_path(self.workspace, name)
                    observed = FileVersion(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.is_file() and not path.is_symlink() else None
                except (ValueError, OSError):
                    recovery.append(name)
                    continue
                if observed == before:
                    continue
                if observed != after:
                    recovery.append(name)
                    continue
                try:
                    self._replace(name, before)
                except OSError:
                    recovery.append(name)
            journal.update(status="recovery_required" if recovery else "rolled_back", recovery_paths=recovery)
            self.json(journal_name, journal)
            raise
        self.promoted.update(ids)
        self.base = tree
        self.epoch += 1
        result = {"status": "integrated", "files": list(changes), "artifact_hash": tree_hash(tree), "journal": str(self.directory / journal_name)}
        try:
            self.json(f"baseline-{self.epoch}.json", {"hash": tree_hash(tree), "files": {name: value.metadata() for name, value in tree.items()}})
        except OSError:
            result["reason"] = "文件已发布，但后续基线清单归档失败，请核对发布日志。"
        return result

    @synchronized
    def cleanup(self):
        # 只删除后端自己创建的视图；补丁、对象、基线和发布日志保留用于检查。
        for path in self.directory.iterdir():
            if path.name.startswith("view-") or path.name.startswith("integration-"):
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
