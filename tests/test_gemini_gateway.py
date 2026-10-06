import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.llm.gemini_gateway import (
    assemble_native_response,
    build_native_request,
    build_observation_schema,
    normalize_native_json,
    parse_native_sse,
    replace_native_video_with_url,
    validate_observation_checklist,
)
from flayr_core.llm.payload import build_video_fact_payload


def test_gateway_http_200_refusal_is_not_an_incomplete_answer():
    refused = {
        "modelVersion": "gemini-3.7-flash",
        "candidates": [{"content": {"parts": [{"text":
            "The prompt could not be submitted. The prompt contains sensitive words."}]}}],
        "usageMetadata": {"promptTokenCount": 15564},
    }
    before = copy.deepcopy(refused)
    with unittest.TestCase().assertRaisesRegex(ValueError, "refused the request"):
        assemble_native_response([refused], expected_model="gemini-3.7-flash")
    assert refused == before
    refused["candidates"][0]["content"]["parts"][0]["text"] = "partial ordinary answer"
    with unittest.TestCase().assertRaisesRegex(ValueError, "did not finish normally"):
        assemble_native_response([refused], expected_model="gemini-3.7-flash")


def test_observation_schema_preserves_sparse_fields_and_source_bound_lists():
    payload = build_video_fact_payload("test", "creator", {}, [])
    text = payload["messages"][1]["content"][0]["text"]
    example = json.JSONDecoder().raw_decode(text.split("## 输出 JSON", 1)[1].lstrip())[0]
    before = copy.deepcopy(example)
    schema = build_observation_schema(example, {})
    unit = schema["properties"]["evidence_units"]["items"]
    assert unit["required"] == ["id", "time_range", "fact_quality"]
    assert schema["properties"]["evidence_checklist"]["maxItems"] == 0
    assert "claim_only" in unit["properties"]["fact_quality"]["properties"]["proof"]["enum"]
    assert example == before
    validate_observation_checklist({"evidence_checklist": []}, {})
    raw = {"evidence_checklist": [{"item": "invented benefit"}]}
    unchanged = copy.deepcopy(raw)
    with unittest.TestCase().assertRaisesRegex(ValueError, "unfrozen"):
        validate_observation_checklist(raw, {})
    assert raw == unchanged
    analysis = {"brand_proposition": {"propositions": ["confirmed source"], "painpoints": []}}
    constrained = build_observation_schema(example, analysis)
    assert constrained["properties"]["evidence_checklist"]["items"]["properties"]["item"]["enum"] == ["confirmed source"]
    validate_observation_checklist({"evidence_checklist": [{"item": "confirmed source"}]}, analysis)
    for invalid in ("not a list", [""], [None]):
        with unittest.TestCase().assertRaises(ValueError):
            build_observation_schema(example, {"brand_proposition": {"propositions": invalid}})


def test_native_json_only_removes_complete_outer_fence():
    raw = '{"facts": [], "text": "keep exactly"}'
    assert normalize_native_json(raw) == raw
    assert normalize_native_json("```json\n" + raw + "\n```") == raw
    assert normalize_native_json("```\n" + raw + "\n```") == raw
    for invalid in ("Explanation\n" + raw, "```json\n" + raw,
                    "```json\n" + raw + "\n```\nextra", raw + raw, "[]", '{"x":}'):
        with unittest.TestCase().assertRaises(ValueError):
            normalize_native_json(invalid)


def test_observation_schema_allows_existing_optional_trust_source_fields():
    payload = build_video_fact_payload("test", "creator", {}, [])
    text = payload["messages"][1]["content"][0]["text"]
    example = json.JSONDecoder().raw_decode(text.split("## 输出 JSON", 1)[1].lstrip())[0]
    before = copy.deepcopy(example)
    schema = build_observation_schema(example, {})
    unit = schema["properties"]["evidence_units"]["items"]
    properties = unit["properties"]
    assert properties["trust_source_signals"] == {
        "type": "ARRAY", "items": {"type": "STRING", "enum": [
            "authority", "independent_user", "process_transparency",
            "social_consensus", "traceable_data",
        ]},
    }
    assert properties["trust_source_reference"] == {"type": "STRING"}
    assert "trust_source_signals" not in unit["required"]
    assert "trust_source_reference" not in unit["required"]
    assert unit["required"] == ["id", "time_range", "fact_quality"]
    assert example == before


def test_native_request_preserves_business_text_and_media():
    payload = {"messages": [
        {"role": "system", "content": "locked rules"},
        {"role": "user", "content": [
            {"type": "text", "text": "locked evidence"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,YQ=="}},
        ]},
    ], "max_tokens": 8192, "temperature": 0}
    original = copy.deepcopy(payload)
    schema = {"type": "OBJECT", "required": ["facts"]}
    body = build_native_request(payload, response_schema=schema)
    assert body["systemInstruction"]["parts"] == [{"text": "locked rules"}]
    assert body["contents"][0]["parts"] == [
        {"text": "locked evidence"},
        {"inlineData": {"mimeType": "video/mp4", "data": "YQ=="}},
    ]
    assert body["generationConfig"]["maxOutputTokens"] == 8192
    assert body["generationConfig"]["responseSchema"] == schema
    assert payload == original


def test_stream_reassembles_content_but_not_thoughts():
    chunks = [
        {"modelVersion": "gemini-3.7-flash", "candidates": [{"content": {"parts": [
            {"text": "private", "thought": True}, {"text": '{"facts":'},
        ]}}]},
        {"modelVersion": "gemini-3.7-flash", "candidates": [{
            "content": {"parts": [{"text": "[]}"}]}, "finishReason": "STOP",
        }], "usageMetadata": {"totalTokenCount": 7}},
    ]
    raw = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    result = assemble_native_response(parse_native_sse(raw), expected_model="gemini-3.7-flash")
    assert result["content"] == '{"facts":[]}'
    assert result["usage_metadata"] == {"totalTokenCount": 7}


def test_truncated_or_blocked_response_cannot_be_success():
    for reason in (None, "MAX_TOKENS", "SAFETY"):
        chunk = {"modelVersion": "gemini-3.7-flash", "candidates": [{
            "content": {"parts": [{"text": '{"facts":[]}'}]}, "finishReason": reason,
        }]}
        with unittest.TestCase().assertRaisesRegex(ValueError, "finish normally"):
            assemble_native_response([chunk], expected_model="gemini-3.7-flash")


def test_wrong_model_cannot_be_success():
    with unittest.TestCase().assertRaisesRegex(ValueError, "identity"):
        assemble_native_response([{"modelVersion": "other"}], expected_model="gemini-3.7-flash")


def test_external_media_is_not_silently_fetched():
    with unittest.TestCase().assertRaisesRegex(ValueError, "local inlined"):
        build_native_request({"messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": [{"type": "video_url", "video_url": {"url": "https://example.com/v.mp4"}}]},
        ]})


def test_ocr_user_only_preserves_plain_text_contract():
    payload = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Read subtitles or return NONE"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YQ=="}},
    ]}], "max_tokens": 1000}
    original = copy.deepcopy(payload)
    body = build_native_request(payload, response_mime_type="text/plain")
    assert "systemInstruction" not in body
    assert body["generationConfig"]["responseMimeType"] == "text/plain"
    assert body["generationConfig"]["maxOutputTokens"] == 1000
    assert body["contents"][0]["parts"][0]["text"] == "Read subtitles or return NONE"
    assert body["contents"][0]["parts"][1]["inlineData"]["mimeType"] == "image/jpeg"
    assert payload == original
    with unittest.TestCase().assertRaisesRegex(ValueError, "requires JSON"):
        build_native_request(payload, response_schema={}, response_mime_type="text/plain")


def test_video_url_substitution_is_hash_bound_and_preserves_other_inputs():
    body = {"contents": [{"parts": [
        {"text": "unchanged facts"},
        {"inlineData": {"mimeType": "video/mp4", "data": "YQ=="}},
        {"inlineData": {"mimeType": "image/jpeg", "data": "Yg=="}},
    ]}], "generationConfig": {"responseSchema": {"type": "OBJECT"}}}
    original = copy.deepcopy(body)
    uri = "https://storage.example/video.mp4?signature=temporary"
    sha = hashlib.sha256(b"a").hexdigest()
    replaced = replace_native_video_with_url(body, video_sha256=sha, video_uri=uri)
    assert body == original
    assert replaced["contents"][0]["parts"][1] == {"fileData": {"mimeType": "video/mp4", "fileUri": uri}}
    replaced["contents"][0]["parts"][1] = original["contents"][0]["parts"][1]
    assert replaced == original
    with unittest.TestCase().assertRaisesRegex(ValueError, "fingerprint"):
        replace_native_video_with_url(body, video_sha256="wrong", video_uri=uri)
    for bad_url in ("http://storage.example/a", "https://user:pass@storage.example/a",
                    "https://storage.example/a#fragment", "https://storage.example:444/a"):
        with unittest.TestCase().assertRaises(ValueError):
            replace_native_video_with_url(body, video_sha256=sha, video_uri=bad_url)
    for videos in ([], [original["contents"][0]["parts"][1]] * 2,
                   [{"fileData": {"mimeType": "video/mp4", "fileUri": uri}}]):
        with unittest.TestCase().assertRaisesRegex(ValueError, "exactly one"):
            replace_native_video_with_url({"contents": [{"parts": videos}]}, video_sha256=sha, video_uri=uri)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
