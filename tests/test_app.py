import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
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
        if method == "GET" and path.startswith("/api/drafts/") and path.count("/") == 3 and "?" not in path:
            path += "?" + urllib.parse.urlencode({"reviewer_name": "Demo HTTP Reviewer"})
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
        for path in ("/", "/drafts", "/ledger", "/static/style.css", "/static/note.js", "/static/drafts.js", "/static/ledger.js"):
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
            self.assertEqual(guarded.call_count, 3)
        self.assertEqual(Store(self.store.data_dir).get_draft(identifier)["note"], "Updated demo.")
        self.assertEqual(len(self.store.list_drafts()), 1)
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 3)

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
        self.assertEqual(self.request(f"/api/drafts/{first['id']}")[0], 503)
        self.assertEqual(self.store.get_draft(first["id"])["note"], first["note"])

    def test_opening_draft_writes_one_viewed_event_with_reviewer_before_returning_note(self):
        import os
        saved, _ = self.ready_generation()
        draft_before = self.store.get_draft(saved["id"])
        generation_before = self.store.latest_generation(saved["id"])
        reviewer = "Michelle Poe RN, Right Human in the Loop"
        path = f"/api/drafts/{saved['id']}?" + urllib.parse.urlencode({"reviewer_name": reviewer})
        original_fsync = os.fsync
        synced = []

        def inspect_sync(descriptor):
            original_fsync(descriptor)
            row = json.loads(self.store.ledger_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(row["event"], "VIEWED")
            self.assertEqual(row["reviewer_name"], reviewer)
            synced.append(row["hash"])

        # The HTML shell contains no draft content and must not create a second event.
        self.assertEqual(self.request(f"/notes/{saved['id']}")[0], 200)
        with patch("app.main.gate", wraps=gate) as guarded, patch("app.store.os.fsync", side_effect=inspect_sync):
            status, headers, opened = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(opened, draft_before)
        self.assertEqual(len(synced), 1)
        guarded.assert_called_once()
        self.assertEqual(guarded.call_args.kwargs["action"], "view")
        rows = self.store.ledger_rows()["rows"]
        viewed = [row for row in rows if row.get("event") == "VIEWED"]
        self.assertEqual(len(viewed), 1)
        row = viewed[0]
        self.assertEqual(row["draft_id"], saved["id"])
        self.assertEqual(row["previous_hash"], rows[-2]["hash"])
        self.assertEqual(set(row), {
            "version", "sequence", "event_id", "event", "draft_id", "reviewer_name",
            "timestamp", "previous_hash", "hash",
        })
        self.assertNotIn(saved["note"], json.dumps(row))
        self.assertEqual(self.store.latest_generation(saved["id"]), generation_before)
        self.assertEqual(self.store.get_draft(saved["id"]), draft_before)
        with patch("app.store._append_ledger", side_effect=OSError("test failure")):
            status, _, failure = self.request(path)
        self.assertEqual(status, 503)
        self.assertNotIn("note", failure)
        self.assertNotIn(saved["note"], json.dumps(failure))
        for name in ("", "AI"):
            missing_name = f"/api/drafts/{saved['id']}?" + urllib.parse.urlencode({"reviewer_name": name})
            self.assertEqual(self.request(missing_name)[0], 422)
        self.assertEqual(len([row for row in self.store.ledger_rows()["rows"] if row.get("event") == "VIEWED"]), 1)

    def test_viewed_events_preserve_mixed_hash_chain_and_released_status(self):
        from app.store import LedgerIntegrityError
        saved, generated = self.ready_generation()
        for name, release_first in (("First Reviewer", False), ("Second Reviewer", True)):
            if release_first:
                decision = self.request(f"/api/generations/{generated['id']}/release", "POST", {"reviewer_name": "Release Reviewer"})[2]
                self.assertEqual(decision["status"], "RELEASED")
            path = f"/api/drafts/{saved['id']}?" + urllib.parse.urlencode({"reviewer_name": name})
            self.assertEqual(self.request(path)[0], 200)
        self.request("/api/drafts", "POST", self.payload("Another fictional note."))
        rows = self.store.ledger_rows()["rows"]
        chain = verify_ledger(self.store.ledger_path)
        self.assertEqual(chain.length, 7)
        self.assertEqual(chain.head, rows[-1]["hash"])
        self.assertEqual([row["reviewer_name"] for row in rows if row.get("event") == "VIEWED"], ["First Reviewer", "Second Reviewer"])
        self.assertEqual({row["version"] for row in rows}, {1, 2, 5, 6})
        for previous, current in zip(rows, rows[1:]):
            self.assertEqual(current["previous_hash"], previous["hash"])
        self.assertEqual(self.store.latest_generation(saved["id"])["status"], "RELEASED")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 1)
        rows[3]["reviewer_name"] = "Changed Reviewer"
        self.store.ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with self.assertRaises(LedgerIntegrityError):
            verify_ledger(self.store.ledger_path)

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

    def ready_generation(self):
        from test_generation import SAFE_OUTPUT
        saved = self.request("/api/drafts", "POST", self.payload())[2]
        with patch("app.generation.produce_views", return_value=SAFE_OUTPUT):
            generated = self.request(f"/api/drafts/{saved['id']}/generate", "POST", {"reviewer_name": "Demo Generator"})[2]
        return saved, generated

    def test_release_refusal_ledger_and_quarterly_over_http(self):
        released_draft, released = self.ready_generation()
        refused_draft, refused = self.ready_generation()
        self.ready_generation()  # A held output must not be counted.
        status, _, decision = self.request(f"/api/generations/{released['id']}/release", "POST", {"reviewer_name": "Demo Reviewer"})
        self.assertEqual(status, 200)
        self.assertEqual(decision["status"], "RELEASED")
        status, _, decision = self.request(f"/api/generations/{refused['id']}/refuse", "POST", {"reviewer_name": "Demo Reviewer", "reason_code": "other", "reason": "Revise this fictional output."})
        self.assertEqual(decision["status"], "REFUSED")
        self.assertEqual(self.request(f"/api/drafts/{released_draft['id']}/generation")[2]["status"], "RELEASED")
        self.assertEqual(self.request(f"/api/drafts/{refused_draft['id']}/generation")[2]["review_reason"], "Revise this fictional output.")
        ledger = self.request("/api/ledger")[2]
        self.assertEqual(ledger["count"], 11)
        self.assertEqual(ledger["rows"][-1]["status"], "REFUSED")
        self.assertEqual(ledger["rows"][-1]["reason_code"], "other")
        from test_generation import SAFE_OUTPUT
        self.assertEqual(ledger["rows"][-2]["released_funder_view"], SAFE_OUTPUT["funder_view"])
        self.assertIsNone(ledger["rows"][-1]["released_funder_view"])
        totals = self.request("/api/quarterly?year=2026&quarter=4")[2]
        self.assertEqual(totals, {"year": 2026, "quarter": 4, "released_rows": 1, "session_count": 1})
        self.assertEqual(self.request("/api/quarterly?year=2026&quarter=3")[2]["session_count"], 0)

    def test_review_requires_name_and_reason_and_valid_target(self):
        _, generated = self.ready_generation()
        path = f"/api/generations/{generated['id']}"
        self.assertEqual(self.request(path + "/release", "POST", {})[2]["failed_rules"], ["named_human"])
        result = self.request(path + "/refuse", "POST", {"reviewer_name": "Demo Reviewer"})[2]
        self.assertEqual(result["reason_codes"]["release_gate"], "refusal_reason_required")
        self.assertEqual(self.request(f"/api/generations/{uuid4()}/release", "POST", {"reviewer_name": "Demo Reviewer"})[0], 404)
        self.assertEqual(self.request(path + "/release", "POST", {"reviewer_name": "Demo Reviewer", "override": True})[0], 422)
        self.assertEqual(self.request("/api/quarterly?year=2026&quarter=5")[0], 422)

    def test_unsafe_other_text_is_blocked_over_http_without_echoing_or_logging_it(self):
        saved, generated = self.ready_generation()
        private = "fictional.private@example.test"
        status, _, result = self.request(f"/api/generations/{generated['id']}/refuse", "POST", {
            "reviewer_name": "Demo Reviewer", "reason_code": "other", "reason": private,
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["checks"]["no_identifiers"], "FAIL")
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn(private, json.dumps(self.request("/api/ledger")[2]))
        self.assertEqual(self.request(f"/api/drafts/{saved['id']}/generation")[2]["status"], "READY_FOR_REVIEW")
        result = self.request(f"/api/generations/{generated['id']}/refuse", "POST", {
            "reviewer_name": "Demo Reviewer", "reason_code": "duplicate",
        })[2]
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(self.request("/api/ledger")[2]["rows"][-1]["reason_code"], "duplicate")

    def test_ledger_and_quarterly_fail_closed_on_broken_chain(self):
        self.ready_generation()
        with self.store.ledger_path.open("ab") as ledger:
            ledger.write(b"broken\n")
        self.assertEqual(self.request("/api/ledger")[0], 503)
        self.assertEqual(self.request("/api/quarterly?year=2026&quarter=4")[0], 503)


if __name__ == "__main__":
    unittest.main()
