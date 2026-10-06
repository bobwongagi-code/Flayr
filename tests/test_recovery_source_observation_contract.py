import copy
import unittest
from unittest.mock import patch

from scripts.flayr_core.llm.payload import (
    TRUST_SOURCE_OBSERVATION_RULE,
    build_video_fact_payload,
    build_video_fact_recovery_payload,
)


class RecoverySourceObservationContractTests(unittest.TestCase):
    def recovery(self, facts=None):
        with patch("scripts.flayr_core.llm.payload._replace_recovery_full_media", return_value=[]):
            return build_video_fact_recovery_payload(
                "gemini-3.7-flash", "creator", {}, [], facts or {}, ["S5"]
            )

    def test_initial_and_recovery_share_exact_source_observation_rule(self):
        initial = build_video_fact_payload("gemini-3.7-flash", "creator", {}, [])
        recovery = self.recovery()
        self.assertIn(TRUST_SOURCE_OBSERVATION_RULE, initial["messages"][0]["content"])
        text = "\n".join(b.get("text", "") for b in recovery["messages"][1]["content"])
        self.assertIn(TRUST_SOURCE_OBSERVATION_RULE, text)
        self.assertIn("没有则省略", text)

    def test_source_detail_can_be_appended_without_changing_locked_observation(self):
        facts = {"evidence_units": [{"id": "C1", "visual_fact": "source detail unresolved"}]}
        original = copy.deepcopy(facts)
        recovery = self.recovery(facts)
        self.assertEqual(facts, original)
        instruction = recovery["messages"][0]["content"]
        self.assertIn("用新的唯一 ID 追加来源细节候选", instruction)
        self.assertIn("不把认证标志自动认定为独立权威背书", instruction)
        self.assertIn("不得改写、删除或合并已有 evidence_units", instruction)
        self.assertNotIn("_flayr_native_schema", recovery)


if __name__ == "__main__":
    unittest.main()
