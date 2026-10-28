"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: graph
layer: [security]
tags: [security, llm]
owner: engine-team
status: active
--- /L9_META ---

LLM Output Validation & Security
Implements: Gap Analysis P1-5 (AI Governance)

This module provides:
1. Prompt injection defense (input sanitization)
2. Output schema validation (Pydantic integration)
3. Cost/token monitoring

Installation:
    pip install pydantic

Usage:
    from engine.security.llm import sanitize_llm_input, validate_llm_output
"""

import json
import logging
import re
from collections.abc import Generator
from typing import TypeVar

from pydantic import AliasChoices, AliasPath, BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ============================================================
# Input Sanitization (Prompt Injection Defense)
# ============================================================

INJECTION_PATTERNS = [
    # Instruction injection
    r"ignore (previous|all|above) (instructions|commands|prompts)",
    r"forget (everything|all) (you were told|previous)",
    r"you (are|must|will) now",
    r"new (instructions|system prompt|role):",
    # Role manipulation
    r"(system|assistant):\s*(now|you are|from now)",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[INST\]",
    r"\[/INST\]",
    # Context poisoning
    r"previous (response|output|message) (was|said|contained)",
    r"you (previously|already|earlier) (said|stated|mentioned)",
    # Data exfiltration attempts
    r"(show|display|reveal|output) (your|the) (system prompt|instructions|context)",
]

INJECTION_REGEX = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE | re.MULTILINE)


def sanitize_llm_input(user_input: str, max_length: int = 2000) -> str:
    """
    Sanitize user input before sending to LLM.

    Defenses:
    - Remove prompt injection patterns
    - Truncate to max length
    - Strip special tokens
    - Normalize whitespace

    Args:
        user_input: Raw user input
        max_length: Maximum allowed length

    Returns:
        Sanitized input safe for LLM prompts

    Raises:
        ValueError: If input contains severe injection attempt
    """
    if not user_input:
        return ""

    # Check the raw input: special tokens that the stripping below removes
    # (e.g. <|im_start|>) are themselves rejection patterns.
    _reject_injection(user_input)

    # Truncate
    if len(user_input) > max_length:
        user_input = user_input[:max_length]
        logger.info(f"Input truncated to {max_length} characters")

    # Remove special tokens (model-specific)
    special_tokens = [
        "<|im_start|>",
        "<|im_end|>",
        "[INST]",
        "[/INST]",
        "<s>",
        "</s>",
        "###",
        "```system",
    ]
    for token in special_tokens:
        user_input = user_input.replace(token, "")

    # Normalize whitespace
    user_input = " ".join(user_input.split()).strip()

    # RU-072 / CEG-COR-119: check again on the final string — token stripping and
    # whitespace normalization can reassemble a blocked phrase
    # ("ignore  previous", "ignore prev<s>ious") that the raw check missed.
    _reject_injection(user_input)

    return user_input


def _reject_injection(text: str) -> None:
    """Raise ValueError when ``text`` matches an injection pattern."""
    matches = INJECTION_REGEX.findall(text.lower())
    if matches:
        logger.warning(f"Potential prompt injection detected: {matches[:3]}")
        raise ValueError(
            "Input contains potentially malicious patterns. Please rephrase your request without meta-instructions."
        )


# ============================================================
# Output Validation (Schema Enforcement)
# ============================================================


def _alias_keys(alias: str | AliasPath | AliasChoices) -> set[str]:
    """Top-level input keys a validation alias accepts."""
    if isinstance(alias, str):
        return {alias}
    if isinstance(alias, AliasPath):
        head = alias.path[0] if alias.path else None
        return {head} if isinstance(head, str) else set()
    keys: set[str] = set()
    for choice in alias.choices:
        keys |= _alias_keys(choice)
    return keys


def _accepted_input_keys(schema: type[BaseModel]) -> set[str]:
    """Top-level keys ``schema`` accepts on validation: field names and/or aliases.

    Mirrors pydantic's ``validate_by_alias`` / ``validate_by_name``
    (``populate_by_name``) so a key such as ``"from"`` that targets a field
    whose validation alias is ``"from"`` is not reported as undeclared.
    """
    config = schema.model_config
    by_alias = config.get("validate_by_alias", True)
    by_name = bool(config.get("validate_by_name") or config.get("populate_by_name"))
    keys: set[str] = set()
    for name, field in schema.model_fields.items():
        alias = field.validation_alias
        if alias is None or by_name:
            keys.add(name)
        if alias is not None and by_alias:
            keys |= _alias_keys(alias)
    return keys


def validate_llm_output(llm_response: str, expected_schema: type[T], strict: bool = True) -> T:
    """
    Validate LLM JSON output against Pydantic schema.

    Args:
        llm_response: Raw LLM response (JSON string)
        expected_schema: Pydantic model class
        strict: If True, reject extra fields

    Returns:
        Validated Pydantic model instance

    Raises:
        ValueError: If the response is not valid JSON.
        ValidationError: If the parsed JSON doesn't match the schema.

    Example:
        from pydantic import BaseModel

        class QueryResult(BaseModel):
            nodes: list[str]
            count: int

        response = llm.generate(...)
        validated = validate_llm_output(response, QueryResult)
    """
    try:
        # Parse JSON
        data = json.loads(llm_response)
    except json.JSONDecodeError as e:
        logger.exception("LLM returned invalid JSON")
        # pydantic's ValidationError is not string-constructible in v2; raise a
        # ValueError (its superclass) so the JSON-parse failure surfaces cleanly.
        msg = f"LLM output is not valid JSON: {e}"
        raise ValueError(msg) from e

    # RU-073 / CEG-COR-120: model_validate(strict=True) only disables type
    # coercion; extra keys are ignored unless the model forbids them. strict
    # promises "reject extra fields", so undeclared keys are refused here.
    # Declared keys are the ones pydantic itself accepts: field names and/or
    # validation aliases, per the schema's validate_by_name/validate_by_alias.
    if strict and isinstance(data, dict):
        extra = sorted(set(data) - _accepted_input_keys(expected_schema))
        if extra:
            msg = f"LLM output has unexpected field(s) for {expected_schema.__name__}: {extra}"
            raise ValueError(msg)

    # Validate against schema
    try:
        if strict:
            # Reject extra fields
            validated = expected_schema.model_validate(data, strict=True)
        else:
            # Allow extra fields
            validated = expected_schema.model_validate(data)

        logger.info(f"LLM output validated against {expected_schema.__name__}")
        return validated

    except ValidationError:
        logger.exception("LLM output failed schema validation")
        raise


# ============================================================
# Sandboxed Code Execution — REMOVED (Audit Finding T1-01)
# ============================================================
# REMOVED: safe_exec() and _worker() deleted due to RCE risk.
# RestrictedPython + multiprocessing.Process exec() does not provide
# sufficient sandboxing against __subclasses__ traversal, frame
# introspection, or marshal deserialization attacks.
#
# If sandboxed code execution is required in future, use an OS-level
# sandbox (gVisor, nsjail, Firecracker) or a WebAssembly runtime.
#
# See: Audit Finding T1-01 (CRITICAL)


# ============================================================
# Cost Monitoring
# ============================================================

from contextlib import contextmanager
from datetime import UTC, datetime

import structlog

cost_logger = structlog.get_logger("llm.cost")


@contextmanager
def track_llm_usage(model: str, user_id: str | None = None) -> Generator[None, None, None]:
    """
    Context manager to track LLM token usage and cost.

    Usage:
        with track_llm_usage(model="gpt-4-turbo", user_id="user123"):
            response = openai.chat.completions.create(...)
            # Token counts logged automatically
    """
    start_time = datetime.now(UTC)

    try:
        yield
    finally:
        duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000

        cost_logger.info(
            "llm_call", model=model, user_id=user_id, duration_ms=duration_ms, timestamp=start_time.isoformat()
        )

        # See DEFERRED.md: DEFERRED-001 — token/cost extraction per provider SDK
