from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from scripts.validate_learning_review import (
    FOUNDATION_FIELDS, INTERNAL_NOTES, REQUIRED, main, validate, validate_author_copy,
)


def example_review():
    return {
        "product_context": {"name": "Example product", "review_foundation": {
            field: {"text": "A scoped review proposal", "basis": "analysis_proposal",
                    "source": "Teaching T1 at 10-15s; proposed interpretation"}
            for field in FOUNDATION_FIELDS
        }},
        "teaching_reference": "teaching-points.md",
        "cases": [{"id": "01", "product_question": "What does using it show?",
                   "reference_logic": "T1 explains the action's purpose.",
                   "teaching_points": ["T1"],
                   "whole_video": {field: "Scoped reasoning" for field in REQUIRED},
                   "facts": [{"kind": "unreviewed", "evidence": "video.mp4 1-5s",
                              "text": "Candidate only; not yet verified."}]}],
    }


class LearningReviewMaterialTests(unittest.TestCase):
    def setUp(self):
        self.data = example_review()

    def test_complete_materials_do_not_change_approval(self):
        self.data["review_status"] = "pending_human_review"
        before = copy.deepcopy(self.data)
        self.assertEqual(validate(self.data), [])
        self.assertEqual(self.data, before)

    def test_marketing_topics_alone_cannot_replace_foundation(self):
        self.data["product_context"] = {"name": "Cream", "marketing_topics": ["Texture"]}
        self.assertTrue(any("review_foundation" in e for e in validate(self.data)))

    def test_each_foundation_field_requires_text_source_and_basis(self):
        for field in FOUNDATION_FIELDS:
            for key in ("text", "source", "basis"):
                for bad in (None, "", "  ", [], False):
                    with self.subTest(field=field, key=key, bad=bad):
                        data = copy.deepcopy(self.data)
                        data["product_context"]["review_foundation"][field][key] = bad
                        self.assertTrue(validate(data))
            data = copy.deepcopy(self.data)
            del data["product_context"]["review_foundation"][field]
            self.assertTrue(validate(data))

    def test_explicit_unknown_is_allowed_without_inventing_facts(self):
        self.data["product_context"]["review_foundation"]["consumer_outcome"] = {
            "text": "Outcome not established", "basis": "unknown",
            "source": "Only the supplied packaging was available; no outcome verified",
        }
        self.assertEqual(validate(self.data), [])

    def test_whole_reasoning_fields_required(self):
        for field in REQUIRED:
            data = copy.deepcopy(self.data)
            del data["cases"][0]["whole_video"][field]
            self.assertTrue(validate(data))

    def test_product_and_teaching_connections_required(self):
        for field in ("product_question", "reference_logic", "teaching_points"):
            data = copy.deepcopy(self.data)
            del data["cases"][0][field]
            self.assertTrue(validate(data))

    def test_evidence_sources_required_but_unreviewed_allowed(self):
        for field in ("kind", "evidence", "text"):
            data = copy.deepcopy(self.data)
            del data["cases"][0]["facts"][0][field]
            self.assertTrue(validate(data))

    def test_no_forced_improvement_or_before_after_for_every_product(self):
        self.data["cases"][0]["whole_video"]["priority_gap"] = "No supported improvement found."
        self.assertEqual(validate(self.data), [])

    def test_invalid_shapes_and_duplicate_ids(self):
        for data in (None, [], {}, {"cases": [None]}):
            self.assertTrue(validate(data))
        self.data["cases"].append(copy.deepcopy(self.data["cases"][0]))
        self.assertTrue(any("duplicate" in e for e in validate(self.data)))

    def test_cli_is_read_only_and_reports_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            (path.parent / "teaching-points.md").write_text("# T1\nTeaching source", encoding="utf-8")
            path.write_text(json.dumps(self.data), encoding="utf-8")
            original = path.read_bytes()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(path)]), 0)
            self.assertIn("Completeness only", output.getvalue())
            self.assertEqual(path.read_bytes(), original)

    def test_cli_rejects_missing_or_empty_teaching_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(self.data), encoding="utf-8")
            teaching = path.parent / "teaching-points.md"
            for content in (None, "  \n", b"\xff"):
                with self.subTest(content=content):
                    if isinstance(content, str):
                        teaching.write_text(content, encoding="utf-8")
                    elif isinstance(content, bytes):
                        teaching.write_bytes(content)
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                        main([str(path)])
                    self.assertEqual(caught.exception.code, 1)

    def test_cli_resolves_teaching_relative_to_cases_not_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            (path.parent / "teaching-points.md").write_text("T1: Source", encoding="utf-8")
            path.write_text(json.dumps(self.data), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(path)]), 0)
            self.data["teaching_reference"] = str(path.parent / "teaching-points.md")
            path.write_text(json.dumps(self.data), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(path)]), 0)

    def test_source_check_does_not_assert_teaching_ids_or_semantics_are_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            (path.parent / "teaching-points.md").write_text("Source without parsed IDs", encoding="utf-8")
            path.write_text(json.dumps(self.data), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(path)]), 0)
            self.assertIn("Reference IDs and evidence contents are not verified", output.getvalue())

    def test_cli_rejects_invalid_json_and_incomplete_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for content in ("not-json", "{}"):
                path.write_text(content, encoding="utf-8")
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main([str(path)])
                self.assertEqual(caught.exception.code, 1)


class CreatorWordingTests(unittest.TestCase):
    def test_handbook_risks_are_reported_without_rewriting(self):
        for text in (
            "建议达人向标杆学习。", "S3给medium。",
            "出单率大概率能再翻一倍。", "转化率提升10%。",
            "销量保证会增长。", "真实变化最能出单！",
            "产品出早了，买家被吓跑了。", "同款卖爆的那条这样拍。",
            "跑得最快的一个案例。", "这样做一定能爆单。",
        ):
            with self.subTest(text=text):
                errors = validate_author_copy(text)
                self.assertTrue(errors)
                self.assertTrue(any("line 1" in error for error in errors))

    def test_concrete_advice_and_sourced_numbers_are_not_blanket_banned(self):
        text = (
            "同款参考视频：眼周特写。\n"
            "把前后画面拼在一起，保持相同光线，标上实际记录时间。\n"
            "口播提到价格RM20和销量300单；这些台词仍需听审。\n"
            "图中写着small size。\n"
            "感兴趣可以点下方小黄车看看。"
        )
        self.assertEqual(validate_author_copy(text), [])

    def test_source_paths_and_explicit_internal_section_are_not_scanned(self):
        text = (
            "![眼周画面](/private/达人/benchmark/S3-medium.jpg)\n"
            "[查看片段](/video/标杆.mp4)\n"
            f"{INTERNAL_NOTES}\n标杆S3给medium；旧稿写出单率翻倍。"
        )
        self.assertEqual(validate_author_copy(text), [])

    def test_personal_result_records_are_not_treated_as_sales_forecasts(self):
        # Passing the wording scan does not verify that these records are true.
        for text in (
            "第1天和第7天的眼周记录放在一起，大家看这个位置。",
            "我用了7天，眼周颜色比之前淡了一些。",
            "这边用了，这边没用，大家看这个位置。",
            "擦之前和擦之后放在一起，能看到地砖上的污渍被擦掉。",
        ):
            with self.subTest(text=text):
                self.assertEqual(validate_author_copy(text), [])

    def test_visible_captions_and_headings_are_checked(self):
        for text in (
            "![标杆画面](/image.jpg)", "[达人片段](/video.mp4)",
            "# Stage2分析", f"说明里写着{INTERNAL_NOTES}，仍然出现标杆。",
        ):
            with self.subTest(text=text):
                self.assertTrue(validate_author_copy(text))

    def test_empty_author_body_cannot_pass_with_only_internal_notes(self):
        for text in ("", "  \n", f"{INTERNAL_NOTES}\n内部草稿"):
            with self.subTest(text=text):
                self.assertTrue(validate_author_copy(text))

    def test_cli_fails_closed_for_wording_and_missing_drafts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases.json"
            cases.write_text(json.dumps(example_review()), encoding="utf-8")
            (root / "teaching-points.md").write_text("T1 source", encoding="utf-8")
            draft = root / "reply.md"
            bad = "出单率大概率能再翻一倍。"
            draft.write_text(bad, encoding="utf-8")
            for path in (draft, root / "missing.md"):
                with self.subTest(path=path):
                    with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                        main([str(cases), "--author-copy", str(path)])
                    self.assertEqual(caught.exception.code, 1)
            self.assertEqual(draft.read_text(encoding="utf-8"), bad)
            draft.write_text("可以拍清眼周状态。", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([str(cases), "--author-copy", str(draft)]), 0)
            self.assertIn("Limited wording check only", output.getvalue())

    def test_standalone_author_check_needs_no_learning_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / "suggestion.md"
            draft.write_text("可以把前后画面拼在一起。", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--author-copy", str(draft)]), 0)
            self.assertIn("Limited wording check only", output.getvalue())
            self.assertNotIn("Teaching reference is readable", output.getvalue())

    def test_standalone_check_rejects_any_bad_draft_and_changes_none(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("a.md", "b.md", "c.md")]
            originals = ("可以拍清使用。", "给达人看S3。", "可以查看同款参考视频。")
            arguments = []
            for path, text in zip(paths, originals):
                path.write_text(text, encoding="utf-8")
                arguments.extend(["--author-copy", str(path)])
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(arguments)
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(tuple(p.read_text(encoding="utf-8") for p in paths), originals)

    def test_no_inputs_are_not_a_successful_check(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
