"""Closed contracts for deterministic commercial-priority aggregation.

Commercial relevance is a classification fact, not a severity score. Missing
or invalid values remain unknown and sort after known values; they are never
coerced to ``none``.
"""

from __future__ import annotations

from typing import Any


COMMERCIAL_PRIORITY_SCHEMA_VERSION = 1
PAINPOINT_RELEVANCE_VALUES = frozenset({
    "benchmark_only",
    "creator_only",
    "both",
    "none",
})
PAINPOINT_RELEVANCE_RANK = {
    "benchmark_only": 0,
    "both": 1,
    "none": 1,
    "creator_only": 2,
}
UNKNOWN_RELEVANCE_RANK = 99
COMMERCIAL_PRIORITY_SOURCES = frozenset({"global", "stage"})
COMMERCIAL_PRIORITY_TIERS = frozenset({"P0", "P1", "P2", "P3", "P4", "P5"})


def classify_painpoint_relevance(value: Any) -> dict[str, Any]:
    """Return the only allowed commercial-relevance representation.

    ``missing`` and ``invalid_value`` are deliberately distinct audit reasons,
    while both remain non-participating unknowns in the ordering.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return {
            "status": "unknown",
            "value": None,
            "priority_rank": None,
            "reason_code": "missing",
            "source": "stage_analysis.painpoint_relevance",
        }
    normalized = str(value).strip().lower()
    if normalized not in PAINPOINT_RELEVANCE_VALUES:
        return {
            "status": "unknown",
            "value": None,
            "priority_rank": None,
            "reason_code": "invalid_value",
            "source": "stage_analysis.painpoint_relevance",
        }
    return {
        "status": "known",
        "value": normalized,
        "priority_rank": PAINPOINT_RELEVANCE_RANK[normalized],
        "reason_code": "known",
        "source": "stage_analysis.painpoint_relevance",
    }


def relevance_sort_rank(relevance: Any) -> int:
    """Return a deterministic rank without treating unknown as a fact."""
    if not isinstance(relevance, dict) or relevance.get("status") != "known":
        return UNKNOWN_RELEVANCE_RANK
    rank = relevance.get("priority_rank")
    return rank if isinstance(rank, int) else UNKNOWN_RELEVANCE_RANK


def validate_commercial_priorities(value: Any) -> list[str]:
    """Validate the generated priority list against the closed output contract."""
    if not isinstance(value, list):
        return ["commercial_priorities must be a list"]
    errors: list[str] = []
    ids: set[str] = set()
    for index, item in enumerate(value):
        prefix = f"commercial_priorities[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if item.get("schema_version") != COMMERCIAL_PRIORITY_SCHEMA_VERSION:
            errors.append(f"{prefix}.schema_version is invalid")
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            errors.append(f"{prefix}.id is required")
        elif item_id in ids:
            errors.append(f"{prefix}.id is duplicated: {item_id}")
        ids.add(item_id)
        if item.get("source") not in COMMERCIAL_PRIORITY_SOURCES:
            errors.append(f"{prefix}.source is invalid")
        if item.get("tier") not in COMMERCIAL_PRIORITY_TIERS:
            errors.append(f"{prefix}.tier is invalid")
        for field in ("title", "summary", "reference_id"):
            if not str(item.get(field) or "").strip():
                errors.append(f"{prefix}.{field} is required")
        if not isinstance(item.get("root_cause_ids"), list):
            errors.append(f"{prefix}.root_cause_ids must be a list")
        if item.get("source") == "stage":
            relevance = item.get("commercial_relevance")
            if not isinstance(relevance, dict):
                errors.append(f"{prefix}.commercial_relevance is required for stage priorities")
                continue
            status = relevance.get("status")
            if status not in {"known", "unknown"}:
                errors.append(f"{prefix}.commercial_relevance.status is invalid")
            if status == "known":
                value_name = relevance.get("value")
                if value_name not in PAINPOINT_RELEVANCE_VALUES:
                    errors.append(f"{prefix}.commercial_relevance.value is invalid")
                elif relevance.get("priority_rank") != PAINPOINT_RELEVANCE_RANK[value_name]:
                    errors.append(f"{prefix}.commercial_relevance.priority_rank is inconsistent")
                if relevance.get("reason_code") != "known":
                    errors.append(f"{prefix}.commercial_relevance.reason_code is inconsistent")
            elif relevance.get("value") is not None or relevance.get("priority_rank") is not None:
                errors.append(f"{prefix}.unknown commercial relevance must not contain a value or rank")
            elif relevance.get("reason_code") not in {"missing", "invalid_value"}:
                errors.append(f"{prefix}.unknown commercial relevance has invalid reason_code")
    return list(dict.fromkeys(errors))


__all__ = [
    "COMMERCIAL_PRIORITY_SCHEMA_VERSION",
    "PAINPOINT_RELEVANCE_VALUES",
    "PAINPOINT_RELEVANCE_RANK",
    "classify_painpoint_relevance",
    "relevance_sort_rank",
    "validate_commercial_priorities",
]
