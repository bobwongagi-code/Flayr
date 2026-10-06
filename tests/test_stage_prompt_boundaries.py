from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import build_stage_group_judgment_payload  # noqa: E402
from flayr_core.stage_contract_registry import S3_EXPRESSION_PROMPT  # noqa: E402
from flayr_core.stage_contract_registry import stage_evidence_contract  # noqa: E402
from flayr_core.translation import build_translation_payload  # noqa: E402


class StagePromptBoundaryTests(unittest.TestCase):
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
