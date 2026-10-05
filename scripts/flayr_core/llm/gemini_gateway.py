"""Unactivated native Gemini gateway adapter; preserves Flayr business payloads."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .parse import _FACT_QUALITY_VALUES


def build_observation_schema(example: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Project the existing Stage1-A example without requiring sparse unit fields."""
    def shape(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {"type": "OBJECT", "required": list(value),
                    "properties": {key: shape(item) for key, item in value.items()}}
        if isinstance(value, list):
            return {"type": "ARRAY", "items": shape(value[0]) if value else {"type": "STRING"}}
        if isinstance(value, bool):
            return {"type": "BOOLEAN"}
        if isinstance(value, int):
            return {"type": "INTEGER"}
        if isinstance(value, float):
            return {"type": "NUMBER"}
        return {"type": "STRING"}

    schema = shape(example)
    unit = schema["properties"]["evidence_units"]["items"]
    unit["required"] = ["id", "time_range", "fact_quality"]
    quality = unit["properties"]["fact_quality"]
    quality["required"] = list(_FACT_QUALITY_VALUES)
    for field, values in _FACT_QUALITY_VALUES.items():
        quality["properties"][field]["enum"] = sorted(values)
    labels = _frozen_checklist_labels(analysis)
    checklist = schema["properties"]["evidence_checklist"]
    if labels:
        checklist["items"]["properties"]["item"]["enum"] = labels
    else:
        checklist["maxItems"] = 0
    checklist["items"]["properties"]["channels"]["items"]["enum"] = ["visual", "voiceover", "subtitle"]
    schema["properties"]["stage_evidence_contract_version"]["enum"] = [example["stage_evidence_contract_version"]]
    return schema


def _frozen_checklist_labels(analysis: dict[str, Any]) -> list[str]:
    brand = analysis.get("brand_proposition") or {}
    collections = [brand.get(name, []) for name in ("propositions", "painpoints")]
    if any(not isinstance(values, list) for values in collections):
        raise ValueError("frozen checklist collections must be lists")
    labels = [value for values in collections for value in values]
    if any(not isinstance(value, str) or not value.strip() for value in labels):
        raise ValueError("frozen checklist labels must be nonempty text")
    return list(dict.fromkeys(value.strip() for value in labels))


def validate_observation_checklist(value: dict[str, Any], analysis: dict[str, Any]) -> None:
    """Check source membership before normalization; never repair provider facts."""
    labels = set(_frozen_checklist_labels(analysis))
    items = value.get("evidence_checklist")
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("item"), str)
        or item["item"] not in labels for item in items
    ):
        raise ValueError("evidence_checklist contains an unfrozen source")


def normalize_native_json(content: str) -> str:
    """Accept one complete JSON object, optionally enclosed in one Markdown fence."""
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("native JSON answer must be an object")  # noqa: TRY004 - invalid provider value
    return text


def build_native_request(
    payload: dict[str, Any], *, response_schema: dict[str, Any] | None = None,
    response_mime_type: str = "application/json",
) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) not in (1, 2):
        raise ValueError("native adapter requires one user and an optional system message")
    system = messages[0] if len(messages) == 2 else None
    user = messages[-1]
    if (system is not None and system.get("role") != "system") or user.get("role") != "user":
        raise ValueError("unexpected message roles")
    if system is not None and not isinstance(system.get("content"), str):
        raise TypeError("system content must be text")
    if response_mime_type not in {"application/json", "text/plain"}:
        raise ValueError("unsupported response MIME type")
    if response_schema is not None and response_mime_type != "application/json":
        raise ValueError("response Schema requires JSON output")
    content = user.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise TypeError("user content must be text or media blocks")
    parts = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str):
                raise ValueError("invalid text block")
            parts.append({"text": block["text"]})
        elif kind in {"image_url", "video_url"}:
            url = block.get(kind, {}).get("url", "")
            if not isinstance(url, str) or not url.startswith("data:"):
                raise ValueError("native adapter requires local inlined media")
            try:
                header, data = url[5:].split(";base64,", 1)
            except ValueError as exc:
                raise ValueError("invalid media data URL") from exc
            expected = "image/" if kind == "image_url" else "video/"
            if not header.startswith(expected) or not data:
                raise ValueError("media MIME type does not match block")
            parts.append({"inlineData": {"mimeType": header, "data": data}})
        else:
            raise ValueError(f"unsupported native media block: {kind}")
    config: dict[str, Any] = {
        "temperature": payload.get("temperature", 0),
        "responseMimeType": response_mime_type,
    }
    limit = payload.get("max_tokens", payload.get("max_completion_tokens"))
    if limit is not None:
        config["maxOutputTokens"] = int(limit)
    if response_schema is not None:
        config["responseSchema"] = copy.deepcopy(response_schema)
    result = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": config,
    }
    if system is not None:
        result["systemInstruction"] = {"parts": [{"text": system["content"]}]}
    return result


def replace_native_video_with_url(
    request: dict[str, Any], *, video_sha256: str, video_uri: str,
) -> dict[str, Any]:
    """Replace one hash-bound MP4; caller owns upload, URL access and expiry checks."""
    uri = urlsplit(video_uri)
    if (uri.scheme != "https" or not uri.hostname or uri.username or uri.password
            or uri.fragment or uri.port not in (None, 443)):
        raise ValueError("video URL must be HTTPS without credentials or fragment")
    result = copy.deepcopy(request)
    videos = [part for content in result.get("contents", [])
              for part in content.get("parts", [])
              if part.get("inlineData", {}).get("mimeType", "").startswith("video/")
              or part.get("fileData", {}).get("mimeType", "").startswith("video/")]
    if len(videos) != 1 or videos[0].get("inlineData", {}).get("mimeType") != "video/mp4":
        raise ValueError("expected exactly one inline MP4")
    part = videos[0]
    media = base64.b64decode(part["inlineData"]["data"], validate=True)
    if not media or hashlib.sha256(media).hexdigest() != video_sha256:
        raise ValueError("video fingerprint mismatch")
    del part["inlineData"]
    part["fileData"] = {"mimeType": "video/mp4", "fileUri": video_uri}
    return result


def assemble_native_response(
    chunks: list[dict[str, Any]], *, expected_model: str
) -> dict[str, Any]:
    """Assemble native deltas without inventing fields or accepting truncation."""
    text, versions, reasons = [], set(), []
    usage = None
    for chunk in chunks:
        if chunk.get("error"):
            raise ValueError("native provider returned an error")
        feedback = chunk.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ValueError("native provider blocked the request")
        version = chunk.get("modelVersion")
        if version:
            versions.add(version)
        if isinstance(chunk.get("usageMetadata"), dict):
            usage = chunk["usageMetadata"]
        candidates = chunk.get("candidates") or []
        if len(candidates) > 1:
            raise ValueError("native adapter requires a single candidate")
        for candidate in candidates:
            if candidate.get("index", 0) != 0:
                raise ValueError("unexpected candidate index")
            reason = candidate.get("finishReason")
            if reason:
                reasons.append(reason)
            for part in (candidate.get("content") or {}).get("parts") or []:
                if part.get("thought"):
                    continue
                if isinstance(part.get("text"), str):
                    text.append(part["text"])
    if versions != {expected_model}:
        raise ValueError("native response model identity missing or mismatched")
    if reasons != ["STOP"]:
        raise ValueError("native response did not finish normally")
    content = "".join(text)
    if not content.strip():
        raise ValueError("native response contains no answer")
    return {"content": content, "model": expected_model, "usage_metadata": usage}


def parse_native_sse(raw: str) -> list[dict[str, Any]]:
    chunks = []
    data_lines = []
    for line in [*raw.splitlines(), ""]:
        if not line:
            if data_lines:
                value = json.loads("\n".join(data_lines))
                if not isinstance(value, dict):
                    raise ValueError("native SSE event must be an object")
                chunks.append(value)
                data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return chunks
