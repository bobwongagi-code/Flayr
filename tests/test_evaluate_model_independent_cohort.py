from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_model_independent_cohort import _read_extraction
from scripts.flayr_core.llm.compact_eval import VISUAL_EXTRACTION_SCHEMA_VERSION


class ModelIndependentCohortInputTests(unittest.TestCase):
    def _write_record(self, root: Path, result: dict) -> Path:
        path = root / "visual_extraction_evaluation.json"
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
                    "result": result,
                    "video_role_order": ["creator", "benchmark"],
                    "video_source_duration_seconds": [10.0, 10.0],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_current_schema_with_invalid_content_is_not_reported_completed(self) -> None:
        result = {
            "schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "creator_evidence_units": [],
            "benchmark_evidence_units": [
                {
                    "id": "B1",
                    "time_range": "0s - 1s",
                    "information": "事实",
                    "functions": ["S1"],
                    "evidence_strength": "direct",
                    "fact_quality": {},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            extracted, metadata = _read_extraction(self._write_record(Path(tmp), result))
        self.assertIsNone(extracted)
        self.assertEqual(metadata["status"], "invalid_contract")
        self.assertTrue(metadata["contract_errors"])

    def test_legacy_schema_is_reported_as_incompatible(self) -> None:
        result = {
            "schema_version": 1,
            "creator_evidence_units": [],
            "benchmark_evidence_units": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            extracted, metadata = _read_extraction(self._write_record(Path(tmp), result))
        self.assertIsNone(extracted)
        self.assertEqual(metadata["status"], "incompatible_schema")

    def test_missing_source_duration_is_not_accepted_for_time_validation(self) -> None:
        result = {
            "schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "creator_evidence_units": [],
            "benchmark_evidence_units": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_record(Path(tmp), result)
            record = json.loads(path.read_text(encoding="utf-8"))
            record.pop("video_source_duration_seconds")
            path.write_text(json.dumps(record), encoding="utf-8")
            extracted, metadata = _read_extraction(path)
        self.assertIsNone(extracted)
        self.assertEqual(metadata["status"], "invalid_contract")
        self.assertTrue(any("duration" in error for error in metadata["contract_errors"]))

    def test_malformed_source_roles_are_reported_as_contract_errors(self) -> None:
        result = {
            "schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "creator_evidence_units": [],
            "benchmark_evidence_units": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_record(Path(tmp), result)
            record = json.loads(path.read_text(encoding="utf-8"))
            record["video_role_order"] = [{"role": "creator"}, "benchmark"]
            path.write_text(json.dumps(record), encoding="utf-8")
            extracted, metadata = _read_extraction(path)
        self.assertIsNone(extracted)
        self.assertEqual(metadata["status"], "invalid_contract")
        self.assertTrue(any("video_role_order" in error for error in metadata["contract_errors"]))


if __name__ == "__main__":
    unittest.main()
