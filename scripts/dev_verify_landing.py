#!/usr/bin/env python3
"""2026-06-10 落地轮的一次性验证脚本：编译 + schema + Q19 单测 + tag 透传 + 死代码确认。

存在原因：环境 Bash 分类器故障期间，复杂内联命令跑不了，固化成脚本用最简命令执行。
验证完成后可删，或保留作为该轮回归的快速检查。
"""

from __future__ import annotations

import json
import os
import py_compile
import sys
import tempfile
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
from flayr_core.postprocess.validate import (  # noqa: E402
    validate_narrative_evidence_consistency,
    validate_s1_hook_flags,
    validate_s2_contract_flags,
    validate_s3_usage_flags,
)


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
from flayr_core.postprocess.repair_stages import has_hard_endorsement, repair_s1_hook_boundaries  # noqa: E402

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

# 4c. S1 Hook flag 化（切片 A）：四维推执行分 + hook_exists 红线 + 命题锚 + 残差亮点门
from flayr_core.llm.parse import normalize_hook_flags, normalize_s2_flags, normalize_s3_flags  # noqa: E402


def _hook(exists, htype, cam=False, cp=False, snd=False, rhy=False, anchors=None, landing=None):
    return {"exists": exists, "type": htype,
            "dims": {"camera": cam, "copy": cp, "sound": snd, "rhythm": rhy},
            "hook_boundary_seconds": 3.0,
            "hook_boundary_reason": "3.0s 后开始产品引出",
            "s2_start_signal": "产品成为解决方案主角",
            "landing_met": landing,
            "landing_reason": "0-3s 钩子窗口内对象张力承诺齐全",
            "window_evidence": "0-3s 钩子窗口",
            "landing_window_leak": False,
            "anchors_proposition": anchors}


def _s1_stage(creator_hook, benchmark_hook, **extra):
    st = {"creator_execution": None, "benchmark_execution": None,
          "creator_summary": "x", "benchmark_summary": "y",
          "creator_hook": creator_hook, "benchmark_hook": benchmark_hook}
    st.update(extra)
    return st


# 四维均满 + 双方相等 → e=0 → small（flag 推执行分，绕过模型 0-2）
_full = _hook(True, "B", True, True, True, True)
_t = _derive_one("S1", _s1_stage(_full, dict(_full)), None, [])
check("S1 四维双满→small（flag 推执行分）", _t.get("severity") == "small" and _t.get("E") == 0)

# 达人 0 维 vs 标杆 4 维（双方均有 Hook，不触红线）→ e=2.0 → S1 large 红线
_c0 = _hook(True, "B", False, False, False, False)
_t2 = _derive_one("S1", _s1_stage(_c0, dict(_full)), None, [])
check("S1 达人四维全缺→large（e=2 核心缺失）", _t2.get("severity") == "large" and _t2.get("E") == 2)

# hook_exists 红线：达人无 Hook、标杆有 Hook → large，且独立于四维
_t3 = _derive_one("S1", _s1_stage(_hook(False, "unknown"), dict(_full)), None, [])
check("S1 hook_exists 红线（达人无Hook标杆有）→large", _t3.get("severity") == "large" and "红线" in _t3.get("reason", ""))

# 命题锚放大：小差距(e=0.5)下，标杆锚命题、达人没 → 放大到 large；无 anchors 则保持 small
_c2 = _hook(True, "B", True, True, False, False)            # 2 维 met → exec 1.0
_b3 = _hook(True, "B", True, True, True, False)             # 3 维 met → exec 1.5，e=0.5
_t_no = _derive_one("S1", _s1_stage(_c2, _b3), None, [])
check("S1 小差距无命题锚→small", _t_no.get("severity") == "small")
_c2a = _hook(True, "B", True, True, False, False, anchors=False)
_b3a = _hook(True, "B", True, True, True, False, anchors=True)
_t_an = _derive_one("S1", _s1_stage(_c2a, _b3a), None, [])
check("S1 命题锚（标杆锚达人没）→放大 large", _t_an.get("severity") == "large" and "锚定品命题" in _t_an.get("reason", ""))

_c_full_no_anchor = _hook(True, "G", True, True, True, True, anchors=False, landing=True)
_b_full_anchor = _hook(True, "C", True, True, True, True, anchors=True, landing=True)
_t_anchor_floor = _derive_one("S1", _s1_stage(_c_full_no_anchor, _b_full_anchor), None, [])
check("S1 命题锚下限：件齐但达人只泛留人→medium",
      _t_anchor_floor.get("severity") == "medium" and "命题锚下限" in _t_anchor_floor.get("reason", ""))

# 残差亮点门：标杆四维全 met 且 type≠unknown → 开；type=unknown → 不开
_t_hl = _derive_one("S1", _s1_stage(_c0, dict(_full)), None, [])
check("S1 亮点门：标杆四维全met+类型明确→开", _t_hl.get("hook_highlight_allowed") is True)
_unk = _hook(True, "unknown", True, True, True, True)
_t_unk = _derive_one("S1", _s1_stage(_c0, _unk), None, [])
check("S1 亮点门：类型 unknown→不开", _t_unk.get("hook_highlight_allowed") is None)

# flag 缺失 → 回退模型执行分（优雅降级，不崩）
_t_fb = _derive_one("S1", _s1_stage(None, None, creator_execution=1.0, benchmark_execution=2.0), None, [])
check("S1 flag 缺失→回退模型执行分", _t_fb.get("status") == "derived" and _t_fb.get("E") == 1.0)

# landing 封顶：达人四维 3/4(=1.5) 但钩子没打穿(landing=false) → 执行分封顶 1.0
_c3_noland = _hook(True, "B", True, True, True, False, landing=False)   # 3 维但 landing=false
_b4_land = _hook(True, "C", True, True, True, True, landing=True)        # 4 维 landing=true
_t_cap = _derive_one("S1", _s1_stage(_c3_noland, _b4_land), None, [])
check("S1 landing 封顶（件齐没打穿→exec≤1.0，e=1.0）", _t_cap.get("E") == 1.0)

# landing 下限（carslan 重演）：标杆立住、达人没立住 → 至少 medium，纠正"件齐误判 small"
check("S1 landing 下限（标杆立住达人没→medium）",
      _t_cap.get("severity") == "medium" and "没打穿" in _t_cap.get("reason", ""))

# 双方都没立住 → 不触发下限（同样没打穿，差距小，保持 small）
_c3_nl = _hook(True, "B", True, True, True, False, landing=False)
_b3_nl = _hook(True, "C", True, True, True, False, landing=False)
_t_both = _derive_one("S1", _s1_stage(_c3_nl, _b3_nl), None, [])
check("S1 双方都没立住→不触发下限（small）", _t_both.get("severity") == "small")

# S2 契约 flag：只判 S1→S2 衔接，不做四维主观分
def _s2_flag(exists=True, merged=False, module="A", handoff=True, compat=True, identity=True, role=True, risky=False):
    return {
        "exists": exists,
        "merged_with_s3": merged,
        "module_type": module,
        "handoff_met": handoff,
        "s1_s2_compatible": compat,
        "product_identity_clear": identity,
        "product_role_clear": role,
        "excluded_or_risky_module": risky,
        "start_seconds": 3.0,
        "end_seconds": 6.0,
        "handoff_reason": "S1 提出痛点，S2 用产品身份和解决方案接住",
        "evidence_ids": ["C1"],
    }


_s2_good = _s2_flag()
_s2_bad = _s2_flag(handoff=False, compat=False, role=False)
_s2_trace = _derive_one(
    "S2",
    {"creator_s2": _s2_bad, "benchmark_s2": _s2_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S2": 1.0},
    [],
)
check("S2 契约 flag：标杆接住达人没接住→至少 medium",
      _s2_trace.get("severity") == "medium")

_s2_merged = _derive_one(
    "S2",
    {"creator_s2": _s2_flag(merged=True), "benchmark_s2": _s2_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S2": 1.0},
    [],
)
check("S2 merged_with_s3=true 且产品身份/角色清楚→不因缺独立S2扣分",
      _s2_merged.get("severity") == "small" and _s2_merged.get("E") == 0)

_s2_upstream_missing = _derive_one(
    "S2",
    {
        "creator_s2": _s2_flag(merged=True, handoff=False, compat=False, identity=True, role=True),
        "benchmark_s2": _s2_good,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S2": 1.0},
    [],
)
check("S2 上游缺 S1 但产品身份/角色清楚→不重复计罚",
      _s2_upstream_missing.get("severity") == "small" and _s2_upstream_missing.get("E") == 0)

_s2_risky = _derive_one(
    "S2",
    {"creator_s2": _s2_flag(risky=True, handoff=False), "benchmark_s2": _s2_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S2": 1.0},
    [],
)
check("S2 风险引出方式→reason 标注风险模块",
      "高风险引出方式" in _s2_risky.get("reason", ""))

# S3 使用过程 flag：真实使用 + 核心卖点可见是主轴，场景丰富只做加分
def _s3_flag(exists=True, module="A", real=True, core=True, context=True, continuity=True, richness=False, fake=False):
    return {
        "exists": exists,
        "module_type": module,
        "real_usage_met": real,
        "core_selling_point_visible": core,
        "usage_context_fit": context,
        "continuity_met": continuity,
        "richness_met": richness,
        "fake_or_staged": fake,
        "start_seconds": 8.0,
        "end_seconds": 18.0,
        "usage_reason": "真实使用动作中演示核心卖点",
        "evidence_ids": ["C2"],
    }


_s3_good = _s3_flag(richness=True)
_s3_no_core = _s3_flag(core=False, richness=True)
_s3_trace = _derive_one(
    "S3",
    {"creator_s3": _s3_no_core, "benchmark_s3": _s3_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S3": 1.6},
    [],
)
check("S3 核心卖点没在动作里可见→至少 medium",
      _s3_trace.get("severity") in {"medium", "large"})

_s3_fake = _derive_one(
    "S3",
    {"creator_s3": _s3_flag(fake=True), "benchmark_s3": _s3_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S3": 1.6},
    [],
)
check("S3 显假摆拍→执行分按 0 处理",
      _s3_fake.get("severity") == "large" and _s3_fake.get("E") == 2)

_s3_thin = _derive_one(
    "S3",
    {"creator_s3": _s3_flag(richness=False), "benchmark_s3": _s3_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S3": 1.0},
    [],
)
check("S3 核心卖点可见但素材不丰富→小到中差距",
      _s3_thin.get("severity") in {"small", "medium"} and _s3_thin.get("E") == 1)

# parse 归一：容忍 'S1-B：反差' / 'yes' / 1 等写法
_nh = normalize_hook_flags({"exists": "true", "type": "S1-B：反差震惊型", "dims": {"camera": "yes", "copy": 1}})
check("S1 parse 归一 hook_flags（type→B, dims 容错）",
      _nh["type"] == "B" and _nh["dims"]["camera"] is True and _nh["dims"]["copy"] is True
      and _nh["dims"]["sound"] is False and _nh["exists"] is True)

_ns2 = normalize_s2_flags({
    "exists": "yes",
    "merged_with_s3": 0,
    "module_type": "S2-B：解谜式",
    "handoff_met": 1,
    "s1_s2_compatible": "true",
    "product_identity_clear": "true",
    "product_role_clear": "false",
    "excluded_or_risky_module": "no",
    "start_seconds": "3.7",
    "end_seconds": 8,
    "handoff_reason": "解谜引出产品",
    "evidence_ids": "C1",
})
check("S2 parse 归一 contract flags（type→B, bool/时间/evidence 容错）",
      _ns2["module_type"] == "B"
      and _ns2["exists"] is True
      and _ns2["merged_with_s3"] is False
      and _ns2["product_role_clear"] is False
      and _ns2["start_seconds"] == 3.7
      and _ns2["evidence_ids"] == ["C1"])

_ns3 = normalize_s3_flags({
    "exists": "yes",
    "module_type": "S3-D：步骤拆解式",
    "real_usage_met": 1,
    "core_selling_point_visible": "true",
    "usage_context_fit": "no",
    "continuity_met": "yes",
    "richness_met": 0,
    "fake_or_staged": "false",
    "start_seconds": "8.5",
    "end_seconds": 18,
    "usage_reason": "分步演示用法",
    "evidence_ids": "C2",
})
check("S3 parse 归一 usage flags（type→D, bool/时间/evidence 容错）",
      _ns3["module_type"] == "D"
      and _ns3["exists"] is True
      and _ns3["usage_context_fit"] is False
      and _ns3["start_seconds"] == 8.5
      and _ns3["evidence_ids"] == ["C2"])

_leaky = normalize_hook_flags({
    "exists": True,
    "type": "B",
    "dims": {"camera": True, "copy": True, "sound": True, "rhythm": False},
    "hook_boundary_seconds": 4.5,
    "hook_boundary_reason": "4.5s 后产品开始作为解决方案出现",
    "s2_start_signal": "5.2s 口播产品名",
    "landing_met": True,
    "landing_reason": "0-4.5s 有反差，5.2s 产品解决油光所以闭环",
    "window_evidence": "0-4.5s 反差口播",
    "anchors_proposition": True,
})
check("S1 hook boundary leak 自动压 landing=false",
      _leaky["landing_window_leak"] is True and _leaky["landing_met"] is False)

with tempfile.TemporaryDirectory() as td:
    role_dir = Path(td)
    (role_dir / "transcript.srt").write_text(
        "1\n00:00:00,000 --> 00:00:06,800\n反差句：本来没期待但结果超预期\n\n"
        "2\n00:00:06,800 --> 00:00:14,360\n脸很油，能拯救我们的就是这个产品\n",
        encoding="utf-8",
    )
    _boundary_result = {
        "video_understanding": {
            "creator": {
                "evidence_units": [
                    {
                        "id": "C1",
                        "time_range": "0.0s - 10.4s",
                        "information": "油光痛点与产品需求场景",
                        "voiceover_zh": "本来没期待但结果超预期。脸很油，能拯救我们的就是这个产品。",
                        "functions": ["S1_hook", "S2_intro"],
                    }
                ]
            }
        },
        "stage_analysis": [
            {
                "stage": "S1 Hook",
                "creator_hook": {
                    "exists": True,
                    "type": "B",
                    "dims": {"camera": True, "copy": True, "sound": True, "rhythm": False},
                    "hook_boundary_seconds": 10.4,
                    "hook_boundary_reason": "10.4s 产品正式亮相",
                    "s2_start_signal": "产品亮相",
                    "landing_met": True,
                    "landing_reason": "0-10.4s 对象油皮、张力超预期、承诺能拯救油光齐全",
                    "window_evidence": "0-10.4s 口播",
                    "landing_window_leak": False,
                    "anchors_proposition": True,
                },
                "benchmark_hook": {
                    "exists": True,
                    "type": "C",
                    "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True},
                    "hook_boundary_seconds": 3.0,
                    "hook_boundary_reason": "3s 后产品引出开始",
                    "s2_start_signal": "产品名和解决方案出现",
                    "landing_met": True,
                    "landing_reason": "0-3s 三件套齐全",
                    "window_evidence": "0-3s 钩子窗口",
                    "landing_window_leak": False,
                    "anchors_proposition": True,
                },
            }
        ],
    }
    repair_s1_hook_boundaries(_boundary_result, {"videos": {"creator": {"work_dir": str(role_dir)}}})
    _fixed = _boundary_result["stage_analysis"][0]["creator_hook"]
    check("S1 边界候选后处理：10.4s 收回 6.8s 且 leak 压 landing=false",
          _fixed["hook_boundary_seconds"] == 6.8
          and _fixed["landing_window_leak"] is True
          and _fixed["landing_met"] is False)

_early_boundary_result = {
    "video_understanding": {
        "benchmark": {
            "evidence_units": [
                {
                    "id": "B1",
                    "time_range": "0.0s - 10.4s",
                    "information": "列举经期不适、疲劳、脸色暗沉等痛点。",
                    "voiceover_zh": "很久没来月经，痛经非常严重，脸色暗淡，这个呢，看起来老了。",
                    "visual_fact": "达人展示痛点插图和面部特写。",
                    "functions": ["S1_hook"],
                },
                {
                    "id": "B2",
                    "time_range": "10.4s - 13.8s",
                    "information": "引出产品营养素和认证。",
                    "voiceover_zh": "这个它含有14种营养素，是有KKM批准的。",
                    "visual_fact": "产品宣传图展示认证和成分。",
                    "functions": ["S2_intro", "S5_trust"],
                },
            ]
        }
    },
    "stage_analysis": [
        {
            "stage": "S1 Hook",
            "creator_hook": dict(_boundary_result["stage_analysis"][0]["benchmark_hook"]),
            "benchmark_hook": {
                "exists": True,
                "type": "A",
                "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True},
                "hook_boundary_seconds": 3.18,
                "hook_boundary_reason": "误把痛点枚举中的这个切成 S2",
                "s2_start_signal": "这个",
                "landing_met": True,
                "landing_reason": "0-10.4s 对象经期女性、痛经疲劳张力、后续解决方向明确",
                "window_evidence": "0-10.4s 痛点枚举",
                "landing_window_leak": False,
                "anchors_proposition": True,
            },
        }
    ],
}
repair_s1_hook_boundaries(_early_boundary_result, {"videos": {"benchmark": {}}})
_early_fixed = _early_boundary_result["stage_analysis"][0]["benchmark_hook"]
check("S1 边界候选后处理：粗 cue 不把 B1 中途误切成 S2",
      _early_fixed["hook_boundary_seconds"] == 10.4
      and _early_fixed["landing_met"] is True
      and _early_fixed["landing_window_leak"] is False)

with tempfile.TemporaryDirectory() as td:
    role_dir = Path(td)
    (role_dir / "transcript.srt").write_text(
        "1\n00:00:00,000 --> 00:00:03,700\n买一送一，现在买最划算\n\n"
        "2\n00:00:03,700 --> 00:00:08,000\n再送 6 片，还有优惠\n",
        encoding="utf-8",
    )
    _promo_result = {
        "video_understanding": {
            "creator": {
                "evidence_units": [
                    {
                        "id": "C1",
                        "time_range": "0.0s - 3.7s",
                        "information": "买一送一促销钩子。",
                        "voiceover_zh": "买一送一，现在买最划算。",
                        "visual_fact": "达人手持两盒面膜。",
                        "functions": ["S1_hook"],
                    }
                ]
            }
        },
        "stage_analysis": [
            {
                "stage": "S1 Hook",
                "creator_hook": {
                    "exists": True,
                    "type": "G",
                    "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True},
                    "hook_boundary_seconds": 3.7,
                    "hook_boundary_reason": "3.7s 后继续讲赠品优惠",
                    "s2_start_signal": "继续讲赠品优惠",
                    "landing_met": True,
                    "landing_reason": "0-3.7s 对象、优惠张力、现在买最划算的承诺齐全。",
                    "window_evidence": "0-3.7s 促销 Hook",
                    "landing_window_leak": False,
                    "anchors_proposition": False,
                },
                "benchmark_hook": dict(_boundary_result["stage_analysis"][0]["benchmark_hook"]),
            }
        ],
    }
    repair_s1_hook_boundaries(_promo_result, {"videos": {"creator": {"work_dir": str(role_dir)}}})
    _promo_fixed = _promo_result["stage_analysis"][0]["creator_hook"]
    check("S1 leak 复核：促销 Hook 内的优惠词不算窗口泄漏",
          _promo_fixed["landing_met"] is True and _promo_fixed["landing_window_leak"] is False)

_floor_result = {
    "video_understanding": {
        "benchmark": {
            "evidence_units": [
                {
                    "id": "B1",
                    "time_range": "0.0s - 5.4s",
                    "information": "开场口播牙齿坏了，展示儿童牙膏产品。",
                    "voiceover": "Rosak gigi",
                    "voiceover_zh": "牙齿坏了，糟糕。",
                    "visual_fact": "达人手持牙膏和牙刷向镜头展示。",
                    "audio_fact": "有人声和轻快 BGM。",
                    "functions": ["S2_intro"],
                }
            ]
        }
    },
    "stage_analysis": [
        {
            "stage": "S1 Hook",
            "creator_hook": dict(_boundary_result["stage_analysis"][0]["benchmark_hook"]),
            "benchmark_hook": {
                "exists": False,
                "type": "unknown",
                "dims": {"camera": False, "copy": False, "sound": False, "rhythm": False},
                "hook_boundary_seconds": 0.0,
                "hook_boundary_reason": "模型判无 Hook",
                "s2_start_signal": "",
                "landing_met": False,
                "landing_reason": "无前段独立 Hook",
                "window_evidence": "",
                "landing_window_leak": False,
                "anchors_proposition": False,
            },
        }
    ],
}
repair_s1_hook_boundaries(_floor_result, {"videos": {"benchmark": {}}})
_floor_fixed = _floor_result["stage_analysis"][0]["benchmark_hook"]
check("S1 facts floor：有痛点口播和画面时不允许 hook 全无",
      _floor_fixed["exists"] is True
      and _floor_fixed["dims"]["camera"] is True
      and _floor_fixed["dims"]["copy"] is True
      and _floor_fixed["dims"]["sound"] is True
      and _floor_fixed["dims"]["rhythm"] is False)

_anchor_result = {
    "video_understanding": {
        "creator": {
            "evidence_units": [
                {
                    "id": "C1",
                    "time_range": "0.0s - 5.0s",
                    "information": "开场明确女性人群，并引出多种补充剂整合方案。",
                    "voiceover_zh": "这个是专为女性准备的哦，谁如果已经吃了很多种补充剂。",
                    "visual_fact": "达人手持女性复合维生素瓶。",
                    "functions": ["S1_hook", "S2_intro"],
                }
            ]
        },
        "benchmark": {
            "evidence_units": [
                {
                    "id": "B1",
                    "time_range": "0.0s - 10.4s",
                    "information": "列举痛经、疲劳、面色暗沉等女性生理痛点。",
                    "voiceover_zh": "很久没来月经，痛经很严重，也很容易累，脸色暗沉。",
                    "visual_fact": "画面配合腹痛插图和面部特写。",
                    "functions": ["S1_hook"],
                }
            ]
        },
    },
    "stage_analysis": [
        {
            "stage": "S1 Hook",
            "creator_hook": {
                "exists": True,
                "type": "D",
                "dims": {"camera": True, "copy": True, "sound": True, "rhythm": False},
                "hook_boundary_seconds": 3.7,
                "hook_boundary_reason": "3.7s 后进入产品承接",
                "s2_start_signal": "开始讲补充剂整合方案",
                "landing_met": False,
                "landing_reason": "0-5s 明确女性和多补充剂人群。",
                "window_evidence": "0-5s 女性/补充剂",
                "landing_window_leak": False,
                "anchors_proposition": True,
            },
            "benchmark_hook": {
                "exists": True,
                "type": "A",
                "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True},
                "hook_boundary_seconds": 10.4,
                "hook_boundary_reason": "10.4s 后产品引出",
                "s2_start_signal": "产品营养素出现",
                "landing_met": True,
                "landing_reason": "0-10.4s 命中痛经、疲劳、脸色暗沉。",
                "window_evidence": "0-10.4s 痛经疲劳暗沉",
                "landing_window_leak": False,
                "anchors_proposition": True,
            },
        }
    ],
}
_period_bp = {
    "brand_proposition": {
        "propositions": ["生理期专用配方", "补气血(含铁)", "经期情绪舒缓"],
        "painpoints": ["经期腹痛", "情绪波动", "疲劳乏力", "面色暗沉", "手脚冰冷"],
    },
    "videos": {"creator": {}, "benchmark": {}},
}
repair_s1_hook_boundaries(_anchor_result, _period_bp)
_anchor_creator = _anchor_result["stage_analysis"][0]["creator_hook"]
_anchor_benchmark = _anchor_result["stage_analysis"][0]["benchmark_hook"]
check("S1 anchors：女性/补充剂泛词不算 are_xie 核心命题锚",
      _anchor_creator["anchors_proposition"] is False
      and _anchor_benchmark["anchors_proposition"] is True)

_valid_hook = {
    "exists": True,
    "type": "B",
    "dims": {"camera": True, "copy": True, "sound": False, "rhythm": True},
    "hook_boundary_seconds": 3.0,
    "hook_boundary_reason": "3s 后产品引出开始",
    "s2_start_signal": "产品名和解决方案出现",
    "landing_met": False,
    "landing_reason": "0-3s 有反差但承诺不明确",
    "window_evidence": "0-3s 口播低期待到高结果",
    "landing_window_leak": False,
    "anchors_proposition": True,
}
try:
    validate_s1_hook_flags(
        {"stage_analysis": [{"stage": "S1 Hook", "creator_hook": _valid_hook, "benchmark_hook": dict(_valid_hook)}]},
        {"s1_hook_flags_required": True},
    )
    _s1_gate_ok = True
except SystemExit:
    _s1_gate_ok = False
check("S1 hook flag 门禁：完整字段通过", _s1_gate_ok)

try:
    validate_s1_hook_flags({"stage_analysis": [{"stage": "S1 Hook"}]}, {"s1_hook_flags_required": True})
    _s1_gate_failed = False
except SystemExit as exc:
    _s1_gate_failed = "缺少 creator_hook" in str(exc) and "缺少 benchmark_hook" in str(exc)
check("S1 hook flag 门禁：主链缺字段触发 repair", _s1_gate_failed)

try:
    validate_s1_hook_flags({"stage_analysis": [{"stage": "S1 Hook", "creator_hook": None, "benchmark_hook": None}]}, {})
    _s1_legacy_ok = True
except SystemExit:
    _s1_legacy_ok = False
check("S1 hook flag 门禁：旧结果无标记不误伤", _s1_legacy_ok)

_valid_s2 = _s2_flag()
try:
    validate_s2_contract_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出", "creator_s2": _valid_s2, "benchmark_s2": dict(_valid_s2)}]},
        {"s2_flags_required": True},
    )
    _s2_gate_ok = True
except SystemExit:
    _s2_gate_ok = False
check("S2 contract flag 门禁：完整字段通过", _s2_gate_ok)

try:
    validate_s2_contract_flags({"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}]}, {"s2_flags_required": True})
    _s2_gate_failed = False
except SystemExit as exc:
    _s2_gate_failed = "缺少 creator_s2" in str(exc) and "缺少 benchmark_s2" in str(exc)
check("S2 contract flag 门禁：主链缺字段触发 repair", _s2_gate_failed)

_valid_s3 = _s3_flag()
try:
    validate_s3_usage_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程", "creator_s3": _valid_s3, "benchmark_s3": dict(_valid_s3)},
            ]
        },
        {"s3_flags_required": True},
    )
    _s3_gate_ok = True
except SystemExit:
    _s3_gate_ok = False
check("S3 usage flag 门禁：完整字段通过", _s3_gate_ok)

try:
    validate_s3_usage_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}, {"stage": "S3 使用过程"}]},
        {"s3_flags_required": True},
    )
    _s3_gate_failed = False
except SystemExit as exc:
    _s3_gate_failed = "缺少 creator_s3" in str(exc) and "缺少 benchmark_s3" in str(exc)
check("S3 usage flag 门禁：主链缺字段触发 repair", _s3_gate_failed)

# 5. 死代码已清 + 模块仍可导入
import flayr_core.prompt as prompt_module  # noqa: E402

check("prompt.render_stage_frame_markdown 已删", not hasattr(prompt_module, "render_stage_frame_markdown"))

# 6. speech_mode 四分支：有口播、字幕驱动、音乐驱动、纯视觉驱动
from flayr_core.speech_mode import classify_speech_mode  # noqa: E402
from flayr_core.llm.payload import (  # noqa: E402
    build_llm_comparison_payload,
    build_llm_repair_payload,
    build_stage_review_payload,
    hook_anchor_terms,
)
from flayr_core.report import stage_skipped  # noqa: E402
from flayr import resolve_ocr_policy  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    tmp_dir = Path(tmp)
    spoken = tmp_dir / "spoken"
    spoken.mkdir()
    (spoken / "transcript.txt").write_text("Ini memang senang pakai.", encoding="utf-8")
    (spoken / "transcript.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nIni memang senang pakai.\n",
        encoding="utf-8",
    )
    check("speech_mode spoken", classify_speech_mode(spoken, {"transcription_status": "completed"})["mode"] == "spoken")

    subtitle = tmp_dir / "subtitle"
    subtitle.mkdir()
    (subtitle / "transcript.txt").write_text("music", encoding="utf-8")
    (subtitle / "subtitle_track.json").write_text(
        json.dumps({"segments": [{"text": "RM9.90", "start": 0, "end": 2}]}),
        encoding="utf-8",
    )
    check(
        "speech_mode subtitle_driven",
        classify_speech_mode(subtitle, {"transcription_status": "completed"})["mode"] == "subtitle_driven",
    )

    music = tmp_dir / "music"
    music.mkdir()
    (music / "transcript.txt").write_text("[music]", encoding="utf-8")
    audio = music / "audio.wav"
    audio.write_bytes(b"RIFF")
    check(
        "speech_mode music_driven",
        classify_speech_mode(music, {"audio_path": str(audio), "transcription_status": "completed"})["mode"] == "music_driven",
    )

    visual = tmp_dir / "visual"
    visual.mkdir()
    (visual / "transcript.txt").write_text("Whisper unavailable or audio extraction failed.", encoding="utf-8")
    check(
        "speech_mode visual_driven",
        classify_speech_mode(visual, {"transcription_status": "placeholder"})["mode"] == "visual_driven",
    )

# 7. S1 hook flag 化不再依赖冻结品库：无 brand_proposition 也必须触发 flags
_foundation = {
    "product_profile": {"hook_proposition": "清洁更卫生", "physical_task": "避免手碰脏刷头"},
    "category_profile": {"painpoints": ["异味", "细菌滋生"]},
}
_props, _pains, _source = hook_anchor_terms({}, _foundation)
check("S1 hook anchors 回退 Step-0", _props[:2] == ["清洁更卫生", "避免手碰脏刷头"] and "异味" in _pains)
_payload = build_llm_comparison_payload(
    "test-model",
    "analysis input",
    {},
    {"product_foundation": _foundation, "videos": {}},
)
_content = _payload["messages"][1]["content"]
_user_text = _content[0]["text"] if isinstance(_content, list) else str(_content)
check("S1 hook flags 无冻结品库仍强制输出", "S1 强制" in _user_text and "creator_hook" in _user_text)

from dev_s1_b2_matrix import classify_issues, hook_summary  # noqa: E402

_jitter_side = hook_summary([
    {"hook_boundary_seconds": 16.9, "landing_met": True, "exists": True, "anchors_proposition": True, "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True}},
    {"hook_boundary_seconds": 17.7, "landing_met": True, "exists": True, "anchors_proposition": True, "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True}},
])
_jitter_issues = classify_issues(2, 2, ["medium", "medium"], _jitter_side, _jitter_side)
check("S1 B2 审计：1秒内边界 jitter 不算 unstable",
      _jitter_side["boundary"]["jitter_tolerated"] is True
      and "creator_boundary_unstable" not in _jitter_issues
      and "benchmark_boundary_unstable" not in _jitter_issues)

_repair_payload = build_llm_repair_payload(
    "test-model",
    "{}",
    "S1 hook flag 输出不完整",
    "analysis input",
    {"benchmark": {"evidence_units": [{"id": "B1", "time_range": "0s - 3s"}]}},
)
_repair_user = _repair_payload["messages"][1]["content"]
check("repair payload 携带 locked facts", "已锁定单视频事实清单" in _repair_user and '"B1"' in _repair_user)

_review_payload = build_stage_review_payload(
    "test-model",
    {"videos": {}},
    {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
    {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 5s", "benchmark_time_range": "0s - 5s"}]},
    ["S1"],
)
_review_user = _review_payload["messages"][1]["content"][0]["text"]
check("Phase C S1 回看强制重判 hook flags", "creator_hook" in _review_user and "benchmark_hook" in _review_user)

_skip, _reason = stage_skipped({"stage": "S2 产品引出", "severity": "medium", "gap": "达人未涉及产品身份，标杆有明确引出"})
check("报告折叠：medium/large 差距不因'未涉及'被隐藏", not _skip)


class _OcrArgs:
    no_ocr = False
    with_ocr = True
    ocr_mode = "on"
    llm_dry_run = False
    llm_api_url = "https://api.openai.com/v1/chat/completions"
    llm_api_key_env = "FLAYR_TEST_OPENAI_KEY"
    llm_api_key_keychain_service = None
    llm_api_key_keychain_account = "API_KEY"
    llm_model = "gpt-test"


os.environ["FLAYR_TEST_OPENAI_KEY"] = "not-a-dashscope-key"
_should_ocr, _ocr_key, _ocr_reason = resolve_ocr_policy(_OcrArgs())
check("OCR 非 DashScope 配置快速禁用", not _should_ocr and _ocr_reason == "disabled_non_dashscope_config")
os.environ.pop("FLAYR_TEST_OPENAI_KEY", None)

print()
print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
