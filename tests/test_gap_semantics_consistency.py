from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import build_stage_group_judgment_payload  # noqa: E402


SMALL_DEFINITION = "本阶段存在有证据支持、可辨认但较小的差距；不以双方都执行为前提"
ONE_SIDED_SMALL_BOUNDARY = (
    "单侧有限执行与另一侧未执行可以形成 small，但不能仅按有无动作或执行侧数量自动定档；"
    "仍以既有资格事实及实际差距判断。"
)


class GapSemanticsConsistencyTests(unittest.TestCase):
    def test_shared_small_definition_matches_active_documents_and_payloads(self) -> None:
        structure = (ROOT / "structure_library_full.md").read_text(encoding="utf-8")
        expert = json.loads((ROOT / "references/expert-gap-calibration.json").read_text(encoding="utf-8"))
        freeze = json.loads((ROOT / "references/semantic-baseline-freeze.json").read_text(encoding="utf-8"))
        gt = (ROOT / "references/ground-truth-labels.md").read_text(encoding="utf-8")
        protocol = (ROOT / "references/model-selection-evaluation-protocol.md").read_text(encoding="utf-8")

        self.assertIn(SMALL_DEFINITION, structure)
        self.assertEqual(expert["shared_gap_semantics"]["small"], SMALL_DEFINITION)
        self.assertEqual(freeze["shared_gap_semantics"]["small"], SMALL_DEFINITION)
        self.assertIn(SMALL_DEFINITION, gt)
        self.assertIn(SMALL_DEFINITION, protocol)
        self.assertIn(ONE_SIDED_SMALL_BOUNDARY, structure)

        old_small_definitions = (
            "双方都执行了本阶段功能，存在可见但较小的执行差距",
            "双方都执行且存在可见小差距时记 `small`",
            "`small` 表示双方都执行且存在可见的小差距",
        )
        for text in (structure, expert.__str__(), freeze.__str__(), gt, protocol):
            for old_definition in old_small_definitions:
                self.assertNotIn(old_definition, text)

        for semantics in (expert["shared_gap_semantics"], freeze["shared_gap_semantics"]):
            self.assertEqual(semantics["none"], "双方都执行了该阶段功能，且实际表现基本相同")
            self.assertIn("事实不完整、冲突或无法稳定判断", semantics["uncertain"])
            self.assertIn("幅度为", semantics["one_sided_execution"])
            self.assertIn("small", semantics["one_sided_execution"])
        self.assertEqual(
            expert["shared_gap_semantics"]["not_applicable"],
            "双方覆盖完整且都没有执行该阶段功能；不设置方向，不进入差距分母",
        )
        self.assertEqual(
            freeze["shared_gap_semantics"]["not_applicable"],
            "双方覆盖完整且都没有执行该阶段功能，或该阶段按专家协议不涉及；不设置方向，不进入gap分母",
        )

    def test_shared_small_definition_reaches_every_stage_group(self) -> None:
        for stages in (["S1", "S2"], ["S3", "S4"], ["S5"], ["S6"]):
            with self.subTest(stages=stages):
                payload = build_stage_group_judgment_payload("test-model", "", {}, {}, list(stages))
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn(SMALL_DEFINITION, text)
                self.assertIn(ONE_SIDED_SMALL_BOUNDARY, text)


if __name__ == "__main__":
    unittest.main()
