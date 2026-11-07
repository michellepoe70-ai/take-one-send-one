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
    DEFAULT_RULES_PATH, RULE_NAMES, contains_identifiers, digest, is_named_human,
    load_policy, requests_client_list,
)


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

    base_fields = {
        "version", "sequence", "event_id", "timestamp", "action", "draft_id",
        "payload_hash", "previous_hash", "hash",
    }
    generation_fields = {
        "generation_id", "status", "checks", "failed_rules", "reason_codes",
        "rules_hash", "output_hash", "reviewer_name",
    }
    with path.open("rb") as ledger:
        for sequence, line in enumerate(ledger, start=1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise LedgerIntegrityError("Invalid ledger structure.")
                version = row.get("version")
                required = base_fields if version == 1 else base_fields | generation_fields
                if (
                    not line.endswith(b"\n")
                    or not isinstance(row, dict)
                    or set(row) != required
                    or type(row["version"]) is not int
                    or row["version"] not in {1, 2}
                    or type(row["sequence"]) is not int
                    or row["sequence"] != sequence
                    or row["previous_hash"] != previous
                    or any(not isinstance(row[key], str) for key in base_fields - {"version", "sequence"})
                    or len(row["payload_hash"]) != 64
                ):
                    raise LedgerIntegrityError("Invalid ledger structure.")
                if version == 1:
                    if row["action"] not in {"draft.create.requested", "draft.update.requested"}:
                        raise LedgerIntegrityError("Invalid draft action.")
                elif (
                    row["action"] != "views.generate"
                    or row["status"] not in {"STARTED", "READY_FOR_REVIEW", "BLOCKED", "ERROR"}
                    or not isinstance(row["checks"], dict) or set(row["checks"]) != set(RULE_NAMES)
                    or any(value not in {"PASS", "FAIL", "NOT_EVALUATED"} for value in row["checks"].values())
                    or row["failed_rules"] != [name for name in RULE_NAMES if row["checks"][name] == "FAIL"]
                    or (row["status"] == "BLOCKED") != bool(row["failed_rules"])
                    or not isinstance(row["reason_codes"], dict)
                    or any(not isinstance(value, str) for value in row["reason_codes"].values())
                    or any(not isinstance(row[key], str) for key in ("generation_id", "reviewer_name", "rules_hash", "output_hash"))
                    or len(row["rules_hash"]) != 64 or len(row["output_hash"]) != 64
                ):
                    raise LedgerIntegrityError("Invalid generation audit row.")
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
        if row is None:
            return None
        result = dict(row)
        for key in ("funder_view", "checks", "reason_codes"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        try:
            current_policy_hash = load_policy(self.rules_path).hash
        except (OSError, ValueError, TypeError, KeyError):
            current_policy_hash = None
        source = {key: draft[key] for key in ("client_reference", "session_date", "note")}
        if result["source_hash"] != digest(source) or result["rules_hash"] != current_policy_hash:
            result.update(status="STALE", internal_note=None, funder_view=None)
        result["failed_rules"] = [name for name in RULE_NAMES if result["checks"][name] == "FAIL"]
        return result


def gate(
    store: Store, draft: DraftInput | None = None, draft_id: str | None = None, *,
    action: str = "save", reviewer_name: str = "", release_requested: bool = False,
    generator=None,
) -> dict:
    """All draft mutations: lock, verify, durably log intent, then change SQLite.

    BEGIN IMMEDIATE obtains SQLite's cross-process writer lock; it changes no draft
    data. Holding it until commit also serializes ledger appends across workers.
    A database failure after logging leaves a valid request row in the ledger.
    """
    if action not in {"save", "generate"}:
        raise ValueError("Unknown gate action.")
    if action == "save" and draft is None:
        raise ValueError("A draft is required for saving.")
    payload = draft.model_dump(mode="json") if draft is not None else None
    with closing(store.connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        existing = None
        if draft_id is not None:
            existing = connection.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if existing is None:
                raise DraftNotFoundError()

        chain = verify_ledger(store.ledger_path)
        for saved in connection.execute("SELECT ledger_hash FROM drafts UNION ALL SELECT ledger_hash FROM generations"):
            if saved["ledger_hash"] not in chain.hashes:
                raise LedgerIntegrityError("A saved draft has a missing ledger row.")

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

            # All five rule decisions live inside the sole write/action gate.
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
                    "version": 2, "sequence": chain.length + 1, "event_id": str(uuid4()),
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
                    from app.generation import GenerationUnavailable
                    reasons["generation"] = (
                        str(error) if isinstance(error, GenerationUnavailable)
                        and str(error) in {"api_key_not_configured", "generation_failed"}
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
