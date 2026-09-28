"""Human attestations that a specific source contains no product speech."""

from __future__ import annotations

import re
from typing import Any


VERIFIED_NO_PRODUCT_SPEECH = "verified_no_product_speech"


def valid_no_product_speech_attestation(
    value: Any,
    *,
    source_sha256: str | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    digest = str(value.get("source_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False
    if source_sha256 is not None and digest != str(source_sha256).strip().lower():
        return False
    return (
        value.get("status") == VERIFIED_NO_PRODUCT_SPEECH
        and value.get("scope") == "product_speech"
        and bool(str(value.get("attestation_source") or "").strip())
        and bool(str(value.get("attestation_statement") or "").strip())
        and isinstance(value.get("attestation_file"), dict)
        and bool(str(value["attestation_file"].get("path") or "").strip())
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(value["attestation_file"].get("sha256") or "").lower()))
    )


def has_verified_no_product_speech(info: Any) -> bool:
    if not isinstance(info, dict) or info.get("transcription_status") != VERIFIED_NO_PRODUCT_SPEECH:
        return False
    fingerprint = info.get("preprocess_fingerprint")
    source = fingerprint.get("source_video") if isinstance(fingerprint, dict) else None
    source_sha256 = source.get("sha256") if isinstance(source, dict) else None
    return valid_no_product_speech_attestation(
        info.get("no_product_speech_attestation"),
        source_sha256=str(source_sha256 or ""),
    )
