"""Constrained funder schema and small deterministic policy primitives."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


RULE_NAMES = ("allowed_fields", "no_identifiers", "no_narrative", "named_human", "release_gate")
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


def is_named_human(name: str) -> bool:
    return (
        2 <= len(name.strip()) <= 100 and any(character.isalpha() for character in name)
        and name.casefold().strip() not in {"ai", "agent", "system", "unknown", "anonymous", "none", "n/a"}
    )
