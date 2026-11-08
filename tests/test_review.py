import copy
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

from app.policy import digest, load_policy
from app.refusals import REFUSAL_REASONS
from app.store import DraftInput, GenerationNotFoundError, LedgerIntegrityError, Store, gate, verify_ledger
from seed import SEED_NOTES
from test_generation import SAFE_OUTPUT


REVIEWER = "Demo Review Tester"


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="take-one-review-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.store.initialize()
        self.draft, self.generated = self.make_ready()

    def make_ready(self, period="2026-11", sessions=1):
        draft = gate(self.store, DraftInput(
            client_reference="REVIEW-TEST-" + str(uuid4()),
            session_date=period + "-07", note=SEED_NOTES[0],
        ))
        output = copy.deepcopy(SAFE_OUTPUT)
        output["funder_view"].update(reporting_period=period, session_count=sessions)
        generated = gate(
            self.store, draft_id=draft["id"], action="generate", reviewer_name="Initial Demo Reviewer",
            generator=lambda source, policy: output,
        )
        return draft, generated

    def review(self, action="release", generation=None, name=REVIEWER, reason="", reason_code=None):
        if reason_code is None:
            reason_code = "other" if action == "refuse" and reason else ""
        return gate(
            self.store, action=action, generation_id=(generation or self.generated)["id"],
            reviewer_name=name, reason=reason, reason_code=reason_code,
        )

    def test_release_is_append_only_and_survives_reopening(self):
        prefix = self.store.ledger_path.read_bytes()
        result = self.review()
        self.assertEqual(result["status"], "RELEASED")
        self.assertEqual(set(result["checks"].values()), {"PASS"})
        self.assertTrue(self.store.ledger_path.read_bytes().startswith(prefix))
        self.assertEqual(verify_ledger(self.store.ledger_path).length, 4)
        reopened = Store(self.store.data_dir).latest_generation(self.draft["id"])
        self.assertEqual(reopened["status"], "RELEASED")
        self.assertEqual(reopened["reviewer_name"], REVIEWER)
        self.assertEqual(reopened["review_ledger_hash"], result["ledger_hash"])
        self.assertEqual(reopened["funder_view"], SAFE_OUTPUT["funder_view"])
        row = self.store.ledger_rows()["rows"][-1]
        self.assertEqual(row["version"], 5)
        self.assertEqual(row["released_funder_view"], SAFE_OUTPUT["funder_view"])
        self.assertEqual(row["released_funder_view"]["language"], "en")
        self.assertEqual(row["released_funder_view"]["reporting_period"], "2026-11")
        self.assertEqual(row["released_funder_hash"], digest(row["released_funder_view"]))
        self.assertEqual(row["hash"], digest({key: value for key, value in row.items() if key != "hash"}))
        self.assertNotIn(SAFE_OUTPUT["internal_note"], json.dumps(row))
        self.assertNotIn(self.draft["client_reference"], json.dumps(row))

    def test_refusal_requires_reason_and_records_it(self):
        missing = self.review("refuse")
        self.assertEqual(missing["status"], "BLOCKED")
        self.assertEqual(missing["reason_codes"]["release_gate"], "refusal_reason_required")
        result = self.review("refuse", reason="  Needs a clearer follow-up plan.  ")
        self.assertEqual(result["status"], "REFUSED")
        latest = self.store.latest_generation(self.draft["id"])
        self.assertEqual(latest["review_reason"], "Needs a clearer follow-up plan.")
        self.assertEqual(latest["status"], "REFUSED")
        row = self.store.ledger_rows()["rows"][-1]
        self.assertEqual(row["reason"], latest["review_reason"])
        self.assertEqual(row["reason_code"], "other")
        self.assertEqual(latest["review_reason_code"], "other")
        self.assertIsNone(row["released_funder_view"])
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 0)

    def test_both_actions_require_a_named_human(self):
        for action in ("release", "refuse"):
            with self.subTest(action=action):
                result = self.review(action, name="", reason="Fictional test reason.")
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("named_human", result["failed_rules"])
                self.assertIsNone(self.store.ledger_rows()["rows"][-1]["released_funder_view"])
        self.assertEqual(self.store.latest_generation(self.draft["id"])["status"], "READY_FOR_REVIEW")

    def test_each_fixed_refusal_code_is_recorded_without_free_text(self):
        for code in REFUSAL_REASONS:
            with self.subTest(code=code):
                draft, generated = self.make_ready()
                result = self.review("refuse", generation=generated, reason_code=code)
                self.assertEqual(result["status"], "REFUSED")
                row = self.store.ledger_rows()["rows"][-1]
                self.assertEqual(row["reason_code"], code)
                self.assertEqual(row["reason"], "")
                self.assertEqual(row["checks"]["no_identifiers"], "PASS")
                self.assertEqual(self.store.latest_generation(draft["id"])["review_reason_code"], code)
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 0)

    def test_unsafe_other_text_is_checked_before_append_and_never_stored(self):
        from app.store import _append_ledger
        private_values = (self.draft["client_reference"], "fictional.private@example.test", "305-555-0199", "ICD-10 F99")
        for private in private_values:
            with self.subTest(kind=private_values.index(private)):
                def inspect_append(path, row):
                    self.assertEqual(row["status"], "BLOCKED")
                    self.assertEqual(row["checks"]["no_identifiers"], "FAIL")
                    self.assertEqual(row["reason"], "")
                    self.assertNotIn(private, json.dumps(row))
                    _append_ledger(path, row)
                with patch("app.store._append_ledger", side_effect=inspect_append) as append:
                    result = self.review("refuse", reason_code="other", reason=private)
                append.assert_called_once()
                self.assertEqual(result["reason_codes"]["no_identifiers"], "identifier_in_refusal_reason")
                self.assertNotIn(private, json.dumps(result))
                self.assertNotIn(private, self.store.ledger_path.read_text(encoding="utf-8"))
                self.assertEqual(self.store.latest_generation(self.draft["id"])["status"], "READY_FOR_REVIEW")
        with closing(self.store.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)

    def test_invalid_codes_and_non_other_text_are_rejected_without_copying_text(self):
        for code, text, expected in (
            ("", "", "refusal_reason_required"),
            ("unapproved-private-code", "", "refusal_reason_invalid"),
            ("duplicate", "private extra detail", "refusal_details_only_for_other"),
            ("other", "x" * 1001, "refusal_reason_too_long"),
        ):
            with self.subTest(expected=expected):
                result = self.review("refuse", reason_code=code, reason=text)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["reason_codes"]["release_gate"], expected)
                self.assertEqual(result["reason"], "")
        self.assertNotIn("unapproved-private-code", self.store.ledger_path.read_text(encoding="utf-8"))
        self.assertNotIn("private extra detail", self.store.ledger_path.read_text(encoding="utf-8"))

    def test_identifiers_are_screened_even_when_text_is_sent_with_a_fixed_code(self):
        result = self.review("refuse", reason_code="duplicate", reason="fictional.private@example.test")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["checks"]["no_identifiers"], "FAIL")
        self.assertEqual(self.store.ledger_rows()["rows"][-1]["reason"], "")

    def test_blocked_donor_generation_cannot_be_released(self):
        donor = gate(self.store, DraftInput(client_reference="DONOR-TEST", session_date="2026-11-07", note=SEED_NOTES[2]))
        blocked = gate(self.store, draft_id=donor["id"], action="generate")
        result = self.review(generation=blocked)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("release_gate", result["failed_rules"])
        self.assertIn("no_identifiers", result["failed_rules"])
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 0)

    def test_editing_source_blocks_release(self):
        gate(self.store, DraftInput(client_reference=self.draft["client_reference"], session_date="2026-11-07", note="Changed fictional note."), self.draft["id"])
        result = self.review()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"]["release_gate"], "stale_generation")

    def test_rule_change_blocks_release(self):
        rules = copy.deepcopy(load_policy().schema)
        rules["properties"]["language"]["enum"].append("another-demo-language")
        rules_path = self.store.data_dir / "changed-rules.json"
        rules_path.write_text(json.dumps(rules), encoding="utf-8")
        self.store.rules_path = rules_path
        result = self.review()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"]["release_gate"], "rules_changed")

    def test_changed_generated_content_is_rejected_even_if_still_in_schema(self):
        changed = copy.deepcopy(SAFE_OUTPUT["funder_view"])
        changed["language"] = "es"
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE generations SET funder_view = ? WHERE id = ?", (json.dumps(changed), self.generated["id"]))
        result = self.review()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"]["release_gate"], "generated_content_changed")

    def test_invalid_reporting_month_cannot_be_released(self):
        output = copy.deepcopy(SAFE_OUTPUT)
        output["funder_view"]["reporting_period"] = "2026-10"
        generated = gate(self.store, draft_id=self.draft["id"], action="generate", reviewer_name=REVIEWER, generator=lambda source, policy: output)
        result = self.review(generation=generated)
        self.assertEqual(result["reason_codes"]["release_gate"], "reporting_period_mismatch")

    def test_changed_database_source_hash_cannot_bypass_stale_check(self):
        from app.policy import digest
        updated = {"client_reference": self.draft["client_reference"], "session_date": "2026-11-07", "note": "Revised fictional note."}
        gate(self.store, DraftInput(**updated), self.draft["id"])
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE generations SET source_hash = ? WHERE id = ?", (digest(updated), self.generated["id"]))
        result = self.review()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"]["release_gate"], "generated_content_changed")

    def test_changed_database_rules_hash_cannot_bypass_policy_check(self):
        schema = copy.deepcopy(load_policy().schema)
        schema["properties"]["language"]["enum"].append("another-demo-code")
        path = self.store.data_dir / "changed-rules.json"
        path.write_text(json.dumps(schema), encoding="utf-8")
        self.store.rules_path = path
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE generations SET rules_hash = ? WHERE id = ?", (load_policy(path).hash, self.generated["id"]))
        self.assertEqual(self.review()["reason_codes"]["release_gate"], "generated_content_changed")

    def test_parallel_releases_count_once_and_final_decision_cannot_flip(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.review(), range(2)))
        self.assertEqual(sorted(result["status"] for result in results), ["BLOCKED", "RELEASED"])
        self.assertEqual(self.review("refuse", reason="Late refusal.")["status"], "BLOCKED")
        self.assertEqual(self.store.latest_generation(self.draft["id"])["status"], "RELEASED")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 1)

    def test_new_generation_of_released_note_cannot_double_count(self):
        self.review()
        generated = gate(self.store, draft_id=self.draft["id"], action="generate", reviewer_name=REVIEWER, generator=lambda source, policy: copy.deepcopy(SAFE_OUTPUT))
        result = self.review(generation=generated)
        self.assertEqual(result["reason_codes"]["release_gate"], "note_already_released")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 1)

    def test_old_generation_cannot_be_released_after_a_newer_one(self):
        gate(self.store, draft_id=self.draft["id"], action="generate", reviewer_name=REVIEWER, generator=lambda source, policy: copy.deepcopy(SAFE_OUTPUT))
        self.assertEqual(self.review()["reason_codes"]["release_gate"], "newer_generation_exists")

    def test_quarterly_counts_only_released_snapshots_and_sums_counts(self):
        for period, sessions in (("2026-10", 1), ("2026-12", 0), ("2026-09", 1), ("2027-01", 1)):
            _, generated = self.make_ready(period, sessions)
            self.assertEqual(self.review(generation=generated)["status"], "RELEASED")
        _, refused = self.make_ready()
        self.review("refuse", generation=refused, reason="Not approved in this test.")
        donor = gate(self.store, DraftInput(client_reference="DONOR-TOTALS", session_date="2026-11-07", note=SEED_NOTES[2]))
        gate(self.store, draft_id=donor["id"], action="generate")
        self.assertEqual(self.store.quarterly_totals(2026, 4), {"year": 2026, "quarter": 4, "released_rows": 2, "session_count": 1})
        self.assertEqual(self.store.quarterly_totals(2026, 3)["session_count"], 1)
        self.assertEqual(self.store.quarterly_totals(2027, 1)["session_count"], 1)
        self.assertEqual(self.store.quarterly_totals(2025, 4)["released_rows"], 0)

    def test_later_edits_do_not_change_released_snapshot_totals(self):
        self.review()
        gate(self.store, DraftInput(client_reference=self.draft["client_reference"], session_date="2027-01-07", note="Later revised fictional note."), self.draft["id"])
        latest = self.store.latest_generation(self.draft["id"])
        self.assertEqual(latest["status"], "RELEASED")
        self.assertFalse(latest["is_current"])
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 1)
        self.assertEqual(self.store.quarterly_totals(2027, 1)["session_count"], 0)

    def test_review_fsync_is_before_database_insert(self):
        events = []
        connect = self.store.connect
        fsync = os.fsync
        def traced():
            connection = connect()
            connection.set_trace_callback(lambda sql: events.append(sql.split()[0].upper()))
            return connection
        def synced(fd):
            fsync(fd)
            row = json.loads(self.store.ledger_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(row["released_funder_view"], SAFE_OUTPUT["funder_view"])
            events.append("LEDGER_SYNCED")
        with patch.object(self.store, "connect", side_effect=traced), patch("app.store.os.fsync", side_effect=synced):
            self.review()
        self.assertLess(events.index("LEDGER_SYNCED"), events.index("INSERT"))
        self.assertLess(events.index("LEDGER_SYNCED"), events.index("COMMIT"))

    def test_failed_fsync_keeps_output_held_and_out_of_totals(self):
        with patch("app.store.os.fsync", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.review()
        self.assertEqual(self.store.latest_generation(self.draft["id"])["status"], "READY_FOR_REVIEW")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 0)

    def test_database_failure_after_ledger_does_not_count_an_uncommitted_release(self):
        with closing(self.store.connect()) as connection:
            connection.execute("CREATE TRIGGER fail_review BEFORE INSERT ON reviews BEGIN SELECT RAISE(FAIL, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.review()
        self.assertEqual(self.store.ledger_rows()["rows"][-1]["status"], "RELEASED")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 0)
        with closing(self.store.connect()) as connection:
            connection.execute("DROP TRIGGER fail_review")
        self.assertEqual(self.review()["status"], "RELEASED")
        self.assertEqual(self.store.quarterly_totals(2026, 4)["released_rows"], 1)

    def test_tampered_release_snapshot_stops_totals(self):
        self.review()
        changed = copy.deepcopy(SAFE_OUTPUT["funder_view"])
        changed["session_count"] = 0
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE reviews SET funder_snapshot = ?", (json.dumps(changed),))
        with self.assertRaises(LedgerIntegrityError):
            self.store.quarterly_totals(2026, 4)

    def test_release_snapshot_cannot_be_changed_without_breaking_verification(self):
        self.review()
        original = self.store.ledger_path.read_bytes()
        rows = [json.loads(line) for line in original.splitlines()]
        for change in ("missing", "content", "snapshot_hash"):
            with self.subTest(change=change):
                changed = copy.deepcopy(rows)
                row = changed[-1]
                if change == "missing":
                    del row["released_funder_view"]
                else:
                    row["released_funder_view"]["session_count"] = 0
                if change == "snapshot_hash":
                    # Updating the content hash alone must still break the row hash.
                    row["released_funder_hash"] = digest(row["released_funder_view"])
                else:
                    # A missing snapshot or inconsistent snapshot hash must fail
                    # even when the row's own hash has been recalculated.
                    row["hash"] = digest({key: value for key, value in row.items() if key != "hash"})
                self.store.ledger_path.write_text(
                    "".join(json.dumps(item) + "\n" for item in changed), encoding="utf-8",
                )
                with self.assertRaises(LedgerIntegrityError):
                    verify_ledger(self.store.ledger_path)
        self.store.ledger_path.write_bytes(original)

    def test_refused_row_cannot_include_a_funder_snapshot(self):
        self.review("refuse", reason="Fictional refusal.")
        rows = self.store.ledger_rows()["rows"]
        row = rows[-1]
        row["released_funder_view"] = copy.deepcopy(SAFE_OUTPUT["funder_view"])
        row["hash"] = digest({key: value for key, value in row.items() if key != "hash"})
        self.store.ledger_path.write_text(
            "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8",
        )
        with self.assertRaises(LedgerIntegrityError):
            verify_ledger(self.store.ledger_path)

    def test_legacy_release_and_new_release_share_one_unchanged_chain(self):
        self.review()
        rows = self.store.ledger_rows()["rows"]
        legacy = rows[-1]
        legacy["version"] = 3
        del legacy["released_funder_view"]
        del legacy["reason_code"]
        legacy["hash"] = digest({key: value for key, value in legacy.items() if key != "hash"})
        self.store.ledger_path.write_text(
            "".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8",
        )
        with closing(self.store.connect()) as connection, connection:
            connection.execute("UPDATE reviews SET ledger_hash = ?", (legacy["hash"],))
            connection.execute("ALTER TABLE reviews DROP COLUMN reason_code")
        self.store.initialize()
        self.store.initialize()  # The additive migration is idempotent.
        self.assertEqual(self.store.latest_generation(self.draft["id"])["review_reason_code"], "")
        prefix = self.store.ledger_path.read_bytes()
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 1)
        _, generated = self.make_ready()
        self.assertEqual(self.review(generation=generated)["status"], "RELEASED")
        self.assertTrue(self.store.ledger_path.read_bytes().startswith(prefix))
        self.assertEqual(self.store.ledger_rows()["rows"][-1]["version"], 5)
        self.assertEqual(self.store.quarterly_totals(2026, 4)["session_count"], 2)

    def test_ledger_lists_every_row_with_full_hashes_and_no_note_text(self):
        self.review("refuse", reason="Fictional refusal reason.")
        ledger = self.store.ledger_rows()
        self.assertEqual(ledger["count"], 4)
        self.assertEqual([row["sequence"] for row in ledger["rows"]], [1, 2, 3, 4])
        self.assertEqual(ledger["head_hash"], ledger["rows"][-1]["hash"])
        for row in ledger["rows"]:
            self.assertEqual(len(row["hash"]), 64)
            self.assertEqual(len(row["previous_hash"]), 64)
        self.assertNotIn(SEED_NOTES[0], json.dumps(ledger))

    def test_empty_ledger_has_zero_totals_and_unknown_target_is_404_equivalent(self):
        empty = Store(self.store.data_dir / "empty")
        empty.initialize()
        self.assertEqual(empty.ledger_rows()["rows"], [])
        self.assertEqual(empty.quarterly_totals(2026, 4)["released_rows"], 0)
        with self.assertRaises(GenerationNotFoundError):
            gate(empty, action="release", generation_id=str(uuid4()), reviewer_name=REVIEWER)


if __name__ == "__main__":
    unittest.main()
