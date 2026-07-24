from __future__ import annotations

import unittest

from scripts.flayr_core.semantic_model import SEMANTIC_MODEL_VERSION, SemanticAnalysis


class SemanticModelTests(unittest.TestCase):
    def test_model_result_is_bounded_before_view_projection(self) -> None:
        source = {
            "one_line_summary": "共享语义结论",
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "severity": "medium",
                    "bd_priority": "must not cross the nested boundary",
                    "creator_tip": "must not cross the nested boundary",
                }
            ],
            "improvements": [
                {
                    "title": "共享改进机会",
                    "priority": 1,
                    "planA": "view-only field",
                    "gmvClass": "view-only field",
                }
            ],
            "product": {"name": "测试产品"},
            "video_understanding": {"creator": {"evidence_units": []}},
            "bd_priority": "must not become a semantic field",
            "creator_tip": "must not become a semantic field",
            "planA": "view-only field",
            "candidate_experiments": [{"title": "legacy creator context"}],
            "highlights": [{"text": "legacy creator context"}],
        }

        semantic = SemanticAnalysis.from_mapping(source)

        self.assertEqual(SEMANTIC_MODEL_VERSION, semantic.metadata()["version"])
        self.assertEqual(semantic.stages[0].code, "S1")
        self.assertEqual(semantic.improvements[0].get("title"), "共享改进机会")
        self.assertNotIn("bd_priority", semantic.data)
        self.assertNotIn("creator_tip", semantic.data)
        self.assertNotIn("planA", semantic.data)
        self.assertNotIn("bd_priority", semantic.stages[0].data)
        self.assertNotIn("creator_tip", semantic.stages[0].data)
        self.assertNotIn("planA", semantic.improvements[0].data)
        self.assertNotIn("gmvClass", semantic.improvements[0].data)
        self.assertEqual(
            semantic.creator_context["candidate_experiments"][0]["title"],
            "legacy creator context",
        )

        source["stage_analysis"][0]["severity"] = "large"
        self.assertEqual(semantic.stages[0].get("severity"), "medium")

    def test_view_context_is_not_mixed_into_shared_semantics(self) -> None:
        semantic = SemanticAnalysis.from_mapping(
            {
                "one_line_summary": "结论",
                "stage_analysis": [],
                "improvements": [],
                "content_intent": "creator-only context",
                "candidate_experiments": [{"title": "x", "planA": "view-only field"}],
            }
        )

        self.assertNotIn("candidate_experiments", semantic.data)
        self.assertNotIn("content_intent", semantic.data)
        self.assertEqual(semantic.creator_context["content_intent"], "creator-only context")
        self.assertNotIn("planA", semantic.creator_context["candidate_experiments"][0])
        self.assertEqual(semantic.metadata()["view_contracts"], ["bd_internal", "creator"])


if __name__ == "__main__":
    unittest.main()
