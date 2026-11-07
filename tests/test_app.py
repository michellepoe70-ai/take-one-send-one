import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import uvicorn

from app.main import create_app
from app.store import Store, gate, verify_ledger


class AppTests(unittest.TestCase):
    """Exercise real HTTP with the approved server and the standard library."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="take-one-http-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.addCleanup(self.listener.close)
        self.base = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(self.store.data_dir), log_level="error", access_log=False,
        ))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.listener]}, daemon=True,
        )
        self.addCleanup(self.stop_server)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "Test server did not start.")

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "Test server did not stop.")

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={} if data is None else {"Content-Type": "application/json"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            parsed = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, response.headers, parsed

    def payload(self, note="Fictional session: español, nòt an kreyòl."):
        return {"client_reference": "DEMO-HTTP", "session_date": "2026-11-07", "note": note}

    def test_pages_and_health_are_served_without_caching(self):
        for path in ("/", "/drafts", "/static/style.css", "/static/note.js", "/static/drafts.js"):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/health")[2], {"status": "ok"})

    def test_create_list_reopen_update_use_one_gate_and_persist(self):
        with patch("app.main.gate", wraps=gate) as guarded:
            status, headers, first = self.request("/api/drafts", "POST", self.payload())
            self.assertEqual(status, 201)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(first["note"], self.payload()["note"])
            identifier = first["id"]
            listing = self.request("/api/drafts")[2]
            self.assertEqual(len(listing), 1)
            self.assertNotIn("note", listing[0])
            self.assertNotIn("ledger_hash", listing[0])
            self.assertEqual(self.request(f"/notes/{identifier}")[0], 200)
            self.assertEqual(self.request(f"/api/drafts/{identifier}")[2]["note"], first["note"])
            status, _, updated = self.request(f"/api/drafts/{identifier}", "PUT", self.payload("Updated demo."))
            self.assertEqual(status, 200)
            self.assertEqual(updated["created_at"], first["created_at"])
            self.assertEqual(guarded.call_count, 2)
        self.assertEqual(Store(self.store.data_dir).get_draft(identifier)["note"], "Updated demo.")
        self.assertEqual(len(self.store.list_drafts()), 1)
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 2)

    def test_invalid_input_and_missing_drafts_do_not_write(self):
        for payload in (
            self.payload("  "),
            {**self.payload(), "session_date": "not-a-date"},
            {**self.payload(), "unknown_field": "unexpected"},
        ):
            self.assertEqual(self.request("/api/drafts", "POST", payload)[0], 422)
        missing = f"/api/drafts/{uuid4()}"
        self.assertEqual(self.request(missing)[0], 404)
        self.assertEqual(self.request(missing, "PUT", self.payload())[0], 404)
        self.assertEqual(self.request("/api/drafts/invalid-id")[0], 422)
        self.assertFalse(self.store.ledger_path.exists())

    def test_broken_chain_returns_503_and_preserves_old_draft(self):
        first = self.request("/api/drafts", "POST", self.payload())[2]
        with self.store.ledger_path.open("ab") as ledger:
            ledger.write(b"broken\n")
        status, _, error = self.request(f"/api/drafts/{first['id']}", "PUT", self.payload("Must not save."))
        self.assertEqual(status, 503)
        self.assertIn("Ledger verification failed", error["detail"])
        self.assertEqual(self.request(f"/api/drafts/{first['id']}")[2]["note"], first["note"])

    def test_ledger_write_failure_returns_503_and_no_draft(self):
        with patch("app.store.os.fsync", side_effect=OSError("test failure")):
            status, _, error = self.request("/api/drafts", "POST", self.payload())
        self.assertEqual(status, 503)
        self.assertIn("Draft was not saved", error["detail"])
        self.assertEqual(self.request("/api/drafts")[2], [])

    def test_runtime_data_and_env_are_not_served(self):
        for path in ("/.env", "/data/ledger.jsonl", "/data/drafts.db", "/static/%2e%2e/.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_demo_3_generation_is_blocked_and_audited_over_http(self):
        from seed import SEED_NOTES
        first = self.request("/api/drafts", "POST", self.payload(SEED_NOTES[2]))[2]
        path = f"/api/drafts/{first['id']}/generate"
        with patch("app.generation.produce_views") as model:
            status, _, result = self.request(path, "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["failed_rules"], ["no_identifiers"])
        model.assert_not_called()
        persisted = self.request(f"/api/drafts/{first['id']}/generation")[2]
        self.assertEqual(persisted["status"], "BLOCKED")
        self.assertEqual(persisted["ledger_hash"], result["ledger_hash"])
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 2)

    def test_generation_endpoint_uses_saved_note_and_holds_valid_views(self):
        from test_generation import SAFE_OUTPUT
        first = self.request("/api/drafts", "POST", self.payload())[2]
        with patch("app.generation.produce_views", return_value=SAFE_OUTPUT) as model:
            status, _, result = self.request(
                f"/api/drafts/{first['id']}/generate", "POST", {"reviewer_name": "Demo Counselor"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(result["funder_view"], SAFE_OUTPUT["funder_view"])
        self.assertEqual(model.call_args.args[0]["note"], first["note"])
        self.assertEqual(self.request(f"/api/drafts/{first['id']}/generation")[2]["funder_view"], SAFE_OUTPUT["funder_view"])


if __name__ == "__main__":
    unittest.main()
