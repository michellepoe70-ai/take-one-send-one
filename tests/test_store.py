import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from app.store import (
    DraftInput, DraftNotFoundError, GENESIS_HASH, LedgerIntegrityError,
    Store, gate, verify_ledger,
)


def sample(note="Fictional note: español y nòt an kreyòl."):
    return DraftInput(client_reference="DEMO-001", session_date="2026-11-07", note=note)


class GateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="take-one-gate-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.store.initialize()

    def test_create_update_are_append_only_and_persist_across_restart(self):
        first = gate(self.store, sample())
        prefix = self.store.ledger_path.read_bytes()
        updated = gate(self.store, sample("Updated fictional note."), first["id"])
        ledger = self.store.ledger_path.read_bytes()
        self.assertTrue(ledger.startswith(prefix))
        rows = [json.loads(line) for line in ledger.splitlines()]
        self.assertEqual(rows[0]["previous_hash"], GENESIS_HASH)
        self.assertEqual(rows[1]["previous_hash"], rows[0]["hash"])
        self.assertEqual([row["sequence"] for row in rows], [1, 2])
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 2)
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["created_at"], first["created_at"])
        restarted = Store(self.store.data_dir)
        restarted.initialize()
        self.assertEqual(restarted.get_draft(first["id"])["note"], "Updated fictional note.")
        self.assertEqual(restarted.ledger_path.read_bytes(), ledger)
        self.assertNotIn(sample().note.encode(), ledger)
        self.assertNotIn(b"DEMO-001", ledger)
        self.assertNotIn(b'"note"', ledger)

    def test_ledger_fsync_finishes_before_any_draft_insert_or_commit(self):
        events = []
        connect = self.store.connect
        fsync = os.fsync

        def traced_connect():
            connection = connect()
            connection.set_trace_callback(lambda sql: events.append(sql.split()[0].upper()))
            return connection

        def synced(descriptor):
            self.assertEqual(verify_ledger(self.store.ledger_path).length, 1)
            fsync(descriptor)
            events.append("LEDGER_SYNCED")

        with patch.object(self.store, "connect", side_effect=traced_connect):
            with patch("app.store.os.fsync", side_effect=synced):
                gate(self.store, sample())
        self.assertLess(events.index("LEDGER_SYNCED"), events.index("INSERT"))
        self.assertLess(events.index("LEDGER_SYNCED"), events.index("COMMIT"))

    def test_append_failure_blocks_draft_update(self):
        draft = gate(self.store, sample())
        original = self.store.ledger_path.read_bytes()
        with patch("app.store._append_ledger", side_effect=OSError("test disk failure")):
            with self.assertRaises(OSError):
                gate(self.store, sample("Must not be saved."), draft["id"])
        self.assertEqual(self.store.get_draft(draft["id"])["note"], sample().note)
        self.assertEqual(self.store.ledger_path.read_bytes(), original)

    def test_fsync_failure_blocks_database_write(self):
        with patch("app.store.os.fsync", side_effect=OSError("test fsync failure")):
            with self.assertRaises(OSError):
                gate(self.store, sample())
        self.assertEqual(self.store.list_drafts(), [])

    def test_database_failure_keeps_intent_and_next_save_extends_chain(self):
        with closing(self.store.connect()) as connection:
            connection.execute(
                "CREATE TRIGGER fail_save BEFORE INSERT ON drafts "
                "BEGIN SELECT RAISE(FAIL, 'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            gate(self.store, sample())
        self.assertEqual(self.store.list_drafts(), [])
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 1)
        prefix = self.store.ledger_path.read_bytes()
        with closing(self.store.connect()) as connection:
            connection.execute("DROP TRIGGER fail_save")
        gate(self.store, sample())
        self.assertTrue(self.store.ledger_path.read_bytes().startswith(prefix))
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 2)
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_changed_row_blocks_new_saves_without_rewriting_ledger(self):
        gate(self.store, sample())
        row = json.loads(self.store.ledger_path.read_bytes())
        row["action"] = "draft.update.requested"
        tampered = (json.dumps(row) + "\n").encode()
        self.store.ledger_path.write_bytes(tampered)
        with self.assertRaises(LedgerIntegrityError):
            gate(self.store, sample())
        self.assertEqual(self.store.ledger_path.read_bytes(), tampered)
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_partial_tail_blocks_saves(self):
        gate(self.store, sample())
        with self.store.ledger_path.open("ab") as ledger:
            ledger.write(b'{"partial":')
        broken = self.store.ledger_path.read_bytes()
        with self.assertRaises(LedgerIntegrityError):
            gate(self.store, sample())
        self.assertEqual(self.store.ledger_path.read_bytes(), broken)
        self.assertEqual(len(self.store.list_drafts()), 1)

    def test_deleted_ledger_is_detected_by_saved_draft_reference(self):
        gate(self.store, sample())
        self.store.ledger_path.unlink()
        with self.assertRaises(LedgerIntegrityError):
            gate(self.store, sample())
        self.assertFalse(self.store.ledger_path.exists())

    def test_removed_tail_is_detected_by_saved_draft_reference(self):
        gate(self.store, sample())
        prefix = self.store.ledger_path.read_bytes()
        gate(self.store, sample("Second fictional draft."))
        self.store.ledger_path.write_bytes(prefix)
        with self.assertRaises(LedgerIntegrityError):
            gate(self.store, sample())
        self.assertEqual(len(self.store.list_drafts()), 2)

    def test_concurrent_saves_do_not_fork_the_chain(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: gate(self.store, sample()), range(16)))
        self.assertEqual(len({draft["id"] for draft in results}), 16)
        self.assertEqual(len(self.store.list_drafts()), 16)
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 16)

    def test_missing_draft_and_invalid_input_do_not_create_ledger_rows(self):
        with self.assertRaises(DraftNotFoundError):
            gate(self.store, sample(), str(uuid4()))
        with self.assertRaises(ValidationError):
            sample("   ")
        self.assertFalse(self.store.ledger_path.exists())


if __name__ == "__main__":
    unittest.main()
