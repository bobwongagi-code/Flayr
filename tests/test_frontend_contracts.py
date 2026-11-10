from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node is required for browser-protocol execution tests"
    )
    def test_actual_app_with_deferred_responses(self) -> None:
        for scenario in (
            "api_errors",
            "entity_contracts",
            "poll_selection",
            "poll_order",
            "poll_slow",
            "list_order",
            "list_partial_update",
            "missing_report",
            "upload_protocol",
            "upload_singleflight",
        ):
            with self.subTest(scenario=scenario):
                environment = {
                    key: os.environ[key]
                    for key in ("PATH", "SystemRoot", "TEMP", "TMP")
                    if key in os.environ
                }
                environment["NODE_NO_WARNINGS"] = "1"
                result = subprocess.run(
                    [
                        shutil.which("node"),
                        "--experimental-vm-modules",
                        str(ROOT / "tests" / "frontend_runtime.mjs"),
                        str(ROOT),
                        scenario,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=15,
                    env=environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"passed":true', result.stdout)

    def test_frontend_is_split_into_static_shell_and_modules(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="styles.css">', index)
        self.assertIn('<script type="module" src="app.js"></script>', index)
        self.assertNotIn("<style>", index)
        self.assertNotIn("<script>\n", index)
        self.assertIn("./components/audience-switch.js", app)
        self.assertIn("./components/job-list.js", app)
        self.assertIn("./components/report-view.js", app)

    def test_audience_memory_is_scoped_and_degraded_status_is_visible(self) -> None:
        audience = (ROOT / "frontend" / "components" / "audience-switch.js").read_text(
            encoding="utf-8"
        )
        job_list = (ROOT / "frontend" / "components" / "job-list.js").read_text(
            encoding="utf-8"
        )
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("environment, workspaceId, jobId, 'audience'", audience)
        self.assertIn("已完成（部分分析能力降级）", job_list)
        self.assertIn("report-degraded-detail", app)
        self.assertIn("method:'HEAD'", app)
        self.assertIn("transitionId", app)
        creator_report = (ROOT / "assets" / "creator_report.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("degraded-note", creator_report)
        self.assertIn("report.analysisState === 'degraded'", creator_report)

    def test_report_switching_retains_previous_frame_until_new_frame_is_ready(
        self,
    ) -> None:
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("var previousFrame = reportAudienceState.activeFrame", app)
        self.assertIn("frameReady = true", app)
        self.assertIn("previousFrame.classList.add('report-frame-exiting')", app)
        self.assertIn("showToast('报告打开失败，请稍后重试')", app)
        self.assertIn(
            "if (transitionId !== reportAudienceState.transitionId){\n        frame.remove();",
            app,
        )

    def test_report_switching_has_no_default_audience(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("请选择要查看的报告", index)
        self.assertIn("var storedAudience = readStoredReportAudience", app)
        self.assertIn("updateReportAudienceControls(job, null, false)", app)


if __name__ == "__main__":
    unittest.main()
