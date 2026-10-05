import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.llm.comparison_contract import (
    normalize_comparison_contract,
    normalize_video_product_identity,
)
from flayr_core.llm.gemini_gateway import build_observation_schema
from flayr_core.llm.payload import build_video_fact_payload, build_video_identity_payload


def example(builder):
    payload = builder("test", "creator", {"videos": {"creator": {}}}, [])
    text = payload["messages"][-1]["content"][0]["text"]
    return json.JSONDecoder().raw_decode(text.split("## 输出 JSON", 1)[1].lstrip())[0]


def test_observation_and_preflight_request_the_same_identity_fields():
    full = example(build_video_fact_payload)["product_identity"]
    preflight = example(build_video_identity_payload)["product_identity"]
    assert full == preflight
    assert set(full) == {
        "brand_or_product_name", "brand", "product_line", "product_category",
        "functional_form", "variant_attributes", "core_job", "target_object",
        "use_mechanism", "desired_outcome", "identity_basis", "confidence",
    }
    assert "form_factor" not in full


def test_native_schema_keeps_shared_job_fields_and_unknown_strings():
    full = example(build_video_fact_payload)
    before = copy.deepcopy(full)
    identity = build_observation_schema(full, {})["properties"]["product_identity"]
    assert set(identity["properties"]) == set(full["product_identity"])
    for name in ("core_job", "target_object", "use_mechanism", "desired_outcome", "product_line"):
        assert name in identity["required"]
        assert identity["properties"][name] == {"type": "STRING"}
        assert "无法确认留空" in full["product_identity"][name]
    assert identity["properties"]["variant_attributes"]["type"] == "ARRAY"
    assert full == before


def test_legacy_five_field_identity_remains_readable_without_invented_jobs():
    raw = {
        "brand_or_product_name": "Observed brand", "product_category": "Observed category",
        "form_factor": "Observed mesh container", "identity_basis": "mixed", "confidence": "high",
    }
    before = copy.deepcopy(raw)
    normalized = normalize_video_product_identity(raw)
    assert normalized["brand_or_product_name"] == raw["brand_or_product_name"]
    assert normalized["form_factor"] == raw["form_factor"]
    assert normalized["functional_form"] == raw["form_factor"]
    for name in ("brand", "product_line", "core_job", "target_object", "use_mechanism", "desired_outcome"):
        assert normalized[name] == ""
    assert normalized["variant_attributes"] == []
    assert raw == before


def test_explicit_functional_form_takes_precedence_over_legacy_container():
    raw = {
        "functional_form": "Loose powder", "form_factor": "Blue round container",
        "variant_attributes": ["Blue packaging"], "core_job": "Set makeup",
    }
    normalized = normalize_video_product_identity(raw)
    assert normalized["functional_form"] == "Loose powder"
    assert normalized["form_factor"] == "Loose powder"
    assert normalized["variant_attributes"] == ["Blue packaging"]
    assert normalized["core_job"] == "Set makeup"


def test_unknown_identity_does_not_become_comparable():
    normalized = normalize_video_product_identity({})
    assert normalized["core_job"] == ""
    assert normalized["target_object"] == ""
    assert normalized["desired_outcome"] == ""
    contract = normalize_comparison_contract({
        "identity_relation": "uncertain", "substitution_relation": "strong_substitute",
        "shared_job": {
            "same_consumer_job": True, "same_target_object": True,
            "same_desired_outcome": True, "same_purchase_decision": True,
            "complement_or_dependency": False,
        },
        "stage_eligibility": {"S3": {"status": "direct"}},
    })
    assert contract["overall_status"] == "uncertain"
    assert contract["comparable_stages"] == []


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
