from __future__ import annotations

from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_status (
    endpoint TEXT PRIMARY KEY,
    last_page INTEGER NOT NULL DEFAULT 1,
    last_cursor TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT NOT NULL,
    type TEXT NOT NULL,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    attachment_discovered INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_id, type)
);

CREATE TABLE IF NOT EXISTS attachments_queue (
    entity_id TEXT NOT NULL,
    type TEXT NOT NULL,
    attachment_id TEXT,
    file_name TEXT NOT NULL,
    mime_type TEXT,
    downloaded INTEGER NOT NULL DEFAULT 0,
    local_path TEXT,
    PRIMARY KEY (entity_id, type, file_name)
);
"""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get_sync_status(self, endpoint: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM sync_status WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()

    def upsert_sync_status(
        self,
        endpoint: str,
        *,
        last_page: int | None = None,
        last_cursor: str | None = None,
        completed: bool | None = None,
        last_error: str | None = None,
    ) -> None:
        current = self.get_sync_status(endpoint)
        if current is None:
            self.connection.execute(
                """
                INSERT INTO sync_status (endpoint, last_page, last_cursor, completed, last_error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    endpoint,
                    last_page if last_page is not None else 1,
                    last_cursor,
                    1 if completed else 0,
                    last_error,
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE sync_status
                SET last_page = ?,
                    last_cursor = ?,
                    completed = ?,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE endpoint = ?
                """,
                (
                    last_page if last_page is not None else current["last_page"],
                    last_cursor if last_cursor is not None else current["last_cursor"],
                    current["completed"] if completed is None else int(completed),
                    last_error,
                    endpoint,
                ),
            )
        self.connection.commit()

    def clear_endpoint(self, endpoint: str) -> None:
        self.connection.execute("DELETE FROM sync_status WHERE endpoint = ?", (endpoint,))
        self.connection.commit()

    def upsert_entity(self, entity_id: str, entity_type: str, has_attachments: bool) -> None:
        self.connection.execute(
            """
            INSERT INTO entities (entity_id, type, has_attachments, attachment_discovered)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(entity_id, type) DO UPDATE SET
                has_attachments = excluded.has_attachments
            """,
            (entity_id, entity_type, int(has_attachments)),
        )

    def mark_entity_discovered(self, entity_id: str, entity_type: str) -> None:
        self.connection.execute(
            """
            UPDATE entities
            SET attachment_discovered = 1
            WHERE entity_id = ? AND type = ?
            """,
            (entity_id, entity_type),
        )

    def iter_entities_pending_attachment_discovery(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT entity_id, type
            FROM entities
            WHERE has_attachments = 1 AND attachment_discovered = 0
            ORDER BY type, entity_id
            """
        ).fetchall()

    def enqueue_attachment(
        self,
        entity_id: str,
        entity_type: str,
        file_name: str,
        attachment_id: str | None,
        mime_type: str | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO attachments_queue (entity_id, type, attachment_id, file_name, mime_type, downloaded)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(entity_id, type, file_name) DO UPDATE SET
                attachment_id = COALESCE(excluded.attachment_id, attachments_queue.attachment_id),
                mime_type = COALESCE(excluded.mime_type, attachments_queue.mime_type)
            """,
            (entity_id, entity_type, attachment_id, file_name, mime_type),
        )

    def iter_pending_attachments(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT entity_id, type, attachment_id, file_name, mime_type
            FROM attachments_queue
            WHERE downloaded = 0
            ORDER BY type, entity_id, file_name
            """
        ).fetchall()

    def mark_attachment_downloaded(self, entity_id: str, entity_type: str, file_name: str, local_path: str) -> None:
        self.connection.execute(
            """
            UPDATE attachments_queue
            SET downloaded = 1,
                local_path = ?
            WHERE entity_id = ? AND type = ? AND file_name = ?
            """,
            (local_path, entity_id, entity_type, file_name),
        )

    def list_entities_with_attachments(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT entity_id, type
            FROM entities
            WHERE has_attachments = 1
            ORDER BY type, entity_id
            """
        ).fetchall()

    def list_attachments_for_entity(self, entity_id: str, entity_type: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT entity_id, type, attachment_id, file_name, mime_type, downloaded, local_path
            FROM attachments_queue
            WHERE entity_id = ? AND type = ?
            ORDER BY file_name
            """,
            (entity_id, entity_type),
        ).fetchall()

    def get_summary(self) -> dict[str, int]:
        entity_count = self.connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        queue_count = self.connection.execute("SELECT COUNT(*) FROM attachments_queue").fetchone()[0]
        downloaded_count = self.connection.execute(
            "SELECT COUNT(*) FROM attachments_queue WHERE downloaded = 1"
        ).fetchone()[0]
        return {
            "entities": entity_count,
            "attachments_queued": queue_count,
            "attachments_downloaded": downloaded_count,
        }

    def list_all_attachments(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT entity_id, type, attachment_id, file_name, mime_type, downloaded, local_path
            FROM attachments_queue
            ORDER BY type, entity_id, file_name
            """
        ).fetchall()

    def commit(self) -> None:
        self.connection.commit()
