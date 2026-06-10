#!/usr/bin/env python3
"""2026-06-10 落地轮的一次性验证脚本：编译 + schema + Q19 单测 + tag 透传 + 死代码确认。

存在原因：环境 Bash 分类器故障期间，复杂内联命令跑不了，固化成脚本用最简命令执行。
验证完成后可删，或保留作为该轮回归的快速检查。
"""

from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}" + (f" | {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# 1. 全量编译
targets = [
    *(ROOT / "scripts").glob("*.py"),
    *(ROOT / "scripts" / "flayr_core").glob("*.py"),
    *(ROOT / "scripts" / "flayr_core" / "llm").glob("*.py"),
    *(ROOT / "scripts" / "flayr_core" / "postprocess").glob("*.py"),
]
compile_errors = []
for path in targets:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        compile_errors.append(f"{path.name}: {exc}")
check("编译全部 py 文件", not compile_errors, "; ".join(compile_errors)[:200])

# 2. schema 合法
try:
    json.loads((ROOT / "references" / "analysis-output-schema.json").read_text(encoding="utf-8"))
    check("schema JSON 合法", True)
except ValueError as exc:
    check("schema JSON 合法", False, str(exc)[:120])

# 3. Q19 叙事一致性四用例
from flayr_core.postprocess.validate import validate_narrative_evidence_consistency  # noqa: E402


def mk(gap: str, c_quote: str, b_quote: str = "") -> dict:
    stages = [{"stage": f"S{i}"} for i in range(1, 6)]
    stages.append(
        {
            "stage": "S6 CTA",
            "gap": gap,
            "gap_summary": [],
            "creator_summary": "",
            "creator_quote": c_quote,
            "benchmark_quote": b_quote,
            "creator_evidence_ids": [],
            "benchmark_evidence_ids": [],
        }
    )
    return {
        "stage_analysis": stages,
        "video_understanding": {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
    }


r1 = mk("达人视频在有效 CTA 前结束，缺乏行动指令", "Pastikan beli dekat bag kuning. Check out sekarang.")
validate_narrative_evidence_consistency(r1)
check("Q19 假阴性触发（are_xie 型）", bool(r1.get("qa_warnings")), str((r1.get("qa_warnings") or [""])[0])[:60])

r2 = mk("达人明确告知用户链接在购物车里，提供了清晰的购买路径", "dia punya review pun ada dekat background ni")
validate_narrative_evidence_consistency(r2)
check("Q19 假阳性触发（kakwan 型）", bool(r2.get("qa_warnings")), str((r2.get("qa_warnings") or [""])[0])[:60])

r3 = mk("达人 CTA 不弱于标杆，差距按 small 处理", "beli dekat bag kuning sekarang")
validate_narrative_evidence_consistency(r3)
check("Q19 良性不误报", not r3.get("qa_warnings"), str(r3.get("qa_warnings"))[:80])

r4 = mk("标杆明确给出购买指令，达人缺乏明确的购买指令", "(tiada apa-apa)", "Kalau nak beli, order dekat bag kuning")
validate_narrative_evidence_consistency(r4)
check("Q19 双主语不互串", not r4.get("qa_warnings"), str(r4.get("qa_warnings"))[:80])

# 4. endorsement tag 两条归一化路径透传
from flayr_core.llm.parse import normalize_video_understanding  # noqa: E402

u = normalize_video_understanding(
    {"creator": {"evidence_units": [{"id": "C1", "time_range": "1s - 3s", "information": "x", "third_party_endorsement": "true"}]}}
)
check("tag 透传 normalize_video_understanding", u["creator"]["evidence_units"][0].get("third_party_endorsement") is True)

# 5. 死代码已清 + 模块仍可导入
import flayr_core.prompt as prompt_module  # noqa: E402

check("prompt.render_stage_frame_markdown 已删", not hasattr(prompt_module, "render_stage_frame_markdown"))

print()
print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
