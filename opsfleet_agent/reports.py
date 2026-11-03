"""The Saved Reports library: a local SQLite file.

Every method takes the owner and only ever touches that owner's reports, so other
people's reports are invisible to the agent whatever it asks for. Deleting moves
reports to the trash: they can be restored for TRASH_DAYS days, then are removed
for good. Every delete and restore is recorded in the deletions table.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

TRASH_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    trashed_at TEXT
);
CREATE TABLE IF NOT EXISTS deletions (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    report_ids TEXT NOT NULL,   -- JSON list
    deleted_at TEXT NOT NULL,
    restored_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReportStore:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.purge_old_trash()

    def save(self, owner: str, conversation_id: str, title: str, body: str) -> dict:
        report = {"id": uuid.uuid4().hex[:8], "title": title, "created_at": now()}
        with self.db:
            self.db.execute(
                "INSERT INTO reports (id, owner, conversation_id, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (report["id"], owner, conversation_id, title, body, report["created_at"]),
            )
        return report

    def find(self, owner: str, text: str = "", conversation_id: str | None = None) -> list[dict]:
        """The owner's reports (not trashed), optionally filtered by text and conversation."""
        sql = "SELECT id, title, created_at, conversation_id FROM reports WHERE owner = ? AND trashed_at IS NULL"
        params: list = [owner]
        if text:
            # LIKE is case-insensitive for ASCII; escape the wildcards so "50%" means a literal 50%.
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql += " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
            params += [pattern, pattern]
        if conversation_id:
            sql += " AND conversation_id = ?"
            params.append(conversation_id)
        return [dict(row) for row in self.db.execute(sql + " ORDER BY created_at", params)]

    def read(self, owner: str, report_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id, title, body, created_at FROM reports WHERE owner = ? AND id = ? AND trashed_at IS NULL",
            (owner, report_id),
        ).fetchone()
        return dict(row) if row else None

    def trash(self, owner: str, conversation_id: str, report_ids: list[str]) -> int:
        """Move the owner's reports to the trash. Returns how many were moved."""
        stamp = now()
        marks = ",".join("?" * len(report_ids))
        with self.db:
            moved = self.db.execute(
                f"UPDATE reports SET trashed_at = ? WHERE owner = ? AND trashed_at IS NULL AND id IN ({marks})",
                [stamp, owner, *report_ids],
            ).rowcount
            self.db.execute(
                "INSERT INTO deletions (id, owner, conversation_id, report_ids, deleted_at) VALUES (?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:8], owner, conversation_id, json.dumps(report_ids), stamp),
            )
        return moved

    def undo_last_delete(self, owner: str) -> list[str]:
        """Restore the owner's most recent delete that is still in the trash. Returns the restored titles."""
        last = self.db.execute(
            "SELECT id, report_ids FROM deletions WHERE owner = ? AND restored_at IS NULL "
            "ORDER BY deleted_at DESC, rowid DESC LIMIT 1",
            (owner,),
        ).fetchone()
        if not last:
            return []
        ids = json.loads(last["report_ids"])
        marks = ",".join("?" * len(ids))
        with self.db:
            self.db.execute(
                f"UPDATE reports SET trashed_at = NULL WHERE owner = ? AND id IN ({marks})", [owner, *ids]
            )
            self.db.execute("UPDATE deletions SET restored_at = ? WHERE id = ?", (now(), last["id"]))
        rows = self.db.execute(f"SELECT title FROM reports WHERE owner = ? AND id IN ({marks})", [owner, *ids])
        return [row["title"] for row in rows]

    def purge_old_trash(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")
        with self.db:
            self.db.execute("DELETE FROM reports WHERE trashed_at IS NOT NULL AND trashed_at < ?", (cutoff,))
