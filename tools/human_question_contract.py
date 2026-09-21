"""Closed typed-question contract for the Kanban ``needs_input`` boundary.

Ported selectively from the Human Wait v1 contract at immutable commit
``4a9cc92db1c1a844550984bdfec824c24ae222dc``: canonical JSON/hash,
closed-object validation, bounded UTF-8 text, question identifiers and typed
TEXT/CHOICE answers. Task/root correlation is owned by ``kanban_db`` rather
than supplied by the caller.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


HUMAN_QUESTION_SCHEMA = "kanban.human_question.v1"
HUMAN_ANSWER_SCHEMA = "kanban.human_answer.v1"
MAX_QUESTION_PROMPT_BYTES = 2_000
MAX_QUESTION_CONTEXT_BYTES = 1_000
MAX_CHOICE_BYTES = 200
MAX_HUMAN_QUESTION_BYTES = 8_192
MAX_ANSWER_BYTES = 4_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = (
    "schema_version",
    "question_id",
    "audience",
    "prompt",
    "answer_kind",
    "choices",
    "required",
    "context",
    "question_sha256",
)
_ANSWER_FIELDS = (
    "schema_version",
    "answer_id",
    "task_id",
    "root_task_id",
    "question_id",
    "question_sha256",
    "answer_kind",
    "status",
    "value",
    "answer_sha256",
)


class HumanQuestionContractError(ValueError):
    """A typed human-question payload violates its closed contract."""


def canonical_json(value: Any) -> str:
    """Canonical JSON shared by the typed question checksum and SQLite record."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise HumanQuestionContractError("question is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _closed(value: Any, expected: Sequence[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise HumanQuestionContractError("invalid closed question")
    return value


def _text(value: Any, label: str, *, max_bytes: int, allow_empty: bool = False) -> str:
    if type(value) is not str or value != value.strip() or (not value and not allow_empty):
        raise HumanQuestionContractError(f"invalid {label}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise HumanQuestionContractError(f"invalid {label}") from exc
    if len(encoded) > max_bytes or "\x00" in value:
        raise HumanQuestionContractError(f"invalid {label}")
    return value


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise HumanQuestionContractError(f"invalid {label}")
    return value


def validate_human_question(value: Any) -> dict[str, Any]:
    """Validate and normalize the durable, hash-bound typed question payload."""
    item = _closed(value, _FIELDS)
    if item["schema_version"] != HUMAN_QUESTION_SCHEMA:
        raise HumanQuestionContractError("invalid question schema_version")
    audience = item["audience"]
    answer_kind = item["answer_kind"]
    if type(audience) is not str or audience not in {"HUMAN", "EXPERT"}:
        raise HumanQuestionContractError("invalid audience")
    if type(answer_kind) is not str or answer_kind not in {"TEXT", "CHOICE"}:
        raise HumanQuestionContractError("invalid answer_kind")
    choices_raw = item["choices"]
    if type(choices_raw) not in {list, tuple}:
        raise HumanQuestionContractError("invalid choices")
    choices = [_text(choice, "choice", max_bytes=MAX_CHOICE_BYTES) for choice in choices_raw]
    if (
        (answer_kind == "TEXT" and choices)
        or (answer_kind == "CHOICE" and not 2 <= len(choices) <= 8)
        or len(choices) != len(set(choices))
    ):
        raise HumanQuestionContractError("choices do not match answer_kind")
    if type(item["required"]) is not bool:
        raise HumanQuestionContractError("invalid required")
    body = {
        "schema_version": HUMAN_QUESTION_SCHEMA,
        "question_id": _identifier(item["question_id"], "question_id"),
        "audience": audience,
        "prompt": _text(item["prompt"], "prompt", max_bytes=MAX_QUESTION_PROMPT_BYTES),
        "answer_kind": answer_kind,
        "choices": choices,
        "required": item["required"],
        "context": _text(
            item["context"], "context", max_bytes=MAX_QUESTION_CONTEXT_BYTES, allow_empty=True,
        ),
    }
    digest = item["question_sha256"]
    if type(digest) is not str or _HASH_RE.fullmatch(digest) is None:
        raise HumanQuestionContractError("invalid question_sha256")
    if digest != canonical_sha256(body):
        raise HumanQuestionContractError("divergent question_sha256")
    result = {**body, "question_sha256": digest}
    if len(canonical_json(result).encode("utf-8")) > MAX_HUMAN_QUESTION_BYTES:
        raise HumanQuestionContractError("question is too large")
    return result


def validate_human_answer(value: Any, *, question: Any) -> dict[str, Any]:
    """Validate a closed, question-bound answer before a Kanban resume write."""
    normalized_question = validate_human_question(question)
    item = _closed(value, _ANSWER_FIELDS)
    if item["schema_version"] != HUMAN_ANSWER_SCHEMA:
        raise HumanQuestionContractError("invalid answer schema_version")
    if (
        item["question_id"] != normalized_question["question_id"]
        or item["question_sha256"] != normalized_question["question_sha256"]
        or item["answer_kind"] != normalized_question["answer_kind"]
    ):
        raise HumanQuestionContractError("answer is bound to another question")
    status = item["status"]
    if type(status) is not str or status not in {"ANSWERED", "SKIPPED"}:
        raise HumanQuestionContractError("invalid answer status")
    if status == "SKIPPED":
        if normalized_question["required"] or item["value"] is not None:
            raise HumanQuestionContractError("a required question cannot be skipped")
        answer_value = None
    else:
        answer_value = _text(item["value"], "answer.value", max_bytes=MAX_ANSWER_BYTES)
        if (
            normalized_question["answer_kind"] == "CHOICE"
            and answer_value not in normalized_question["choices"]
        ):
            raise HumanQuestionContractError("answer choice is outside the closed question")
    body = {
        "schema_version": HUMAN_ANSWER_SCHEMA,
        "answer_id": _identifier(item["answer_id"], "answer_id"),
        "task_id": _identifier(item["task_id"], "task_id"),
        "root_task_id": _identifier(item["root_task_id"], "root_task_id"),
        "question_id": normalized_question["question_id"],
        "question_sha256": normalized_question["question_sha256"],
        "answer_kind": normalized_question["answer_kind"],
        "status": status,
        "value": answer_value,
    }
    digest = item["answer_sha256"]
    if type(digest) is not str or _HASH_RE.fullmatch(digest) is None:
        raise HumanQuestionContractError("invalid answer_sha256")
    if digest != canonical_sha256(body):
        raise HumanQuestionContractError("divergent answer_sha256")
    return {**body, "answer_sha256": digest}
