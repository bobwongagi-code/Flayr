"""flayr_core.llm.parse：JSON 解析 + schema 规范化。

职责：
  - 把 LLM 返回的原始文本 / 半结构 dict 解析为合法 JSON
  - 按 references/analysis-output-schema.json 把 dict 字段补齐、归一为 schema 规范结构
  - 提供阶段常量 STAGES、口播有效性判断 is_effective_voiceover 等基础工具
    （这些被 postprocess 也复用；放在 parse 是为了让依赖单向：postprocess → parse）

不依赖 postprocess 任何函数；不做业务规则修补；不引用业务规则关键词。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds


# 阶段固定列表：S1-S6，作为 stage_analysis 数组长度与顺序校验依据。
# 这里的"参考时间段"（如 "0~3s"）仅在 LLM 未给出真实 time_range 时作为兜底回填字符串，
# 不参与下游的帧选取计算——帧选取应以 LLM 输出的真实 benchmark_time_range / creator_time_range 为准。
STAGES = [
    ("S1 Hook", "0~3s", "用户凭什么停下来"),
    ("S2 产品引出", "3~6s", "产品为什么现在出现"),
    ("S3 使用过程", "6~15s", "用户能不能看懂怎么用"),
    ("S4 效果呈现", "15~23s", "用户能不能看见价值"),
    ("S5 信任放大", "23~27s", "用户凭什么相信"),
    ("S6 CTA", "最后 3~5s", "用户为什么现在下单"),
]


# ---------------------------------------------------------------------------
# JSON 文本解析
# ---------------------------------------------------------------------------

def parse_json_text(text: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 文本，必要时做轻度修复。

    处理顺序：
      1. 去掉 ```json fence
      2. 去尾随逗号
      3. 第一次 json.loads
      4. 失败则尝试转义未配对的引号后再 loads
      5. 仍失败则 fail loud
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = remove_trailing_commas(cleaned)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        repaired = remove_trailing_commas(escape_unquoted_string_quotes(cleaned))
        try:
            result = json.loads(repaired)
        except json.JSONDecodeError:
            raise SystemExit(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SystemExit("LLM output JSON must be an object.")
    return result


def remove_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def escape_unquoted_string_quotes(text: str) -> str:
    """转义 LLM 常误产生的字符串内部未转义引号。"""
    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        remainder = text[index + 1 :]
        next_nonspace = next((item for item in remainder if not item.isspace()), "")
        if next_nonspace in {":", ",", "}", "]"}:
            repaired.append(char)
            in_string = False
        else:
            repaired.append('\\"')
    return "".join(repaired)


# ---------------------------------------------------------------------------
# 字段级 normalize 工具
# ---------------------------------------------------------------------------

def required_text(item: dict[str, Any], key: str) -> str:
    """已弱化为软兜底：缺字段时填占位，让流程跑通，由报告读者人工识别空字段。

    名字保留为 required_text 避免破坏调用点；后续 QA-RULES 实施时可由 R02/R03 接管
    "必填字段"的严格性，届时本函数可改回抛 SystemExit 或拆为 hard/soft 两个版本。
    """
    value = str(item.get(key) or "").strip()
    return value or f"（LLM 未填写 {key}，需人工补充）"


def normalize_evidence(value: Any) -> list[str]:
    if isinstance(value, list):
        evidence = [str(item).strip() for item in value if str(item).strip()]
        return evidence[:5]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_support_status(value: Any, quote: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"supported", "voice_only", "visual_only", "conflict"}:
        return status
    return "voice_only" if str(quote or "").strip() else "visual_only"


def normalize_base_frame_suitability(value: Any, best_time: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"usable", "no_suitable_frame"}:
        return status
    return "usable" if str(best_time or "").strip() else "no_suitable_frame"


def normalized_base_frame_time(item: dict[str, Any]) -> str:
    if normalize_base_frame_suitability(item.get("base_frame_suitability"), item.get("best_base_frame_time")) == "no_suitable_frame":
        return ""
    return str(item.get("best_base_frame_time") or "").strip()


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def normalize_task_completion(value: Any) -> str:
    """把 task_completion 归一为 complete|partial|missing（达人侧功能完成度）。

    2026-06-11 门禁 T5 发现：模型原始输出是自由文本（'both_complete'/'completed'/
    'benchmark_complete_creator_partial'/'双方均完成了任务'…），旧 normalize_choice 的
    fallback 把一切压成 partial——语义直接错（both_complete 应为 complete）。
    本函数是过渡 shim：prompt 已强制枚举（治本），shim 兜历史漂移；映射规则覆盖
    门禁实测的全部观察值。语义锚定达人侧：双侧编码取 creator 段。
    """
    text = str(value or "").strip().lower()
    if text in {"complete", "partial", "missing"}:
        return text
    # 英文双侧编码：取 creator 侧状态
    match = re.search(r"creator[_\s]*(completed?|partial|incomplete|missing|only|superior|better|stronger|weaker)", text)
    if match:
        word = match.group(1)
        if word in {"complete", "completed", "only", "superior", "better", "stronger"}:
            return "complete"
        if word == "missing":
            return "missing"
        return "partial"
    # 只提 benchmark 强（隐含达人弱）
    if re.search(r"benchmark[_\s]*(superior|better|stronger)", text):
        return "partial"
    # 中文达人侧
    if "达人" in text:
        creator_part = text.split("达人", 1)[1]
        # "达人未完成/未涉及…"直接开头 = 功能未达成 → missing（"未能充分"类弱否定除外）；
        # "达人完成了X，但未完成Y" 后文才出现否定 = partial。
        if re.match(r"\s*未(?!能)", creator_part) or re.search(r"未涉及|未设计|没有(做|设计|涉及)|未做", creator_part):
            return "missing"
        if re.search(r"未完成|部分|基本完成|不完整|不足|仅", creator_part):
            return "partial"
        if re.search(r"完成|做到", creator_part):
            return "complete"
    # 单值/双方（"均(清晰/出色…)完成"允许间插修饰词，但"均未完成"归 missing 在前已拦）
    if re.search(r"both[_\s]*missing|均未(完成|涉及|设计)|missing|absent|none|未涉及|未设计", text):
        return "missing"
    if re.search(r"both[_\s]*(completed?|done|full)|^(completed?|full|done|finished|good|well|ok)$|(双方)?均(?!未).{0,6}完成|完成出色|出色完成", text):
        return "complete"
    if re.search(r"partial|incomplete|部分|基本完成|不完整|不足|weak", text):
        return "partial"
    return "partial"


def normalize_execution_score(value: Any) -> float | None:
    """单侧执行分归一：0=不执行，0.5=敷衍，1=合格，2=好（4d 推导的输入事实）。

    解析失败返回 None——下游 derive 对 None 优雅跳过、保留模型 severity，
    所以这里宁缺毋滥，不做强行兜底映射。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) in {0.0, 0.5, 1.0, 2.0} else None
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        number = float(text)
        return number if number in {0.0, 0.5, 1.0, 2.0} else None
    except ValueError:
        pass
    # 容忍少量语义词漂移（与 task_completion 自由文本的教训一致：枚举指令挡不全）
    if re.search(r"未执行|不执行|没有执行|缺失|none", text):
        return 0.0
    if re.search(r"敷衍|轻带|几乎无效|perfunctory", text):
        return 0.5
    if re.search(r"合格|完成|adequate|ok", text):
        return 1.0
    if re.search(r"出色|优秀|很好|excellent|strong", text):
        return 2.0
    return None


def normalize_painpoint_relevance(value: Any) -> str | None:
    """痛点命中归一（4d）：四值枚举；缺失/不合法返回 None → derive 退回词法匹配兜底。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_stage_standard_delivery(value: Any) -> str | None:
    """到位标准达成归一（全阶段统一，泛化自 proposition_delivery）：四值枚举；
    该阶段双方是否有效达到本阶段的『本品到位标准』（锚点按阶段查，见 prompt 对照表）。
    先作为事实收集，暂不参与 derive 卡分；缺失/不合法返回 None。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_category_profile(value: Any) -> dict[str, Any] | None:
    """品类画像归一（4d）：模型只报事实与世界知识，权重政策在代码（postprocess/derive.py）。"""
    if not isinstance(value, dict):
        return None
    painpoints = [str(p).strip() for p in value.get("painpoints") or [] if str(p).strip()][:10]
    return {
        "category_name": str(value.get("category_name") or "").strip(),
        "price_tier": normalize_choice(value.get("price_tier"), {"low", "mid", "high"}, "mid"),
        # 来源占位（model_fallback）：postprocess 若发现运营档位会改写为 operator 并填 price
        "price_tier_source": "model_fallback",
        "price": "",
        "decision_threshold": normalize_choice(value.get("decision_threshold"), {"impulse", "considered"}, "considered"),
        "drive_type": normalize_choice(value.get("drive_type"), {"emotional", "functional", "mixed"}, "functional"),
        "painpoints": painpoints,
    }


def normalize_product_profile(value: Any) -> dict[str, Any] | None:
    """产品商业 DNA 归一：判分前模型先立的"本品视觉命题"尺子。

    core_visual_proposition 是 S2-S4 执行分锚点（"该展示成什么样才算到位"按品现推，跨品类泛化）。
    模型只报产品事实 + 品类世界知识；后续运营/DNA 库可经 postprocess 覆盖（同 price_tier 降级链）。
    """
    if not isinstance(value, dict):
        return None
    multipliers = [str(m).strip() for m in value.get("trust_multipliers") or [] if str(m).strip()][:6]
    dimensions = [str(d).strip() for d in value.get("visual_diff_dimensions") or [] if str(d).strip()][:3]
    selling_points = [str(s).strip() for s in value.get("core_selling_points") or [] if str(s).strip()][:6]
    return {
        # 可视化分叉：no（香水/保健品等效果拍不出）时 S4 视觉审计失效，判断权重应转 S5/达人可信度
        "visualizable": normalize_choice(value.get("visualizable"), {"yes", "no"}, "yes"),
        "physical_task": str(value.get("physical_task") or "").strip(),
        # S1 钩子命题：本品最有拦截力的点（模型推，运营可经降级链覆盖）
        "hook_proposition": str(value.get("hook_proposition") or "").strip(),
        # S3 主轴：本品核心卖点（使用过程要演示传递的对象，模型推/运营可供给）
        "core_selling_points": selling_points,
        # S3 场景层：本品典型使用场景（卖点演示的舞台，判场景适配/丰富/连贯的基准）
        "usage_context": str(value.get("usage_context") or "").strip(),
        "core_visual_proposition": str(value.get("core_visual_proposition") or "").strip(),
        # before/after 应变化的视觉维度（S4 核验对比只看这些；未来 CV 检测层的维度钩子）
        "visual_diff_dimensions": dimensions,
        "trust_multipliers": multipliers,
        "shooting_requirement": str(value.get("shooting_requirement") or "").strip(),
        # 来源占位（model_inferred）：postprocess 命中 DNA 库或运营供给时改写为 library/operator
        "dna_source": "model_inferred",
        "confidence": normalize_choice(value.get("confidence"), {"high", "low"}, "high"),
    }


def normalize_bool_flag(value: Any) -> bool:
    """把模型可能输出的 true/"yes"/1/"是" 等统一成 bool。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "是", "有"}


def normalize_product_coverage(value: Any) -> str:
    return normalize_choice(value, {"none", "low", "medium", "high"}, "none")


def normalize_module_id(value: Any, index: int) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == "UNKNOWN":
        return "unknown"
    if re.fullmatch(rf"S{index}-[A-Z]", normalized):
        return normalized
    return "unknown"


def normalize_voice_performance(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "pace": str(item.get("pace") or "未评估").strip(),
        "energy": str(item.get("energy") or "未评估").strip(),
        "key_pause": bool(item.get("key_pause", False)),
        "note": str(item.get("note") or "未提供口播表现判断。").strip(),
    }


def normalize_holistic_assessment(value: Any) -> dict[str, str]:
    item = value if isinstance(value, dict) else {}
    keys = (
        "structure_integrity",
        "selling_point_efficiency",
        "audience_resonance",
        "pace_and_emotion",
        "trust_and_purchase_impulse",
        "conversion_prediction",
    )
    return {key: str(item.get(key) or "未完成评估。").strip() for key in keys}


def normalize_product_visibility(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "first_appearance_sec": item.get("first_appearance_sec"),
        "total_screen_time_sec": item.get("total_screen_time_sec"),
        "video_duration_sec": item.get("video_duration_sec"),
        "ratio": item.get("ratio"),
        "estimation_note": str(item.get("estimation_note") or "未提供统计依据。").strip(),
    }


def normalize_loop_closure(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "pain_resolved_in_s4": bool(item.get("pain_resolved_in_s4", False)),
        "benefit_delivered_in_s6": bool(item.get("benefit_delivered_in_s6", False)),
        "suspense_revealed": bool(item.get("suspense_revealed", False)),
        "suspense_reveal_time": item.get("suspense_reveal_time"),
        "note": str(item.get("note") or "未完成闭环校验。").strip(),
    }


def normalize_video_understanding(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, Any] = {}
    for role in ("benchmark", "creator"):
        item = source.get(role) if isinstance(source.get(role), dict) else {}
        units = item.get("evidence_units") if isinstance(item.get("evidence_units"), list) else []
        normalized[role] = {
            "content_summary": str(item.get("content_summary") or "").strip(),
            "communication_strategy": str(item.get("communication_strategy") or "").strip(),
            "evidence_units": [
                {
                    "id": str(unit.get("id") or f"{role[0].upper()}{index}").strip(),
                    "time_range": str(unit.get("time_range") or "").strip(),
                    "information": str(unit.get("information") or "").strip(),
                    "voiceover": str(unit.get("voiceover") or "").strip(),
                    "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                    "visual_fact": str(unit.get("visual_fact") or "").strip(),
                    "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                    "product_visible": normalize_bool_flag(unit.get("product_visible")),
                    "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                    "third_party_endorsement": normalize_bool_flag(unit.get("third_party_endorsement")),
                }
                for index, unit in enumerate(units, start=1)
                if isinstance(unit, dict)
            ][:20],
        }
    return normalized


def normalize_severity(value: Any) -> str:
    severity = str(value or "medium").strip().lower()
    # LLM 有时输出 high/low 而非 large/small，做兼容映射
    alias = {"high": "large", "low": "small", "big": "large", "minor": "small"}
    severity = alias.get(severity, severity)
    if severity not in {"large", "medium", "small"}:
        return "medium"
    return severity


def normalize_time_range_value(value: Any) -> str:
    if isinstance(value, dict):
        start = value.get("start", value.get("start_time"))
        end = value.get("end", value.get("end_time"))
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return f"{format_seconds(start)} - {format_seconds(end)}"
    return str(value or "")


def normalize_priority(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# 顶层 schema 归一化
# ---------------------------------------------------------------------------

def adapt_misnested_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    """处理 LLM 偶发的字段嵌套错位，在严格校验前先做兜底重排。"""
    adapted = dict(result)
    if "stage_analysis" not in adapted and isinstance(adapted.get("product_visibility"), list):
        adapted["stage_analysis"] = adapted["product_visibility"]
        adapted["product_visibility"] = {
            "first_appearance_sec": 0.0,
            "total_screen_time_sec": 0.0,
            "video_duration_sec": 0.0,
            "ratio": 0.0,
            "estimation_note": "模型将阶段数组误写入产品可见度字段；此处需人工结合报告帧复核。",
        }
    # 通用修复：product_visibility 缺 first_appearance_sec 时视为字段错位/缺失
    # （观察到 LLM 会写成 stage-keyed dict、evidence_units 列表、或完全省略）
    pv = adapted.get("product_visibility")
    if not isinstance(pv, dict) or "first_appearance_sec" not in pv:
        if pv is not None:
            adapted["misplaced_product_visibility"] = pv
        adapted["product_visibility"] = {
            "first_appearance_sec": 0.0,
            "total_screen_time_sec": 0.0,
            "video_duration_sec": 0.0,
            "ratio": 0.0,
            "estimation_note": "LLM 未输出可识别的 product_visibility 字段（first_appearance_sec 等），需人工复核。原数据保留在 misplaced_product_visibility。",
        }
    if isinstance(adapted.get("holistic_assessment"), str):
        text = adapted["holistic_assessment"]
        adapted["holistic_assessment"] = {
            "structure_integrity": text,
            "selling_point_efficiency": text,
            "audience_resonance": text,
            "pace_and_emotion": text,
            "trust_and_purchase_impulse": text,
            "conversion_prediction": text,
        }
    if not adapted.get("one_line_summary"):
        adapted["one_line_summary"] = adapted.get("executive_summary") or adapted.get("one_line_verdict") or "基于视频证据完成结构对比。"
    if not adapted.get("executive_summary"):
        adapted["executive_summary"] = adapted.get("one_line_summary")
    if not adapted.get("loop_closure"):
        adapted["loop_closure"] = {
            "pain_resolved_in_s4": False,
            "benefit_delivered_in_s6": False,
            "suspense_revealed": False,
            "suspense_reveal_time": None,
            "note": "模型未单独输出闭环字段，需结合阶段证据复核。",
        }
    return adapted


def normalize_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    """把 LLM dict 归一为 schema 规范结构；缺字段或阶段数不对会抛 SystemExit。"""
    result = adapt_misnested_analysis_result(result)
    stage_analysis = result.get("stage_analysis")
    improvements = result.get("improvements")
    executive_summary = str(result.get("one_line_summary") or result.get("executive_summary") or "").strip()

    if not isinstance(stage_analysis, list) or len(stage_analysis) != len(STAGES):
        raise SystemExit("analysis_result must contain stage_analysis with 6 items.")
    # improvements 数量上限 5，下限 1：LLM 主动判断只有 1 条值得改时不强迫凑数，
    # 避免编造内容污染报告。质量问题（应该有 3 条但只给 1 条）由 prompt 工程和后续 QA-RULES 兜底。
    if not isinstance(improvements, list) or not (1 <= len(improvements) <= 5):
        raise SystemExit("analysis_result must contain 1 to 5 improvements.")

    normalized_stages = []
    for index, item in enumerate(stage_analysis):
        if not isinstance(item, dict):
            raise SystemExit("Each stage_analysis item must be an object.")
        stage_name, default_range, core_question = STAGES[index]
        benchmark_time_range = normalize_time_range_value(item.get("benchmark_time_range") or item.get("time_range") or default_range)
        creator_time_range = normalize_time_range_value(item.get("creator_time_range") or item.get("time_range") or default_range)
        normalized_stages.append(
            {
                "stage": str(item.get("stage") or stage_name),
                "time_range": str(item.get("time_range") or f"标杆 {benchmark_time_range} / 达人 {creator_time_range}"),
                "benchmark_time_range": benchmark_time_range,
                "creator_time_range": creator_time_range,
                "core_question": str(item.get("core_question") or core_question),
                "creator_module_id": normalize_module_id(item.get("creator_module_id"), index + 1),
                "benchmark_module_id": normalize_module_id(item.get("benchmark_module_id"), index + 1),
                "module_fit": normalize_choice(item.get("module_fit"), {"fit", "degraded", "unfit", "unknown"}, "unknown"),
                "module_fit_reason": str(item.get("module_fit_reason") or "").strip(),
                "task_completion": normalize_task_completion(item.get("task_completion")),
                "gap_type": normalize_choice(item.get("gap_type"), {"structural", "execution", "resource"}, "structural"),
                "gap_summary": normalize_evidence(item.get("gap_summary")),
                "voice_performance": normalize_voice_performance(item.get("voice_performance")),
                "benchmark_summary": required_text(item, "benchmark_summary"),
                "benchmark_key_message": str(item.get("benchmark_key_message") or item.get("benchmark_summary") or "").strip(),
                "benchmark_evidence_ids": normalize_evidence(item.get("benchmark_evidence_ids")),
                "benchmark_visual_evidence": normalize_evidence(item.get("benchmark_visual_evidence")),
                "benchmark_support_status": normalize_support_status(item.get("benchmark_support_status"), item.get("benchmark_quote")),
                "benchmark_quote": str(item.get("benchmark_quote") or "").strip(),
                "benchmark_quote_zh": str(item.get("benchmark_quote_zh") or "").strip(),
                "creator_summary": required_text(item, "creator_summary"),
                "creator_key_message": str(item.get("creator_key_message") or item.get("creator_summary") or "").strip(),
                "creator_evidence_ids": normalize_evidence(item.get("creator_evidence_ids")),
                "creator_visual_evidence": normalize_evidence(item.get("creator_visual_evidence")),
                "creator_support_status": normalize_support_status(item.get("creator_support_status"), item.get("creator_quote")),
                "creator_quote": str(item.get("creator_quote") or "").strip(),
                "creator_quote_zh": str(item.get("creator_quote_zh") or "").strip(),
                "gap": required_text(item, "gap"),
                "evidence": normalize_evidence(item.get("evidence")),
                "severity": normalize_severity(item.get("severity")),
                # 模型直判快照（归一时定格）：severity 后续会被 stabilize/derive 改写，
                # 校准对照口径必须用这份，不能用链上任何一步之后的值（code review #5）
                "model_severity": normalize_severity(item.get("severity")),
                # 4d：两侧独立执行分（0/0.5/1/2），缺失为 None → derive 优雅跳过
                "creator_execution": normalize_execution_score(item.get("creator_execution")),
                "benchmark_execution": normalize_execution_score(item.get("benchmark_execution")),
                # 4d：痛点命中事实（替代词法匹配定 C 系数），缺失为 None → derive 词法兜底
                "painpoint_relevance": normalize_painpoint_relevance(item.get("painpoint_relevance")),
                # 到位标准达成事实（全阶段统一，见 prompt 对照表；先收集，暂不卡分）
                "stage_standard_delivery": normalize_stage_standard_delivery(item.get("stage_standard_delivery")),
            }
        )

    normalized_improvements = []
    for index, item in enumerate(improvements, start=1):
        if not isinstance(item, dict):
            raise SystemExit("Each improvement item must be an object.")
        creator_time_range = str(item.get("creator_time_range") or item.get("time_range") or "").strip()
        benchmark_time_range = str(item.get("benchmark_time_range") or item.get("time_range") or "").strip()
        normalized_improvements.append(
            {
                "title": required_text(item, "title"),
                "target_stage": str(item.get("target_stage") or "").strip(),
                "gmv_impact": str(item.get("gmv_impact") or "").strip(),
                "gap_type": normalize_choice(item.get("gap_type"), {"structural", "execution", "resource"}, "structural"),
                "time_range": required_text(item, "time_range"),
                "creator_time_range": creator_time_range or required_text(item, "time_range"),
                "benchmark_time_range": benchmark_time_range or required_text(item, "time_range"),
                "problem": required_text(item, "problem"),
                "benchmark_reference": required_text(item, "benchmark_reference"),
                "benchmark_evidence_ids": normalize_evidence(item.get("benchmark_evidence_ids")),
                "suggestion": required_text(item, "suggestion"),
                "actions": normalize_evidence(item.get("actions")) or normalize_evidence(item.get("suggestion")),
                "gmv_reason": required_text(item, "gmv_reason"),
                "evidence": normalize_evidence(item.get("evidence")),
                "creator_script": str(item.get("creator_script") or "").strip(),
                "creator_script_zh": str(item.get("creator_script_zh") or "").strip(),
                "base_frame_suitability": normalize_base_frame_suitability(item.get("base_frame_suitability"), item.get("best_base_frame_time")),
                "best_base_frame_time": normalized_base_frame_time(item),
                "base_frame_evidence_id": str(item.get("base_frame_evidence_id") or "").strip(),
                "base_frame_reason": str(item.get("base_frame_reason") or "").strip(),
                "aigc_prompt": str(item.get("aigc_prompt") or "").strip(),
                "aigc_image_path": str(item.get("aigc_image_path") or "").strip(),
                "expected_effect": str(item.get("expected_effect") or item.get("gmv_reason") or "").strip(),
                "priority": normalize_priority(item.get("priority"), index),
            }
        )

    # key_conclusions：消费者视角关键结论（1-5 条）
    raw_conclusions = result.get("key_conclusions")
    key_conclusions: list[str] = []
    if isinstance(raw_conclusions, list):
        for item in raw_conclusions:
            text = str(item).strip()
            if text:
                key_conclusions.append(text)
        key_conclusions = key_conclusions[:5]

    return {
        "one_line_verdict": str(result.get("one_line_verdict") or "").strip(),
        "one_line_summary": executive_summary,
        "executive_summary": executive_summary,
        "holistic_assessment": normalize_holistic_assessment(result.get("holistic_assessment")),
        "key_conclusions": key_conclusions,
        "product_visibility": normalize_product_visibility(result.get("product_visibility")),
        "category_profile": normalize_category_profile(result.get("category_profile")),
        "product_profile": normalize_product_profile(result.get("product_profile")),
        "loop_closure": normalize_loop_closure(result.get("loop_closure")),
        "video_understanding": normalize_video_understanding(result.get("video_understanding")),
        "stage_analysis": normalized_stages,
        "improvements": normalized_improvements,
    }


# ---------------------------------------------------------------------------
# 单视频事实抽取归一化（fact extraction 模式专用）
# ---------------------------------------------------------------------------

def normalize_video_fact_result(role: str, result: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    code = "B" if role == "benchmark" else "C"
    units = result.get("evidence_units")
    if not isinstance(units, list) or not units:
        raise SystemExit(f"{role} fact extraction returned no evidence_units.")
    normalized = {
        "content_summary": str(result.get("content_summary") or "").strip(),
        "communication_strategy": str(result.get("communication_strategy") or "").strip(),
        "evidence_units": [],
    }
    for index, unit in enumerate(units[:8], start=1):
        if not isinstance(unit, dict):
            continue
        normalized["evidence_units"].append(
            {
                "id": normalized_fact_id(unit.get("id"), code, index),
                "time_range": str(unit.get("time_range") or "").strip(),
                "information": str(unit.get("information") or "").strip(),
                "voiceover": str(unit.get("voiceover") or "").strip(),
                "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                "visual_fact": str(unit.get("visual_fact") or "").strip(),
                "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                "audio_fact": str(unit.get("audio_fact") or "").strip(),
                "product_visible": normalize_bool_flag(unit.get("product_visible")),
                "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                "third_party_endorsement": normalize_bool_flag(unit.get("third_party_endorsement")),
            }
        )
    validate_single_video_facts(role, normalized, analysis)
    return normalized


def normalized_fact_id(value: Any, code: str, index: int) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(rf"{code}[A-Z0-9_]*\d*", text) else f"{code}{index}"


def validate_single_video_facts(role: str, facts: dict[str, Any], analysis: dict[str, Any]) -> None:
    """单视频 fact 校验：拒绝跨视频串证据；缺失 information 直接 fail。"""
    info = analysis.get("videos", {}).get(role, {})
    transcript = normalized_transcript_text(read_transcript_text(info))
    other_role = "creator" if role == "benchmark" else "benchmark"
    other_transcript = normalized_transcript_text(read_transcript_text(analysis.get("videos", {}).get(other_role, {})))
    for unit in facts.get("evidence_units", []):
        quote = str(unit.get("voiceover") or "").strip()
        normalized_quote = normalized_transcript_text(quote)
        if quote and len(normalized_quote) >= 12 and normalized_quote not in transcript:
            if normalized_quote in other_transcript:
                raise SystemExit(f"{role} fact {unit.get('id')} voiceover is from {other_role} transcript.")
            subtitle = str(unit.get("subtitle_fact") or "").strip()
            if quote not in subtitle:
                unit["subtitle_fact"] = f"{subtitle}；{quote}".strip("；")
            unit["voiceover"] = ""
            unit["voiceover_zh"] = ""
        if not str(unit.get("information") or "").strip():
            raise SystemExit(f"{role} fact {unit.get('id')} missing information.")


# ---------------------------------------------------------------------------
# 共享工具：被 parse 和 postprocess 都需要
# ---------------------------------------------------------------------------

def is_effective_voiceover(value: Any) -> bool:
    """判断字符串是否是有效口播（排除音乐占位、空字符串、显式无效声明）。"""
    text = str(value or "").strip().lower()
    if not text or text in {"*outro music*", "[music]", "(music)", "music", "（音乐渐弱）"}:
        return False
    if "无有效口播" in text or ("音乐" in text and "环境声" in text):
        return False
    return True


def read_transcript_text(info: dict[str, Any]) -> str:
    path = Path(str(info.get("transcript_path") or Path(str(info.get("work_dir") or "")) / "transcript.txt"))
    return path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""


def normalized_transcript_text(value: str) -> str:
    """把转写文本归一为只包含字母数字的小写形式，用于跨视频字符串比对。"""
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)
