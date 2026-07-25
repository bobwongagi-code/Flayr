"""Repository portability and CI contract checks."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    def test_validation_manifest_has_no_machine_specific_root(self) -> None:
        manifest_path = ROOT / "references" / "validation-inputs.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        serialized = json.dumps(manifest, ensure_ascii=False)
        self.assertIn("${FLAYR_VALIDATION_ROOT}", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("/Documents/", serialized)

    def test_cli_and_committed_docs_have_no_personal_keychain_or_model_path(self) -> None:
        cli = (ROOT / "scripts" / "flayr.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("/Users/", cli)
        self.assertNotIn("VidLingo.Qwen", readme)

    def test_ci_covers_supported_python_versions_across_host_os(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("ubuntu-latest", workflow)
        self.assertIn("macos-latest", workflow)
        self.assertIn("windows-latest", workflow)
        for version in ("3.11", "3.12", "3.13"):
            self.assertIn(version, workflow)
        self.assertIn("run-quality-gates.sh", workflow)
        for tool in ("ruff", "black", "mypy", "bandit", "pip_audit", "coverage"):
            self.assertRegex(workflow + (ROOT / "scripts" / "run-quality-gates.sh").read_text(encoding="utf-8"), re.escape(tool))

    def test_local_reports_and_frontend_have_no_remote_font_requests(self) -> None:
        for relative_path in (
            "assets/bd_report.html",
            "assets/creator_report.html",
            "frontend/index.html",
        ):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("fonts.googleapis.com", content, relative_path)
            self.assertNotIn("fonts.gstatic.com", content, relative_path)

        styles = (ROOT / "frontend" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", styles)
        self.assertNotIn("fonts.gstatic.com", styles)

    def test_release_and_dependency_contracts_are_explicit(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        lock = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8").lower()
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)
        self.assertIn("pillow==12.3.0", lock)
        self.assertNotIn("dashscope", lock)
        self.assertNotIn("certifi", lock)
        self.assertIn("requirements-dev.lock", workflow)


if __name__ == "__main__":
    unittest.main()
