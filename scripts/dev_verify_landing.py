#!/usr/bin/env python3
"""Flayr S1-S6 确定性规则与分析契约的离线回归检查。"""

from __future__ import annotations

import json
import inspect
import os
import py_compile
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
    validate_evidence_alignment,
    validate_narrative_evidence_consistency,
    validate_s1_hook_flags,
    validate_s2_contract_flags,
    validate_s3_usage_flags,
    validate_s4_effect_flags,
    validate_s5_trust_flags,
    validate_s6_cta_flags,
    validate_chain_relationships,
    validate_stage_ownership,
)
from flayr_core.postprocess.chain import sanitize_promise_chain_scope, stamp_product_foundation  # noqa: E402
from flayr_core.postprocess.claims_my import reconcile_certification_ownership  # noqa: E402


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


def mk_cert_boundary_result(summary: str, hook_extra: dict | None = None) -> dict:
    """构造认证校验用最小 6 阶段结果。"""
    hook_extra = hook_extra or {}
    stages = []
    for i in range(1, 7):
        stage = {
            "stage": f"S{i} " + ["Hook", "产品引出", "使用过程", "效果呈现", "信任放大", "CTA"][i - 1],
            "benchmark_time_range": "0s - 1s",
            "creator_time_range": "0s - 1s",
            "benchmark_evidence_ids": ["B1"],
            "creator_evidence_ids": ["C1"],
            "benchmark_summary": "普通阶段说明",
            "creator_summary": "普通阶段说明",
        }
        if i == 1:
            stage["benchmark_summary"] = summary
            stage["benchmark_hook"] = {
                "hook_boundary_reason": "10.4秒后开始介绍产品属性。",
                "s2_start_signal": "画面开始清楚展示产品。",
                **hook_extra,
            }
        stages.append(stage)
    return {
        "stage_analysis": stages,
        "video_understanding": {
            "benchmark": {
                "evidence_units": [
                    {"id": "B1", "time_range": "0s - 1s", "information": "只展示经期痛点和腹痛画面。"}
                ]
            },
            "creator": {
                "evidence_units": [
                    {"id": "C1", "time_range": "0s - 1s", "information": "达人开场介绍人群。"}
                ]
            },
        },
    }


_cert_boundary_only = mk_cert_boundary_result("通过痛点画面建立共鸣。")
try:
    validate_evidence_alignment(_cert_boundary_only)
    validate_stage_ownership(_cert_boundary_only)
    _cert_boundary_ok = True
except SystemExit as exc:
    _cert_boundary_ok = False
    _cert_boundary_error = str(exc)
else:
    _cert_boundary_error = ""
check("认证校验：S1 hook 边界字段提到 S2 认证不误杀", _cert_boundary_ok, _cert_boundary_error[:100])

_cert_real_claim = mk_cert_boundary_result("通过 KKM 认证建立开场信任。")
try:
    validate_evidence_alignment(_cert_real_claim)
    _cert_claim_failed = False
except SystemExit as exc:
    _cert_claim_failed = "认证结论没有被所引用事实单元支持" in str(exc)
check("认证校验：S1 正文认证主张仍需证据支撑", _cert_claim_failed)

_cert_s5_flag = mk_cert_boundary_result("通过痛点画面建立共鸣。")
_cert_s5_flag["stage_analysis"][4]["benchmark_evidence_ids"] = ["B2"]
_cert_s5_flag["stage_analysis"][4]["benchmark_s5"] = {
    "trust_reason": "展示 KKM 认证作为第三方信任背书。",
    "evidence_ids": ["B2"],
}
_cert_s5_flag["video_understanding"]["benchmark"]["evidence_units"].append(
    {"id": "B2", "time_range": "0s - 1s", "information": "画面展示 KKM 批准和 Halal 标识。"}
)
try:
    validate_evidence_alignment(_cert_s5_flag)
    validate_stage_ownership(_cert_s5_flag)
    _cert_s5_ok = True
except SystemExit as exc:
    _cert_s5_ok = False
    _cert_s5_error = str(exc)
else:
    _cert_s5_error = ""
check("认证校验：结构化 S5 flag 自带证据可支撑认证主张", _cert_s5_ok, _cert_s5_error[:100])

_cert_s2_claim = mk_cert_boundary_result("通过痛点画面建立共鸣。")
_cert_s2_claim["stage_analysis"][1]["benchmark_s2"] = {
    "handoff_reason": "承接 S1 痛点，给出 KKM 认证解决方案。",
    "evidence_ids": ["B2"],
}
_cert_s2_claim["video_understanding"]["benchmark"]["evidence_units"].append(
    {"id": "B2", "time_range": "0s - 1s", "information": "画面展示 KKM 批准和 Halal 标识。"}
)
try:
    validate_stage_ownership(_cert_s2_claim)
    _cert_s2_rejected = False
except SystemExit as exc:
    _cert_s2_rejected = "只能归入 S5" in str(exc)
check("认证校验：S2 不得承载认证主张", _cert_s2_rejected)

_cert_creator_s2 = mk_cert_boundary_result("通过痛点画面建立共鸣。")
_cert_creator_s2["stage_analysis"][1]["creator_s2"] = {
    "handoff_reason": "承接 S1 痛点，给出 KKM 认证解决方案。",
    "evidence_ids": ["C2"],
}
_cert_creator_s2["video_understanding"]["creator"]["evidence_units"].append(
    {"id": "C2", "time_range": "0s - 1s", "information": "画面展示 KKM 批准和 Halal 标识。"}
)
try:
    validate_stage_ownership(_cert_creator_s2)
    _cert_creator_s2_rejected = False
except SystemExit as exc:
    _cert_creator_s2_rejected = "只能归入 S5" in str(exc)
check("认证校验：达人 S2 不得承载认证主张", _cert_creator_s2_rejected)

reconcile_certification_ownership(_cert_creator_s2)
_creator_s2_after_move = _cert_creator_s2["stage_analysis"][1].get("creator_s2") or {}
_creator_s5_after_move = _cert_creator_s2["stage_analysis"][4]
check(
    "认证归属修复：达人认证迁移到 S5 且清理 S2 flag",
    "KKM" not in str(_creator_s2_after_move)
    and "C_CERT_S5" in _creator_s5_after_move.get("creator_evidence_ids", [])
    and "认证" in str(_creator_s5_after_move),
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
from flayr_core.postprocess.proposition import materialize_cross_stage_inputs, materialize_quality_audits  # noqa: E402
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


def _s5_flag(
    exists=True,
    module="A",
    trust_type="hard",
    trust_basis="authority",
    visible=True,
    credible=True,
    specific=True,
    relevance=True,
    independent=True,
    duplicate=False,
    voice_only=False,
    risky=False,
):
    return {
        "exists": exists,
        "module_type": module,
        "trust_evidence_type": trust_type,
        "trust_basis": trust_basis,
        "trust_source_visible": visible,
        "trust_source_credible": credible,
        "trust_claim_specific": specific,
        "product_relevance_met": relevance,
        "independent_trust_purpose": independent,
        "duplicates_other_stage": duplicate,
        "voice_only": voice_only,
        "risky_or_unsupported": risky,
        "start_seconds": 20.0,
        "end_seconds": 24.0,
        "trust_reason": "认证/检测材料证明本品可信",
        "evidence_ids": ["C5"],
    }


def _s6_flag(
    exists=True,
    module="B",
    direct=True,
    path=True,
    offer=True,
    urgency=True,
    recall=True,
    fit=True,
    ending=True,
    s4_depends=True,
    risk=False,
):
    return {
        "exists": exists,
        "module_type": module,
        "direct_order_met": direct,
        "action_path_clear": path,
        "offer_or_incentive_clear": offer,
        "urgency_met": urgency,
        "product_value_recalled": recall,
        "module_fit_met": fit,
        "ending_position_met": ending,
        "depends_on_valid_s4": s4_depends,
        "compliance_risk": risk,
        "start_seconds": 25.0,
        "end_seconds": 30.0,
        "cta_reason": "明确下单路径和限时利益点",
        "evidence_ids": ["C6"],
    }


_s5_hard_vs_voice = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(voice_only=True),
        "benchmark_s5": _s5_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S5": 1.6},
    [],
    None,
    {"benchmark": _Endorsement(False, True, True), "creator": _Endorsement(True, False, True)},
)
check("S5 高决策品硬信任画面佐证强于口播孤证→large",
      _s5_hard_vs_voice.get("severity") == "large" and _s5_hard_vs_voice.get("E") == 1.5)

_s5_low_price_hard_vs_voice = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(voice_only=True),
        "benchmark_s5": _s5_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S5": 0.6},
    [],
    None,
    {"benchmark": _Endorsement(False, True, True), "creator": _Endorsement(True, False, True)},
)
check("S5 低客单价硬信任差距不自动拉large",
      _s5_low_price_hard_vs_voice.get("severity") != "large")

_s5_soft_not_killed = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(trust_type="soft", trust_basis="independent_user", visible=True, credible=True, specific=True),
        "benchmark_s5": _s5_flag(trust_type="soft", trust_basis="independent_user", visible=True, credible=True, specific=True),
        "creator_summary": "用户好评",
        "benchmark_summary": "用户好评",
    },
    {"S5": 1.0},
    [],
    None,
    {"benchmark": _Endorsement(False, False, True), "creator": _Endorsement(False, False, True)},
)
check("S5 软信任 flag 存在→不被双方无硬背书闸误杀",
      "均无硬背书" not in _s5_soft_not_killed.get("reason", "")
      and _s5_soft_not_killed.get("severity") == "small")

_s5_opening_comment_not_trust = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(trust_type="soft", trust_basis="independent_user", independent=False, duplicate=True),
        "benchmark_s5": _s5_flag(trust_type="soft", trust_basis="independent_user", independent=True, duplicate=False),
        "creator_summary": "开头回答粉丝评论作为 Hook",
        "benchmark_summary": "独立用户证言",
    },
    {"S5": 1.0},
    [],
    None,
    {"benchmark": _Endorsement(False, False, True), "creator": _Endorsement(False, False, True)},
)
check("S5-C 开头评论/粉丝问答归 S1，不重复算 S5",
      _s5_opening_comment_not_trust.get("severity") == "small"
      and _s5_opening_comment_not_trust.get("E") == 1)

_s5_scene_duplicate_not_trust = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(module="D", trust_type="soft", independent=False, duplicate=True),
        "benchmark_s5": _s5_flag(module="D", trust_type="soft", independent=True, duplicate=False),
        "creator_summary": "S3/S4 的多场景使用被误写为场景广度",
        "benchmark_summary": "独立适用人群/场景标签建立信任",
    },
    {"S5": 1.0},
    [],
    None,
    {"benchmark": _Endorsement(False, False, True), "creator": _Endorsement(False, False, True)},
)
check("S5-D 场景广度不得重复 S3/S4 多场景",
      _s5_scene_duplicate_not_trust.get("severity") == "small"
      and _s5_scene_duplicate_not_trust.get("E") == 1)

_s5_spec_not_trust = _derive_one(
    "S5",
    {
        "creator_s5": _s5_flag(
            exists=False, trust_type="none", trust_basis="offer_or_spec", independent=False,
            visible=True, credible=False, specific=True,
        ),
        "benchmark_s5": _s5_flag(
            exists=False, trust_type="none", trust_basis="offer_or_spec", independent=False,
            visible=True, credible=False, specific=True,
        ),
        "creator_summary": "展示刷头数量和可用时长",
        "benchmark_summary": "展示套装数量和可用时长",
    },
    {"S5": 1.0},
    [],
    None,
    {"benchmark": _Endorsement(False, False, True), "creator": _Endorsement(False, False, True)},
)
check("S5 产品数量/时长不是独立信任背书",
      _s5_spec_not_trust.get("severity") == "small" and "均未涉及" in _s5_spec_not_trust.get("reason", ""))

_s6_missing = _derive_one(
    "S6",
    {
        "creator_s6": _s6_flag(exists=False, direct=False, path=False, offer=False, urgency=False, recall=False, fit=False),
        "benchmark_s6": _s6_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S6": 1.8},
    [],
)
check("S6 达人无 CTA 标杆有明确 CTA→large 红线",
      _s6_missing.get("severity") == "large" and _s6_missing.get("E") == 2)

_s6_opening_price_not_cta = _derive_one(
    "S6",
    {
        "creator_s6": _s6_flag(ending=False),
        "benchmark_s6": _s6_flag(),
        "creator_summary": "开头用低价做 Hook",
        "benchmark_summary": "结尾明确促单",
    },
    {"S6": 1.8},
    [],
)
check("S6-A 价格出现在开头归 S1，不算结尾 CTA",
      _s6_opening_price_not_cta.get("severity") == "large"
      and _s6_opening_price_not_cta.get("E") == 2)

_s6_invalid_effect_summary = _derive_one(
    "S6",
    {
        "creator_s6": _s6_flag(module="D", s4_depends=False),
        "benchmark_s6": _s6_flag(module="D", s4_depends=True),
        "creator_summary": "效果总结但 S4 未成立",
        "benchmark_summary": "复用有效 S4 效果催单",
    },
    {"S6": 1.8},
    [],
)
check("S6-D 效果总结必须依赖有效 S4 输出",
      _s6_invalid_effect_summary.get("severity") in {"medium", "large"}
      and _s6_invalid_effect_summary.get("E") >= 1)

_s6_creator_better = _derive_one(
    "S6",
    {
        "creator_s6": _s6_flag(),
        "benchmark_s6": _s6_flag(direct=True, path=True, offer=False, urgency=False, recall=False, fit=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S6": 1.8},
    [],
)
check("S6 达人 CTA 更强→零差距红线保持 small",
      _s6_creator_better.get("severity") == "small" and _s6_creator_better.get("E") == 0)

_matrix_result = {
    "category_profile": {"decision_threshold": "impulse", "drive_type": "functional", "painpoints": ["油光"]},
    "product_profile": {
        "hook_proposition": "油光变哑光",
        "physical_task": "补妆不花",
        "core_selling_points": ["强力吸油不拔干"],
        "usage_context": "出门补妆",
        "core_visual_proposition": "油光脸变哑光脸",
        "visual_proof_points": [
            {
                "priority": "primary",
                "proof_target": "油光变哑光",
                "visual_standard": "强光下反光明显下降",
                "visual_diff_dimensions": ["亮度反光"],
                "related_selling_points": ["强力吸油不拔干"],
            }
        ],
        "visual_diff_dimensions": ["亮度反光"],
        "trust_multipliers": ["真人实测"],
    },
    "stage_analysis": [
        {"stage": "S1 Hook", "creator_hook": {"type": "B", "anchors_proposition": True}, "benchmark_hook": {"type": "A", "anchors_proposition": True}},
        {"stage": "S2 产品引出", "creator_s2": {"exists": True, "module_type": "A", "s1_s2_compatible": True, "handoff_met": True, "product_identity_clear": True, "product_role_clear": True}, "benchmark_s2": {"exists": True, "module_type": "A", "s1_s2_compatible": True, "handoff_met": True, "product_identity_clear": True, "product_role_clear": True}},
        {"stage": "S3 使用过程", "creator_s3": {"exists": True, "core_selling_point_visible": True, "demonstrated_selling_points": ["强力吸油不拔干"]}, "benchmark_s3": {"exists": True, "core_selling_point_visible": True, "demonstrated_selling_points": ["强力吸油不拔干"]}},
        {"stage": "S4 效果呈现", "creator_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True}, "benchmark_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True}},
        {"stage": "S5 信任放大", "creator_s5": {"exists": False}, "benchmark_s5": {"exists": False}},
        {"stage": "S6 CTA", "creator_s6": {"exists": True, "module_type": "D", "depends_on_valid_s4": False}, "benchmark_s6": {"exists": True, "module_type": "D", "depends_on_valid_s4": False}},
    ],
}
materialize_cross_stage_inputs(_matrix_result, {"brand_proposition": {"propositions": ["柔焦隐形毛孔"], "painpoints": ["油光"]}})
materialize_quality_audits(_matrix_result, {})
check(
    "命题矩阵：S1 用冻结命题，S3/S4/S5/S6 用 product_profile",
    _matrix_result["product_proposition_matrix"]["S1"]["hook_propositions"] == ["柔焦隐形毛孔"]
    and _matrix_result["product_proposition_matrix"]["S3"]["core_selling_points"] == ["强力吸油不拔干"]
    and _matrix_result["product_proposition_matrix"]["S4"]["core_visual_proposition"] == "油光脸变哑光脸"
    and _matrix_result["product_proposition_matrix"]["S4"]["visual_proof_points"][0]["priority"] == "primary"
    and bool(_matrix_result["product_proposition_matrix"]["S6"]["cta_value_hooks"]),
)
check(
    "S1→S2 兼容矩阵代码化：B→A 覆盖模型 true 为 false",
    _matrix_result["stage_analysis"][1]["creator_s2"].get("computed_s1_s2_compatible") is False
    and _matrix_result["stage_analysis"][1].get("creator_absolute_status") == "incompatible",
)
check(
    "S4→S6-D 跨阶段依赖代码化：有效 S4 使 S6-D 可复用",
    _matrix_result["stage_analysis"][5]["creator_s6"].get("computed_depends_on_valid_s4") is True
    and _matrix_result["cross_stage_state"]["roles"]["creator"]["s4_output_available"] is True,
)
check(
    "卖点链审计不回填 S2：S2/S3/S4 闭环状态单独暴露",
    _matrix_result["cross_stage_state"]["roles"]["creator"]["selling_point_chain"]["status"] == "closed",
)

# 4c. S1 Hook flag 化（切片 A）：四维推执行分 + hook_exists 红线 + 命题锚 + 残差亮点门
from flayr_core.llm.parse import (  # noqa: E402
    normalize_hook_flags,
    normalize_product_profile,
    normalize_promise_chain,
    normalize_s2_flags,
    normalize_s3_flags,
    normalize_s3_s4_relationship,
    normalize_s4_flags,
    normalize_s5_flags,
    normalize_s6_flags,
)


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

# S3 使用过程 flag：真实使用 + 核心卖点可见是主轴，场景组织/表现层只做加分
def _s3_flag(
    exists=True,
    module="A",
    usage=True,
    result_only=False,
    mouth_static=False,
    real=True,
    core=True,
    framing=True,
    action_proof=True,
    context=True,
    continuity=True,
    richness=False,
    fake=False,
    scene="single_scene",
    single_continuity=True,
    single_variation=False,
    multi_logic=False,
    multi_transition=False,
    multi_role=False,
    role_design=False,
    role_interaction=False,
    overlays=None,
):
    return {
        "exists": exists,
        "module_type": module,
        "usage_process_visible": usage,
        "result_only_without_process": result_only,
        "mouth_only_or_static": mouth_static,
        "real_usage_met": real,
        "core_selling_point_visible": core,
        "process_framing_met": framing,
        "action_proof_met": action_proof,
        "demonstrated_selling_points": ["核心卖点"],
        "missing_selling_points": [] if core else ["核心卖点"],
        "scene_mode": scene,
        "usage_context_fit": context,
        "continuity_met": continuity,
        "richness_met": richness,
        "single_scene_continuity_met": single_continuity,
        "single_scene_variation_met": single_variation,
        "multi_scene_logic_met": multi_logic,
        "multi_scene_transition_met": multi_transition,
        "multi_scene_role_adaptation_met": multi_role,
        "role_design_met": role_design,
        "role_interaction_met": role_interaction,
        "presentation_overlays": overlays or ["none"],
        "fake_or_staged": fake,
        "start_seconds": 8.0,
        "end_seconds": 18.0,
        "usage_reason": "真实使用动作中演示核心卖点",
        "evidence_ids": ["C2"],
    }


_s3_good = _s3_flag(richness=True, single_variation=True)
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

_s3_core_incomplete = _derive_one(
    "S3",
    {
        "creator_s3": {**_s3_flag(richness=False), "missing_selling_points": ["核心卖点"]},
        "benchmark_s3": _s3_good,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.0},
    [],
)
check("S3 核心卖点有缺口→多余表现层不能抵消标杆完整证明",
      _s3_core_incomplete.get("severity") == "medium"
      and _s3_core_incomplete.get("E") == 1
      and "薄演示下限" in _s3_core_incomplete.get("reason", ""))

_s3_context_thin = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(richness=False, context=False),
        "benchmark_s3": _s3_good,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.0},
    [],
)
check("S3 基础演示成立但场景/丰富度弱于标杆→至少 medium",
      _s3_context_thin.get("severity") == "medium"
      and _s3_context_thin.get("E") == 1.5)

_s3_bad_framing = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(framing=False, richness=True, single_variation=True),
        "benchmark_s3": _s3_good,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.0},
    [],
)
check("S3 使用过程拍不全/没对准→执行分封顶并触发 medium 下限",
      _s3_bad_framing.get("severity") == "medium"
      and _s3_bad_framing.get("E") == 1.5
      and _s3_bad_framing.get("s3_process_framing") == {"creator": False, "benchmark": True})

_s3_action_without_proof = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(action_proof=False, richness=True, single_variation=True),
        "benchmark_s3": _s3_good,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.0},
    [],
)
check("S3 有动作但未形成可复核卖点证明→执行分封顶并触发 medium 下限",
      _s3_action_without_proof.get("severity") == "medium"
      and _s3_action_without_proof.get("E") == 1.5
      and _s3_action_without_proof.get("s3_action_proof") == {"creator": False, "benchmark": True})

_s3_both_thin = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(richness=False),
        "benchmark_s3": _s3_flag(richness=False),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.0},
    [],
)
check("S3 双方都只是基础演示→不触发薄演示下限",
      _s3_both_thin.get("severity") == "small" and _s3_both_thin.get("E") == 0)

_s3_result_only = _derive_one(
    "S3",
    {"creator_s3": _s3_flag(usage=False, result_only=True), "benchmark_s3": _s3_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S3": 1.6},
    [],
)
check("S3 只有结果没有过程→最高 0.5",
      _s3_result_only.get("severity") in {"medium", "large"} and _s3_result_only.get("E") == 1.5)

_s3_static = _derive_one(
    "S3",
    {"creator_s3": _s3_flag(usage=False, mouth_static=True), "benchmark_s3": _s3_good, "creator_summary": "x", "benchmark_summary": "y"},
    {"S3": 1.6},
    [],
)
check("S3 只口播/静态展示→执行分按 0 处理",
      _s3_static.get("severity") == "large" and _s3_static.get("E") == 2)

_s3_multi_strong = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(scene="multi_scene", single_continuity=False, multi_logic=True, multi_transition=True, multi_role=True),
        "benchmark_s3": _s3_flag(scene="multi_scene", single_continuity=False, multi_logic=True, multi_transition=True, multi_role=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.6},
    [],
)
check("S3 多场景组织完整→可与标杆持平",
      _s3_multi_strong.get("severity") == "small" and _s3_multi_strong.get("E") == 0)

_s3_multi_scattered = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(scene="multi_scene", single_continuity=False, multi_logic=False, multi_transition=False, multi_role=False, richness=True),
        "benchmark_s3": _s3_flag(scene="single_scene", single_variation=True, richness=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.6},
    [],
)
check("S3 多场景散乱→不因场景多自动加分",
      _s3_multi_scattered.get("severity") in {"medium", "large"} and _s3_multi_scattered.get("E") == 1)

_s3_people = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(scene="multi_person", single_continuity=False, role_design=True, role_interaction=True),
        "benchmark_s3": _s3_flag(scene="multi_person", single_continuity=False, role_design=True, role_interaction=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.6},
    [],
)
check("S3 多人使用角色清楚且互动服务卖点→可成立",
      _s3_people.get("severity") == "small" and _s3_people.get("E") == 0)

_s3_single_full = _derive_one(
    "S3",
    {
        "creator_s3": _s3_flag(scene="single_scene", single_continuity=True, richness=False),
        "benchmark_s3": _s3_flag(scene="single_scene", single_continuity=True, richness=False),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.2},
    [],
)
check("S3 单场景完整证明核心卖点→不因缺少多场景/变化被降级",
      _s3_single_full.get("severity") == "small" and _s3_single_full.get("E") == 0)

_s3_creator_multi_missing = _s3_flag(
    scene="multi_scene",
    single_continuity=False,
    multi_logic=True,
    multi_transition=True,
    multi_role=True,
    richness=True,
)
_s3_creator_multi_missing["missing_selling_points"] = ["高载液量"]
_s3_benchmark_single_complete = _s3_flag(scene="single_scene", single_continuity=True, single_variation=True, richness=True)
_s3_benchmark_single_complete["missing_selling_points"] = []
_s3_colorkey_like = _derive_one(
    "S3",
    {
        "creator_s3": _s3_creator_multi_missing,
        "benchmark_s3": _s3_benchmark_single_complete,
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S3": 1.2},
    [],
)
check("S3 多场景但漏核心卖点→不因丰富度抵消标杆完整证明",
      _s3_colorkey_like.get("severity") == "medium" and _s3_colorkey_like.get("E") == 1)


def _s4_flag(
    effect=True,
    attribution=True,
    result_only=False,
    linked=True,
    tamper=False,
    effect_type="before_after",
    salience="strong",
    matched=True,
    control=True,
    focus=True,
    visual_diff=True,
    module_constraints=True,
    maximized=True,
    close_inspection=False,
):
    return {
        "effect_type": effect_type,
        "effect_visible": effect,
        "effect_salience": salience,
        "effect_proposition_matched": matched,
        "comparison_control_met": control,
        "closeup_or_focus_met": focus,
        "visual_difference_observed": visual_diff,
        "module_constraints_met": module_constraints,
        "effect_maximized": maximized,
        "requires_close_inspection": close_inspection,
        "effect_attribution_supported": attribution,
        "result_only_without_process": result_only,
        "process_linked_effect": linked,
        "tamper_or_cut_risk": tamper,
        "effect_reason": "效果可见且因果清楚",
        "evidence_ids": ["C3"],
    }


_s4_result_weak = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(attribution=False, result_only=True, linked=False),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 只有结果且无因果桥→效果展示封顶弱",
      _s4_result_weak.get("severity") in {"medium", "large"} and _s4_result_weak.get("E") == 1.5)

_s4_result_bound = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(result_only=True, linked=False),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 只有结果但产品结果强绑定→最多中等可信",
      _s4_result_bound.get("severity") in {"small", "medium"} and _s4_result_bound.get("E") == 1)

_s4_strong_same = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 强效果：显著+命题命中+对比控制+聚焦+最大化→满执行",
      _s4_strong_same.get("severity") == "small" and _s4_strong_same.get("E") == 0)

_s4_process_visual = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(effect_type="process_visualization", salience="clear", control=False, maximized=False),
        "benchmark_s4": _s4_flag(effect_type="process_visualization", salience="strong", control=False, focus=True, maximized=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.0},
    [],
)
check("S4 过程可视化不要求 comparison_control 才能满执行",
      _s4_process_visual.get("severity") == "medium"
      and _s4_process_visual.get("E") == 1)

_s4_subtle = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(salience="subtle", close_inspection=True),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 变化需要仔细看→封顶弱",
      _s4_subtle.get("severity") in {"medium", "large"} and _s4_subtle.get("E") == 1.5)

_s4_not_max = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(salience="clear", control=False, focus=False, maximized=False),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 结构存在但未最大化→不能拿满分",
      _s4_not_max.get("severity") in {"small", "medium"} and _s4_not_max.get("E") == 1)

_s4_no_visual_diff = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(salience="strong", visual_diff=False, module_constraints=True),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 看不出指定视觉差异→不能拿满分",
      _s4_no_visual_diff.get("severity") in {"small", "medium"} and _s4_no_visual_diff.get("E") == 1)

_s4_module_broken = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(salience="strong", visual_diff=True, module_constraints=False),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 模块硬约束不成立→不能拿满分",
      _s4_module_broken.get("severity") in {"small", "medium"} and _s4_module_broken.get("E") == 1)

_s4_thin_effect = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(salience="clear", control=False, focus=False, maximized=False),
        "benchmark_s4": _s4_flag(salience="strong", control=True, focus=True, maximized=True),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.0},
    [],
)
check("S4 效果可见但未最大化，标杆更显著→至少 medium",
      _s4_thin_effect.get("severity") == "medium"
      and _s4_thin_effect.get("E") == 1
      and "薄效果下限" in _s4_thin_effect.get("reason", ""))

_s4_aesthetic = _derive_one(
    "S4",
    {
        "creator_s4": _s4_flag(effect_type="aesthetic_display", linked=False, control=False, focus=True, maximized=True),
        "benchmark_s4": _s4_flag(),
        "creator_summary": "x",
        "benchmark_summary": "y",
    },
    {"S4": 1.4},
    [],
)
check("S4 颜值陈列只算 aesthetic_display，不伪装强效果",
      _s4_aesthetic.get("severity") in {"small", "medium"} and _s4_aesthetic.get("E") == 1)

from types import SimpleNamespace  # noqa: E402
import flayr_core.llm.s4_visual_verifier as s4_visual_verifier_module  # noqa: E402
from flayr_core.llm.s4_visual_verifier import apply_s4_visual_verifier_result, maybe_apply_s4_visual_verifier  # noqa: E402

_s4_verifier_result = {
    "stage_analysis": [
        {"stage": "S1 Hook"},
        {"stage": "S2 产品引出"},
        {"stage": "S3 使用过程"},
        {
            "stage": "S4 效果呈现",
            "creator_s4": _s4_flag(salience="strong", visual_diff=True, module_constraints=True),
            "benchmark_s4": _s4_flag(salience="strong", visual_diff=True, module_constraints=True),
            "creator_summary": "x",
            "benchmark_summary": "y",
            "severity": "small",
        },
    ],
    "improvements": [],
}
_s4_verifier_applied = apply_s4_visual_verifier_result(
    _s4_verifier_result,
    {
        "creator": {
            "visual_difference_observed": False,
            "module_constraints_met": True,
            "effect_salience": "subtle",
            "requires_close_inspection": True,
            "effect_maximized": False,
            "reason": "前后变化需要仔细找。",
        },
        "benchmark": {
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_salience": "strong",
            "requires_close_inspection": False,
            "effect_maximized": True,
            "reason": "前后差异一眼可见。",
        },
    },
    {},
)
_s4_verifier_stage = _s4_verifier_result["stage_analysis"][3]
check("S4 独立视觉复核覆盖字段并重推 severity",
      _s4_verifier_applied
      and _s4_verifier_stage["creator_s4"]["effect_visible"] is False
      and _s4_verifier_stage["creator_s4"]["effect_proposition_matched"] is False
      and _s4_verifier_stage["creator_s4"]["visual_difference_observed"] is False
      and _s4_verifier_stage["creator_s4"]["effect_salience"] == "subtle"
      and _s4_verifier_stage["creator_s4"]["effect_reason"] == "前后变化需要仔细找。"
      and _s4_verifier_stage["severity"] in {"medium", "large"}
      and _s4_verifier_stage.get("severity_derivation", {}).get("status") == "derived")

_orig_build_s4_payload = s4_visual_verifier_module.build_s4_visual_verifier_payload
_orig_call_s4_api = s4_visual_verifier_module.call_llm_api
try:
    s4_visual_verifier_module.build_s4_visual_verifier_payload = lambda *_args, **_kwargs: {"messages": []}

    def _raise_system_exit(*_args, **_kwargs):
        raise SystemExit("boom")

    s4_visual_verifier_module.call_llm_api = _raise_system_exit
    _s4_verifier_fail_result = {
        "product_profile": {"proof_contract_source": "operator"},
        "stage_analysis": [
            {"stage": "S1 Hook"},
            {"stage": "S2 产品引出"},
            {"stage": "S3 使用过程"},
            {"stage": "S4 效果呈现", "creator_s4": _s4_flag(), "benchmark_s4": _s4_flag()},
        ]
    }
    maybe_apply_s4_visual_verifier(
        args=SimpleNamespace(llm_dry_run=False, llm_model="x", llm_api_url="http://invalid"),
        api_key="x",
        result=_s4_verifier_fail_result,
        analysis={},
        run_dir=Path(tempfile.gettempdir()),
    )
finally:
    s4_visual_verifier_module.build_s4_visual_verifier_payload = _orig_build_s4_payload
    s4_visual_verifier_module.call_llm_api = _orig_call_s4_api
check("S4 独立视觉复核失败不拖垮主链",
      _s4_verifier_fail_result.get("s4_visual_verifier", {}).get("applied") is False
      and "boom" in _s4_verifier_fail_result.get("s4_visual_verifier", {}).get("reason", ""))

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
    "usage_process_visible": 1,
    "result_only_without_process": "false",
    "mouth_only_or_static": "no",
    "real_usage_met": 1,
    "core_selling_point_visible": "true",
    "process_framing_met": "no",
    "action_proof_met": "false",
    "demonstrated_selling_points": ["控油"],
    "missing_selling_points": "遮毛孔",
    "scene_mode": "single-scene",
    "usage_context_fit": "no",
    "continuity_met": "yes",
    "richness_met": 0,
    "single_scene_continuity_met": "yes",
    "single_scene_variation_met": "no",
    "multi_scene_logic_met": "no",
    "multi_scene_transition_met": "no",
    "multi_scene_role_adaptation_met": "no",
    "role_design_met": "no",
    "role_interaction_met": "no",
    "presentation_overlays": "step_breakdown, closeup",
    "fake_or_staged": "false",
    "start_seconds": "8.5",
    "end_seconds": 18,
    "usage_reason": "分步演示用法",
    "evidence_ids": "C2",
})
check("S3 parse 归一 usage flags（type→D, bool/时间/evidence 容错）",
      _ns3["module_type"] == "D"
      and _ns3["exists"] is True
      and _ns3["process_framing_met"] is False
      and _ns3["action_proof_met"] is False
      and _ns3["usage_context_fit"] is False
      and _ns3["scene_mode"] == "single_scene"
      and _ns3["presentation_overlays"] == ["step_breakdown", "closeup"]
      and _ns3["start_seconds"] == 8.5
      and _ns3["evidence_ids"] == ["C2"])

_ns4 = normalize_s4_flags({
    "effect_type": "process-visualization",
    "effect_visible": "yes",
    "effect_salience": "clear",
    "effect_proposition_matched": "true",
    "comparison_control_met": "yes",
    "closeup_or_focus_met": 1,
    "visual_difference_observed": "true",
    "module_constraints_met": "no",
    "effect_maximized": "no",
    "requires_close_inspection": "false",
    "effect_attribution_supported": 0,
    "result_only_without_process": "true",
    "process_linked_effect": "false",
    "tamper_or_cut_risk": "no",
    "effect_reason": "只有结果没有过程",
    "evidence_ids": "C3",
})
check("S4 parse 归一 effect flags（bool/evidence 容错）",
      _ns4["effect_type"] == "process_visualization"
      and _ns4["effect_visible"] is True
      and _ns4["effect_salience"] == "clear"
      and _ns4["effect_proposition_matched"] is True
      and _ns4["visual_difference_observed"] is True
      and _ns4["module_constraints_met"] is False
      and _ns4["effect_maximized"] is False
      and _ns4["effect_attribution_supported"] is False
      and _ns4["result_only_without_process"] is True
      and _ns4["evidence_ids"] == ["C3"])

_ns5 = normalize_s5_flags({
    "module_type": "S5-B",
    "exists": "yes",
    "trust_evidence_type": "mixed",
    "trust_basis": "authority",
    "trust_source_visible": "true",
    "trust_source_credible": "true",
    "trust_claim_specific": "false",
    "product_relevance_met": "true",
    "independent_trust_purpose": "true",
    "duplicates_other_stage": "false",
    "voice_only": "false",
    "risky_or_unsupported": "false",
    "start_seconds": "20.5",
    "end_seconds": "24",
    "trust_reason": "画面出现认证和用户评价",
    "evidence_ids": ["C5"],
})
check("S5 parse 归一 trust flags",
      _ns5["module_type"] == "B"
      and _ns5["trust_evidence_type"] == "mixed"
      and _ns5["trust_basis"] == "authority"
      and _ns5["trust_source_visible"] is True
      and _ns5["trust_claim_specific"] is False
      and _ns5["independent_trust_purpose"] is True
      and _ns5["duplicates_other_stage"] is False
      and _ns5["evidence_ids"] == ["C5"])

_ns6 = normalize_s6_flags({
    "module_type": "S6-B",
    "exists": "yes",
    "direct_order_met": "true",
    "action_path_clear": "true",
    "offer_or_incentive_clear": "false",
    "urgency_met": "true",
    "product_value_recalled": "true",
    "module_fit_met": "true",
    "ending_position_met": "true",
    "depends_on_valid_s4": "false",
    "compliance_risk": "false",
    "start_seconds": "25",
    "end_seconds": "30",
    "cta_reason": "明确让用户点击购物车",
    "evidence_ids": ["C6"],
})
check("S6 parse 归一 CTA flags",
      _ns6["module_type"] == "B"
      and _ns6["direct_order_met"] is True
      and _ns6["offer_or_incentive_clear"] is False
      and _ns6["ending_position_met"] is True
      and _ns6["depends_on_valid_s4"] is False
      and _ns6["evidence_ids"] == ["C6"])

_npp = normalize_product_profile({
    "visualizable": "yes",
    "proof_mode": "sensory-proxy",
    "effect_requires_process": "yes",
    "core_selling_points": ["香味持久"],
    "visual_proof_points": [
        {
            "priority": "primary",
            "proof_target": "香味体验",
            "visual_standard": "儿童闻香后主动靠近",
            "visual_diff_dimensions": ["表情反应"],
        },
        {
            "priority": "secondary",
            "proof_target": "泡沫形态",
            "visual_standard": "按压后泡沫稳定成型",
        },
    ],
})
check("product_profile 归一 proof_mode/effect_requires_process",
      _npp["proof_mode"] == "sensory_proxy" and _npp["effect_requires_process"] == "true")
check("product_profile 归一 visual_proof_points",
      _npp["visual_proof_points"][0]["priority"] == "primary"
      and _npp["visual_proof_points"][1]["proof_target"] == "泡沫形态")
_contract_profile = normalize_product_profile({
    "proof_contract": {
        "mode": "instant_visual",
        "consumer_outcome": "油光明显减少",
        "signal_type": "state_change",
        "observable_signal": "同一脸颊的油光反光强度",
        "before_state": "油光明显",
        "after_state": "反光减弱",
        "proof_condition": "同一脸颊、同一光线、同一距离的前后对比",
    },
    "visual_proof_points": [
        {"priority": "primary", "proof_target": "柔焦隐形", "visual_standard": "同一光源下"},
        {"priority": "secondary", "proof_target": "防蹭", "visual_standard": "纸巾按压无转移"},
    ],
})
check("proof_contract 生成可观察 S4 primary",
      _contract_profile["proof_contract"]["valid"] is True
      and _contract_profile["visual_proof_points"][0]["proof_target"] == "油光明显减少"
      and _contract_profile["visual_proof_points"][0]["visual_standard"] == "油光明显 vs 反光减弱"
      and _contract_profile["visual_proof_points"][0]["visual_diff_dimensions"] == ["同一脸颊的油光反光强度"])
_proof_plan_profile = normalize_product_profile({
    "short_video_proof_plan": {
        "candidates": [
            {"id": "P1", "selling_point": "控油定妆", "visual_space": "high", "functional_centrality": "high", "comprehension_cost": "low", "delivery_stage": "S4", "proof_mode": "instant_visual", "reason": "可直接展示油光到哑光"},
            {"id": "P2", "selling_point": "防蹭稳定", "visual_space": "high", "functional_centrality": "medium", "comprehension_cost": "low", "delivery_stage": "S4", "proof_mode": "process_result", "reason": "可做按压测试"},
            {"id": "P3", "selling_point": "粉质细腻", "visual_space": "medium", "functional_centrality": "medium", "comprehension_cost": "medium", "delivery_stage": "S3", "proof_mode": "", "reason": "适合上脸过程说明"},
        ],
        "s4_anchor_candidate_id": "P1",
        "selection_source": "model_category_default",
        "anchor_confidence": "high",
    },
    "proof_contract": {
        "anchor_candidate_id": "P1",
        "mode": "instant_visual",
        "consumer_outcome": "妆面由油亮变均匀哑光",
        "signal_type": "state_change",
        "observable_signal": "同一脸颊的反光强度变化",
        "before_state": "油亮反光明显",
        "after_state": "反光减弱为均匀哑光",
        "proof_condition": "同一脸颊同光线同距离前后对比",
    },
})
check("短视频证明计划：保留多卖点但只选一个 S4 anchor",
      _proof_plan_profile["short_video_proof_plan"]["valid"] is True
      and _proof_plan_profile["proof_contract"]["valid"] is True
      and len(_proof_plan_profile["short_video_proof_plan"]["candidates"]) == 3
      and _proof_plan_profile["short_video_proof_plan"]["s4_anchor_candidate_id"] == "P1"
      and _proof_plan_profile["visual_proof_points"][0]["proof_target"] == "妆面由油亮变均匀哑光")
_bad_rank_plan = normalize_product_profile({
    "short_video_proof_plan": {
        "candidates": [
            {"id": "P1", "selling_point": "控油定妆", "visual_space": "high", "functional_centrality": "high", "comprehension_cost": "low", "delivery_stage": "S4", "proof_mode": "instant_visual"},
            {"id": "P2", "selling_point": "防蹭稳定", "visual_space": "high", "functional_centrality": "medium", "comprehension_cost": "low", "delivery_stage": "S4", "proof_mode": "process_result"},
        ],
        "s4_anchor_candidate_id": "P2",
        "selection_source": "model_category_default",
        "anchor_confidence": "high",
    },
    "proof_contract": {
        "anchor_candidate_id": "P2", "mode": "process_result", "consumer_outcome": "转移减少", "signal_type": "process_event",
        "observable_signal": "纸巾上的粉底残留量", "before_state": "按压后残留明显", "after_state": "按压后残留减少", "proof_condition": "同一纸巾同等压力按压",
    },
})
check("短视频证明计划：较弱 S4 候选不能越级成为 anchor",
      _bad_rank_plan["short_video_proof_plan"]["valid"] is False
      and _bad_rank_plan["proof_contract"]["valid"] is False
      and "最高候选" in _bad_rank_plan["proof_contract"]["validation_reason"])
_cup_plan = normalize_product_profile({
    "short_video_proof_plan": {
        "candidates": [
            {"id": "P1", "selling_point": "温显功能", "visual_space": "high", "functional_centrality": "high", "comprehension_cost": "low", "delivery_stage": "S4", "proof_mode": "instant_visual"},
            {"id": "P2", "selling_point": "316 不锈钢材质", "visual_space": "low", "functional_centrality": "medium", "comprehension_cost": "high", "delivery_stage": "S2", "proof_mode": ""},
        ],
        "s4_anchor_candidate_id": "P1", "selection_source": "model_category_default", "anchor_confidence": "high",
    },
    "proof_contract": {
        "anchor_candidate_id": "P1", "mode": "instant_visual", "consumer_outcome": "杯身温度一眼可知", "signal_type": "state_change",
        "observable_signal": "杯身温显数字变化", "before_state": "温度未显示", "after_state": "温度数字清楚显示", "proof_condition": "近景固定拍摄杯盖显示区",
    },
})
check("短视频证明计划：重要但低可视卖点分流而非丢弃",
      _cup_plan["short_video_proof_plan"]["valid"] is True
      and any(item["id"] == "P2" and item["delivery_stage"] == "S2" for item in _cup_plan["short_video_proof_plan"]["candidates"]))
_trust_plan = normalize_product_profile({
    "short_video_proof_plan": {
        "candidates": [
            {"id": "P1", "selling_point": "本地认证", "visual_space": "low", "functional_centrality": "high", "comprehension_cost": "medium", "delivery_stage": "S5", "proof_mode": ""},
            {"id": "P2", "selling_point": "长期营养补充", "visual_space": "low", "functional_centrality": "high", "comprehension_cost": "high", "delivery_stage": "S5", "proof_mode": ""},
        ],
        "s4_anchor_candidate_id": "", "selection_source": "model_category_default", "anchor_confidence": "low",
    },
    "proof_contract": {
        "anchor_candidate_id": "", "mode": "trust_substituted", "consumer_outcome": "长期补充更值得信任", "signal_type": "trust_evidence",
        "observable_signal": "包装上的本地认证标识", "before_state": "", "after_state": "", "proof_condition": "认证名称与产品信息同框清楚可读",
    },
    "visual_proof_points": [{"priority": "primary", "proof_target": "气色变化", "visual_standard": "肤色更红润"}],
})
check("短视频证明计划：无 S4 候选不伪造视觉 anchor",
      _trust_plan["short_video_proof_plan"]["valid"] is True
      and _trust_plan["proof_contract"]["valid"] is True
      and _trust_plan["visual_proof_points"] == [])
_invalid_contract = normalize_product_profile({
    "proof_contract": {
        "mode": "instant_visual",
        "consumer_outcome": "柔焦效果",
        "signal_type": "state_change",
        "observable_signal": "同一光源下",
        "before_state": "油光明显",
        "after_state": "妆面哑光",
        "proof_condition": "面部特写",
    },
})
check("proof_contract 拒绝只有拍摄条件的直接视觉合同",
      _invalid_contract["proof_contract"]["valid"] is False
      and "拍摄条件" in _invalid_contract["proof_contract"]["validation_reason"]
      and _invalid_contract["visual_proof_points"] == [])
_compound_contract = normalize_product_profile({
    "proof_contract": {
        "mode": "instant_visual",
        "consumer_outcome": "去油光且隐形毛孔",
        "signal_type": "state_change",
        "observable_dimension": "油光反射强度与毛孔可见度",
        "observable_signal": "油光与毛孔同时变化",
        "before_state": "油光明显",
        "after_state": "反光减弱",
        "proof_condition": "同一脸颊同光线前后对比",
    },
})
check("proof_contract 拒绝复合 primary",
      _compound_contract["proof_contract"]["valid"] is False
      and "observable_dimension" in _compound_contract["proof_contract"]["validation_reason"])
_same_object_state_contract = normalize_product_profile({
    "proof_contract": {
        "mode": "process_result",
        "consumer_outcome": "刷头随水流消失",
        "signal_type": "state_change",
        "observable_dimension": "刷头完整性与存在状态",
        "observable_signal": "刷头从完整状态变为水中无残留",
        "before_state": "刷头完整附着",
        "after_state": "水中无刷头残留",
        "proof_condition": "近景记录冲水全过程",
    },
})
check("proof_contract 折叠同一对象状态同义项",
      _same_object_state_contract["proof_contract"]["valid"] is True
      and _same_object_state_contract["proof_contract"]["observable_dimension"] == "刷头状态")
_missing_mode_contract = normalize_product_profile({
    "proof_contract": {
        "consumer_outcome": "油光减少",
        "signal_type": "state_change",
        "observable_signal": "皮肤反光强度变化",
        "before_state": "油光明显",
        "after_state": "反光减弱",
        "proof_condition": "同一脸颊同光线前后对比",
    },
})
check("proof_contract 缺 mode 触发重答",
      _missing_mode_contract["proof_contract"]["valid"] is False
      and "mode" in _missing_mode_contract["proof_contract"]["validation_reason"])
_trust_contract = normalize_product_profile({
    "proof_contract_source": "curated",
    "proof_contract": {
        "mode": "trust_substituted",
        "consumer_outcome": "长期营养补充更值得信任",
        "signal_type": "trust_evidence",
        "observable_signal": "可核验的本地认证标识",
        "before_state": "",
        "after_state": "",
        "proof_condition": "认证来源与产品关联清楚可读，并在包装上完整展示",
    },
    "visual_proof_points": [{"priority": "primary", "proof_target": "气色改善", "visual_standard": "肤色变红润"}],
})
check("非直接视觉合同不保留伪 S4 primary",
      _trust_contract["proof_contract"]["valid"] is True
      and not any(point["priority"] == "primary" for point in _trust_contract["visual_proof_points"]))
_trust_skip_result = {
    "product_profile": _trust_contract,
    "stage_analysis": [{"stage": "S4 效果呈现"}],
}
check("非直接视觉合同跳过 S4 视觉复核",
      "trust_substituted" in s4_visual_verifier_module._visual_verifier_skip_reason(_trust_skip_result))
_compound_vpp = normalize_product_profile({
    "visual_proof_points": [
        {
            "priority": "primary",
            "proof_target": "清洁力与溶解性双重验证",
            "visual_standard": "污渍被擦除后，刷头在水中迅速崩解并随水流冲走",
            "visual_diff_dimensions": ["污渍存在vs污渍消失", "固体刷头vs完全溶解"],
            "related_selling_points": ["自带清洁剂遇水即溶", "刷头可降解直接冲走"],
        }
    ],
})
check("product_profile 拆分复合 primary",
      _compound_vpp["visual_proof_points"][0]["priority"] == "primary"
      and _compound_vpp["visual_proof_points"][0]["proof_target"] == "清洁力"
      and _compound_vpp["visual_proof_points"][0]["visual_diff_dimensions"] == ["污渍存在vs污渍消失"]
      and _compound_vpp["visual_proof_points"][1]["priority"] == "secondary")
_process_polluted_vpp = normalize_product_profile({
    "visual_proof_points": [
        {
            "priority": "primary",
            "proof_target": "清洁效果",
            "visual_standard": "刷头入水即刻起泡",
            "visual_diff_dimensions": ["污渍存在 vs 洁净如新"],
        }
    ],
})
check("product_profile 修正结果型 primary 的机制标准",
      _process_polluted_vpp["visual_proof_points"][0]["visual_standard"] == "污渍存在 vs 洁净如新")

_rel = normalize_s3_s4_relationship({
    "creator_relationship": "result-without-process",
    "benchmark_relationship": "process_creates_effect",
    "creator_reason": "达人只有结果图",
    "benchmark_reason": "标杆边用边出效果",
})
check("S3/S4 relationship 归一",
      _rel["creator_relationship"] == "result_without_process"
      and _rel["benchmark_relationship"] == "process_creates_effect")

_chain = normalize_promise_chain({
    "s1_promise": "油光变哑光",
    "s2_answer": "粉饼作为解决方案",
    "s3_proof_target": "上脸控油",
    "s4_outcome": "半脸哑光",
    "chain_closed": "true",
    "broken_at": "none",
    "break_reason": "S1-S4 同一命题闭环",
})
check("promise_chain 归一",
      _chain["chain_closed"] is True and _chain["broken_at"] == "none")

_fallback_chain = {
    "s3_s4_relationship": normalize_s3_s4_relationship(None),
    "promise_chain": normalize_promise_chain(None),
}
try:
    validate_chain_relationships(
        _fallback_chain,
        {"s3_flags_required": True, "s4_flags_required": True},
    )
    _fallback_chain_ok = True
except SystemExit:
    _fallback_chain_ok = False
check("S3/S4 关系归一：模型漏整块时 fallback 不拖垮主链", _fallback_chain_ok)

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

_s3_missing_action_proof = dict(_valid_s3)
_s3_missing_action_proof.pop("action_proof_met")
try:
    validate_s3_usage_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程", "creator_s3": _s3_missing_action_proof, "benchmark_s3": dict(_valid_s3)},
            ]
        },
        {"s3_flags_required": True},
    )
    _s3_missing_action_proof_failed = False
except SystemExit as exc:
    _s3_missing_action_proof_failed = "creator_s3.action_proof_met" in str(exc)
check("S3 usage flag 门禁：缺 action_proof_met 触发 repair", _s3_missing_action_proof_failed)

try:
    validate_s3_usage_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}, {"stage": "S3 使用过程"}]},
        {"s3_flags_required": True},
    )
    _s3_gate_failed = False
except SystemExit as exc:
    _s3_gate_failed = "缺少 creator_s3" in str(exc) and "缺少 benchmark_s3" in str(exc)
check("S3 usage flag 门禁：主链缺字段触发 repair", _s3_gate_failed)

_absent_s3 = _s3_flag(
    exists=False,
    module="unknown",
    usage=False,
    result_only=True,
    mouth_static=True,
    real=False,
    core=False,
    framing=False,
    context=False,
    continuity=False,
    overlays=["none"],
)
_absent_s3["evidence_ids"] = []
try:
    validate_s3_usage_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程", "creator_s3": _absent_s3, "benchmark_s3": dict(_valid_s3)},
            ]
        },
        {"s3_flags_required": True},
    )
    _s3_absent_empty_evidence_ok = True
except SystemExit:
    _s3_absent_empty_evidence_ok = False
check("S3 usage flag 门禁：确认无使用过程时允许空 evidence_ids", _s3_absent_empty_evidence_ok)

_present_s3_no_evidence = _s3_flag()
_present_s3_no_evidence["evidence_ids"] = []
try:
    validate_s3_usage_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程", "creator_s3": _present_s3_no_evidence, "benchmark_s3": dict(_valid_s3)},
            ]
        },
        {"s3_flags_required": True},
    )
    _s3_present_empty_evidence_failed = False
except SystemExit as exc:
    _s3_present_empty_evidence_failed = "creator_s3.evidence_ids" in str(exc)
check("S3 usage flag 门禁：存在使用过程时仍要求 evidence_ids", _s3_present_empty_evidence_failed)

_valid_s4 = _s4_flag()
try:
    validate_s4_effect_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现", "creator_s4": _valid_s4, "benchmark_s4": dict(_valid_s4)},
            ]
        },
        {"s4_flags_required": True},
    )
    _s4_gate_ok = True
except SystemExit:
    _s4_gate_ok = False
check("S4 effect flag 门禁：完整字段通过", _s4_gate_ok)

try:
    validate_s4_effect_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}, {"stage": "S3 使用过程"}, {"stage": "S4 效果呈现"}]},
        {"s4_flags_required": True},
    )
    _s4_gate_failed = False
except SystemExit as exc:
    _s4_gate_failed = "缺少 creator_s4" in str(exc) and "缺少 benchmark_s4" in str(exc)
check("S4 effect flag 门禁：主链缺字段触发 repair", _s4_gate_failed)

_absent_s4 = _s4_flag(
    effect=False,
    attribution=False,
    result_only=True,
    linked=False,
    effect_type="none",
    salience="none",
    matched=False,
    control=False,
    focus=False,
    visual_diff=False,
    module_constraints=False,
    maximized=False,
)
_absent_s4["evidence_ids"] = []
try:
    validate_s4_effect_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现", "creator_s4": _absent_s4, "benchmark_s4": dict(_valid_s4)},
            ]
        },
        {"s4_flags_required": True},
    )
    _s4_absent_empty_evidence_ok = True
except SystemExit:
    _s4_absent_empty_evidence_ok = False
check("S4 effect flag 门禁：确认无效果时允许空 evidence_ids", _s4_absent_empty_evidence_ok)

_visible_s4_no_evidence = _s4_flag()
_visible_s4_no_evidence["evidence_ids"] = []
try:
    validate_s4_effect_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现", "creator_s4": _visible_s4_no_evidence, "benchmark_s4": dict(_valid_s4)},
            ]
        },
        {"s4_flags_required": True},
    )
    _s4_visible_empty_evidence_failed = False
except SystemExit as exc:
    _s4_visible_empty_evidence_failed = "creator_s4.evidence_ids" in str(exc)
check("S4 effect flag 门禁：存在效果时仍要求 evidence_ids", _s4_visible_empty_evidence_failed)

_valid_s5 = _s5_flag()
try:
    validate_s5_trust_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大", "creator_s5": _valid_s5, "benchmark_s5": dict(_valid_s5)},
            ]
        },
        {"s5_flags_required": True},
    )
    _s5_gate_ok = True
except SystemExit:
    _s5_gate_ok = False
check("S5 trust flag 门禁：完整字段通过", _s5_gate_ok)

_s5_spec_as_trust = _s5_flag(trust_basis="offer_or_spec")
try:
    validate_s5_trust_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大", "creator_s5": _s5_spec_as_trust, "benchmark_s5": dict(_valid_s5)},
            ]
        },
        {"s5_flags_required": True},
    )
    _s5_spec_as_trust_failed = False
except SystemExit as exc:
    _s5_spec_as_trust_failed = "creator_s5.trust_basis 不构成独立信任" in str(exc)
check("S5 trust flag 门禁：产品规格不得伪装独立背书", _s5_spec_as_trust_failed)

try:
    validate_s5_trust_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}, {"stage": "S3 使用过程"}, {"stage": "S4 效果呈现"}, {"stage": "S5 信任放大"}]},
        {"s5_flags_required": True},
    )
    _s5_gate_failed = False
except SystemExit as exc:
    _s5_gate_failed = "缺少 creator_s5" in str(exc) and "缺少 benchmark_s5" in str(exc)
check("S5 trust flag 门禁：主链缺字段触发 repair", _s5_gate_failed)

_absent_s5 = _s5_flag(
    exists=False,
    trust_type="none",
    trust_basis="none",
    independent=False,
    visible=False,
    credible=False,
    specific=False,
    relevance=False,
)
_absent_s5["evidence_ids"] = []
try:
    validate_s5_trust_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大", "creator_s5": _absent_s5, "benchmark_s5": dict(_valid_s5)},
            ]
        },
        {"s5_flags_required": True},
    )
    _s5_absent_empty_evidence_ok = True
except SystemExit:
    _s5_absent_empty_evidence_ok = False
check("S5 trust flag 门禁：确认无信任环节时允许空 evidence_ids", _s5_absent_empty_evidence_ok)

_present_s5_no_evidence = _s5_flag()
_present_s5_no_evidence["evidence_ids"] = []
try:
    validate_s5_trust_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大", "creator_s5": _present_s5_no_evidence, "benchmark_s5": dict(_valid_s5)},
            ]
        },
        {"s5_flags_required": True},
    )
    _s5_present_empty_evidence_failed = False
except SystemExit as exc:
    _s5_present_empty_evidence_failed = "creator_s5.evidence_ids" in str(exc)
check("S5 trust flag 门禁：存在信任环节时仍要求 evidence_ids", _s5_present_empty_evidence_failed)

_valid_s6 = _s6_flag()
try:
    validate_s6_cta_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大"},
                {"stage": "S6 CTA", "creator_s6": _valid_s6, "benchmark_s6": dict(_valid_s6)},
            ]
        },
        {"s6_flags_required": True},
    )
    _s6_gate_ok = True
except SystemExit:
    _s6_gate_ok = False
check("S6 CTA flag 门禁：完整字段通过", _s6_gate_ok)

try:
    validate_s6_cta_flags(
        {"stage_analysis": [{"stage": "S1 Hook"}, {"stage": "S2 产品引出"}, {"stage": "S3 使用过程"}, {"stage": "S4 效果呈现"}, {"stage": "S5 信任放大"}, {"stage": "S6 CTA"}]},
        {"s6_flags_required": True},
    )
    _s6_gate_failed = False
except SystemExit as exc:
    _s6_gate_failed = "缺少 creator_s6" in str(exc) and "缺少 benchmark_s6" in str(exc)
check("S6 CTA flag 门禁：主链缺字段触发 repair", _s6_gate_failed)

_absent_s6 = _s6_flag(exists=False, direct=False, path=False, offer=False, urgency=False, recall=False, fit=False)
_absent_s6["evidence_ids"] = []
try:
    validate_s6_cta_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大"},
                {"stage": "S6 CTA", "creator_s6": _absent_s6, "benchmark_s6": dict(_valid_s6)},
            ]
        },
        {"s6_flags_required": True},
    )
    _s6_absent_empty_evidence_ok = True
except SystemExit:
    _s6_absent_empty_evidence_ok = False
check("S6 CTA flag 门禁：确认无 CTA 时允许空 evidence_ids", _s6_absent_empty_evidence_ok)

_present_s6_no_evidence = _s6_flag()
_present_s6_no_evidence["evidence_ids"] = []
try:
    validate_s6_cta_flags(
        {
            "stage_analysis": [
                {"stage": "S1 Hook"},
                {"stage": "S2 产品引出"},
                {"stage": "S3 使用过程"},
                {"stage": "S4 效果呈现"},
                {"stage": "S5 信任放大"},
                {"stage": "S6 CTA", "creator_s6": _present_s6_no_evidence, "benchmark_s6": dict(_valid_s6)},
            ]
        },
        {"s6_flags_required": True},
    )
    _s6_present_empty_evidence_failed = False
except SystemExit as exc:
    _s6_present_empty_evidence_failed = "creator_s6.evidence_ids" in str(exc)
check("S6 CTA flag 门禁：存在 CTA 时仍要求 evidence_ids", _s6_present_empty_evidence_failed)

_valid_relationship_result = {
    "s3_s4_relationship": {
        "creator_relationship": "result_without_process",
        "benchmark_relationship": "process_creates_effect",
        "creator_reason": "达人只给出结果画面，缺少使用过程支撑。",
        "benchmark_reason": "标杆通过连续使用动作直接产生可见效果。",
    },
    "promise_chain": {
        "s1_promise": "油光脸需要快速变哑光",
        "s2_answer": "粉饼被引出为控油解决方案",
        "s3_proof_target": "上脸按压控油过程",
        "s4_outcome": "半脸哑光和毛孔弱化效果",
        "chain_closed": False,
        "broken_at": "S3",
        "break_reason": "达人承诺成立，但使用过程没有把控油动作证明出来。",
    },
}
try:
    validate_chain_relationships(
        _valid_relationship_result,
        {"s3_flags_required": True, "s4_flags_required": True},
    )
    _chain_gate_ok = True
except SystemExit:
    _chain_gate_ok = False
check("S3/S4 关系门禁：完整字段通过", _chain_gate_ok)

try:
    validate_chain_relationships({}, {"s3_flags_required": True, "s4_flags_required": True})
    _chain_gate_failed = False
except SystemExit as exc:
    _chain_gate_failed = "缺少 s3_s4_relationship" in str(exc) and "缺少 promise_chain" in str(exc)
check("S3/S4 关系门禁：主链缺字段触发 repair", _chain_gate_failed)

try:
    validate_chain_relationships({}, {})
    _chain_legacy_ok = True
except SystemExit:
    _chain_legacy_ok = False
check("S3/S4 关系门禁：旧结果无标记不误伤", _chain_legacy_ok)

_cta_polluted_result = json.loads(json.dumps(_valid_relationship_result, ensure_ascii=False))
_cta_polluted_result["promise_chain"]["break_reason"] = "S1-S4 已证明产品好用，但购买指令不清晰导致转化链条弱。"
try:
    validate_chain_relationships(
        _cta_polluted_result,
        {"s3_flags_required": True, "s4_flags_required": True},
    )
    _chain_cta_pollution_failed = False
except SystemExit as exc:
    _chain_cta_pollution_failed = "不得把 S5/S6/CTA 作为断点" in str(exc)
check("S3/S4 关系门禁：CTA 不得污染 S1-S4 承诺链", _chain_cta_pollution_failed)

_sanitized_chain = {
    "promise_chain": {
        "chain_closed": False,
        "broken_at": "unknown",
        "break_reason": "达人已经证明产品好用，但购买指令不清晰导致转化链条弱。",
    }
}
sanitize_promise_chain_scope(_sanitized_chain)
check("S3/S4 关系修复：未知断点中的 CTA 污染自动归回 S6",
      _sanitized_chain["promise_chain"]["chain_closed"] is True
      and _sanitized_chain["promise_chain"]["broken_at"] == "none")

_unsanitized_chain = {
    "promise_chain": {
        "chain_closed": False,
        "broken_at": "S4",
        "break_reason": "S4 没有兑现结果，同时 CTA 不清晰。",
    }
}
sanitize_promise_chain_scope(_unsanitized_chain)
check("S3/S4 关系修复：明确 S4 断点不被 CTA 清洗",
      _unsanitized_chain["promise_chain"]["chain_closed"] is False
      and _unsanitized_chain["promise_chain"]["broken_at"] == "S4")

_stamped = {
    "product_profile": {
        "proof_mode": "instant_visual",
        "effect_requires_process": "partial",
        "core_visual_proposition": "模型新字段默认值应保留",
    }
}
stamp_product_foundation(
    _stamped,
    {
        "product_foundation": {
            "product_profile": {
                "core_visual_proposition": "旧地基核心视觉命题仍权威覆盖",
                "proof_mode": None,
                "effect_requires_process": None,
            }
        }
    },
)
check("Step-0 旧地基覆盖：空新字段不清掉 normalized 默认值",
      _stamped["product_profile"]["core_visual_proposition"] == "旧地基核心视觉命题仍权威覆盖"
      and _stamped["product_profile"]["proof_mode"] == "instant_visual"
      and _stamped["product_profile"]["effect_requires_process"] == "partial")

# 5. 死代码已清 + 模块仍可导入
import flayr_core.prompt as prompt_module  # noqa: E402

check("prompt.render_stage_frame_markdown 已删", not hasattr(prompt_module, "render_stage_frame_markdown"))

# 6. speech_mode 四分支：有口播、字幕驱动、音乐驱动、纯视觉驱动
from flayr_core.speech_mode import classify_speech_mode  # noqa: E402
from flayr_core.llm.payload import (  # noqa: E402
    build_llm_comparison_payload,
    build_llm_repair_payload,
    build_product_foundation_payload,
    build_stage_review_payload,
    hook_anchor_terms,
    load_brand_proposition,
)
from flayr_core.llm.pipeline import (  # noqa: E402
    _process_llm_result,
    finalize_analysis_result,
    has_product_foundation_anchor,
    merge_analysis_result,
    product_foundation_validation_reason,
)
from flayr_core.llm.api import LLM_MAX_OUTPUT_TOKENS, increase_output_budget, is_retryable_error  # noqa: E402
from flayr_core.report import stage_skipped  # noqa: E402
from flayr import (  # noqa: E402
    build_preprocess_fingerprint,
    create_run_dir,
    load_existing_video_result,
    resolve_ocr_policy,
)
from flayr_core.stage_ownership import CERTIFICATION_OWNERSHIP_PROMPT  # noqa: E402
from flayr_core.stage_catalog import DEFAULT_STAGES, fallback_artifact_ranges, stage_tuples  # noqa: E402
from flayr_core.llm.parse import STAGES as PARSE_STAGES  # noqa: E402

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
_fallback_ranges = fallback_artifact_ranges(20.0)
check(
    "阶段目录：解析与预处理回退共用唯一来源",
    PARSE_STAGES == stage_tuples()
    and [item[0] for item in _fallback_ranges] == [stage.name for stage in DEFAULT_STAGES]
    and _fallback_ranges[-1][2:] == (15.0, 20.0),
)
check(
    "LLM 结果入口统一委托唯一收口链",
    "finalize_analysis_result" in inspect.getsource(merge_analysis_result)
    and "finalize_analysis_result" in inspect.getsource(_process_llm_result)
    and inspect.getsource(finalize_analysis_result).count("validate_analysis_dimensions") == 1,
)

with tempfile.TemporaryDirectory() as tmp:
    cache_root = Path(tmp)
    video_path = cache_root / "source.mp4"
    video_path.write_bytes(b"first-video")
    role_dir = cache_root / "creator"
    frames_dir = role_dir / "frames"
    frames_dir.mkdir(parents=True)
    transcript_path = role_dir / "transcript.txt"
    transcript_path.write_text("cached transcript", encoding="utf-8")
    cache_args = SimpleNamespace(
        skip_whisper=False,
        whisper_language="auto",
        translate_with_llm=False,
        translation_model="",
        llm_model="",
        llm_api_url="",
        product_name="",
        product_notes="",
        ocr_mode="off",
        with_ocr=False,
        no_ocr=False,
        llm_dry_run=True,
    )
    cache_deps = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "whisper": "whisper-cli", "whisper_model": None, "whisper_model_th": None}
    fingerprint = build_preprocess_fingerprint(video_path, cache_deps, cache_args)
    (role_dir / "_preprocess.json").write_text(
        json.dumps({"frames_dir": str(frames_dir), "transcript_path": str(transcript_path), "preprocess_fingerprint": fingerprint}),
        encoding="utf-8",
    )
    check("预处理缓存：同视频同配置命中", load_existing_video_result(role_dir, fingerprint) is not None)
    video_path.write_bytes(b"changed-video")
    changed_fingerprint = build_preprocess_fingerprint(video_path, cache_deps, cache_args)
    check("预处理缓存：视频内容变化拒绝复用", load_existing_video_result(role_dir, changed_fingerprint) is None)
    cache_args.whisper_language = "th"
    config_fingerprint = build_preprocess_fingerprint(video_path, cache_deps, cache_args)
    check("预处理缓存：转写配置变化拒绝复用", load_existing_video_result(role_dir, config_fingerprint) is None)

with tempfile.TemporaryDirectory() as tmp:
    import flayr as flayr_module  # noqa: E402

    original_runs_dir = flayr_module.DEFAULT_RUNS_DIR
    flayr_module.DEFAULT_RUNS_DIR = Path(tmp)
    try:
        first_run = create_run_dir(SimpleNamespace(output_dir=None, mode="improve"))
        second_run = create_run_dir(SimpleNamespace(output_dir=None, mode="improve"))
    finally:
        flayr_module.DEFAULT_RUNS_DIR = original_runs_dir
    check("默认运行目录：同秒任务不复用目录", first_run != second_run and first_run.is_dir() and second_run.is_dir())

from flayr_core import translation as translation_module  # noqa: E402
from flayr_core import proposal_video as proposal_video_module  # noqa: E402
from flayr_core import voice_clone as voice_clone_module  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    translation_dir = Path(tmp)
    (translation_dir / "transcript.txt").write_text("Ini contoh", encoding="utf-8")
    translation_result = {"errors": []}
    translation_args = SimpleNamespace(
        translation_model="test-model",
        llm_model="",
        product_name="",
        product_notes="",
        llm_dry_run=False,
        llm_api_url="https://example.invalid",
    )
    original_key_reader = translation_module.read_llm_api_key
    original_call = translation_module.call_llm_api
    translation_module.read_llm_api_key = lambda _args: "test-key"
    translation_module.call_llm_api = lambda *_args: (_ for _ in ()).throw(SystemExit("network failed"))
    try:
        translation_module.translate_transcript_with_llm(translation_args, "creator", translation_dir, translation_result)
    finally:
        translation_module.read_llm_api_key = original_key_reader
        translation_module.call_llm_api = original_call
    check(
        "LLM 翻译失败：记录错误且不中断主流程",
        translation_result.get("translation_status") == "failed"
        and any("network failed" in str(item) for item in translation_result.get("errors", [])),
    )


def _curl_capture(module, callback):
    commands = []
    original = module.run_command

    def fake_run(command):
        commands.append(command)
        if "-o" in command:
            Path(str(command[command.index("-o") + 1])).write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    module.run_command = fake_run
    try:
        callback()
    finally:
        module.run_command = original
    return commands


proposal_commands = _curl_capture(
    proposal_video_module,
    lambda: proposal_video_module.curl_json("POST", "https://example.invalid", "secret-key", {}, False),
)
voice_commands = _curl_capture(
    voice_clone_module,
    lambda: voice_clone_module._curl_json(["https://example.invalid"], "secret-key"),
)
all_curl_commands = [command for group in (proposal_commands, voice_commands) for command in group]
check(
    "可选视频/音色请求：API key 不进入 curl argv",
    all("secret-key" not in " ".join(str(item) for item in command) for command in all_curl_commands)
    and all(any(str(item).startswith("@") for item in command) for command in all_curl_commands),
)

from collections import Counter  # noqa: E402


def _agreement(values):
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return "", 0.0
    top, count = Counter(vals).most_common(1)[0]
    return top, round(count / len(vals), 2)


def _hook_summary(hooks):
    if not hooks:
        return {}
    dims = {}
    for key in ("camera", "copy", "sound", "rhythm"):
        _, ratio = _agreement([(hook.get("dims") or {}).get(key) for hook in hooks])
        dims[key] = ratio
    boundary_values = [
        float(value)
        for hook in hooks
        for value in [hook.get("hook_boundary_seconds")]
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    boundary_mode, boundary_agree = _agreement(boundary_values)
    boundary_span = round(max(boundary_values) - min(boundary_values), 2) if len(boundary_values) >= 2 else 0.0
    return {
        "exists": {"mode": _agreement([hook.get("exists") for hook in hooks])[0], "agreement": _agreement([hook.get("exists") for hook in hooks])[1]},
        "landing": {"mode": _agreement([hook.get("landing_met") for hook in hooks])[0], "agreement": _agreement([hook.get("landing_met") for hook in hooks])[1]},
        "boundary": {
            "mode": boundary_mode,
            "agreement": boundary_agree,
            "span_seconds": boundary_span,
            "jitter_tolerated": boundary_span <= 1.0,
        },
        "dims_agreement": dims,
        "anchors": {
            "mode": _agreement([hook.get("anchors_proposition") for hook in hooks])[0],
            "agreement": _agreement([hook.get("anchors_proposition") for hook in hooks])[1],
        },
    }


def _classify_hook_issues(requested, successful, s1_values, creator, benchmark):
    issues = []
    if successful < min(2, requested):
        issues.append("api_failure")
    if len(set(s1_values)) > 1:
        issues.append("s1_severity_unstable")
    for side_name, side in (("creator", creator), ("benchmark", benchmark)):
        if not side:
            issues.append(f"{side_name}_hook_missing")
            continue
        boundary = side.get("boundary") or {}
        if boundary.get("agreement", 0) < 0.67 and not boundary.get("jitter_tolerated"):
            issues.append(f"{side_name}_boundary_unstable")
        if side.get("landing", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_landing_unstable")
        if side.get("exists", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_exists_unstable")
        if side.get("anchors", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_anchors_unstable")
        weak_dims = [
            dim for dim, ratio in (side.get("dims_agreement") or {}).items()
            if ratio < 0.67
        ]
        if weak_dims:
            issues.append(f"{side_name}_dims_unstable:{','.join(weak_dims)}")
    return issues

_jitter_side = _hook_summary([
    {"hook_boundary_seconds": 16.9, "landing_met": True, "exists": True, "anchors_proposition": True, "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True}},
    {"hook_boundary_seconds": 17.7, "landing_met": True, "exists": True, "anchors_proposition": True, "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True}},
])
_jitter_issues = _classify_hook_issues(2, 2, ["medium", "medium"], _jitter_side, _jitter_side)
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
check(
    "认证归属策略同时注入对比与修复 prompt",
    CERTIFICATION_OWNERSHIP_PROMPT in _user_text
    and CERTIFICATION_OWNERSHIP_PROMPT in _repair_payload["messages"][0]["content"],
)
with tempfile.TemporaryDirectory() as tmp:
    analysis_input_path = prompt_module.write_analysis_input(
        Path(tmp),
        {
            "analysis_scope": {"label": "视频证据分析", "missing_context": [], "boundary": "仅按视频事实判断"},
            "product": {"name": "测试品", "category": "", "price": "", "target_market": "auto", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "creator_profile": "", "notes": ""},
            "videos": {},
        },
    )
    analysis_input_text = analysis_input_path.read_text(encoding="utf-8")
check(
    "认证归属策略覆盖 analysis_input 且清除旧位置规则",
    CERTIFICATION_OWNERSHIP_PROMPT in analysis_input_text
    and "开头的背书/认证类内容按钩子算" not in analysis_input_text,
)

_review_payload = build_stage_review_payload(
    "test-model",
    {"videos": {}},
    {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
    {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 5s", "benchmark_time_range": "0s - 5s"}]},
    ["S1"],
)
_review_user = _review_payload["messages"][1]["content"][0]["text"]
check("Phase C S1 回看强制重判 hook flags", "creator_hook" in _review_user and "benchmark_hook" in _review_user)
check("Phase C 回看使用 focused window detail mode", "detail_mode=focused_window" in _review_user and "sparse_window" in _review_user)
check(
    "Phase C 回看不把认证当作 S2 起点",
    "产品名/卖点/认证" not in _review_user and CERTIFICATION_OWNERSHIP_PROMPT not in _review_user,
)

_review_s5s6_payload = build_stage_review_payload(
    "test-model",
    {"videos": {}},
    {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
    {
        "stage_analysis": [
            {"stage": "S5 信任放大", "creator_time_range": "20s - 24s", "benchmark_time_range": "20s - 24s"},
            {"stage": "S6 CTA", "creator_time_range": "25s - 30s", "benchmark_time_range": "25s - 30s"},
        ]
    },
    ["S5", "S6"],
)
_review_s5s6_user = _review_s5s6_payload["messages"][1]["content"][0]["text"]
check("Phase C S5/S6 回看强制重判 trust/CTA flags",
      "creator_s5" in _review_s5s6_user and "benchmark_s5" in _review_s5s6_user
      and "trust_basis" in _review_s5s6_user
      and "social_consensus 必须同时有明确目标群体/社区" in _review_s5s6_user
      and "产品数量、使用时长、参数、价格、赠品、套餐不是独立信任" in _review_s5s6_user
      and "creator_s6" in _review_s5s6_user and "benchmark_s6" in _review_s5s6_user)

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

with tempfile.TemporaryDirectory() as tmp:
    run_dir = Path(tmp) / "sample-colorkey-b1" / "run_01"
    run_dir.mkdir(parents=True)
    brand = load_brand_proposition(run_dir)
    check("冻结命题可从 run_01 父目录解析样本名", bool(brand) and "急救修护" in (brand.get("propositions") or []))

_name_only_analysis = {"product": {"name": "simplus", "category": "", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "notes": ""}}
check("Step-0 护栏：纯英文品牌名无锚点时跳过", not has_product_foundation_anchor(_name_only_analysis))
_cn_name_analysis = {"product": {"name": "儿童牙膏", "category": "", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "notes": ""}}
check("Step-0 护栏：中文品名可作为弱锚点", has_product_foundation_anchor(_cn_name_analysis))
_brand_analysis = {
    "product": {"name": "colorkey b1", "category": "", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "notes": ""},
    "brand_proposition": {"propositions": ["急救修护"], "painpoints": ["补水"]},
}
_foundation_payload_text = build_product_foundation_payload("test-model", _brand_analysis)["messages"][1]["content"][0]["text"]
check("Step-0 payload 注入人工冻结命题", "人工冻结命题" in _foundation_payload_text and "急救修护" in _foundation_payload_text)
check("Step-0 payload 要求 S4 多视觉证明点", "visual_proof_points" in _foundation_payload_text and "primary" in _foundation_payload_text)
check("Step-0 payload 要求先做卖点分流再选 S4 anchor",
      "short_video_proof_plan" in _foundation_payload_text and "visual_space" in _foundation_payload_text
      and "functional_centrality" in _foundation_payload_text)
check("Step-0 payload 明确计划和合同为同级对象",
      "严禁嵌入 short_video_proof_plan 内" in _foundation_payload_text)
check("Step-0 payload 禁止复合 primary",
      "all-of" in _foundation_payload_text and "一次性马桶刷 primary=清洁结果可见" in _foundation_payload_text)
check("Step-0 payload 要求 proof_contract",
      "proof_contract" in _foundation_payload_text and "before_state" in _foundation_payload_text
      and "保健品不得把气色/体感变化伪装成直接视觉" in _foundation_payload_text)
check("Step-0 地基门禁同时要求计划和合同",
      product_foundation_validation_reason(_proof_plan_profile) == ""
      and "short_video_proof_plan" in product_foundation_validation_reason(_contract_profile))
check("S4 verifier 兜底复合 primary",
      "复合条件" in inspect.getsource(s4_visual_verifier_module.build_s4_visual_verifier_payload)
      and "不能直接把 primary 判 false" in inspect.getsource(s4_visual_verifier_module.build_s4_visual_verifier_payload))
_length_payload = {"max_tokens": 16384}
_old_budget, _new_budget = increase_output_budget(_length_payload)
_capped_old, _capped_new = increase_output_budget({"max_tokens": LLM_MAX_OUTPUT_TOKENS})
check("LLM length 重试提高输出预算并封顶",
      (_old_budget, _new_budget, _length_payload["max_tokens"]) == (16384, 32768, 32768)
      and (_capped_old, _capped_new) == (32768, 32768))
check("LLM TLS 瞬断进入重试", is_retryable_error("LibreSSL SSL_connect: SSL_ERROR_SYSCALL"))

print()
print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
