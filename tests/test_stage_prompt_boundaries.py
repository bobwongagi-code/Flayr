from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import (  # noqa: E402
    build_stage_evidence_qualification_payload,
    build_stage_group_judgment_payload,
)
from flayr_core.llm.phase_c_payload import build_stage_review_payload  # noqa: E402
from flayr_core.llm.compact_eval import (  # noqa: E402
    build_s4_fact_state_payload,
    build_s4_single_pass_payload,
)
from flayr_core.llm.frozen_inputs import FrozenCompactBundle  # noqa: E402
from flayr_core.stage_contract_registry import S3_EXPRESSION_PROMPT  # noqa: E402
from flayr_core.stage_contract_registry import S4_EFFECT_STATE_PROMPT  # noqa: E402
from flayr_core.stage_contract_registry import stage_evidence_contract  # noqa: E402
from flayr_core.stage_evidence_contracts import (  # noqa: E402
    normalize_stage_evidence_checks,
    stage_evidence_contract_issues,
)
from flayr_core.translation import build_translation_payload  # noqa: E402


class StagePromptBoundaryTests(unittest.TestCase):
    def test_all_six_contracts_reach_qualification_and_stage2(self) -> None:
        for code in ("S1", "S2", "S3", "S4", "S5", "S6"):
            with self.subTest(stage=code):
                contract = stage_evidence_contract(code)
                qualification = build_stage_evidence_qualification_payload(
                    "test-model", "creator", {}, {}, [code]
                )
                judgment = build_stage_group_judgment_payload("test-model", "", {}, {}, [code])
                for payload in (qualification, judgment):
                    text = str(payload["messages"])
                    for clause in contract.as_prompt_dict()["boundary_tests"].values():
                        self.assertIn(clause, text)

    def test_s4_visual_definition_reaches_all_four_state_instruction_sites(self) -> None:
        bundle = FrozenCompactBundle(ROOT, {}, {}, (), "test-digest")
        payloads = (
            build_stage_group_judgment_payload("test-model", "", {}, {}, ["S4"]),
            build_stage_review_payload("test-model", {}, {}, {}, ["S4"]),
            build_s4_fact_state_payload("test-model", bundle),
            build_s4_single_pass_payload("test-model", bundle),
        )
        forbidden = (
            "只有结果图或结果叙述", "只有结果画面或结果口播",
            "使用后结果口播最多可支持result_only",
        )
        for payload in payloads:
            with self.subTest(system=payload["messages"][0]["content"][:40]):
                text = str(payload["messages"])
                self.assertIn(S4_EFFECT_STATE_PROMPT, text)
                for clause in forbidden:
                    self.assertNotIn(clause, text)

    def test_s4_invented_or_other_role_reference_cannot_qualify(self) -> None:
        for evidence_id in ("C999", "B1"):
            with self.subTest(evidence_id=evidence_id):
                raw = {
                    "stage": "S4", "status": "partial", "coverage": "complete",
                    "evidence_ids": ["C1", evidence_id],
                    "observed_signals": ["result_presentation"],
                    "signal_bindings": {"result_presentation": {
                        "status": "supported", "evidence_ids": [evidence_id],
                        "reason": "声称有结果，但引用不属于当前视频事实包",
                    }},
                }
                side = {
                    "stage_evidence_checks": normalize_stage_evidence_checks([raw], {"C1"}),
                    "evidence_units": [{"id": "C1", "visual_fact": "只有操作", "evidence_strength": "direct"}],
                }
                issues = stage_evidence_contract_issues(side, require_version=False)
                self.assertIn("S4:signal_binding_invalid_evidence:result_presentation", issues)
                self.assertIn("S4:supported_signal_without_evidence:result_presentation", issues)

    def test_approved_delivery_boundaries_reach_both_contract_and_judgment(self) -> None:
        clauses = {
            "S1": "不能仅凭钩子形式不同就判有差距",
            "S2": "后续包装、规格、功能讲解或使用展示不能倒推为引出更好",
            "S4": "真实但有限的目标结果仍可参与",
        }
        for stage, clause in clauses.items():
            with self.subTest(stage=stage):
                self.assertIn(clause, str(stage_evidence_contract(stage).as_prompt_dict()))
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, [stage])
                self.assertIn(clause, str(payload["messages"]))
        s4 = stage_evidence_contract("S4")
        self.assertIn("result_presentation", s4.participation_signals)
        self.assertIn("result_only_without_process", s4.partial_compatible_disqualifiers)
        self.assertIn("不要求所有品类都采用前后对照", str(s4.as_prompt_dict()))

    def test_translation_does_not_reconstruct_unclear_commerce_speech(self) -> None:
        payload = build_translation_payload("test-model", "unclear words", {})
        system = payload["messages"][0]["content"]
        self.assertIn("唯一内容来源", system)
        self.assertIn("[原文不清：原词]", system)
        self.assertIn("不得补出购买行动", system)

    def test_s3_voiceover_condition_is_preserved_in_reachable_group_prompts(self) -> None:
        structure = (ROOT / "structure_library_full.md").read_text(encoding="utf-8")
        s3_section = structure.split("## 六、S3 功能与使用表达", 1)[1].split("## 七、S4", 1)[0]
        self.assertIn("双方都是口播时", s3_section)
        self.assertIn("双方都是口播时", S3_EXPRESSION_PROMPT)

        for stages in (["S3"], ["S3", "S4"]):
            with self.subTest(stages=stages):
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, list(stages))
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn(S3_EXPRESSION_PROMPT, text)

        s2_payload = build_stage_group_judgment_payload("test-model", "", {}, {}, ["S2"])
        s2_text = s2_payload["messages"][1]["content"][0]["text"]
        self.assertNotIn(S3_EXPRESSION_PROMPT, s2_text)

    def test_s2_baseline_schema_is_reachable_without_candidate_wording(self) -> None:
        candidate_only = (
            "S2 只判断产品身份、它回应前面问题的承接",
            "不得因后续 S3 使用演示",
            "只有内容在 S2 窗口内、通过 S2 qualified_evidence 资格",
            "每阶段只能使用自己的 qualified_evidence",
            "认证不一律排除",
        )

        for stages in (["S1", "S2"], ["S2"]):
            with self.subTest(stages=stages):
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, list(stages))
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn('"creator_s2"', text)
                self.assertIn('"benchmark_s2"', text)
                for clause in candidate_only:
                    self.assertNotIn(clause, text)

        non_s2 = build_stage_group_judgment_payload("test-model", "", {}, {}, ["S4"])
        non_s2_text = non_s2["messages"][1]["content"][0]["text"]
        self.assertNotIn('"creator_s2"', non_s2_text)


if __name__ == "__main__":
    unittest.main()
