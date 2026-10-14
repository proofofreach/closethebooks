from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from xero_absolute_backup.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_sync_status_upsert_and_resume_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            try:
                store.upsert_sync_status("invoices", last_page=3, completed=False)
                row = store.get_sync_status("invoices")
                self.assertIsNotNone(row)
                self.assertEqual(row["last_page"], 3)
                self.assertEqual(row["completed"], 0)
            finally:
                store.close()

    def test_attachment_queue_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.db")
            try:
                store.upsert_entity("abc", "invoices", True)
                pending = store.iter_entities_pending_attachment_discovery()
                self.assertEqual(len(pending), 1)
                store.enqueue_attachment("abc", "invoices", "file.pdf", "att-1", "application/pdf")
                queued = store.iter_pending_attachments()
                self.assertEqual(len(queued), 1)
                store.mark_attachment_downloaded("abc", "invoices", "file.pdf", "/tmp/file.pdf")
                queued = store.iter_pending_attachments()
                self.assertEqual(len(queued), 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
