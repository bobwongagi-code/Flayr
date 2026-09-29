from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import build_stage_group_judgment_payload  # noqa: E402


class StagePromptBoundaryTests(unittest.TestCase):
    def test_s2_boundary_is_rendered_only_for_groups_containing_s2(self) -> None:
        required = (
            "S2 只判断产品身份、它回应前面问题的承接，以及产品角色是否清晰",
            "不得因后续 S3 使用演示、S4 效果或参数/卖点信息丰富而自动给 S2 加分",
            "通过 S2 qualified_evidence 资格且实际承担 S2 承接、身份或角色功能时，才可作为 S2 判断依据",
            "同一已资格化事实可按不同阶段各自实际承担的功能分别支持判断",
            "认证不一律排除",
        )

        for stages in (["S1", "S2"], ["S2"]):
            with self.subTest(stages=stages):
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, list(stages))
                text = payload["messages"][1]["content"][0]["text"]
                for clause in required:
                    self.assertIn(clause, text)

        non_s2 = build_stage_group_judgment_payload("test-model", "", {}, {}, ["S4"])
        non_s2_text = non_s2["messages"][1]["content"][0]["text"]
        for clause in required:
            self.assertNotIn(clause, non_s2_text)


if __name__ == "__main__":
    unittest.main()
