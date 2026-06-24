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

# are_xie 真实形态："标杆那种"是比较指代非主语，不得切换主语归属
r5 = mk(
    "达人视频在有效 CTA 前结束，且缺乏标杆那种强烈的行动指令和紧迫感营造",
    "Pastikan beli dekat bag kuning. Check out sekarang.",
    "Kalau nak beli, order dekat bag kuning",
)
validate_narrative_evidence_consistency(r5)
r5_warnings = r5.get("qa_warnings") or []
check(
    "Q19 比较指代不切换主语（are_xie 真实 gap）",
    len(r5_warnings) == 1 and "达人" in r5_warnings[0],
    str(r5_warnings)[:90],
)

# kakwan 真实形态：主语只在首段，后段逗号继承
r6 = mk("达人虽未使用黄袋这一特定术语，但明确告知用户链接在购物车里，提供了清晰的购买路径，与标杆的引导效果一致", "dia punya review pun ada dekat background ni")
validate_narrative_evidence_consistency(r6)
r6_warnings = r6.get("qa_warnings") or []
check(
    "Q19 主语继承（kakwan 真实多逗号 gap）",
    len(r6_warnings) == 1 and "达人" in r6_warnings[0] and "脑补" in r6_warnings[0],
    str(r6_warnings)[:90],
)

# 4. endorsement tag 两条归一化路径透传
from flayr_core.llm.parse import normalize_video_understanding  # noqa: E402

u = normalize_video_understanding(
    {"creator": {"evidence_units": [{"id": "C1", "time_range": "1s - 3s", "information": "x", "endorsement_verbal": "true", "endorsement_visual": "false"}]}}
)
check("背书双信道透传 normalize_video_understanding",
      u["creator"]["evidence_units"][0].get("endorsement_verbal") is True
      and u["creator"]["evidence_units"][0].get("endorsement_visual") is False)

# 4b. F项背书接管线：全unit聚合 + hard-only口径 + S5闸（软背书/无硬背书→small）
from flayr_core.postprocess.derive import _side_endorsement, _derive_one, _Endorsement  # noqa: E402
from flayr_core.postprocess.repair_stages import has_hard_endorsement  # noqa: E402

# 聚合作用域=全unit：背书落在非S5_trust的unit（如S2_intro）也算，不漏检
_agg = _side_endorsement(
    {"video_understanding": {"creator": {"evidence_units": [
        {"functions": ["S2_intro"], "endorsement_verbal": True, "endorsement_visual": False}]}}}, "creator")
check("背书聚合：非S5_trust unit 的背书也算（全unit作用域）", _agg.verbal is True and _agg.available is True)

# hard-only：硬来源算、软背书(好评/销量/口碑)不算
check("has_hard_endorsement 硬来源=True", has_hard_endorsement("画面出现 KKM 认证、医生推荐"))
check("has_hard_endorsement 软背书=False", not has_hard_endorsement("好评如潮、销量第一、口碑很好、testimoni"))

# S5闸：双方均无硬背书flag → small（用户判例：软背书≤硬背书）
_s5_none = _derive_one("S5", {"creator_execution": 1.0, "benchmark_execution": 1.0, "creator_summary": "好评", "benchmark_summary": "x"},
                       {"S5": 1.0}, [], None, {"benchmark": _Endorsement(False, False, True), "creator": _Endorsement(False, False, True)})
check("S5 双方均无硬背书(flag)→small", _s5_none.get("severity") == "small" and "结构化 flag" in _s5_none.get("reason", ""))

# S5闸：一方有硬背书flag → 不判'均未涉及'，进公式
_s5_one = _derive_one("S5", {"creator_execution": 1.0, "benchmark_execution": 2.0, "creator_summary": "x", "benchmark_summary": "KKM"},
                      {"S5": 1.0}, [], None, {"benchmark": _Endorsement(False, True, True), "creator": _Endorsement(False, False, True)})
check("S5 一方有硬背书→进公式不判均未涉及", "均未涉及" not in _s5_one.get("reason", ""))

# 5. 死代码已清 + 模块仍可导入
import flayr_core.prompt as prompt_module  # noqa: E402

check("prompt.render_stage_frame_markdown 已删", not hasattr(prompt_module, "render_stage_frame_markdown"))

print()
print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
