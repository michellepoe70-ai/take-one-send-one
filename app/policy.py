"""Constrained funder schema and small deterministic policy primitives."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


LEGACY_RULE_NAMES = ("allowed_fields", "no_identifiers", "no_narrative", "named_human", "release_gate")
RULE_NAMES = LEGACY_RULE_NAMES + ("hipaa_safe_harbor",)
PERIOD_PATTERN = "^[0-9]{4}-(0[1-9]|1[0-2])$"
DEFAULT_RULES_PATH = Path(__file__).parent.parent / "rules" / "funder_view.json"
IDENTIFIER_KEYS = {
    "name", "names", "client", "clients", "client_id", "client_list", "client_reference",
    "patient_id", "diagnosis", "diagnosis_code", "diagnosis_codes", "icd_code",
    "email", "phone", "address", "dob", "date_of_birth", "ssn",
}


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Policy:
    schema: dict
    hash: str
    funder_model: type[BaseModel]
    output_model: type[BaseModel]


def load_policy(path: Path = DEFAULT_RULES_PATH) -> Policy:
    schema = json.loads(path.read_text(encoding="utf-8"))
    properties = schema.get("properties", {})
    if (
        schema.get("type") != "object" or schema.get("additionalProperties") is not False
        or not properties or set(schema.get("required", [])) != set(properties)
    ):
        raise ValueError("The funder schema must be a closed object with required fields.")
    fields = {}
    for name, spec in properties.items():
        if spec.get("type") == "string" and spec.get("enum"):
            if not all(isinstance(item, str) and item for item in spec["enum"]):
                raise ValueError("Funder enumerations must contain nonempty strings.")
            annotation = Literal.__getitem__(tuple(spec["enum"]))
            constraint = Field()
        elif name == "reporting_period" and spec.get("pattern") == PERIOD_PATTERN:
            annotation = str
            constraint = Field(pattern=PERIOD_PATTERN)
        elif spec.get("type") == "integer" and "minimum" in spec and "maximum" in spec:
            annotation = int
            constraint = Field(ge=spec["minimum"], le=spec["maximum"])
        else:
            raise ValueError("Unconstrained narrative fields are not permitted.")
        fields[name] = (annotation, constraint)
    funder_model = create_model(
        "FunderView", __config__=ConfigDict(extra="forbid", strict=True), **fields,
    )
    output_model = create_model(
        "NoteViews", __config__=ConfigDict(extra="forbid", strict=True),
        internal_note=(str, Field(min_length=1, max_length=20000)),
        funder_view=(funder_model, ...),
    )
    return Policy(schema, digest(schema), funder_model, output_model)


def requests_client_list(source: str) -> bool:
    text = source.casefold()
    return bool(
        re.search(r"\b(client|clients|patient|patients|participant|participants|clinets)\b", text)
        and re.search(r"\b(list|lookup|look up|find|identify|selected|matching|meeting)\b", text)
        and re.search(r"diagnos|\bicd[- ]?\d*|demographics.{0,80}codes", text)
    )


def contains_identifiers(value, client_reference: str) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in IDENTIFIER_KEYS or contains_identifiers(item, client_reference)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_identifiers(item, client_reference) for item in value)
    if isinstance(value, str):
        return bool(
            (len(client_reference.strip()) >= 3 and client_reference.casefold() in value.casefold())
            or re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
            or re.search(r"(?:\+?\d[\d ().-]{6,}\d)", value)
            or re.search(r"\b(?:ICD[- ]?10|diagnosis\s+code|SSN)\b", value, re.I)
        )
    return False


# HHS Safe Harbor categories. Closed-field checks below also reject unrecognized
# names, locations, media, and opaque codes that cannot be identified by regex.
SAFE_HARBOR_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:names?|first[\s_-]*name|last[\s_-]*name|patient[\s_-]*name|client[\s_-]*name)\s*[:=]",
    r"\b\d{5}(?:-\d{4})?\b|\b(?:street|address|city|county|precinct|zip|postal[\s_-]*code)\b",
    r"\b\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b|\b\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?\b"
    r"|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b"
    r"|\b(?:age|aged)\s*[:=]?\s*(?:9\d|[1-9]\d{2,})\b",
    r"(?:\+?\d[\d ().-]{6,}\d)",
    r"\b(?:fax|facsimile)\b",
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b|\b(?:ssn|social[\s_-]*security)\b",
    r"\b(?:mrn|medical[\s_-]*record(?:[\s_-]*(?:number|no|id))?)\b",
    r"\b(?:health[\s_-]*plan|beneficiary|member[\s_-]*(?:number|no|id))\b",
    r"\b(?:account|acct)(?:[\s_-]*(?:number|no|id))?\b",
    r"\b(?:certificate|licen[cs]e)(?:[\s_-]*(?:number|no|id))?\b",
    r"\b(?:vin|vehicle|licen[cs]e[\s_-]*plate)\b|\b[A-HJ-NPR-Z0-9]{17}\b",
    r"\b(?:device|serial|imei|udi|mac[\s_-]*address)\b",
    r"\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}(?:/\S*)?",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?<!\w)(?:[0-9a-f]{0,4}:){2,}[0-9a-f:.]{0,39}(?!\w)",
    r"\b(?:biometric|finger[\s_-]*print|voice[\s_-]*print|retina|iris[\s_-]*scan|facial[\s_-]*template)\b",
    r"data:image/|<img\b|\b(?:full[\s_-]*face|face[\s_-]*(?:photo|image)|photograph|portrait)\b",
    r"\b(?:unique[\s_-]*(?:id|identifier|code)|identifying[\s_-]*code|uuid|guid)\b"
    r"|\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b",
))
FUNDER_FIELDS = ("reporting_period", "service_type", "session_count", "language", "service_area")


def contains_safe_harbor_identifiers(funder_view, policy: Policy, client_reference: str = "") -> bool:
    """Screen only the five funder values, with exact reporting-code exceptions."""
    if not isinstance(funder_view, dict):
        return True
    properties = policy.schema["properties"]
    for field in FUNDER_FIELDS:
        if field not in funder_view:
            continue  # Missing fields are handled by the existing allowed_fields rule.
        value = funder_view[field]
        spec = properties.get(field, {})
        if field == "reporting_period":
            if isinstance(value, str) and spec.get("pattern") == PERIOD_PATTERN and re.fullmatch(PERIOD_PATTERN, value):
                continue
        if field == "service_area":
            # These exact funder-owned values are program-site codes, not addresses.
            if isinstance(value, str) and value in spec.get("enum", ()):
                continue
            return True
        if contains_identifiers(value, client_reference):
            return True
        if isinstance(value, str) and any(pattern.search(value) for pattern in SAFE_HARBOR_PATTERNS):
            return True
        # Conservative fallback: arbitrary names, addresses, photos/biometrics,
        # and other unique codes cannot pass as unrecognized text or objects.
        if spec.get("type") == "string" and spec.get("enum"):
            if not isinstance(value, str) or value not in spec["enum"]:
                return True
        elif spec.get("type") == "integer":
            if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
                return True
        else:
            return True
    return False


def is_named_human(name: str) -> bool:
    return (
        2 <= len(name.strip()) <= 100 and any(character.isalpha() for character in name)
        and name.casefold().strip() not in {"ai", "agent", "system", "unknown", "anonymous", "none", "n/a"}
    )
