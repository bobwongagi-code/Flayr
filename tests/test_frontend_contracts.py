from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
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
        audience = (ROOT / "frontend" / "components" / "audience-switch.js").read_text(encoding="utf-8")
        job_list = (ROOT / "frontend" / "components" / "job-list.js").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("environment, workspaceId, jobId, 'audience'", audience)
        self.assertIn("已完成（部分分析能力降级）", job_list)
        self.assertIn("report-degraded-detail", app)
        self.assertIn("method:'HEAD'", app)
        self.assertIn("transitionId", app)

    def test_report_switching_has_no_default_audience(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        self.assertIn("请选择要查看的报告", index)
        self.assertIn("var storedAudience = readStoredReportAudience", app)
        self.assertIn("updateReportAudienceControls(job, null, false)", app)


if __name__ == "__main__":
    unittest.main()
