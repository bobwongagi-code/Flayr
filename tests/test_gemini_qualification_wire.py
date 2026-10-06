import copy
import unittest

from scripts.flayr_core.llm.gemini_gateway import (
    build_qualification_wire,
    canonical_qualification_wire,
    prepare_native_qualification_payload,
)
from scripts.flayr_core.llm.payload import build_stage_evidence_qualification_payload


class GeminiQualificationWireTests(unittest.TestCase):
    def valid_wire(self):
        wire, _ = build_qualification_wire(["S2", "S3"])
        for item in wire["stage_evidence_checks"].values():
            item.update(status="unknown", coverage="unknown", evidence_strength="inferred")
            for binding in item["signal_bindings"].values():
                binding["status"] = "unknown"
        return wire

    def test_round_trip_preserves_every_observation_without_repair(self):
        wire = self.valid_wire()
        before = copy.deepcopy(wire)
        result = canonical_qualification_wire(wire, ["S3", "S2"])
        self.assertEqual(result["stage_evidence_checks"], [before["stage_evidence_checks"][code] for code in ["S3", "S2"]])
        self.assertEqual(wire, before)

    def test_invalid_targets_are_not_silently_normalized(self):
        for stages in ([], ["S2", "S2"], ["S7"], ["s2"]):
            with self.subTest(stages=stages), self.assertRaises(ValueError):
                build_qualification_wire(stages)

    def test_production_prompt_keeps_all_rules_and_source_facts(self):
        payload = build_stage_evidence_qualification_payload(
            "gemini-3.7-flash", "creator", {}, {"evidence_units": []}, ["S2", "S3"]
        )
        before = copy.deepcopy(payload)
        updated, schema = prepare_native_qualification_payload(payload, ["S2", "S3"])
        marker = "## 严格 JSON 输出"
        self.assertEqual(payload, before)
        self.assertEqual(updated["messages"][-1]["content"][0]["text"].split(marker)[0],
                         before["messages"][-1]["content"][0]["text"].split(marker)[0])
        self.assertTrue(updated["messages"][-1]["content"][0]["text"].endswith(
            "必须恰好输出目标阶段 S2, S3 的 stage_evidence_checks，不得输出其他阶段。"))
        self.assertEqual(set(schema["properties"]["stage_evidence_checks"]["properties"]), {"S2", "S3"})
        with self.assertRaises(ValueError):
            prepare_native_qualification_payload(payload, ["S6"])

    def test_missing_extra_and_invalid_records_are_rejected(self):
        for mutate in (
            lambda wire: wire.update(stage_evidence_contract_version=8),
            lambda wire: wire.update(stage_evidence_contract_version=True),
            lambda wire: wire["stage_evidence_checks"].pop("S2"),
            lambda wire: wire["stage_evidence_checks"].update(S6={}),
            lambda wire: wire["stage_evidence_checks"]["S2"].update(stage="S3"),
            lambda wire: wire["stage_evidence_checks"]["S2"].update(status="guessed"),
            lambda wire: wire["stage_evidence_checks"]["S2"].update(evidence_ids=[123]),
            lambda wire: wire["stage_evidence_checks"]["S2"].pop("reason"),
        ):
            wire = self.valid_wire()
            mutate(wire)
            before = copy.deepcopy(wire)
            with self.assertRaises(ValueError):
                canonical_qualification_wire(wire, ["S2", "S3"])
            self.assertEqual(wire, before)


if __name__ == "__main__":
    unittest.main()
