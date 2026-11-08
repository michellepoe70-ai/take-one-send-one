"""Local drafts and the single ledger-first write gate."""

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.policy import (
    DEFAULT_RULES_PATH, LEGACY_RULE_NAMES, RULE_NAMES, contains_identifiers,
    contains_safe_harbor_identifiers, digest, is_named_human,
    load_policy, requests_client_list,
)
from app.refusals import REFUSAL_REASONS


GENESIS_HASH = "0" * 64


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_reference: str = Field(min_length=1, max_length=100)
    session_date: date
    note: str = Field(min_length=1, max_length=20000)

    @field_validator("client_reference", "note")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter text before saving.")
        return value


class LedgerIntegrityError(Exception):
    """The ledger or a database reference to it failed verification."""


class DraftNotFoundError(Exception):
    pass


class GenerationNotFoundError(Exception):
    pass


class ReviewerNameRequiredError(ValueError):
    pass


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class Chain:
    head: str
    length: int
    hashes: frozenset[str]


def verify_ledger(path: Path) -> Chain:
    """Verify every complete JSONL row without changing the ledger."""
    previous = GENESIS_HASH
    hashes: set[str] = set()
    count = 0
    if not path.exists():
        return Chain(previous, count, frozenset())

    chain_fields = {"version", "sequence", "event_id", "timestamp", "draft_id", "previous_hash", "hash"}
    base_fields = chain_fields | {"action", "payload_hash"}
    viewed_fields = chain_fields | {"event", "reviewer_name"}
    generation_fields = {
        "generation_id", "status", "checks", "failed_rules", "reason_codes",
        "rules_hash", "output_hash", "reviewer_name",
    }
    review_fields = {"reason", "released_funder_hash"}
    with path.open("rb") as ledger:
        for sequence, line in enumerate(ledger, start=1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise LedgerIntegrityError("Invalid ledger structure.")
                version = row.get("version")
                row_rules = RULE_NAMES if version in {7, 8} else LEGACY_RULE_NAMES
                required = base_fields if version == 1 else base_fields | generation_fields
                if version == 6:
                    required = viewed_fields
                if version in {3, 4, 5, 8}:
                    required |= review_fields
                if version in {4, 5, 8}:
                    required |= {"released_funder_view"}
                if version in {5, 8}:
                    required |= {"reason_code"}
                if (
                    not line.endswith(b"\n")
                    or not isinstance(row, dict)
                    or set(row) != required
                    or type(row["version"]) is not int
                    or row["version"] not in {1, 2, 3, 4, 5, 6, 7, 8}
                    or type(row["sequence"]) is not int
                    or row["sequence"] != sequence
                    or row["previous_hash"] != previous
                    or any(not isinstance(row[key], str) for key in chain_fields - {"version", "sequence"})
                    or (version != 6 and (
                        not isinstance(row["action"], str) or not isinstance(row["payload_hash"], str)
                        or len(row["payload_hash"]) != 64
                    ))
                ):
                    raise LedgerIntegrityError("Invalid ledger structure.")
                if version == 6:
                    if (
                        row["event"] != "VIEWED" or not isinstance(row["reviewer_name"], str)
                        or not is_named_human(row["reviewer_name"])
                    ):
                        raise LedgerIntegrityError("Invalid draft view event.")
                elif version == 1:
                    if row["action"] not in {"draft.create.requested", "draft.update.requested"}:
                        raise LedgerIntegrityError("Invalid draft action.")
                elif (
                    (version in {2, 7} and (
                        row["action"] != "views.generate"
                        or row["status"] not in {"STARTED", "READY_FOR_REVIEW", "BLOCKED", "ERROR"}
                    ))
                    or (version in {3, 4, 5, 8} and (
                        row["action"] not in {"views.release", "views.refuse"}
                        or row["status"] not in {"RELEASED", "REFUSED", "BLOCKED"}
                        or not isinstance(row["reason"], str)
                        or (version in {3, 4} and row["status"] == "REFUSED" and not row["reason"].strip())
                        or (row["status"] == "RELEASED" and row["action"] != "views.release")
                        or (row["status"] == "REFUSED" and row["action"] != "views.refuse")
                        or not isinstance(row["released_funder_hash"], str)
                        or len(row["released_funder_hash"]) != 64
                    ))
                    or not isinstance(row["checks"], dict) or set(row["checks"]) != set(row_rules)
                    or any(value not in {"PASS", "FAIL", "NOT_EVALUATED"} for value in row["checks"].values())
                    or row["failed_rules"] != [name for name in row_rules if row["checks"][name] == "FAIL"]
                    or (row["status"] == "BLOCKED") != bool(row["failed_rules"])
                    or not isinstance(row["reason_codes"], dict)
                    or any(not isinstance(value, str) for value in row["reason_codes"].values())
                    or any(not isinstance(row[key], str) for key in ("generation_id", "reviewer_name", "rules_hash", "output_hash"))
                    or len(row["rules_hash"]) != 64 or len(row["output_hash"]) != 64
                ):
                    raise LedgerIntegrityError("Invalid generation audit row.")
                if version in {3, 4, 5, 8} and row["status"] == "RELEASED" and any(value != "PASS" for value in row["checks"].values()):
                    raise LedgerIntegrityError("A release must pass every rule.")
                if version in {4, 5, 8}:
                    funder = row["released_funder_view"]
                    if row["status"] == "RELEASED":
                        if not isinstance(funder, dict) or row["released_funder_hash"] != digest(funder):
                            raise LedgerIntegrityError("Released ledger content failed verification.")
                    elif funder is not None:
                        raise LedgerIntegrityError("Only a release may contain a funder snapshot.")
                if version in {5, 8}:
                    code = row["reason_code"]
                    if (
                        not isinstance(code, str) or (code and code not in REFUSAL_REASONS)
                        or (row["status"] == "REFUSED" and code not in REFUSAL_REASONS)
                        or (row["action"] == "views.release" and (code or row["reason"]))
                        or (code != "other" and row["reason"])
                        or (row["status"] == "REFUSED" and row["checks"]["no_identifiers"] != "PASS")
                    ):
                        raise LedgerIntegrityError("Invalid coded refusal.")
                recorded = row.pop("hash")
                calculated = hashlib.sha256(_canonical(row)).hexdigest()
                if not hmac.compare_digest(recorded, calculated):
                    raise LedgerIntegrityError("Ledger hash mismatch.")
            except (ValueError, UnicodeError, TypeError, KeyError) as error:
                raise LedgerIntegrityError("Invalid ledger row.") from error
            previous = recorded
            hashes.add(recorded)
            count = sequence
    return Chain(previous, count, frozenset(hashes))


def _verified_chain(store, connection) -> Chain:
    chain = verify_ledger(store.ledger_path)
    for saved in connection.execute(
        "SELECT ledger_hash FROM drafts UNION ALL SELECT ledger_hash FROM generations "
        "UNION ALL SELECT ledger_hash FROM reviews"
    ):
        if saved["ledger_hash"] not in chain.hashes:
            raise LedgerIntegrityError("A saved record has a missing ledger row.")
    return chain


def _append_ledger(path: Path, row: dict) -> None:
    """Append one line; never truncate or rewrite an existing ledger."""
    encoded = _canonical(row) + b"\n"
    with path.open("ab") as ledger:
        if ledger.write(encoded) != len(encoded):
            raise OSError("Incomplete ledger write.")
        ledger.flush()
        os.fsync(ledger.fileno())


class Store:
    def __init__(self, data_dir: Path, rules_path: Path = DEFAULT_RULES_PATH):
        self.data_dir = data_dir
        self.database_path = data_dir / "drafts.db"
        self.ledger_path = data_dir / "ledger.jsonl"
        self.rules_path = rules_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY,
                    client_reference TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ledger_hash TEXT NOT NULL UNIQUE
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    rules_hash TEXT NOT NULL,
                    reviewer_name TEXT NOT NULL,
                    internal_note TEXT,
                    funder_view TEXT,
                    checks TEXT NOT NULL,
                    reason_codes TEXT NOT NULL,
                    ledger_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL UNIQUE,
                    draft_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('RELEASED', 'REFUSED')),
                    reviewer_name TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    funder_snapshot TEXT,
                    ledger_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )"""
            )
            if "reason_code" not in {row["name"] for row in connection.execute("PRAGMA table_info(reviews)")}:
                connection.execute("ALTER TABLE reviews ADD COLUMN reason_code TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_release_per_note "
                "ON reviews(draft_id) WHERE decision = 'RELEASED'"
            )

    def list_drafts(self) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT id, client_reference, session_date, created_at, updated_at "
                "FROM drafts ORDER BY updated_at DESC, id"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_draft(self, draft_id: str) -> dict:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT id, client_reference, session_date, note, created_at, updated_at "
                "FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise DraftNotFoundError()
        return dict(row)

    def latest_generation(self, draft_id: str) -> dict | None:
        draft = self.get_draft(draft_id)
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE draft_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
            review = connection.execute(
                "SELECT * FROM reviews WHERE generation_id = ?", (row["id"],)
            ).fetchone() if row is not None else None
        if row is None:
            return None
        result = dict(row)
        for key in ("funder_view", "checks", "reason_codes"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result["checks"].setdefault("hipaa_safe_harbor", "NOT_EVALUATED")
        try:
            current_policy_hash = load_policy(self.rules_path).hash
        except (OSError, ValueError, TypeError, KeyError):
            current_policy_hash = None
        source = {key: draft[key] for key in ("client_reference", "session_date", "note")}
        result["is_current"] = result["source_hash"] == digest(source) and result["rules_hash"] == current_policy_hash
        if review is not None:
            result.update(
                status=review["decision"], reviewer_name=review["reviewer_name"],
                review_reason=review["reason"], review_reason_code=review["reason_code"], reviewed_at=review["created_at"],
                review_ledger_hash=review["ledger_hash"],
            )
        elif not result["is_current"]:
            result.update(status="STALE", internal_note=None, funder_view=None)
        result["failed_rules"] = [name for name in RULE_NAMES if result["checks"][name] == "FAIL"]
        return result

    def ledger_rows(self) -> dict:
        # The same writer lock gives readers one complete, verified ledger snapshot.
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            chain = _verified_chain(self, connection)
            rows = []
            if self.ledger_path.exists():
                with self.ledger_path.open(encoding="utf-8") as ledger:
                    rows = [json.loads(line) for line in ledger]
            return {"rows": rows, "count": chain.length, "head_hash": chain.head}

    def quarterly_totals(self, year: int, quarter: int) -> dict:
        if not 1 <= year <= 9999 or not 1 <= quarter <= 4:
            raise ValueError("Choose a valid year and quarter.")
        count = sessions = 0
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _verified_chain(self, connection)
            by_hash = {}
            if self.ledger_path.exists():
                with self.ledger_path.open(encoding="utf-8") as ledger:
                    by_hash = {row["hash"]: row for row in (json.loads(line) for line in ledger)}
            for review in connection.execute("SELECT * FROM reviews WHERE decision = 'RELEASED'"):
                try:
                    row = by_hash[review["ledger_hash"]]
                    saved_funder = json.loads(review["funder_snapshot"])
                    funder = row["released_funder_view"] if row["version"] in {4, 5, 8} else saved_funder
                    if (
                        row["status"] != "RELEASED" or row["action"] != "views.release"
                        or row["generation_id"] != review["generation_id"]
                        or row["draft_id"] != review["draft_id"]
                        or row["released_funder_hash"] != digest(funder)
                        or digest(saved_funder) != digest(funder)
                        or type(funder["session_count"]) is not int
                        or not 0 <= funder["session_count"] <= 1
                    ):
                        raise LedgerIntegrityError("A released snapshot failed verification.")
                    period = date.fromisoformat(funder["reporting_period"] + "-01")
                except (KeyError, TypeError, ValueError) as error:
                    raise LedgerIntegrityError("Invalid released snapshot.") from error
                if period.year == year and (period.month - 1) // 3 + 1 == quarter:
                    count += 1
                    sessions += funder["session_count"]
        return {"year": year, "quarter": quarter, "released_rows": count, "session_count": sessions}


def gate(
    store: Store, draft: DraftInput | None = None, draft_id: str | None = None, *,
    action: str = "save", reviewer_name: str = "", release_requested: bool = False,
    generator=None, generation_id: str | None = None, reason: str = "", reason_code: str = "",
) -> dict:
    """Draft access and mutations: lock, verify, durably log, then return or store.

    BEGIN IMMEDIATE obtains SQLite's cross-process writer lock; it changes no draft
    data. Holding it until commit also serializes ledger appends across workers.
    A database failure after logging leaves a valid request row in the ledger.
    """
    if action not in {"save", "generate", "release", "refuse", "view"}:
        raise ValueError("Unknown gate action.")
    if action == "save" and draft is None:
        raise ValueError("A draft is required for saving.")
    if action == "view" and draft_id is None:
        raise ValueError("A draft is required for viewing.")
    payload = draft.model_dump(mode="json") if draft is not None else None
    with closing(store.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        generation = None
        if action in {"release", "refuse"}:
            generation = connection.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
            if generation is None:
                raise GenerationNotFoundError()
            draft_id = generation["draft_id"]
        existing = None
        if draft_id is not None:
            existing = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if existing is None:
                raise DraftNotFoundError()

        chain = _verified_chain(store, connection)

        if action == "view":
            reviewer_name = reviewer_name.strip()
            if not is_named_human(reviewer_name):
                raise ReviewerNameRequiredError("Enter the reviewing counselor’s name to open this note.")
            row = {
                "version": 6, "sequence": chain.length + 1, "event_id": str(uuid4()),
                "event": "VIEWED", "draft_id": draft_id, "reviewer_name": reviewer_name,
                "timestamp": now, "previous_hash": chain.head,
            }
            row["hash"] = hashlib.sha256(_canonical(row)).hexdigest()
            _append_ledger(store.ledger_path, row)
            connection.commit()
            return {key: existing[key] for key in (
                "id", "client_reference", "session_date", "note", "created_at", "updated_at",
            )}

        if action in {"release", "refuse"}:
            checks = {name: "NOT_EVALUATED" for name in RULE_NAMES}
            reasons = {}
            reviewer_name = reviewer_name.strip()
            submitted_reason = reason.strip() if action == "refuse" else ""
            submitted_code = reason_code.strip() if action == "refuse" else ""
            reason_code = submitted_code if submitted_code in REFUSAL_REASONS else ""
            unsafe_reason = action == "refuse" and contains_identifiers(submitted_reason, existing["client_reference"])
            reason = (
                submitted_reason if reason_code == "other" and not unsafe_reason
                and len(submitted_reason) <= 1000 else ""
            )
            if action == "refuse":
                checks["no_identifiers"] = "FAIL" if unsafe_reason else "PASS"
                if unsafe_reason:
                    reasons["no_identifiers"] = "identifier_in_refusal_reason"
            checks["named_human"] = "PASS" if is_named_human(reviewer_name) else "FAIL"
            if checks["named_human"] == "FAIL":
                reasons["named_human"] = "named_reviewer_required"
            prior = connection.execute("SELECT id FROM reviews WHERE generation_id = ?", (generation_id,)).fetchone()
            latest = connection.execute(
                "SELECT id FROM generations WHERE draft_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (draft_id,),
            ).fetchone()
            state_error = None
            if generation["status"] != "READY_FOR_REVIEW":
                state_error = "generation_not_ready"
            elif prior is not None:
                state_error = "already_reviewed"
            elif latest["id"] != generation_id:
                state_error = "newer_generation_exists"
            elif action == "refuse" and not submitted_code:
                state_error = "refusal_reason_required"
            elif action == "refuse" and submitted_code not in REFUSAL_REASONS:
                state_error = "refusal_reason_invalid"
            elif action == "refuse" and len(submitted_reason) > 1000:
                state_error = "refusal_reason_too_long"
            elif action == "refuse" and submitted_code != "other" and submitted_reason:
                state_error = "refusal_details_only_for_other"
            elif action == "release" and connection.execute(
                "SELECT id FROM reviews WHERE draft_id = ? AND decision = 'RELEASED'", (draft_id,)
            ).fetchone() is not None:
                state_error = "note_already_released"
            checks["release_gate"] = "FAIL" if state_error else "PASS"
            if state_error:
                reasons["release_gate"] = state_error

            funder = original = None
            if action == "release":
                source = {key: existing[key] for key in ("client_reference", "session_date", "note")}
                try:
                    funder = json.loads(generation["funder_view"]) if generation["funder_view"] else None
                except (ValueError, TypeError):
                    funder = None
                try:
                    policy = load_policy(store.rules_path)
                    checks["allowed_fields"] = "PASS" if isinstance(funder, dict) and set(funder) == set(policy.schema["properties"]) else "FAIL"
                    if checks["allowed_fields"] == "FAIL":
                        reasons["allowed_fields"] = "unexpected_or_missing_fields"
                    try:
                        policy.funder_model.model_validate(funder)
                        checks["no_narrative"] = "PASS"
                    except (ValidationError, TypeError, ValueError):
                        checks["no_narrative"] = "FAIL"
                        reasons["no_narrative"] = "unconstrained_or_invalid_funder_value"
                    unsafe_harbor = contains_safe_harbor_identifiers(funder, policy, source["client_reference"])
                    checks["hipaa_safe_harbor"] = "FAIL" if unsafe_harbor else "PASS"
                    if unsafe_harbor:
                        reasons["hipaa_safe_harbor"] = "safe_harbor_identifier_in_funder_view"
                    if generation["rules_hash"] != policy.hash and not state_error:
                        state_error = "rules_changed"
                except (OSError, ValueError, TypeError, KeyError):
                    checks["allowed_fields"] = "FAIL"
                    reasons["allowed_fields"] = "invalid_rules_file"
                identifiers = requests_client_list(source["note"]) or contains_identifiers(funder, source["client_reference"])
                checks["no_identifiers"] = "FAIL" if identifiers else "PASS"
                if identifiers:
                    reasons["no_identifiers"] = "identifier_in_funder_view"
                original = None
                with store.ledger_path.open(encoding="utf-8") as ledger:
                    original = next((json.loads(line) for line in ledger if json.loads(line)["hash"] == generation["ledger_hash"]), None)
                content_hash = digest({"internal_note": generation["internal_note"], "funder_view": funder})
                if generation["source_hash"] != digest(source) and not state_error:
                    state_error = "stale_generation"
                if isinstance(funder, dict) and funder.get("reporting_period") != source["session_date"][:7] and not state_error:
                    state_error = "reporting_period_mismatch"
                if (
                    original is None or original.get("status") != "READY_FOR_REVIEW"
                    or original.get("output_hash") != content_hash
                    or original.get("payload_hash") != generation["source_hash"]
                    or original.get("rules_hash") != generation["rules_hash"]
                    or original.get("generation_id") != generation_id
                    or original.get("draft_id") != draft_id
                ) and not state_error:
                    state_error = "generated_content_changed"
                if state_error:
                    checks["release_gate"] = "FAIL"
                    reasons["release_gate"] = state_error

            failed = [name for name in RULE_NAMES if checks[name] == "FAIL"]
            status = "BLOCKED" if failed else ("RELEASED" if action == "release" else "REFUSED")
            row = {
                "version": 8, "sequence": chain.length + 1, "event_id": str(uuid4()),
                "timestamp": now, "action": "views." + action, "draft_id": draft_id,
                "payload_hash": generation["source_hash"], "previous_hash": chain.head,
                "generation_id": generation_id, "status": status, "checks": checks,
                "failed_rules": failed, "reason_codes": reasons,
                "rules_hash": generation["rules_hash"],
                "output_hash": original.get("output_hash", GENESIS_HASH) if action == "release" and original else GENESIS_HASH,
                "reviewer_name": reviewer_name, "reason_code": reason_code, "reason": reason,
                "released_funder_hash": digest(funder) if status == "RELEASED" else GENESIS_HASH,
                "released_funder_view": funder if status == "RELEASED" else None,
            }
            row["hash"] = hashlib.sha256(_canonical(row)).hexdigest()
            _append_ledger(store.ledger_path, row)
            if status != "BLOCKED":
                connection.execute(
                    """INSERT INTO reviews
                       (id, generation_id, draft_id, decision, reviewer_name, reason_code, reason,
                        funder_snapshot, ledger_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid4()), generation_id, draft_id, status, reviewer_name, reason_code, reason,
                     json.dumps(funder, ensure_ascii=False) if status == "RELEASED" else None,
                     row["hash"], now),
                )
            connection.commit()
            return {"status": status, "generation_id": generation_id, "draft_id": draft_id,
                    "reviewer_name": reviewer_name, "reason_code": reason_code, "reason": reason, "checks": checks,
                    "failed_rules": failed, "reason_codes": reasons, "ledger_hash": row["hash"]}

        if action == "generate":
            if existing is None:
                raise DraftNotFoundError()
            source = {key: existing[key] for key in ("client_reference", "session_date", "note")}
            source_hash = digest(source)
            generation_id = str(uuid4())
            checks = {name: "NOT_EVALUATED" for name in RULE_NAMES}
            reasons = {}
            output_hash = GENESIS_HASH
            try:
                policy = load_policy(store.rules_path)
            except (OSError, ValueError, TypeError, KeyError):
                policy = None
                checks["allowed_fields"] = "FAIL"
                reasons["allowed_fields"] = "invalid_rules_file"

            # All rule decisions live inside the sole write/action gate.
            if requests_client_list(source["note"]):
                checks["no_identifiers"] = "FAIL"
                reasons["no_identifiers"] = "client_list_by_diagnosis_requested"
            else:
                checks["no_identifiers"] = "PASS"
                checks["named_human"] = "PASS" if is_named_human(reviewer_name) else "FAIL"
                if checks["named_human"] == "FAIL":
                    reasons["named_human"] = "named_reviewer_required"
                # This step only creates local previews. There is no release capability.
                checks["release_gate"] = "FAIL" if release_requested else "PASS"
                if release_requested:
                    reasons["release_gate"] = "release_requires_separate_human_review"

            def audit(status):
                nonlocal chain
                row = {
                    "version": 7, "sequence": chain.length + 1, "event_id": str(uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    "action": "views.generate", "draft_id": draft_id,
                    "payload_hash": source_hash, "previous_hash": chain.head,
                    "generation_id": generation_id, "status": status,
                    "checks": dict(checks),
                    "failed_rules": [name for name in RULE_NAMES if checks[name] == "FAIL"],
                    "reason_codes": dict(reasons),
                    "rules_hash": policy.hash if policy else GENESIS_HASH,
                    "output_hash": output_hash, "reviewer_name": reviewer_name.strip(),
                }
                row["hash"] = hashlib.sha256(_canonical(row)).hexdigest()
                _append_ledger(store.ledger_path, row)
                chain = Chain(row["hash"], chain.length + 1, chain.hashes | {row["hash"]})
                return row

            result = None
            if any(value == "FAIL" for value in checks.values()):
                status = "BLOCKED"
            else:
                started = audit("STARTED")  # Durable before the model receives any note text.
                try:
                    if generator is None:
                        from app.generation import produce_views
                        generator = produce_views
                    candidate = generator(source, policy)
                except Exception as error:
                    from app.generation import GENERATION_REASON_CODES, GenerationUnavailable
                    reasons["generation"] = (
                        str(error) if isinstance(error, GenerationUnavailable)
                        and str(error) in GENERATION_REASON_CODES
                        else "generation_failed"
                    )
                    status = "ERROR"
                else:
                    funder = candidate.get("funder_view") if isinstance(candidate, dict) else None
                    allowed = (
                        isinstance(candidate, dict) and set(candidate) == {"internal_note", "funder_view"}
                        and isinstance(funder, dict) and set(funder) == set(policy.schema["properties"])
                    )
                    checks["allowed_fields"] = "PASS" if allowed else "FAIL"
                    if not allowed:
                        reasons["allowed_fields"] = "unexpected_or_missing_fields"
                    identifiers = contains_identifiers(funder, source["client_reference"])
                    checks["no_identifiers"] = "FAIL" if identifiers else "PASS"
                    if identifiers:
                        reasons["no_identifiers"] = "identifier_in_funder_view"
                    unsafe_harbor = contains_safe_harbor_identifiers(funder, policy, source["client_reference"])
                    checks["hipaa_safe_harbor"] = "FAIL" if unsafe_harbor else "PASS"
                    if unsafe_harbor:
                        reasons["hipaa_safe_harbor"] = "safe_harbor_identifier_in_funder_view"
                    try:
                        policy.funder_model.model_validate(funder)
                        valid = policy.output_model.model_validate(candidate)
                        checks["no_narrative"] = "PASS"
                    except (ValidationError, TypeError, ValueError):
                        checks["no_narrative"] = "FAIL"
                        reasons["no_narrative"] = "unconstrained_or_invalid_funder_value"
                        valid = None
                    try:
                        output_hash = digest(candidate) if isinstance(candidate, dict) else GENESIS_HASH
                    except (TypeError, ValueError):
                        output_hash = GENESIS_HASH
                    if any(value == "FAIL" for value in checks.values()):
                        status = "BLOCKED"
                    else:
                        status = "READY_FOR_REVIEW"
                        result = valid.model_dump(mode="json")
                # Detect an out-of-band ledger change during a model call.
                chain = verify_ledger(store.ledger_path)
                if chain.head != started["hash"]:
                    raise LedgerIntegrityError("The ledger changed during generation.")

            row = audit(status)
            internal = result["internal_note"] if result else None
            funder = result["funder_view"] if result else None
            connection.execute(
                """INSERT INTO generations
                   (id, draft_id, status, source_hash, rules_hash, reviewer_name,
                    internal_note, funder_view, checks, reason_codes, ledger_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (generation_id, draft_id, status, source_hash, row["rules_hash"],
                 reviewer_name.strip(), internal,
                 json.dumps(funder, ensure_ascii=False) if funder is not None else None,
                 json.dumps(checks), json.dumps(reasons), row["hash"], row["timestamp"]),
            )
            connection.commit()
            return {
                "id": generation_id, "draft_id": draft_id, "status": status,
                "checks": checks, "failed_rules": row["failed_rules"], "reason_codes": reasons,
                "internal_note": internal, "funder_view": funder,
                "reviewer_name": reviewer_name.strip(), "ledger_hash": row["hash"],
                "created_at": row["timestamp"],
            }

        draft_id = draft_id or str(uuid4())
        row = {
            "version": 1,
            "sequence": chain.length + 1,
            "event_id": str(uuid4()),
            "timestamp": now,
            "action": "draft.update.requested" if existing else "draft.create.requested",
            "draft_id": draft_id,
            "payload_hash": hashlib.sha256(_canonical(payload)).hexdigest(),
            "previous_hash": chain.head,
        }
        row["hash"] = hashlib.sha256(_canonical(row)).hexdigest()
        _append_ledger(store.ledger_path, row)

        # No draft INSERT or UPDATE is allowed above the durable ledger append.
        created_at = existing["created_at"] if existing else now
        connection.execute(
            """INSERT INTO drafts
               (id, client_reference, session_date, note, created_at, updated_at, ledger_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   client_reference = excluded.client_reference,
                   session_date = excluded.session_date,
                   note = excluded.note,
                   updated_at = excluded.updated_at,
                   ledger_hash = excluded.ledger_hash""",
            (draft_id, payload["client_reference"], payload["session_date"],
             payload["note"], created_at, now, row["hash"]),
        )
        connection.commit()
    return {"id": draft_id, **payload, "created_at": created_at, "updated_at": now}
