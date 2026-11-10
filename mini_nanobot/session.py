"""
会话、检查点和项目记忆的唯一持久化入口。旧版数据库保持原样。
对会话进行处理
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__(self, home: Path):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = home / "mini_sessions.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT, workspace TEXT,
                messages TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'idle', updated REAL
            );
            CREATE TABLE IF NOT EXISTS memory (workspace TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, payload TEXT
            );
            UPDATE sessions SET status='interrupted' WHERE status='running';
        """)
        # 仅对精简版库做增量迁移，不删除或重建已有会话。
        with self.connection:
            if "validation" not in {row[1] for row in self.connection.execute("PRAGMA table_info(sessions)")}:
                self.connection.execute("ALTER TABLE sessions ADD COLUMN validation TEXT NOT NULL DEFAULT '{}'")
        self.path.chmod(0o600)

    def create(self, workspace: Path) -> dict[str, Any]:
        identity = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions(id,title,workspace,updated) VALUES(?,?,?,?)",
                (identity, "新会话", str(workspace), time.time()),
            )
        return self.get(identity)

    def get(self, identity: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM sessions WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("会话不存在。")
        item = dict(row)
        item["messages"] = json.loads(item["messages"])
        item["validation"] = json.loads(item["validation"])
        return item

    def list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,title,workspace,status,updated FROM sessions ORDER BY updated DESC",
        )]

    def save(self, identity: str, messages: list[dict[str, Any]], summary: str, status: str) -> None:
        user = next((row["content"] for row in messages if row["role"] == "user"), "新会话")
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET messages=?,summary=?,status=?,title=?,updated=? WHERE id=?",
                (json.dumps(messages, ensure_ascii=False), summary, status, str(user)[:35], time.time(), identity),
            )

    def record(self, identity: str, event: dict[str, Any]) -> dict[str, Any]:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO events(session_id,payload) VALUES(?,?)",
                (identity, json.dumps(event, ensure_ascii=False)),
            )
        return {**event, "event_id": cursor.lastrowid}

    def save_validation(self, identity: str, result: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute("UPDATE sessions SET validation=? WHERE id=?",
                                    (json.dumps(result, ensure_ascii=False), identity))

    def events(self, identity: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,payload FROM events WHERE session_id=? ORDER BY id DESC LIMIT 100", (identity,),
        ).fetchall()
        return [{**json.loads(row["payload"]), "event_id": row["id"]} for row in reversed(rows)]

    def read_memory(self, workspace: Path) -> str:
        row = self.connection.execute("SELECT content FROM memory WHERE workspace=?", (str(workspace),)).fetchone()
        return row[0] if row else ""

    def write_memory(self, workspace: Path, content: str) -> None:
        if len(content) > 12000:
            raise ValueError("项目记忆最多 12000 个字符。")
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO memory VALUES(?,?)", (str(workspace), content))

    def close(self) -> None:
        self.connection.close()
