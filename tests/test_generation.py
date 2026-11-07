import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents import AgentOutputSchema

from app.generation import GenerationUnavailable, produce_views
from app.policy import RULE_NAMES, load_policy
from app.store import DraftInput, Store, gate, verify_ledger
from seed import SEED_NOTES, seed


SAFE_OUTPUT = {
    "internal_note": "Fictional session: practiced a grounding exercise; follow-up discussed.",
    "funder_view": {
        "reporting_period": "2026-11", "service_type": "counseling", "session_count": 1,
        "language": "en", "service_area": "Little Havana",
    },
}


class GenerationGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="take-one-generation-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.store.initialize()
        self.source = DraftInput(
            client_reference="DEMO-POLICY-001", session_date="2026-11-07", note=SEED_NOTES[0],
        )
        self.saved = gate(self.store, self.source)

    def generate(self, output=None, **kwargs):
        generator = Mock(return_value=copy.deepcopy(SAFE_OUTPUT if output is None else output))
        result = gate(
            self.store, draft_id=self.saved["id"], action="generate", generator=generator,
            reviewer_name=kwargs.pop("reviewer_name", "Demo Counselor"), **kwargs,
        )
        return result, generator

    def rows(self):
        verify_ledger(self.store.ledger_path)
        return [json.loads(line) for line in self.store.ledger_path.read_text(encoding="utf-8").splitlines()]

    def test_valid_output_is_held_for_review_and_both_views_persist(self):
        prefix = self.store.ledger_path.read_bytes()
        result, generator = self.generate()
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(set(result["checks"]), set(RULE_NAMES))
        self.assertEqual(set(result["checks"].values()), {"PASS"})
        self.assertEqual(result["funder_view"], SAFE_OUTPUT["funder_view"])
        self.assertEqual(self.store.latest_generation(self.saved["id"])["internal_note"], SAFE_OUTPUT["internal_note"])
        self.assertTrue(self.store.ledger_path.read_bytes().startswith(prefix))
        self.assertEqual([row.get("status") for row in self.rows()], [None, "STARTED", "READY_FOR_REVIEW"])
        self.assertEqual(generator.call_count, 1)
        ledger = self.store.ledger_path.read_text(encoding="utf-8")
        self.assertNotIn(self.source.note, ledger)
        self.assertNotIn(self.source.client_reference, ledger)
        self.assertNotIn(SAFE_OUTPUT["internal_note"], ledger)

    def test_started_row_is_durable_before_model_and_no_output_is_persisted_yet(self):
        def inspect_call(source, policy):
            self.assertEqual(self.rows()[-1]["status"], "STARTED")
            with closing(self.store.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0], 0)
            return copy.deepcopy(SAFE_OUTPUT)
        gate(self.store, draft_id=self.saved["id"], action="generate", reviewer_name="Demo Counselor", generator=inspect_call)

    def test_demo_3_is_blocked_by_no_identifiers_without_a_model_call(self):
        saved = gate(self.store, DraftInput(
            client_reference="DEMO-DONOR", session_date="2026-11-07", note=SEED_NOTES[2],
        ))
        model = Mock(side_effect=AssertionError("The donor request must never reach a model."))
        result = gate(self.store, draft_id=saved["id"], action="generate", generator=model)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["failed_rules"], ["no_identifiers"])
        self.assertEqual(result["reason_codes"]["no_identifiers"], "client_list_by_diagnosis_requested")
        self.assertEqual(self.rows()[-1]["status"], "BLOCKED")
        self.assertEqual(self.rows()[-1]["hash"], result["ledger_hash"])
        self.assertIsNone(result["funder_view"])
        model.assert_not_called()

    def test_allowed_fields_blocks_extra_fields_and_withholds_both_outputs(self):
        output = copy.deepcopy(SAFE_OUTPUT)
        output["funder_view"]["extra"] = "unexpected"
        result, _ = self.generate(output)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("allowed_fields", result["failed_rules"])
        self.assertIsNone(result["internal_note"])
        self.assertIsNone(self.store.latest_generation(self.saved["id"])["funder_view"])

    def test_no_identifiers_catches_client_reference_in_an_allowed_field(self):
        output = copy.deepcopy(SAFE_OUTPUT)
        output["funder_view"]["language"] = self.source.client_reference
        result, _ = self.generate(output)
        self.assertIn("no_identifiers", result["failed_rules"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_no_identifiers_catches_client_lists_email_and_diagnosis_fields(self):
        for value in (
            {"client_list": [{"name": "Fictional Person", "diagnosis_code": "F99"}]},
            {"email": "fictional@example.test"},
            {"phone": "305-555-0100"},
        ):
            with self.subTest(value=value):
                output = copy.deepcopy(SAFE_OUTPUT)
                output["funder_view"].update(value)
                result, _ = self.generate(output)
                self.assertIn("no_identifiers", result["failed_rules"])
                self.assertIsNone(result["funder_view"])

    def test_no_narrative_blocks_prose_in_a_funder_field(self):
        output = copy.deepcopy(SAFE_OUTPUT)
        output["funder_view"]["language"] = "Discussed the family's private clinical story."
        result, _ = self.generate(output)
        self.assertEqual(result["checks"]["allowed_fields"], "PASS")
        self.assertIn("no_narrative", result["failed_rules"])
        self.assertIsNone(result["funder_view"])

    def test_named_human_is_required_before_any_model_call(self):
        result, generator = self.generate(reviewer_name="")
        self.assertEqual(result["failed_rules"], ["named_human"])
        self.assertEqual(self.rows()[-1]["status"], "BLOCKED")
        generator.assert_not_called()

    def test_release_gate_blocks_release_even_with_a_named_reviewer(self):
        result, generator = self.generate(release_requested=True)
        self.assertEqual(result["failed_rules"], ["release_gate"])
        self.assertEqual(result["status"], "BLOCKED")
        generator.assert_not_called()

    def test_ledger_failure_prevents_model_call_and_generation_write(self):
        generator = Mock(return_value=SAFE_OUTPUT)
        with patch("app.store.os.fsync", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                gate(self.store, draft_id=self.saved["id"], action="generate", reviewer_name="Demo Counselor", generator=generator)
        generator.assert_not_called()
        self.assertIsNone(self.store.latest_generation(self.saved["id"]))

    def test_provider_errors_are_audited_without_sensitive_error_text(self):
        secret_error = "private provider message must not enter ledger"
        result = gate(
            self.store, draft_id=self.saved["id"], action="generate", reviewer_name="Demo Counselor",
            generator=Mock(side_effect=RuntimeError(secret_error)),
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["reason_codes"], {"generation": "generation_failed"})
        self.assertNotIn(secret_error, self.store.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(self.rows()[-1]["status"], "ERROR")

    def test_bad_rules_fail_closed_before_a_model_call(self):
        rules_path = self.store.data_dir / "invalid-rules.json"
        rules_path.write_text('{"type":"object","properties":{"story":{"type":"string"}}}', encoding="utf-8")
        self.store.rules_path = rules_path
        result, generator = self.generate()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("allowed_fields", result["failed_rules"])
        generator.assert_not_called()

    def test_editing_a_note_hides_outdated_generated_views(self):
        self.generate()
        gate(self.store, self.source.model_copy(update={"note": "Updated fictional note."}), self.saved["id"])
        latest = self.store.latest_generation(self.saved["id"])
        self.assertEqual(latest["status"], "STALE")
        self.assertIsNone(latest["funder_view"])
        self.assertIsNone(latest["internal_note"])

    def test_changing_rules_hides_outdated_generated_views(self):
        rules_path = self.store.data_dir / "funder-rules.json"
        schema = load_policy().schema
        rules_path.write_text(json.dumps(schema), encoding="utf-8")
        self.store.rules_path = rules_path
        self.generate()
        schema["properties"]["language"]["enum"].append("another-demo-code")
        rules_path.write_text(json.dumps(schema), encoding="utf-8")
        self.assertEqual(self.store.latest_generation(self.saved["id"])["status"], "STALE")

    def test_seed_is_eight_notes_idempotent_and_preserves_existing_notes(self):
        before = self.store.get_draft(self.saved["id"])
        first = seed(self.store)
        self.assertEqual(len(first), 8)
        self.assertEqual(self.store.get_draft(first[3])["note"], SEED_NOTES[2])
        ledger_before = self.store.ledger_path.read_bytes()
        self.assertEqual(seed(self.store), first)
        self.assertEqual(self.store.ledger_path.read_bytes(), ledger_before)
        self.assertEqual(self.store.get_draft(self.saved["id"]), before)


class AgentsIntegrationTests(unittest.TestCase):
    def test_actual_sdk_strict_output_schema_and_runner_configuration(self):
        policy = load_policy()
        schema = AgentOutputSchema(policy.output_model).json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"internal_note", "funder_view"})
        final = policy.output_model.model_validate(SAFE_OUTPUT)
        with patch("app.generation._settings", return_value={"OPENAI_API_KEY": "fictional-test-key"}):
            with patch("app.generation.Runner.run", new_callable=AsyncMock) as runner:
                runner.return_value = SimpleNamespace(final_output=final)
                result = produce_views({"session_date": "2026-11-07", "note": SEED_NOTES[0]}, policy)
                agent = runner.call_args.args[0]
                self.assertIs(agent.output_type, policy.output_model)
                self.assertEqual(agent.tools, [])
                self.assertFalse(agent.model_settings.store)
                configuration = runner.call_args.kwargs["run_config"]
                self.assertTrue(configuration.tracing_disabled)
                self.assertFalse(configuration.trace_include_sensitive_data)
                self.assertEqual(runner.call_args.kwargs["max_turns"], 1)
                self.assertEqual(result, SAFE_OUTPUT)

    def test_missing_key_is_explicit_and_no_runner_is_called(self):
        with patch("app.generation._settings", return_value={}):
            with patch("app.generation.Runner.run", new_callable=AsyncMock) as runner:
                with self.assertRaisesRegex(GenerationUnavailable, "api_key_not_configured"):
                    produce_views({"session_date": "2026-11-07", "note": "Fictional note."}, load_policy())
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
