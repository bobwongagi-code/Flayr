"""Synthetic offline checks; none of these tests grade video/content semantics."""

from __future__ import annotations

import base64
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from PIL import Image

from scripts.review_evidence import check_index, read_json, sha256
from scripts.validate_learning_review import main as learning_main
from scripts.validate_review_delivery import inspect
from scripts.validate_review_delivery import main as delivery_main
from tests.test_learning_review_materials import example_review


class EvidenceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packet = self.root / "cases.json"
        self.index_path = self.root / "index.json"
        self.data = example_review()
        self.packet.write_text(json.dumps(self.data), encoding="utf-8")
        for name, content in (
            ("teaching-points.md", b"## T1\nTeaching source"),
            ("video.mp4", b"synthetic video identity; not real media"),
            ("frame.jpg", b"synthetic frame identity; not real media"),
        ):
            (self.root / name).write_bytes(content)
        self.index = {
            "schema_version": 1,
            "packet_sha256": sha256(self.packet),
            "teaching": {**self.file("teaching-points.md"), "points": {"T1": "## T1"}},
            "videos": {"V1": {**self.file("video.mp4"), "duration_seconds": 10}},
            "evidence": {
                "E1": {
                    "video": "V1",
                    "time_range": [1, 5],
                    "frames": [{**self.file("frame.jpg"), "time_seconds": 3}],
                }
            },
            "cases": {"01": {"video": "V1", "facts": [["E1"]]}},
        }

    def file(self, name):
        return {"path": name, "sha256": sha256(self.root / name)}

    def save(self):
        self.index_path.write_text(json.dumps(self.index), encoding="utf-8")

    def run_check(self):
        self.save()
        return check_index(self.packet, self.data, self.index_path)

    def test_valid_index_is_read_only_and_reports_exact_scope(self):
        self.save()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = check_index(self.packet, self.data, self.index_path)
        self.assertEqual(result["bound_facts"], 1)
        self.assertEqual(result["unresolved_facts"], [])
        self.assertIn("no semantic approval", result["scope"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_unresolved_is_explicit_not_fabricated_binding(self):
        self.index["cases"]["01"]["facts"] = [{"unresolved": "Audio not reviewed"}]
        result = self.run_check()
        self.assertEqual(result["bound_facts"], 0)
        self.assertEqual(result["unresolved_facts"][0]["reason"], "Audio not reviewed")

    def test_unknown_teaching_ids_and_mislabeled_quotes_fail(self):
        for points in (
            {"T9": "## T1"},
            {"T1": "missing quotation"},
            {"T1": "Teaching source"},
        ):
            with self.subTest(points=points):
                self.index["teaching"]["points"] = points
                with self.assertRaises(ValueError):
                    self.run_check()

    def test_changed_or_missing_inputs_fail(self):
        for name in ("cases.json", "video.mp4", "frame.jpg", "teaching-points.md"):
            path = self.root / name
            original = path.read_bytes()
            for change in ("modify", "remove"):
                with self.subTest(name=name, change=change):
                    if change == "modify":
                        path.write_bytes(original + b"changed")
                    else:
                        path.unlink()
                    with self.assertRaises((ValueError, OSError)):
                        self.run_check()
                    path.write_bytes(original)

    def test_other_teaching_file_is_not_substitutable(self):
        path = self.root / "other.md"
        path.write_text("## T1", encoding="utf-8")
        self.index["teaching"].update(self.file("other.md"))
        with self.assertRaisesRegex(ValueError, "another teaching"):
            self.run_check()

    def test_unknown_and_wrong_video_evidence_fail(self):
        self.index["videos"]["V2"] = dict(self.index["videos"]["V1"])
        for refs in (["missing"], ["E1", "E1"], [], [False], [{}]):
            with self.subTest(refs=refs):
                self.index["cases"]["01"]["facts"] = [refs]
                with self.assertRaises(ValueError):
                    self.run_check()
        self.index["cases"]["01"].update(video="V2", facts=[["E1"]])
        with self.assertRaisesRegex(ValueError, "wrong-video"):
            self.run_check()

    def test_case_and_fact_coverage_cannot_silently_shrink(self):
        original = copy.deepcopy(self.index)
        for binding in (
            {},
            {"02": original["cases"]["01"]},
            {"01": {"video": "V1", "facts": []}},
            {"01": {"video": "V1", "facts": [{"unresolved": ""}]}},
        ):
            with self.subTest(binding=binding):
                self.index["cases"] = binding
                with self.assertRaises(ValueError):
                    self.run_check()

    def test_ranges_reject_negative_reversed_nonfinite_and_bool(self):
        for times in (
            [-1, 5],
            [5, 1],
            [1, 11],
            [False, 5],
            [float("nan"), 5],
            [1],
            "1-5",
        ):
            with self.subTest(times=times):
                self.index["evidence"]["E1"]["time_range"] = times
                with self.assertRaises(ValueError):
                    self.run_check()

    def test_frame_outside_range_and_invalid_duration_fail(self):
        self.index["evidence"]["E1"]["frames"][0]["time_seconds"] = 8
        with self.assertRaises(ValueError):
            self.run_check()
        self.index["evidence"]["E1"]["frames"][0]["time_seconds"] = 3
        for duration in (False, 0, -1, "10", float("inf"), 10**400):
            with self.subTest(duration=duration):
                self.index["videos"]["V1"]["duration_seconds"] = duration
                with self.assertRaises(ValueError):
                    self.run_check()

    def test_duplicate_json_keys_are_not_silently_accepted(self):
        self.index_path.write_text('{"cases":{},"cases":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            read_json(self.index_path)

    def test_packet_changed_after_parsing_cannot_bind_stale_content(self):
        self.data["cases"][0][
            "product_question"
        ] = "Old parsed data no longer matches bytes"
        with self.assertRaisesRegex(ValueError, "during validation"):
            self.run_check()

    def test_cli_bindings_require_packet_and_preserve_legacy_limits(self):
        self.save()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                learning_main(
                    [str(self.packet), "--evidence-index", str(self.index_path)]
                ),
                0,
            )
        self.assertIn("bound_facts", output.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            learning_main(["--evidence-index", str(self.index_path)])


class FinalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "final.html"
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "green").save(stream, format="PNG")
        self.image = (
            "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
        )

    def write(self, author="可以拍清使用。", extra=""):
        self.path.write_text(
            "<!doctype html><html><head><title>Test</title></head><body>"
            f'<main id="author"><p>{author}</p><img src="{self.image}" alt="使用画面">'
            '<a href="#evidence">来源</a></main><section id="evidence">内部标杆记录</section>'
            + extra
            + "</body></html>",
            encoding="utf-8",
        )

    def errors(self):
        return inspect(self.path, ["author"])["automatic_check"]["errors"]

    def test_valid_self_contained_artifact_is_not_approval(self):
        self.write()
        before = self.path.read_bytes()
        result = inspect(self.path, ["author"])
        self.assertEqual(result["automatic_check"]["status"], "passed")
        self.assertEqual(result["images"], 1)
        self.assertEqual(result["human_confirmation"]["status"], "not_recorded")
        self.assertEqual(result["ai_review"]["status"], "not_recorded")
        self.assertTrue(result["not_verified"])
        self.assertEqual(before, self.path.read_bytes())

    def test_author_regions_are_required_and_internal_text_is_separate(self):
        self.write()
        self.assertFalse(self.errors())
        self.assertTrue(inspect(self.path, ["missing"])["automatic_check"]["errors"])
        self.assertTrue(inspect(self.path, ["evidence"])["automatic_check"]["errors"])

    def test_wording_captions_and_boundary_bypass_fail(self):
        for author in (
            "给达人看标杆。",
            '可以看<img src="' + self.image + '" alt="标杆">',
            "## 内部审核备注，不随作者正文发送\n达人S3",
            "给达<span>人</span>看标<strong>杆</strong>。",
            '给达<span title="说明">人</span>看。',
            "可以拍清使用。 [说明](标杆)",
            '<input type="button" value="标杆">',
            '<input placeholder="给达人看">',
        ):
            with self.subTest(author=author):
                self.write(author)
                self.assertTrue(self.errors())

    def test_machine_paths_in_internal_text_and_encoded_paths_fail(self):
        for path in (
            "/Users/test/file.md",
            "file:///tmp/x",
            "/home/user/file",
            "C:\\Users\\x",
            "%2FUsers%2Ftest%2Ffile",
            "&#47;Users&#47;test&#47;file",
        ):
            with self.subTest(path=path):
                self.write(extra=f"<pre>{path}</pre>")
                self.assertTrue(self.errors())
        self.write(extra="<pre>/Us<span>ers</span>/person/private-file.md</pre>")
        self.assertTrue(self.errors())
        self.write(
            extra='<pre>/Us<span title="说明">ers</span>/person/private-file.md</pre>'
        )
        self.assertTrue(self.errors())

    def test_resource_escape_routes_fail(self):
        for extra in (
            '<img src="local.jpg">',
            '<img src="https://example.test/a.png">',
            '<link rel="stylesheet" href="theme.css">',
            '<base href="https://example.test">',
            '<a href="javascript:alert(1)">x</a>',
            '<iframe srcdoc="x"></iframe>',
            "<style>body{background:url(x)}</style>",
            '<style>@import "x";</style>',
            '<style>body{background:image-set("https://example.test/a.png" 1x)}</style>',
            '<a href="#evidence" ping="https://example.test/log">x</a>',
            '<img srcset="x.png 1x">',
            '<meta http-equiv="refresh" content="0;url=x">',
        ):
            with self.subTest(extra=extra):
                self.write(extra=extra)
                self.assertTrue(self.errors())

    def test_broken_or_mislabeled_embedded_image_fails(self):
        for uri in (
            "data:image/png;base64,aGVsbG8=",
            self.image.replace("image/png", "image/jpeg"),
            "data:image/svg+xml;base64,PHN2Zz4=",
            "data:image/png;base64,!",
        ):
            with self.subTest(uri=uri[:30]):
                self.write(extra=f'<img src="{uri}">')
                self.assertTrue(self.errors())
        stream = io.BytesIO()
        Image.new("RGB", (20, 20), "green").save(stream, format="JPEG")
        truncated = (
            "data:image/jpeg;base64,"
            + base64.b64encode(stream.getvalue()[:-10]).decode()
        )
        self.write(extra=f'<img src="{truncated}">')
        self.assertTrue(self.errors())

    def test_valueless_attributes_do_not_crash(self):
        self.write(extra="<div style></div><meta http-equiv>")
        self.assertFalse(self.errors())

    def test_missing_fragment_and_duplicate_ids_fail(self):
        for extra in (
            '<a href="#missing">x</a>',
            '<span id="author">x</span>',
            "<div><b>unclosed</div>",
            '<img src="x" src="y">',
        ):
            with self.subTest(extra=extra):
                self.write(extra=extra)
                self.assertTrue(self.errors())

    def test_changed_final_file_rejects_old_digest(self):
        self.write()
        old = sha256(self.path)
        self.write(author="可以把使用过程拍完整。")
        errors = inspect(self.path, ["author"], old)["automatic_check"]["errors"]
        self.assertTrue(any("changed" in e for e in errors))

    def test_inline_script_is_explicitly_unverified(self):
        self.write(extra='<script>document.title="local";</script>')
        result = inspect(self.path, ["author"])
        self.assertEqual(result["inline_scripts_requiring_browser_review"], 1)
        self.assertIn("script behavior", " ".join(result["not_verified"]))

    def test_cli_output_is_read_only_and_cannot_silently_omit_regions(self):
        self.write()
        before = self.path.read_bytes()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                delivery_main([str(self.path), "--author-region", "author"]), 0
            )
        self.assertEqual(
            json.loads(output.getvalue())["artifact_sha256"], sha256(self.path)
        )
        for extra in (
            [],
            ["--author-region", ""],
            ["--author-region", "author", "--author-region", "author"],
        ):
            with self.subTest(extra=extra), redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit):
                delivery_main([str(self.path), *extra])
        self.assertEqual(before, self.path.read_bytes())


class ContrastSourcesTests(unittest.TestCase):
    def test_contrasts_have_live_sources_and_scoped_counterexamples_not_scores(self):
        root = Path(__file__).resolve().parents[1] / "references"
        data = read_json(root / "review-contrasts.json")
        self.assertEqual(data["schema_version"], 1)
        ids = set()
        for case in data["cases"]:
            self.assertNotIn(case["id"], ids)
            ids.add(case["id"])
            for key in (
                "scope",
                "observed",
                "wrong",
                "accepted",
                "counterexample",
                "counterexample_basis",
                "check",
            ):
                self.assertTrue(case[key].strip())
            source = case["source"]
            self.assertIn(
                source["quote"], (root / source["path"]).read_text(encoding="utf-8")
            )
            self.assertFalse({"score", "grade", "gap", "ground_truth"} & case.keys())
        self.assertEqual(len(ids), 5)


if __name__ == "__main__":
    unittest.main()
