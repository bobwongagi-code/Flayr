from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.audit_analysis_chain import main


class AuditAnalysisChainCliTests(unittest.TestCase):
    def test_missing_analysis_artifact_returns_failure_and_keeps_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            output = root / "audit.json"
            manifest.write_text(
                json.dumps({"samples": [{"sample_id": "sample", "run_dir": str(root / "missing")}] }),
                encoding="utf-8",
            )
            with patch(
                "sys.argv",
                ["audit_analysis_chain.py", "--manifest", str(manifest), "--output", str(output)],
            ):
                self.assertEqual(main(), 2)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(artifact["summary"]["run_statuses"], {"invalid_artifact": 1})
            self.assertEqual(artifact["records"][0]["audit"]["status"], "invalid_artifact")


if __name__ == "__main__":
    unittest.main()
