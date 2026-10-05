"""Business boundaries stay consistent across observation, qualification and judgment."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.llm.payload import (
    build_stage_evidence_qualification_payload,
    build_stage_group_judgment_payload,
    build_video_fact_payload,
)
from flayr_core.postprocess.derive import _s6_cta_exec
from flayr_core.postprocess.proposition import _absolute_status
from flayr_core.stage_contract_registry import (
    STAGE_BOUNDARY_TESTS,
    required_stage_signals_satisfied,
    stage_evidence_contract,
    stage_evidence_contract_prompt,
)
from flayr_core.stage_ownership import contains_certification


class StageBusinessBoundaryTests(unittest.TestCase):
    def test_s2_accepts_product_hook_without_separate_pain_scene(self):
        text = stage_evidence_contract_prompt(["S2"])
        self.assertIn("不要求先有独立痛点", text)
        self.assertIn("同一证据单元", text)

    def test_s5_observation_does_not_exclude_identifiable_printed_certification(self):
        payload = build_video_fact_payload("test-model", "creator", {"videos": {"creator": {}}}, [], api_url="")
        text = payload["messages"][0]["content"]
        self.assertNotIn("产品瓶身上的印刷小标不算", text)
        self.assertIn("不得仅因印在产品或包装上排除", text)
        self.assertIn("无法辨认", text)

    def test_ccc_detection_is_not_qualification_and_keeps_negative_cases(self):
        self.assertTrue(contains_certification("清晰展示 CCC 标识"))
        self.assertFalse(contains_certification("未见 CCC 认证"))
        self.assertFalse(contains_certification("疑似 CCC，无法辨识"))

    def test_s6_review_distinguishes_price_exploration_from_purchase(self):
        payload = build_stage_evidence_qualification_payload("test-model", "creator", {}, {"evidence_units": []}, ["S6"])
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("查看链接了解价格", text)
        self.assertIn("不限出现位置", text)
        self.assertIn("不能仅因有链接", text)

    def test_all_stages_share_business_boundaries_with_judgment(self):
        for code in STAGE_BOUNDARY_TESTS:
            with self.subTest(stage=code):
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, [code])
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn("## 目标阶段业务边界（合同单一来源）", text)
                for value in STAGE_BOUNDARY_TESTS[code].values():
                    self.assertIn(value, text)

    def test_s6_execution_and_status_do_not_depend_on_ending_position(self):
        late = {"exists": True, "ending_position_met": True, "direct_order_met": True,
                "action_path_clear": True, "module_fit_met": True, "compliance_risk": False,
                "product_value_recalled": True, "module_type": "A"}
        early = {**late, "ending_position_met": False}
        stage = {"creator_s6": early, "benchmark_s6": late}
        before = copy.deepcopy(stage)
        self.assertEqual(_s6_cta_exec(stage), {"creator_exec": 2.0, "bench_exec": 2.0})
        self.assertEqual(_absolute_status("S6", early), _absolute_status("S6", late))
        self.assertEqual(stage, before)

    def test_context_updates_do_not_remove_evidence_requirements(self):
        self.assertFalse(required_stage_signals_satisfied(stage_evidence_contract("S2"), {"product_identity"}))
        self.assertTrue(required_stage_signals_satisfied(
            stage_evidence_contract("S2"), {"product_identity", "problem_to_product_bridge"},
        ))
        self.assertFalse(required_stage_signals_satisfied(stage_evidence_contract("S5"), {"source_basis"}))
        self.assertFalse(required_stage_signals_satisfied(stage_evidence_contract("S6"), {"ending_position", "offer_or_value"}))

    def test_early_position_does_not_create_cta_when_no_action_exists(self):
        flag = {"exists": False, "ending_position_met": False}
        self.assertEqual(_s6_cta_exec({"creator_s6": flag, "benchmark_s6": flag}),
                         {"creator_exec": 0.0, "bench_exec": 0.0})
        self.assertEqual(_absolute_status("S6", flag)[0], "missing")


if __name__ == "__main__":
    unittest.main()
