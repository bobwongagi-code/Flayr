"""S4 qualification receives a proof target without turning it into evidence."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.llm.payload import build_stage_evidence_qualification_payload
from flayr_core.llm.stage_fact_artifacts import request_identity


class S4ProofTargetContextTests(unittest.TestCase):
    def setUp(self):
        self.analysis = {
            "videos": {"creator": {"duration_seconds": 20}},
            "product_foundation": {
                "product_profile": {
                    "proof_contract_source": "inferred",
                    "confidence": "low",
                    "short_video_proof_plan": {"s4_anchor_candidate_id": "charging"},
                    "proof_contract": {
                        "valid": True,
                        "anchor_candidate_id": "charging",
                        "consumer_outcome": "手机进入充电状态",
                        "mode": "instant_visual",
                        "observable_dimension": "手机充电状态",
                    },
                },
            },
        }
        self.facts = {"evidence_units": [{"id": "C1", "visual_fact": "充电宝挂在包上"}]}

    def payload(self, analysis, targets):
        return build_stage_evidence_qualification_payload(
            "test-model", "creator", analysis, self.facts, targets,
        )

    def test_s4_target_is_read_only_background(self):
        before = copy.deepcopy((self.analysis, self.facts))
        payload = self.payload(self.analysis, ["S3", "S4"])
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("## S4 商品证明目标（只读背景，不是视频证据）", text)
        self.assertIn('"consumer_outcome": "手机进入充电状态"', text)
        self.assertIn('"proof_contract_source": "inferred"', text)
        self.assertIn('"confidence": "low"', text)
        self.assertIn("不得因目标存在就推断视频已展示", text)
        self.assertEqual((self.analysis, self.facts), before)

    def test_other_stages_keep_exact_request(self):
        no_foundation = copy.deepcopy(self.analysis)
        no_foundation.pop("product_foundation")
        for stage in ("S1", "S2", "S3", "S5", "S6"):
            with self.subTest(stage=stage):
                self.assertEqual(self.payload(self.analysis, [stage]), self.payload(no_foundation, [stage]))

    def test_invalid_or_unselected_target_is_not_injected(self):
        no_foundation = copy.deepcopy(self.analysis)
        no_foundation.pop("product_foundation")
        baseline = self.payload(no_foundation, ["S4"])
        for field, value in (("valid", False), ("anchor_candidate_id", "other")):
            changed = copy.deepcopy(self.analysis)
            changed["product_foundation"]["product_profile"]["proof_contract"][field] = value
            self.assertEqual(self.payload(changed, ["S4"]), baseline)

    def test_new_context_changes_strict_replay_identity(self):
        no_foundation = copy.deepcopy(self.analysis)
        no_foundation.pop("product_foundation")
        def identity(analysis):
            return request_identity(role="creator", phase="D", group=["S4"],
                                    payload=self.payload(analysis, ["S4"]),
                                    model="test-model", api_url="https://example.com/v1")
        self.assertNotEqual(identity(self.analysis), identity(no_foundation))


if __name__ == "__main__":
    unittest.main()
