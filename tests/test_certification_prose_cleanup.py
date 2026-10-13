"""Certification observations must survive report-only brand cleanup."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.postprocess.repair_claims import remove_unverified_brand_models
from flayr_core.stage_evidence_contracts import STAGE_EVIDENCE_CONTRACT_VERSION


class CertificationProseCleanupTests(unittest.TestCase):
    def result(self, *, strength="direct", subject="correct", visual=True):
        unit = {
            "id": "C7", "evidence_strength": strength,
            "fact_quality": {"subject": subject},
            "visual_fact": "Printed ISO 9001 and ISO 22000" if visual else "Bottle label",
            "trust_source_reference": "ISO 9001, ISO 22000",
            "trust_source_status": "explicit_present",
        }
        return {
            "video_understanding": {"creator": {
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                "evidence_units": [unit], "stage_evidence_checks": [],
            }},
            "stage_analysis": [{"judgment_reason": "ISO 9001、ISO 22000; UnknownBrand 123"}],
            "improvements": [{"suggestion": "ISO 9001; UnknownBrand 123"}],
            "executive_summary": "ISO 22000; UnknownBrand 123",
        }

    def test_visual_source_preserved_without_whitelisting_unrelated_brand(self):
        result = self.result()
        original = copy.deepcopy(result["video_understanding"])
        remove_unverified_brand_models(result, {"product": {"name": "PET CHEWS"}, "videos": {}})
        self.assertEqual(result["stage_analysis"][0]["judgment_reason"], "ISO 9001、ISO 22000; PET CHEWS")
        self.assertEqual(result["improvements"][0]["suggestion"], "ISO 9001; PET CHEWS")
        self.assertEqual(result["executive_summary"], "ISO 22000; PET CHEWS")
        self.assertEqual(result["video_understanding"], original)

    def test_inferred_wrong_subject_or_unseen_reference_is_not_whitelisted(self):
        for kwargs in ({"strength": "inferred"}, {"subject": "incorrect"}, {"visual": False}):
            with self.subTest(kwargs=kwargs):
                result = self.result(**kwargs)
                remove_unverified_brand_models(result, {"product": {"name": "PET CHEWS"}, "videos": {}})
                self.assertEqual(result["stage_analysis"][0]["judgment_reason"], "PET CHEWS、PET CHEWS; PET CHEWS")

    def test_unversioned_model_story_cannot_supply_visual_whitelist(self):
        result = self.result()
        result["video_understanding"]["creator"].pop("stage_evidence_contract_version")
        remove_unverified_brand_models(result, {"product": {"name": "PET CHEWS"}, "videos": {}})
        self.assertEqual(result["executive_summary"], "PET CHEWS; PET CHEWS")


if __name__ == "__main__":
    unittest.main()
