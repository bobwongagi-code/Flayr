"""Audit deterministic changes made after an LLM result is parsed."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable


MAX_CHANGE_ENTRIES = 2_000
MAX_SERIALIZED_VALUE_BYTES = 8_192
MAX_FIELD_SOURCE_ENTRIES = 10_000
_MISSING = object()


def _pointer_token(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _json_safe(value: Any) -> Any:
    """Keep audit output JSON-compatible without serializing arbitrary objects."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"value_type": type(value).__name__, "repr": repr(value)[:512]}
    if len(encoded.encode("utf-8")) <= MAX_SERIALIZED_VALUE_BYTES:
        return value
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {
        "truncated": True,
        "sha256": digest,
        "bytes": len(encoded.encode("utf-8")),
        "preview": encoded[:512],
    }


def _flatten_evidence(value: Any, output: list[str]) -> None:
    if len(output) >= 10:
        return
    if isinstance(value, str):
        if value.strip():
            output.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _flatten_evidence(item, output)
            if len(output) >= 10:
                return
        return
    if isinstance(value, dict):
        for key in ("id", "evidence_id", "evidence_ids", "ref", "refs", "reference"):
            if key in value:
                _flatten_evidence(value[key], output)
                if len(output) >= 10:
                    return


def _container_at(value: Any, tokens: list[str]) -> Any:
    current = value
    for token in tokens:
        if isinstance(current, dict):
            current = current.get(token, _MISSING)
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            current = current[index] if index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _value_at_pointer(value: Any, path: str) -> tuple[Any, bool]:
    if path in {"", "/"}:
        return value, True
    current = _container_at(value, [item.replace("~1", "/").replace("~0", "~") for item in path.split("/")[1:]])
    return (current, current is not _MISSING)


def _iter_leaf_paths(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict) and value:
        paths: list[str] = []
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            paths.extend(_iter_leaf_paths(child, f"{path}/{_pointer_token(key)}"))
        return paths
    if isinstance(value, list) and value:
        paths = []
        for index, child in enumerate(value):
            paths.extend(_iter_leaf_paths(child, f"{path}/{index}"))
        return paths
    return [path or "/"]


def _path_covers(change_path: str, leaf_path: str) -> bool:
    if change_path in {"", "/"}:
        return True
    return leaf_path == change_path or leaf_path.startswith(f"{change_path}/")


def build_field_sources(
    raw_result: Any,
    normalized_result: Any,
    final_result: Any,
    changes: list[dict[str, Any]],
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    """Map every final leaf to its model, normalization, or rule-based source.

    The map deliberately stores pointers and rule metadata rather than values.
    This keeps provenance useful without duplicating sensitive result content.
    """
    raw = raw_result if isinstance(raw_result, dict) else {}
    normalized = normalized_result if isinstance(normalized_result, dict) else {}
    final = final_result if isinstance(final_result, dict) else {}
    leaves = _iter_leaf_paths(final)
    field_map: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []

    for leaf_path in leaves[:MAX_FIELD_SOURCE_ENTRIES]:
        candidates = [
            (index, change)
            for index, change in enumerate(changes)
            if isinstance(change, dict) and _path_covers(str(change.get("path") or "/"), leaf_path)
        ]
        if candidates:
            _, change = max(
                candidates,
                key=lambda item: (
                    len([token for token in str(item[1].get("path") or "/").split("/") if token]),
                    item[0],
                ),
            )
            field_map[leaf_path] = {
                "source_artifact": "postprocess_change_log.json",
                "source_path": str(change.get("path") or "/"),
                "rule": str(change.get("rule") or "unknown"),
                "kind": str(change.get("kind") or "deterministic_derivation"),
                "evidence": list(change.get("evidence") or [])[:10],
            }
            continue

        final_value, final_exists = _value_at_pointer(final, leaf_path)
        raw_value, raw_exists = _value_at_pointer(raw, leaf_path)
        normalized_value, normalized_exists = _value_at_pointer(normalized, leaf_path)
        if raw_exists and final_exists and raw_value == final_value:
            field_map[leaf_path] = {
                "source_artifact": "raw_model_response.json",
                "source_path": leaf_path,
                "rule": "model_response",
                "kind": "model_output",
                "evidence": [],
            }
        elif normalized_exists and final_exists and normalized_value == final_value:
            field_map[leaf_path] = {
                "source_artifact": "validated_normalized_result.json",
                "source_path": leaf_path,
                "rule": "pipeline.normalize_analysis_result",
                "kind": "deterministic_normalization",
                "evidence": [],
            }
        else:
            unresolved.append(leaf_path)
            field_map[leaf_path] = {
                "source_artifact": None,
                "source_path": leaf_path,
                "rule": None,
                "kind": "untracked_change",
                "evidence": [],
            }

    omitted = max(0, len(leaves) - len(field_map))
    if omitted:
        unresolved.extend(leaves[len(field_map):])
    return {
        "schema_version": 1,
        "coverage": "complete" if not truncated and not unresolved and not omitted else "partial",
        "truncated": bool(truncated or omitted),
        "field_count": len(field_map),
        "unresolved_paths": list(dict.fromkeys(unresolved)),
        "fields": field_map,
    }


def _evidence_refs_after_change(after: Any, path: str) -> list[str]:
    tokens = [item for item in path.split("/")[1:] if item]
    refs: list[str] = []
    # Check the changed field's container and progressively wider parents. This
    # keeps the record useful without copying the entire result into every entry.
    for end in range(len(tokens), -1, -1):
        container = _container_at(after, tokens[:end])
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            if "evidence" in str(key).lower() or str(key).lower() in {"supporting_refs", "source_refs"}:
                _flatten_evidence(value, refs)
        if refs:
            break
    return list(dict.fromkeys(refs))


@dataclass
class PostprocessAudit:
    """Collect field-level changes while keeping the business result unchanged."""

    changes: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    def record(self, before: Any, after: Any, rule: str, *, kind: str = "deterministic_derivation") -> None:
        _diff_values(before, after, "", rule, kind, self)

    def run(self, result: dict[str, Any], rule: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        before = copy.deepcopy(result)
        output = function(*args, **kwargs)
        self.record(before, result, rule)
        return output

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "change_count": len(self.changes),
            "truncated": self.truncated,
            "changes": self.changes,
        }


def _append_change(
    audit: PostprocessAudit,
    path: str,
    before: Any,
    after: Any,
    rule: str,
    kind: str,
    document_after: Any,
) -> None:
    if len(audit.changes) >= MAX_CHANGE_ENTRIES:
        audit.truncated = True
        return
    audit.changes.append(
        {
            "path": path or "/",
            "old": None if before is _MISSING else _json_safe(before),
            "new": None if after is _MISSING else _json_safe(after),
            "rule": rule,
            "kind": kind,
            "evidence": _evidence_refs_after_change(document_after, path) if after is not _MISSING else [],
        }
    )


def _diff_values(
    before: Any,
    after: Any,
    path: str,
    rule: str,
    kind: str,
    audit: PostprocessAudit,
    document_after: Any = _MISSING,
) -> None:
    if document_after is _MISSING:
        document_after = after
    if len(audit.changes) >= MAX_CHANGE_ENTRIES:
        audit.truncated = True
        return
    if isinstance(before, dict) and isinstance(after, dict):
        keys = sorted(set(before) | set(after), key=str)
        for key in keys:
            child_path = f"{path}/{_pointer_token(key)}"
            _diff_values(
                before.get(key, _MISSING),
                after.get(key, _MISSING),
                child_path,
                rule,
                kind,
                audit,
                document_after,
            )
        return
    if isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child_path = f"{path}/{index}"
            left = before[index] if index < len(before) else _MISSING
            right = after[index] if index < len(after) else _MISSING
            _diff_values(left, right, child_path, rule, kind, audit, document_after)
        return
    if before is _MISSING or after is _MISSING or before != after:
        _append_change(audit, path, before, after, rule, kind, document_after)
