"""Canonical model-execution configuration for reproducible cohort freezes.

This module describes execution identity only.  It does not configure or call
the model, and it intentionally does not change the analysis pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping


MODEL_EXECUTION_CONFIG_SCHEMA_VERSION = 1


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"model_execution_config.{field} 必须是非空字符串")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"model_execution_config.{field} 不能是空字符串")
    return text


def _number(value: Any, field: str, *, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"model_execution_config.{field} 必须是有限数字")
    result = float(value)
    if result < minimum or (maximum is not None and result > maximum):
        bound = f"[{minimum}, {maximum}]" if maximum is not None else f">={minimum}"
        raise ValueError(f"model_execution_config.{field} 必须在 {bound} 范围内")
    return result


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"model_execution_config.{field} 必须是 >= {minimum} 的整数")
    return int(value)


def _optional_positive_int(value: Any, field: str, *, minimum: int = 1) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field, minimum=minimum)


def _copy_json_object(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"model_execution_config.{field} 必须是 object 或 null")
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"model_execution_config.{field} 必须是 JSON-compatible object") from exc
    return copied


def _stop_sequences(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("model_execution_config.stop 必须是字符串数组或 null")
    result = tuple(str(item) for item in value)
    if any(not item for item in result):
        raise ValueError("model_execution_config.stop 不能包含空字符串")
    return result


@dataclass(frozen=True)
class ModelExecutionTimeout:
    """Transport timeout values used by one frozen execution contract."""

    connect: int
    read: int | None
    low_speed: int
    overall: int

    @classmethod
    def from_mapping(cls, value: Any) -> "ModelExecutionTimeout":
        if not isinstance(value, Mapping):
            raise ValueError("model_execution_config.timeout 必须是 object")
        read = value.get("read")
        return cls(
            connect=_positive_int(value.get("connect"), "timeout.connect"),
            read=_optional_positive_int(read, "timeout.read"),
            low_speed=_positive_int(value.get("low_speed"), "timeout.low_speed"),
            overall=_positive_int(value.get("overall"), "timeout.overall"),
        )

    def as_dict(self) -> dict[str, int | None]:
        return {
            "connect": self.connect,
            "read": self.read,
            "low_speed": self.low_speed,
            "overall": self.overall,
        }


@dataclass(frozen=True)
class ModelExecutionConfig:
    """Complete, JSON-canonical identity of a model execution."""

    provider: str
    api_url: str
    model: str
    fallback_model: str | None
    temperature: float
    max_tokens: int
    top_p: float | None
    seed: int | None
    response_format: dict[str, Any] | None
    stop: tuple[str, ...] | None
    transport_retry: int
    completion_attempts: int
    timeout: ModelExecutionTimeout

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelExecutionConfig":
        if not isinstance(value, Mapping):
            raise ValueError("model_execution_config 必须是 object")
        schema_version = value.get("schema_version", MODEL_EXECUTION_CONFIG_SCHEMA_VERSION)
        if schema_version != MODEL_EXECUTION_CONFIG_SCHEMA_VERSION:
            raise ValueError("model_execution_config.schema_version 不兼容")
        return cls(
            provider=_required_text(value.get("provider"), "provider"),
            api_url=_required_text(value.get("api_url"), "api_url"),
            model=_required_text(value.get("model"), "model"),
            fallback_model=_optional_text(value.get("fallback_model"), "fallback_model"),
            temperature=_number(value.get("temperature"), "temperature", minimum=0.0, maximum=2.0),
            max_tokens=_positive_int(value.get("max_tokens"), "max_tokens"),
            top_p=(
                None
                if value.get("top_p") is None
                else _number(value.get("top_p"), "top_p", minimum=0.0, maximum=1.0)
            ),
            seed=_optional_positive_int(value.get("seed"), "seed", minimum=0),
            response_format=_copy_json_object(value.get("response_format"), "response_format"),
            stop=_stop_sequences(value.get("stop")),
            transport_retry=_positive_int(value.get("transport_retry"), "transport_retry", minimum=0),
            completion_attempts=_positive_int(value.get("completion_attempts"), "completion_attempts"),
            timeout=ModelExecutionTimeout.from_mapping(value.get("timeout")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_EXECUTION_CONFIG_SCHEMA_VERSION,
            "provider": self.provider,
            "api_url": self.api_url,
            "model": self.model,
            "fallback_model": self.fallback_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "seed": self.seed,
            "response_format": self.response_format,
            "stop": list(self.stop) if self.stop is not None else None,
            "transport_retry": self.transport_retry,
            "completion_attempts": self.completion_attempts,
            "timeout": self.timeout.as_dict(),
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.as_dict())
