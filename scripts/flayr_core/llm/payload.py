"""flayr_core.llm.payload：LLM 请求 payload 构造。

每个 build_*_payload 都返回已批准供应商使用的 chat completions 请求体。
不调用 LLM、不解析响应，纯粹组装文本 + 图片 + system prompt。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    parse_timestamp_seconds,
    resolve_artifact_path,
)
from ..proposition_contract import build_product_proposition_contract
from ..market import render_market_knowledge
from ..shot_track import render_shot_track_markdown
from ..speech_mode import speech_mode_prompt
from ..stage_evidence_contracts import (
    STAGE1_OBSERVATION_CONTRACT_VERSION,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE1_QUALIFICATION_GROUPS,
    qualified_stage_evidence_ids,
    stage_evidence_contract_prompt,
    stage_evidence_contract,
    stage_analysis_evidence_view,
    stage1_qualification_projection,
    stage_codes,
)
from ..structure_modules import stage1_event_catalog
from ..stage_ownership import (
    CERTIFICATION_OWNERSHIP_PROMPT,
    contains_certification,
)
from ..subtitle_track import render_subtitle_track_markdown
from ..transcript import (
    load_transcript_words,
    transcript_text_for_range,
    transcript_timing_contract,
)
from ..resources import ResourceBudget
from .api import (
    audio_to_mp3_data_url,
    can_analyze_native_audio,
    can_analyze_native_video,
    can_send_standalone_audio,
    video_to_data_url,
)
from .phase_c_payload import (
    PHASE_C_REVIEW_FPS,
    PHASE_C_REVIEW_MAX_WIDTH,
    PHASE_C_WINDOW_PADDING_SECONDS,
    build_stage_review_payload,
    build_stage_review_video_inputs,
    normalize_stage_codes,
    stage_code,
    stage_review_media_windows,
)
from .full_analysis_payload import (
    GENERIC_FULL_ANALYSIS_OUTPUT_BUDGET,
    QWEN36_PLUS_FULL_ANALYSIS_OUTPUT_BUDGET,
    QWEN36_PLUS_MODEL_PREFIX,
    _uses_qwen36_plus_completion_budget,
    build_llm_payload,
    build_llm_repair_payload,
    full_analysis_output_budget,
    full_analysis_output_fields,
)

ROOT = Path(__file__).resolve().parents[3]
MIN_NATIVE_VIDEO_WINDOW_SECONDS = 2.0


def _certification_units_for_target_stages(
    units: list[dict[str, Any]],
    target_stages: list[str],
) -> list[dict[str, Any]]:
    """Keep certification policy scoped to functions owned by this request."""
    target = {str(stage).strip().upper() for stage in target_stages}
    selected: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        functions = unit.get("functions")
        if not isinstance(functions, list):
            continue
        if any(
            str(function).strip().upper().partition("_")[0] in target
            for function in functions
        ):
            selected.append(unit)
    return selected


def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip() if path.is_file() else "（缺失）"


def _video_evidence_path(info: dict[str, Any], key: str) -> Path:
    evidence = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    raw = str(evidence.get(key) or "").strip()
    path = resolve_artifact_path(info, raw, require_file=True)
    return path if path is not None else Path(f"__missing_{key}__")


def read_track_markdown(track_path: Path, renderer: Any, disabled_hint: str) -> str:
    """读取预处理轨 json 并渲染成 markdown；文件不存在时返回提示（未启用/未生成）。"""
    if not track_path.is_file():
        return disabled_hint
    try:
        track = json.loads(track_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return disabled_hint
    return renderer(track)


# ---------------------------------------------------------------------------
# Payload 构造
# ---------------------------------------------------------------------------

def observation_method_view() -> str:
    """从 observation-guide.md 抽"观察方法视图"——§一整片观察 + §二抽帧框架 + §三四轨，供阶段1
    事实抽取逐维观察（单一来源，消灭内联副本）。丢 §0 宪法（阶段1 不归类）、§四 BGM→severity 与
    §五 失误清单的判断；但 §四/§五 的输入事实（BGM 在场/类型、画中画小窗、遮挡、全片覆盖、口播对齐）
    已落在 §一-§三、不随判断一起丢（删判断留输入事实，同'演示即证据'）。"""
    path = ROOT / "references" / "observation-guide.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"(## 一、.*?)(?=\n## 四、)", text, flags=re.S)
    return m.group(1).strip() if m else ""


def _proof_contract_field_roles_prompt() -> str:
    """把 Step-0 证明合同的字段职责固定成模型可直接执行的边界。"""
    return "\n".join(
        [
            "## proof_contract 字段职责硬边界（必须逐字段遵守）",
            "observable_dimension 只写一个名词性、可复核的测量轴；它回答‘测量哪一个最终结果’，不能写动作链、过程顺序、拍摄条件，也不能把同一对象的不同属性并列进去。",
            "过程动作写 observable_signal：它记录这个维度在画面/记录中实际发生的状态变化或过程事件；多个过程动作可以共同证明同一个维度，但必须全部留在 signal。",
            "proof_condition 只写让证据可信的拍摄/记录条件，例如同光线、固定机位、近景或完整记录；不要把这些条件写进 signal。",
            "字段职责示例（只示范形状，不要求所有产品照抄文字）：",
            "- 正确：observable_dimension=‘刷头卫生状态’；observable_signal=‘旧刷头无需手触被新刷头替换，使用后直接丢弃’。两个动作共同证明一个卫生状态，仍是一个维度。",
            "- 错误：observable_dimension=‘刷头替换与丢弃的卫生状态’。这是把过程动作塞进维度；应保留单一维度‘刷头卫生状态’，把动作移到 observable_signal。",
            "- 错误：observable_dimension=‘刷头卫生状态与更换便捷性’。同一个物理对象的不同属性仍是两个测量轴，不能靠同一对象或一句话把它们合并。",
            "- 错误：observable_signal=‘同一光线近景拍摄’。这是 proof_condition，不是观察到的信号。",
            "如果存在多个独立的最终结果，应拆为不同 candidate/证明点；不要在一个 observable_dimension 中用‘与/及/同时/+’拼接。",
        ]
    )


def build_product_foundation_payload(model: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Step-0 品的商业地基：看视频前，据产品事实 + 品类世界知识确立 category_profile(特征) +
    product_profile(命题)，作为下游 S1-S6 判断的独立尺子。纯文本不附视频——地基独立于任一条
    视频，避免'阶段2 现编标尺又当场自评'的循环。运营未给的字段用品类世界知识补全。"""
    p = analysis.get("product") or {}
    market_knowledge = render_market_knowledge(str(p.get("target_market") or "auto"))
    brand = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    brand_hint = ""
    if brand:
        props = " / ".join(str(item) for item in brand.get("propositions") or [] if str(item).strip())
        pains = " / ".join(str(item) for item in brand.get("painpoints") or [] if str(item).strip())
        brand_hint = "\n".join(
            [
                "## 人工冻结命题（高优先级）",
                "以下命题来自人工策展，优先级高于你对品牌名/型号的世界知识猜测。若产品名与人工命题冲突，以人工命题为准。",
                f"- propositions：{props or '无'}",
                f"- painpoints：{pains or '无'}",
            ]
        )
    text = "\n\n".join(
        [
            "# 品的商业地基确立（Step-0，先于看视频）",
            "你是带货短视频分析系统的产品分析师。在任何视频分析之前，先根据产品信息 + 你的品类世界知识，"
            "确立这个产品的商业地基（特征 category_profile + 命题 product_profile），作为后续 S1-S6 判断的尺子。"
            "只分析产品本身，不涉及任何视频。运营未给的字段用品类世界知识补全。",
            "## 产品信息（运营给定）",
            f"- 产品名：{p.get('name') or '未填写'}",
            f"- 品类：{p.get('category') or '未填写'}",
            f"- 价格：{p.get('price') or '未填写（按品类+型号判市场档位 low/mid/high）'}",
            f"- 核心卖点：{p.get('core_selling_points') or '未填写（按品类世界知识推该品最该主打的卖点）'}",
            f"- 运营确认的视频主卖点：{p.get('primary_selling_point') or '未指定（由模型按可视展示空间、功能中心性和理解成本选择）'}",
            f"- 目标用户/痛点：{p.get('target_user') or '未填写（按品类推目标人群与核心痛点）'}",
            f"- 购买动机：{p.get('purchase_motivation') or '未填写（按品类推）'}",
            f"- 目标市场：{p.get('target_market') or 'auto'}",
            f"- 备注：{p.get('notes') or '无'}",
            brand_hint,
            "## 目标市场知识（仅用于产品地基本地化，不得替代产品事实）",
            market_knowledge,
            "## 输出严格 JSON（两个对象）",
            "category_profile（品类特征，只报事实+世界知识，不做权重判断）：category_name、price_tier(low|mid|high)、"
            "decision_threshold(impulse 冲动可买|considered 需被说服)、drive_type(emotional|functional|mixed)、"
            "painpoints（该品类目标消费者最在意的决策因素，每词中文+本地语放同一数组，6-16 个）。",
            "product_profile（产品商业 DNA，S1-S6 打分的尺子）：visualizable(yes|no 核心价值能否视觉化；no 只表示 S4 的视觉证明权重可转向信任与可信度分析，不表示 S5 必须出现、达人必须提供背书，S5 是否进入比较仍由双侧 Stage1 实际事实决定）、"
            "physical_task（解决的最直观尴尬）、hook_proposition（S1 钩子命题，类型取决于本品、不限痛点——"
            "可痛点/承诺/反差/情绪/向往/视觉吸引/身份代入/场景还原，见 structure_library S1 七型）、"
            "core_selling_points（S3 主轴：使用过程要演示传递的核心卖点，1-6 个）、"
            "usage_context（S3 场景层：本品典型使用场景=卖点演示的舞台）、"
            "short_video_proof_plan（短视频卖点证明计划，先列全候选卖点，再决定各自最适合在哪一阶段传递；"
            "candidates 数组 1-6 项，每项必须含 id、selling_point、visual_space(high|medium|low)、"
            "functional_centrality(high|medium|low)、comprehension_cost(low|medium|high)、delivery_stage(S2|S3|S4|S5)、"
            "proof_mode（仅 delivery_stage=S4 时填 instant_visual|process_result|sensory_proxy|aesthetic_value|social_reaction|long_term_record|trust_substituted|low_decision_light_proof，其他阶段留空）、reason；"
            "primary_candidate_id=整条短视频商业主路线对应的 candidate id；运营给出视频主卖点时必须对应它，否则按可视展示空间、功能中心性、理解成本选全体候选最高项；"
            "s4_anchor_candidate_id=选中的单一 S4 candidate id（只服务 S4 效果测量，没有适合 S4 的候选则留空）；"
            "selection_source=model_category_default|operator_priority|curated_priority（没有运营明确排序时只能填 model_category_default）；"
            "anchor_confidence=high|low。选择 S4 anchor 的固定顺序：①可视展示空间最高；②若同级，产品主要功能中心性最高；③仍同级，普通用户理解成本最低。"
            "这不是删掉其他卖点：不可直接视觉化但重要的卖点应按信息/使用/信任价值分流到 S2/S3/S5，不能为了凑 S4 伪造视觉锚点。"
            "重要 JSON 层级：short_video_proof_plan 在 product_profile 下是一个到 candidates/primary_candidate_id/s4_anchor_candidate_id/selection_source/anchor_confidence 为止的独立对象；"
            "proof_contract、core_visual_proposition、visual_proof_points、proof_mode 等都必须与 short_video_proof_plan 同级，严禁嵌入 short_video_proof_plan 内。"
            "proof_contract（只消费 short_video_proof_plan 已选的 S4 anchor；必须含 anchor_candidate_id，必须等于 s4_anchor_candidate_id；"
            "必须先选 mode，再填各字段：mode=instant_visual|process_result|sensory_proxy|"
            "aesthetic_value|social_reaction|long_term_record|trust_substituted|low_decision_light_proof；"
            "consumer_outcome=这个 S4 anchor 要证明的一个消费者结果，不能直接照抄卖点词；允许用自然语言完整描述同一结果，但不得把多个独立卖点列成清单；"
            "signal_type 必须与 mode 一一匹配：instant_visual=state_change，process_result=state_change|process_event，"
            "sensory_proxy=sensory_response，aesthetic_value=aesthetic_appeal，social_reaction=social_response，"
            "long_term_record=long_term_record，trust_substituted=trust_evidence，low_decision_light_proof=light_proof；"
            "observable_dimension=一个简短、可复核的维度名（如色彩覆盖度），这是单一主证明的硬边界，严禁并列多个卖点或维度；"
            "observable_signal=该维度在画面/记录中实际发生的状态变化；产品的其他卖点仍保留在 short_video_proof_plan 的其它 candidate，不是被删除；before_state/after_state=仅 direct visual（instant_visual/process_result）必填的两种不同状态；"
            "proof_condition=使信号可信的拍摄/记录条件。拍摄条件不能写进 observable_signal 或 before/after。",
            _proof_contract_field_roles_prompt(),
            "结构库约束：S4-A~F 的直接效果模块对保健品均排除；保健品不得把气色/体感变化伪装成直接视觉 state_change，"
            "应选 trust_substituted 或 long_term_record，并把认证/记录留给 S5 或对应记录证据。"
            "core_visual_proposition（旧兼容字段；S4 决定性视觉瞬间=选中 anchor 的到位效果标准，按本品现推、别套通用 before/after）、"
            "visual_proof_points（S4 多视觉证明点，数组 1-4 个；每项含 priority(primary|secondary)、proof_target、"
            "visual_standard、visual_diff_dimensions、related_selling_points。primary 必须是消费者最核心的效果证明；"
            "secondary 是附加卖点证明，不能压过 primary。primary 必须只证明一个消费者最终结果，不得用'与/及/同时/+'把多个卖点焊成一个 all-of 条件；"
            "若一个产品有清洁结果、刷头溶解、免接触、收纳等多个可视卖点，必须拆成 1 个 primary + 若干 secondary。"
            "例：一次性马桶刷 primary=清洁结果可见，secondary=刷头抛弃/溶解/免接触卫生）、"
            "proof_mode（S4 价值证明模式：instant_visual|process_result|sensory_proxy|aesthetic_value|social_reaction|long_term_record|trust_substituted|low_decision_light_proof）、"
            "effect_requires_process（效果是否必须依赖使用过程证明：true|false|partial）、"
            "visual_diff_dimensions（before/after 应变化的视觉维度，1-3 个）、"
            "trust_multipliers（建立专业度/信任的元素，3-6 个）、shooting_requirement（卖点显现所需拍摄条件）、"
            "confidence(high|low，小众或本地新奇特品标 low)。",
            "proof_contract 是选中 S4 anchor 的权威合同：后续 visual_proof_points.primary 必须由它生成。若 mode 是直接视觉，primary 必须等于 consumer_outcome，"
            "visual_standard 必须等于 before_state vs after_state，visual_diff_dimensions 必须等于 observable_signal；"
            "若 mode 非直接视觉，不得输出 visual_proof_points.primary 来冒充 before/after。"
            "只报产品事实与品类世界知识，不臆造具体功效数据/检测数字/价格优惠。",
        ]
    )
    system_prompt = (
        "你是产品商业分析师。只输出严格 JSON（含 category_profile 与 product_profile 两个对象），不要 Markdown。"
        "基于产品事实 + 品类世界知识确立商业地基，运营未给的字段据品类世界知识补全，不臆造具体功效数据。"
        "proof_contract 中 observable_dimension 只能是一个测量轴；过程动作写 observable_signal，拍摄条件写 proof_condition。"
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
    }

def build_comparison_eligibility_payload(model: str, facts: dict[str, Any]) -> dict[str, Any]:
    """根据已锁定的双侧产品身份，判定商品关系、替代关系与逐阶段可比性。"""
    comparison_facts = {
        role: _compact_comparison_facts(facts.get(role))
        for role in ("benchmark", "creator")
    }
    text = (
        "你是短视频商品关系与阶段可比性判定器。只根据以下两条视频已锁定的产品身份与阶段事实，禁止补充视频中没有的事实。\n"
        "先判商品身份，再判替代关系，最后逐阶段判可比性。包装、颜色、色号、容量、套装和外壳只属于 variant_attributes；"
        "不得从包装颜色、盒子圆方或外观差异直接推断为不同产品。只有产品线、功能形态、核心任务或使用机制有证据发生变化，才可判不同产品。\n"
        "identity_relation：exact_product=完全同款；same_product_family=同一产品线的包装/颜色/色号/容量/SKU 变体；"
        "different_product=产品线或功能形态确实不同；uncertain=证据不足。\n"
        "不同产品再判 substitution_relation：strong_substitute 必须同时满足同一消费者任务、同一作用对象、同一目标结果、"
        "同一次购买决策可二选一，并且不是互补品/上下游步骤；partial_substitute=只共享部分任务或结果；none=任务或结果不同。"
        "使用机制不同不妨碍强替代，例如防水胶与防水胶带都用于同一裂缝止漏任务。\n"
        "evidence_units 这里只是 ID/时间索引；每个阶段的完整观察只能从 stage_evidence_units[S1-S6] 读取。"
        "阶段结论只能使用对应 stage 的 qualified_stage_evidence_ids，不能把其他阶段的内容横向借用。"
        "阶段资格为空、unknown 或 conflict 时必须输出 uncertain/not_comparable，不能把证据未采集解释为 absent。\n"
        "stage_eligibility 对 S1-S6 每个阶段输出 status=direct|structural|not_applicable|not_comparable：同品家族全部 direct；"
        "强/部分替代只能 structural 或不比较。S1 需共享目标用户/痛点/购买任务；S2 需共享问题-解决方案角色；"
        "S3 需共享使用任务，只比较过程表达完整度而非机制天然优劣；S4 需共享目标结果和可观察证明维度；"
        "S5 按 structure_library_full.md 定义为可选的‘信任放大’：结构库的跳过条件只指导编排，不是事实先验；不根据品类先验判断达人是否必须做背书，只根据两侧 Stage1 的实际事实。"
        "双方 Stage1 覆盖完整且 S5 都是 absent 时才填 not_applicable；这只表示两条视频都没有使用 S5，不表示品类不需要信任，也不构成达人错误。"
        "一侧 present、另一侧 absent 时仍保持 direct/structural 并比较，标杆有真实背书而达人没有就是有效差距，不能用‘本品不需要背书’否定标杆事实。"
        "任一侧 unknown、conflict 或覆盖未完成时不得关闭 S5，保留比较范围并让下游证据 gate 阻断。"
        "S6 需共享购买场景，只比较 CTA 完成度，"
        "不直接比较不同规格的绝对价格。无替代或身份不确定时全部 not_comparable。\n"
        "输出严格 JSON：{\"identity_relation\":\"exact_product|same_product_family|different_product|uncertain\","
        "\"substitution_relation\":\"same_solution|strong_substitute|partial_substitute|none|uncertain\","
        "\"shared_job\":{\"same_consumer_job\":bool,\"same_target_object\":bool,\"same_desired_outcome\":bool,"
        "\"same_purchase_decision\":bool,\"complement_or_dependency\":bool,\"reason\":\"...\",\"evidence_ids\":[]},"
        "\"stage_eligibility\":{\"S1\":{\"status\":\"...\",\"basis\":\"...\",\"shared_contract\":\"...\","
        "\"restrictions\":[],\"evidence_ids\":[]},\"S2\":{},\"S3\":{},\"S4\":{},\"S5\":{},\"S6\":{}},"
        "\"reason\":\"一句话\",\"evidence_ids\":[],\"confidence\":\"high|medium|low\"}。\n\n"
        "已锁定的产品身份与阶段事实：\n"
        + json.dumps(comparison_facts, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "只输出严格 JSON，不要 Markdown，不要推测。"},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
    }


def _compact_comparison_facts(value: Any) -> dict[str, Any]:
    """保留阶段资格所需的事实，去掉帧、音频与冗长审计字段。"""
    source_value = value if isinstance(value, dict) else {}
    active_contract = source_value.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
    value = stage_analysis_evidence_view(source_value)
    value = value if isinstance(value, dict) else {}
    units = []
    for unit in value.get("evidence_units", []):
        if not isinstance(unit, dict):
            continue
        compact_unit = {
            "id": unit.get("id"),
            "time_range": unit.get("time_range"),
            "evidence_strength": unit.get("evidence_strength"),
            "qualified_stages": unit.get("qualified_stages") or [],
        }
        if not active_contract:
            # Legacy comparison runs predate the stage qualification contract.
            # Preserve their old fact-summary compatibility instead of making
            # an old result silently look empty.  Active runs remain closed
            # world and expose only the ID/time index here.
            for key in ("information", "voiceover_zh", "visual_fact", "subtitle_fact"):
                if key in unit:
                    compact_unit[key] = unit.get(key)
        units.append(compact_unit)
    stage_units: dict[str, list[dict[str, Any]]] = {}
    for stage, stage_items in (value.get("stage_evidence_units") or {}).items():
        compact_items: list[dict[str, Any]] = []
        for unit in stage_items or []:
            if not isinstance(unit, dict):
                continue
            compact_items.append(
                {
                    "id": unit.get("id"),
                    "time_range": unit.get("time_range"),
                    "information": unit.get("information"),
                    "voiceover_zh": unit.get("voiceover_zh"),
                    "visual_fact": unit.get("visual_fact"),
                    "subtitle_fact": unit.get("subtitle_fact"),
                    "audio_fact": unit.get("audio_fact"),
                    "visual_evidence": unit.get("visual_evidence"),
                    "evidence_strength": unit.get("evidence_strength"),
                    "fact_quality": unit.get("fact_quality"),
                    "trust_source_type": unit.get("trust_source_type"),
                }
            )
        stage_units[str(stage)] = compact_items
    qualified = {
        # Recompute against the authoritative source, not the view's compact
        # ID/time index.  Channel qualification (for example S4 visual proof)
        # needs the full locked unit fields that the analysis view intentionally
        # withholds from its flat compatibility index.
        stage: sorted(qualified_stage_evidence_ids(source_value, stage))
        for stage in stage_codes()
    }
    return {
        "product_identity": value.get("product_identity") or {},
        "content_summary": value.get("content_summary") or "",
        "evidence_units": units,
        "stage_evidence_units": stage_units,
        "structure_event_checks": value.get("structure_event_checks") or [],
        "stage_evidence_contract_version": value.get("stage_evidence_contract_version"),
        "stage_evidence_checks": value.get("stage_evidence_checks") or [],
        "qualified_stage_evidence_ids": qualified,
        "evidence_checklist": value.get("evidence_checklist") or [],
    }


def build_product_foundation_repair_payload(
    model: str,
    analysis: dict[str, Any],
    rejected_profile: dict[str, Any],
    validation_reason: str,
) -> dict[str, Any]:
    """Step-0 证明合同违规时，只重答产品地基，不让错误合同进入阶段判断。"""
    payload = build_product_foundation_payload(model, analysis)
    content = payload["messages"][1]["content"]
    content[0]["text"] += (
        "\n\n## 上次输出被拒绝，必须重答\n"
        f"proof_contract 校验失败：{validation_reason}。\n"
        "这不是同义词替换问题，而是字段职责问题。必须把 observable_dimension、observable_signal、proof_condition 重新分工；不要只把 dimension 中的‘替换’改成‘交接’。\n"
        "必须仍输出完整且合法的 category_profile/product_profile JSON，但保留被拒绝 profile 中与本次错误无关的有效字段；只修 proof_contract 及其直接派生的 visual_proof_points，不重做产品命题、不新增第二个 proof_contract。\n"
        + _proof_contract_field_roles_prompt()
        + "\n针对本次错误，若 dimension 把过程写成‘刷头替换与丢弃的卫生状态’，应改为单一维度‘刷头卫生状态’，并把‘旧刷头无需手触被新刷头替换，使用后直接丢弃’放入 observable_signal；这是字段转换示例，不是要求所有产品使用刷头文字。\n"
        "先确认 mode/signal_type 与选中的 S4 anchor 一致，再输出 proof_contract；直接视觉模式仍必须提供不同的 before_state/after_state 和 proof_condition。\n"
        "被拒绝的 product_profile：\n"
        + json.dumps(rejected_profile, ensure_ascii=False, indent=2)
    )
    return payload


def build_improvement_reconciliation_payload(
    model: str,
    result: dict[str, Any],
    missing_stage_codes: list[str],
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """最终 severity 已确定后，只补齐遗漏的大差距提升点，不重新判断阶段。"""
    wanted = {str(code).strip().upper() for code in missing_stage_codes}
    stages = [
        stage
        for stage in result.get("stage_analysis", [])
        if isinstance(stage, dict) and str(stage.get("stage") or "").strip().upper()[:2] in wanted
    ]
    evidence: dict[str, list[dict[str, Any]]] = {}
    understanding = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    for role in ("creator", "benchmark"):
        referenced: set[str] = set()
        role_facts = understanding.get(role) if isinstance(understanding.get(role), dict) else {}
        for stage in stages:
            stage_code = str(stage.get("stage") or "").strip().upper()[:2]
            stage_refs = {
                str(value)
                for value in stage.get(f"{role}_evidence_ids", [])
                if str(value).strip()
            }
            suffix = "hook" if stage_code == "S1" else stage_code.lower()
            flag = stage.get(f"{role}_{suffix}")
            if isinstance(flag, dict):
                stage_refs.update(
                    str(value)
                    for value in flag.get("evidence_ids", [])
                    if str(value).strip()
                )
            if role_facts.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
                stage_refs &= qualified_stage_evidence_ids(role_facts, stage_code)
            referenced.update(stage_refs)
        units = ((understanding.get(role) or {}).get("evidence_units") or []) if isinstance(understanding.get(role), dict) else []
        evidence[role] = [unit for unit in units if isinstance(unit, dict) and str(unit.get("id")) in referenced]

    context = {
        "product": (analysis or {}).get("product") or {},
        "product_profile": result.get("product_profile") or {},
        "missing_large_stages": sorted(wanted),
        "final_stage_analysis": stages,
        "referenced_evidence_units": evidence,
        "existing_improvements": [
            {"target_stage": item.get("target_stage"), "title": item.get("title")}
            for item in result.get("improvements", [])
            if isinstance(item, dict)
        ],
    }
    fields = (
        "title,target_stage,problem,suggestion,actions,gmv_reason,gmv_impact,creator_script,"
        "creator_script_zh,base_frame_suitability,base_frame_reason,expected_effect"
    )
    prompt = (
        "最终确定性 severity 已完成，但部分 large 阶段没有对应 Top 提升点。"
        "你只补缺失阶段的 improvements，不得修改或重判 stage_analysis，也不得重复已有提升点。\n"
        "每个缺失阶段输出一项，target_stage 必须来自 missing_large_stages。"
        "建议必须解决该阶段 flags 暴露的真实缺口，并围绕本品命题；参考标杆的功能意图，不能照抄标杆话术。"
        "所有事实、时间和 evidence id 只能来自输入；creator_script 使用达人视频的本地语言，creator_script_zh 给中文。"
        "若某个目标阶段没有 Stage1 资格化证据，输入中的该侧 referenced_evidence_units 为空；不得用其他阶段或未资格化单元补写。"
        "若达人本人或素材条件不适合改造参考，明确写 base_frame_suitability=no_suitable_frame，不得伪造画面。\n"
        f"每项只能由模型填写这些 prose 字段：{fields}。gap_type、时间范围、evidence ID、evidence、priority 等机械字段由代码从已锁定阶段结果投影，模型不得输出或修改。\n"
        "只输出严格 JSON：{\"improvements\":[...]}。\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是带货短视频改进提案补全器。只补最终大差距对应建议，严格输出 JSON。"},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
    }


def build_video_fact_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    visual_inputs: list[dict[str, Any]],
    api_url: str = "",
    budget: ResourceBudget | None = None,
    include_standalone_audio: bool = True,
) -> dict[str, Any]:
    """单视频事实抽取请求 payload。

    Stage1-A 的权威主输入固定为代码选出的 canonical frames、时间线、
    ASR 和 OCR。原生视频只允许在 Stage1-C/Phase C 的定向窗口中出现，
    不能作为首次事实抽取的隐式全片复扫。
    """
    info = analysis.get("videos", {}).get(role, {})
    code = "B" if role == "benchmark" else "C"
    role_dir = Path(str(info.get("work_dir") or ""))
    mode_prompt = speech_mode_prompt(info.get("speech_mode") if isinstance(info.get("speech_mode"), dict) else {})
    direct_audio_supported = (
        include_standalone_audio
        and can_analyze_native_audio(api_url, model)
        and can_send_standalone_audio(api_url, model)
    )
    audio_data_url = (
        audio_to_mp3_data_url(role_dir / "audio.wav", budget=budget)
        if direct_audio_supported
        else None
    )

    visual_source_hint = "随请求附带本视频的 canonical frames 和 Hook/CTA 时间线证据图；不附带整支原生视频。"
    audio_capability_rule = (
        "你可直接感知音轨；语气、BGM和音效只记录客观观察，不判断其商业贡献。"
        if audio_data_url is not None
        else "你不能直接感知音轨。口播语义只以转录文本为准；audio_fact 必须写未直接感知音轨，不得判断语气、BGM或音效。"
    )

    # 品地基命题注入（Step-0 产出）：告诉事实抽取器该重点盯哪些证据，只导观察不下结论；无地基则退回通用抽取。
    fnd = (analysis.get("product_foundation") or {}).get("product_profile") or {}
    frozen_brand = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    obs_hint = ""
    if fnd:
        csp = "、".join(fnd.get("core_selling_points") or []) or "（无）"
        vdd = "、".join(fnd.get("visual_diff_dimensions") or []) or "（无）"
        proof_points = []
        for point in fnd.get("visual_proof_points") or []:
            if not isinstance(point, dict):
                continue
            proof_points.append(
                f"{point.get('priority') or 'secondary'}:{point.get('proof_target') or ''}→{point.get('visual_standard') or ''}"
            )
        proof_points_text = "；".join(proof_points) or "（无）"
        proof_plan = fnd.get("short_video_proof_plan") if isinstance(fnd.get("short_video_proof_plan"), dict) else {}
        proof_candidates = proof_plan.get("candidates") if isinstance(proof_plan.get("candidates"), list) else []
        proof_plan_text = "；".join(
            f"{item.get('id') or '?'}:{item.get('selling_point') or ''}→{item.get('delivery_stage') or '?'}"
            for item in proof_candidates
            if isinstance(item, dict)
        ) or "（无）"
        obs_hint = "\n".join(
            [
                "## 本品重点观察线索（据产品地基，帮你定位该盯什么；只记客观证据、不下结论）",
                f"- 短视频卖点分流：{proof_plan_text}；S4 选中锚点={proof_plan.get('s4_anchor_candidate_id') or '（无）'}。",
                f"- S4 多视觉证明点：{proof_points_text}——primary 是核心效果证明，secondary 是附加卖点证明，观察时都记，但不要互相替代。",
                f"- 旧兼容核心视觉命题：{fnd.get('core_visual_proposition') or '（无）'}——无多证明点时用它辅助定位决定性瞬间。",
                f"- before/after 应变化的视觉维度：{vdd}——重点观察这些维度的画面证据。",
                f"- 核心卖点：{csp}——留意使用过程中这些卖点有没有被动作演示出来。",
                f"- 典型使用场景：{fnd.get('usage_context') or '（无）'}。",
                "命题相关证据尤其别漏；但不要为凑命题臆造没拍到的东西。",
            ]
        )
    frozen_propositions = [str(item).strip() for item in frozen_brand.get("propositions") or [] if str(item).strip()]
    frozen_painpoints = [str(item).strip() for item in frozen_brand.get("painpoints") or [] if str(item).strip()]
    observation_checklist = ""
    if frozen_propositions or frozen_painpoints:
        items = [("proposition", value) for value in frozen_propositions] + [("painpoint", value) for value in frozen_painpoints]
        observation_checklist = "\n".join(
            [
                "## 冻结品观察清单（逐项核对，不是评分）",
                "下列是运营/人工冻结的品命题和痛点。先完成自由观察，再逐项回答是否有直接画面、口播或字幕证据；未出现必须明确写未覆盖，不能省略。",
                *[f"- {kind}: {value}" for kind, value in items],
                "输出时在 evidence_checklist 中逐项给出 item、covered、evidence_ids、channels；covered=false 时 evidence_ids=[]。",
            ]
        )

    event_catalog_text = "\n".join(
        f"- {item['id']} [{item['priority']}] {item['event']}：{item['signals']}"
        for item in stage1_event_catalog()
    )
    text = "\n\n".join(
        [
            f"# 单视频事实抽取：{role}",
            "",
            f"- 产品：{analysis.get('product', {}).get('name') or '未填写'}",
            f"- 原视频：{info.get('path') or ''}",
            f"- 时长：{format_seconds(info.get('duration_seconds'))}",
            f"- 证据组织模式：{mode_prompt}",
            "",
            "## 观察方法（看视频按以下全部维度逐项观察，不漏项——这是唯一的观察方法来源）",
            observation_method_view(),
            obs_hint,
            observation_checklist,
            "## 结构库事件目录（观察提示，不是阶段评分）\n"
            "用目录帮助覆盖不同类型的原子事实，但不要输出阶段资格或 present/absent 结论；"
            "未观察到的事项不要伪装成明确不存在，留给 Stage1-B 结合覆盖状态判断。\n"
            + event_catalog_text,
            "## Stage1-A 原子事实合同\n"
            "本请求只负责观察当前视频：按时间记录 evidence_units、可观察的结构事件和覆盖清单。"
            "不要判断 S1-S6 是否成立，不要把 evidence unit 归入阶段，不要输出 stage_evidence_checks。"
            "阶段归属、资格、required signal 绑定和明确不存在将在后续独立的 Stage1-B 请求中完成；"
            "functions 只能作为原子事实的功能标签，不能替代 Stage1-B 资格。"
            "每个 evidence_unit 必须填写 fact_quality 的六个观察轴：subject、visibility、composition、completion、proof、causal_link。"
            "这些字段只描述这条事实看得是否清楚、是否是直接对比/结果/主张以及是否有因果连接，不是阶段资格或 severity；"
            "evidence_unit 采用稀疏合同：只输出当前单元真实适用的可选字段，不得为每条事实重复展开空数组、空对象或空字符串；"
            "voiceover 和 voiceover_zh 由代码根据词级 ASR 时间窗生成，模型不得抄写或输出；"
            "information 只用一句简短摘要记录该单元传递的信息及声画同步/提前/滞后关系，不得复述口播全文；"
            "字段职责必须分开：completion 只记录关键动作过程是否完整可见（complete=关键动作从开始到结束可见，partial=只见部分动作，none=没有可见动作过程）；"
            "proof 只记录结果证明形态（direct_comparison=画面直接呈现对照/控制与差异，result_only=只看到结果但没看到产品如何造成结果，"
            "claim_only=只有口播或字幕声称且没有可见结果，none=没有结果证明）；"
            "causal_link 只记录动作与结果的可见连接（supported=连续画面足以归因，weak=有剪切或间接连接，unsupported=结果/主张无法归因到动作）。"
            "看到完整使用动作不等于 direct_comparison；看到结果也不等于 causal_link=supported；口播声称不得写成 result_only 或 direct_comparison。"
            "无法判断时填 uncertain 或 not_applicable，不要省略该对象。"
            "Stage1 输出严格禁止 severity、model_severity、gap、comparison、commercial_priority、recommendations、improvements、stage_analysis 和 stage_evidence_links；"
            "stage1_acquisition、stage1_qualification、evidence_set_* 和 stage1_recovery 是代码拥有的采集/冻结元数据，模型不得输出或覆盖；"
            "这些字段属于后续 Judgment/Resolution/Report，出现时必须拒绝，不得由代码静默丢弃。",
            "## 本地语言转写（语义参考；不提供精确窗口边界）",
            read_text_if_exists(role_dir / "transcript.txt"),
            "",
            "## 口播时间精度合同",
            json.dumps(transcript_timing_contract(info), ensure_ascii=False),
            "窗口内口播归因只能使用窗口安全口播时间线；没有词级时间戳时，跨窗口口播必须标记时间粒度不足，不得伪造精确归因。原始 transcript.srt 和词级索引不进入模型请求，只保留在本地审计产物。",
            "## 窗口安全口播时间线（阶段归因首选）",
            read_text_if_exists(_video_evidence_path(info, "transcript_windowed_path")),
            "",
            "## 中文翻译",
            read_text_if_exists(role_dir / "transcript.zh.txt"),
            "",
            "## 权威字幕轨（OCR 识别，字幕文本以此为准，胜过你自己认字）",
            read_track_markdown(
                role_dir / "subtitle_track.json",
                render_subtitle_track_markdown,
                "（未启用 OCR 字幕轨；字幕以你从画面识别为准）",
            ),
            "",
            "## 镜头切分轨（精确镜头边界，划分 S1-S6 阶段时参考它，别切在镜头中间）",
            read_track_markdown(
                role_dir / "shot_track.json",
                render_shot_track_markdown,
                "（未生成镜头轨）",
            ),
            "",
            "## 输出 JSON",
            json.dumps(
                {
                    "content_summary": "只概括这条视频，不比较另一条视频。",
                    "communication_strategy": "只描述这条视频的口播、字幕、画面、BGM如何配合推进。",
                    "product_identity": {
                        "brand_or_product_name": "只写这条视频里看见或听见/读到的品牌、产品名；无法确认留空。",
                        "product_category": "这条视频实际展示的品类；无法确认留空。",
                        "form_factor": "实际可见的产品形态/关键结构，如便携搅拌杯、慢速榨汁机、手持挂烫机；无法确认留空。",
                        "identity_basis": "visible|spoken|subtitle|mixed|unknown",
                        "confidence": "high|medium|low",
                    },
                    "selling_point_observations": [
                        {
                            "id": "SP1",
                            "candidate_id": "若能对应短视频卖点分流中的 candidate id 则填写；否则留空。",
                            "text": "本视频实际重点传递的卖点，不评价好坏。",
                            "visual_share": 0.6,
                            "speech_share": 0.4,
                            "proof_mode_observed": "实际采用的通用证明方式；无法确认写 unknown。",
                            "proof_signal_present": True,
                            "evidence_ids": [f"{code}1"],
                        }
                    ],
                    "variant_decision_rule": {
                        "speech_explains_choice": False,
                        "visual_comparison_present": False,
                        "reason": "只记录是否明确解释多个 SKU/变体怎么选或为何对比，不评价说服力。",
                        "evidence_ids": [],
                    },
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "attention_scan_audit": {
                        "recording_equipment_visible": False,
                        "foreground_non_task_object_visible": False,
                        "notes": "必须明确检查达人嘴边、手部和产品证明区域；只写看见的事实。",
                        "evidence_ids": [],
                    },
                    "attention_competitors": [
                        {
                            "id": "AC1",
                            "object_label": "持续抢占注意力但不参与产品任务的物体；没有则整个数组为空。",
                            "time_ranges": ["0.0s - 5.0s"],
                            "persistent_motion": True,
                            "high_salience": True,
                            "participates_in_product_task": False,
                            "occludes_proof_area": False,
                            "evidence_ids": [f"{code}1"],
                        }
                    ],
                    "evidence_units": [
                        {
                            "id": f"{code}1",
                            "time_range": "0.0s - 3.0s",
                            "information": "一句话概括该单元传递的信息及可见的声画关系，不复述口播。",
                            "visual_fact": "该时刻画面中实际可见的事实：主体、动作、表情变化、字幕叠字、特效。",
                            "subtitle_fact": "可读字幕；没有则留空。",
                            "evidence_strength": "direct|explicit|inferred|absent；只描述该证据单元自身的事实强度，不确定或缺失留空。",
                            "fact_quality": {
                                "subject": "correct|incorrect|uncertain|not_applicable",
                                "visibility": "clear|partial|obscured|uncertain|not_applicable",
                                "composition": "central|supporting|weak|uncertain|not_applicable",
                                "completion": "complete|partial|none|uncertain|not_applicable",
                                "proof": "direct_comparison|result_only|claim_only|none|uncertain|not_applicable",
                                "causal_link": "supported|weak|unsupported|uncertain|not_applicable",
                            },
                            "product_visible": True,
                            "product_coverage": "该时段产品在画面里的视觉占比：none｜low｜medium｜high。看不到产品写 none。",
                            "endorsement_verbal": False,
                            "endorsement_visual": False,
                            "functions": ["S3_usage", "S4_effect"],
                        }
                    ],
                    "evidence_checklist": [
                        {
                            "item": "必须逐字复用上方冻结品观察清单的一项；没有清单时输出 []。",
                            "covered": True,
                            "evidence_ids": [f"{code}1"],
                            "channels": ["visual|voiceover|subtitle"],
                        }
                    ],
                    "stage_evidence_contract_version": STAGE1_OBSERVATION_CONTRACT_VERSION,
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for item in visual_inputs:
        content.extend(
            [
                {"type": "text", "text": f"图片：{item['label']}，本地路径：{item['path']}"},
                {"type": "image_url", "image_url": {"url": item["data_url"], "detail": "low"}},
            ]
        )
    if audio_data_url is not None:
        content.append(
            {"type": "text", "text": "以下是本视频的完整音频，用于判断 BGM、口播语气、特殊音效。"}
        )
        content.append(
            {"type": "input_audio", "input_audio": {"data": audio_data_url, "format": "mp3"}}
        )

    system_prompt = (
        "你是单视频事实抽取器。只输出严格 JSON，不要 Markdown。"
        "只分析当前这一条视频，禁止引用、比较或猜测另一条视频。"
        f"{visual_source_hint}"
        f"{audio_capability_rule}"
        "严格按用户消息中『观察方法』一节的可用维度逐项观察、不漏项"
        "（含镜头语言/取景完整性、遮挡与 UI 危险区、画中画小窗、拍摄视角、口播与画面对齐、四轨对齐），"
        "必须先读取用户消息中的 speech_mode/证据组织模式，并按其证据优先级组织事实："
        "spoken 以口播时间线为骨架；subtitle_driven 以 OCR 字幕轨为文案骨架；visual_driven 以画面变化和镜头轨为骨架；"
        "music_driven 在可直接感知音轨时以画面变化、BGM/节奏/音效为骨架；否则只按画面变化组织。"
        "不得输出 voiceover 或 voiceover_zh，也不得把屏幕字幕、画面文案或你对画面的理解伪装成口播；"
        "代码会按 time_range 从词级 ASR 确定性绑定窗口安全口播。"
        "按带货短视频的天然结构（钩子→产品引出→使用过程→效果呈现→信任放大→促单）找证据切分 evidence_units，"
        "目标是完整抽出对分析带货视频有价值的原子事实，而非随意找转折点或为凑数量合并事实；"
        "不设固定条数上限，沿时间线排列，id 必须使用指定前缀；代码会根据实际响应是否被输出预算截断记录预算状态，不能用模型字段伪造或掩盖采集不完整，"
        "time_range 用真实时间（如 2.5s - 4.0s）。"
        "product_identity 必须只记录当前视频里实际看见、听见或读到的产品身份；声明产品名只作核对线索，"
        "不得因为输入声明是某品就把视频中看不出的品牌、品类或形态填成该品。"
        "把各维度观察到的画面事实记入 visual_fact；只有当前模型确实收到可感知音轨时，才输出 audio_fact。"
        "information 只写一句简短关系摘要；不得复制口播、字幕或 visual_fact 的全文。按实记录，不做评价；"
        "凡 functions 含 S3_usage 的证据，visual_fact 必须记录证据接收质量：使用对象/场景上下文是否足以理解产品作用对象、"
        "关键动作是否连续可追踪、核心卖点发生区域是否清楚可见、是否只有局部特写且缺少必要上下文。"
        "局部特写本身不是问题；只有当局部镜头让用户看不清产品作用对象、关键动作或证明区域时，才写证据接收不足。"
        "每条还要标 product_visible（该时段画面里能否看到产品本体，true/false）与 product_coverage"
        "（产品视觉占比 none｜low｜medium｜high，看不到写 none）：这两项用于确定性统计产品出镜，"
        "据画面如实标，产品被手遮住或只露局部按真实可见程度给 low；"
        "再标 evidence_strength（direct|explicit|inferred|absent）：direct=本证据单元直接看见/听见的事实，explicit=本证据单元明确支持的事实，inferred=需要跨证据或解释才能得到，absent=本证据单元明确没有该事实；无法确定或缺失留空，绝不把 unknown 当 absent。"
        "再标 endorsement_verbal 与 endorsement_visual（各 true/false，纯观察、不判断算不算有效背书——有效性归后续打分）："
        "endorsement_verbal＝该时段口播/字幕里有没有【出现】halal/KKM/认证/证书/检测/临床/医生/皮肤科/专家/机构/FDA/GMP/SIRIM/BPOM/GMP/certified 等硬来源词（只看词出没出现，不判断是否构成援引背书）；"
        "endorsement_visual＝该时段画面里有没有【出现】独立的硬背书视觉证据（证书/检测报告文件/机构认证标识被画面清晰呈现）——产品瓶身上的印刷小标不算，口播说了但画面没出现也不算（口播归 endorsement_verbal，别把听到的脑补成画面）；"
        "再标 trust_source_signals（数组，只记录实际看见/听见的独立信任来源，允许 authority/traceable_data/independent_user/social_consensus/process_transparency；没有则省略）和 trust_source_reference（逐字或概括写出实际出处；无出处必须省略）。authority/traceable_data 必须有机构名、报告号、官方/平台页面或可辨识认证；independent_user 必须有评论/用户原话；social_consensus 必须同时有明确群体/社区和该群体共同看法；process_transparency 必须有工厂、原料、生产或质检过程。产品数量、价格、参数、时长、赠品、达人自述均不得填。"
        "trust_source_*、variant_*、comparison_purpose_explicit 与 attention_competitor_ids 只在当前单元确有对应事实时输出；"
        "没有时直接省略，禁止逐条复制空数组、空映射、空字符串或默认 none。"
        "每条还要标 functions（list，多选）：这段画面支撑哪些带货功能，枚举 S1_hook/S2_intro/S3_usage/S4_effect/S5_trust/S6_cta，"
        "按信息功能判断、信道无关（口播/字幕/画面/特效综合看，无口播也能判），一段可同时支撑多个"
        "（手在操作+效果出来 → [S3_usage,S4_effect]）；这是描述这段在带货结构里干什么、不是评价好坏，没有对应功能就不标；"
        "time_range 必须对应实际观察窗口；画面看不清的时段在 visual_fact 写画面证据不足待复核；"
        "视频级商业门控只需要你补充纯观察事实，不做优劣结论："
        "selling_point_observations 列出实际占据主要画面或口播的卖点，visual_share 与 speech_share 分开估算且各自在 0-1；"
        "variant_* 只区分同品 SKU/色号/包装变体，不把完全不同产品硬并成变体。single_focus 表示一个变体主导，"
        "explicit_comparison 表示当前单元与相邻单元共同形成明确比较/选择，不要求两个变体必须同帧出现；"
        "comparison_purpose_explicit 只有口播、字幕或镜头结构明确解释比较目的时才 true。"
        "primary_variant_id 与 variant_attribution_confident 不要输出，它们由代码按 70% 阈值计算。"
        "attention_competitors 只记录高显著、持续运动且可能抢走产品证明注意力的非任务物体；产品、演示工具、正常手部动作、必要模特均不得列入。"
        "录音或拍摄设备不是产品任务的一部分：例如达人持续举在嘴边并随手晃动的手持麦克风、线缆或手机支架，"
        "只要高显著且持续抢占注意力，就必须记录；固定、低显著且不抢注意力的设备不记录。"
        "看不清连续运动时 persistent_motion 留 null，不得由离散帧脑补。"
        "attention_scan_audit 必须逐项回答录音/拍摄设备是否可见、前景非任务物体是否可见，并给证据单元；"
        "任一项为 true 时 attention_competitors 不得为空，需记录物体、时段和运动状态。"
        "完成三项扫描后必须输出 gate_observation_status，三个字段均只允许 complete|unknown；"
        "只有确实逐项看完并按 schema 输出时才写 complete，任何一项无法判断或没有检查都写 unknown。"
        "无 BGM 或无明显音效时 audio_fact 写无，不要臆造。"
        "不得臆造牙齿前后对比、用户评论、证书、检测报告、认证、价格、优惠或功效。"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
    }
    if str(model or "").strip().lower().startswith("qwen3-vl"):
        # Stage1-A/C are extraction tasks. JSON mode prevents a completed VL
        # response from being discarded solely because it wrapped or damaged
        # the requested object; thinking adds cost here without adding facts.
        payload["response_format"] = {"type": "json_object"}
        payload["enable_thinking"] = False
    return payload


def build_stage_evidence_qualification_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    facts: dict[str, Any],
    target_stages: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build the media-free Stage1-B qualification request.

    Stage1-A owns observation.  Stage1-B only projects those locked atomic
    observations onto the six stage contracts; it must not re-watch media or
    invent a missing fact.  Keeping this request text-only also makes a
    qualification failure distinguishable from an acquisition failure.
    """
    info = analysis.get("videos", {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {}
    units = []
    for unit in facts.get("evidence_units") or []:
        if not isinstance(unit, dict):
            continue
        units.append(
            {
                "id": unit.get("id"),
                "time_range": unit.get("time_range"),
                "information": unit.get("information"),
                "visual_fact": unit.get("visual_fact"),
                "voiceover": unit.get("voiceover"),
                "voiceover_zh": unit.get("voiceover_zh"),
                "subtitle_fact": unit.get("subtitle_fact"),
                "evidence_strength": unit.get("evidence_strength"),
                "fact_quality": unit.get("fact_quality"),
                "product_visible": unit.get("product_visible"),
                "product_coverage": unit.get("product_coverage"),
                "functions": unit.get("functions"),
                "trust_source_signals": unit.get("trust_source_signals"),
                "trust_source_reference": unit.get("trust_source_reference"),
            }
        )
    normalized_targets = [
        code
        for code in stage_codes()
        if code in {
            str(stage).strip().upper()[:2]
            for stage in (target_stages or stage_codes())
            if str(stage).strip()
        }
    ]
    target_text = ", ".join(normalized_targets) or "S1-S6"
    acquisition = dict(
        facts.get("stage1_acquisition")
        if isinstance(facts.get("stage1_acquisition"), dict)
        else {}
    )
    # Technical replay changes only the transport provenance. Preserve the
    # frozen live request identity instead of feeding "replay" back into the
    # next semantic request. Removing this legacy audit block entirely would
    # require a versioned contract migration and invalidate existing artifacts.
    provider_artifacts = []
    for item in acquisition.get("provider_artifacts") or []:
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        if normalized_item.get("execution_source") == "replay":
            normalized_item["execution_source"] = "provider"
        provider_artifacts.append(normalized_item)
    if "provider_artifacts" in acquisition:
        acquisition["provider_artifacts"] = provider_artifacts
    context = {
        "role": role,
        "duration_seconds": info.get("duration_seconds"),
        "stage1_acquisition": acquisition,
        "evidence_units": units,
    }
    target_certification_units = _certification_units_for_target_stages(
        units, normalized_targets
    )
    certification_policy = ""
    if "S5" not in normalized_targets and contains_certification(
        json.dumps(target_certification_units, ensure_ascii=False)
    ):
        certification_policy = (
            CERTIFICATION_OWNERSHIP_PROMPT
            + "\n当前目标阶段不拥有第三方认证归属；认证不得作为非 S5 阶段的资格信号。"
            + (
                "混合 evidence unit 可以因其中的产品身份或问题到产品承接部分被 S2 引用；"
                "但 reason 和 signal binding 只能依据非认证部分，认证不得作为 S2 信号。"
                if "S2" in normalized_targets
                else "reason 和 signal binding 只能依据非认证部分，认证不得作为任何非 S5 阶段信号。"
            )
            + "若必须解释混合 evidence unit，只能明确说明认证归入 S5，且当前阶段仅引用非认证部分；"
            "不得在其他字段复述认证或把认证作为当前阶段信号。"
        )
    output_stages = _stage_evidence_qualification_examples(normalized_targets)
    s6_language_review = (
        "## S6 本地化口播复核\n"
        "Fun-ASR 是可审计的语音事实入口，但短口语可能出现分词、近音词或单词拼写错误。"
        "当某条已锁定观察被标记为 S6_cta，或完整口播包含可能的本地电商行动/路径表达时，"
        "必须结合整句、销售语境和当地平台惯用表达核对语义，不能只按一个错误 token 的字面翻译否定。"
        "例如 beg/bakul kuning、yellow bag/cart 以及 klik/tekan/tap/beli/order/checkout/link/retail 等只作为检索线索，"
        "不是自动命中规则；只有整句至少能支持面向观众的行动或可执行购买路径时才可判 present，"
        "是否有行动与路径双重支撑属于后续强弱判断。"
        "coverage 表示相关素材范围是否已完整检查，不表示所有备选信号是否都出现。"
        "S6 是 required_signal_mode=any：explicit_action 或 purchase_path 任一项由合格证据支持，且相关范围已完整检查时，"
        "应输出 present/complete；另一项可记录为 missing，不得因另一个备选 required signal 缺失改成 partial。"
        "判读时原始口播和整句句法优先，voiceover_zh 只是上游释义，冲突时不得用释义覆盖原句。"
        "要区分可获得性表达（如 ada dekat、boleh dapat/beli/order dekat）与实体处置表达（如 buang、letak、masuk dalam）；"
        "前者在销售收尾语境中可以支持 purchase_path，后者才通常描述把物品放入实体袋子。"
        "画面里存在实体袋子不能单独否定口播中的平台隐语；若近音修复仍有多个合理解释，保持 unknown 并在 reason 写明歧义。"
        if "S6" in normalized_targets
        else ""
    )
    s5_independence_review = (
        "## S5 独立来源边界\n"
        "S5 衡量独立信任来源，不把产品、品牌或当前达人的自述重新命名为用户证言。"
        "达人本人以用户身份出镜、讲自己的使用经历、演示产品，或比较自己以前使用的工具，仍是当前达人自述，不能构成 independent_user。"
        "independent_user 必须能归因到与当前达人不同的第三方用户，例如可识别的评论、晒单、访谈或第三方用户原话；"
        "authority、traceable_data、social_consensus 和 process_transparency 也必须分别有其真实独立来源。"
        "若只有达人自述，应标记 product_claim_only，并把 independent_origin 绑定为 missing；不得因为内容具体、画中画对比或达人自称真实用户就判 present。"
        if "S5" in normalized_targets
        else ""
    )
    text = "\n\n".join(
        [
            f"# Stage1 阶段资格投影：{role}（{target_text}）",
            f"你只负责把已锁定的原子观察投影到目标阶段 {target_text}。不要看视频、不要补写事实、不要比较 benchmark 与 creator。",
            "只能引用输入中实际存在的 evidence_units。渠道不可用、时间边界不精确或观察范围不足以确认任何阶段参与时，必须返回 unknown/conflict；不能把缺失当 absent。",
            "present 只能由 direct 或 explicit 的真实 evidence_ids 支撑；inferred、unknown、冲突或缺字段不得触发正式资格。",
            "partial 表示已用 direct/explicit 证据确认该阶段确实被执行或尝试，但尚未满足全部 required signals。partial 必须引用证据、coverage 只能是 complete 或 partial，且至少一个 participation_signal 有 supported binding；不得用 partial 包装未绑定事实、歧义、排除项或采集失败。",
            "‘做了但较弱/不完整’不得压成 unknown；unknown 只表示观察本身不充分、边界不清、引用未绑定或相互冲突。partial 也不等于完整证明，缺失的 required signals 必须保留在 missing_signals。",
            "absent 只有在相关观察范围已完整覆盖、且合同要求的信号明确未出现时才允许。离散采样、粗粒度口播或未完成覆盖不能证明 absent。",
            "not_applicable 只有在比较合同明确说明该阶段不适用时才允许，并必须在 reason 中写明依据；不能用它掩盖采集缺失。",
            "每个 signal_binding 必须引用当前输入中真实存在的 evidence_id；不得跨角色、跨视频或跨阶段创造引用。",
            "fact_quality 是 Canonical Stage1-A/C 对每条观察的描述性质量元数据；只能结合输入中的这些字段判断资格，不能把缺失或 uncertain 猜成已验证。",
            "## 阶段合同",
            stage_evidence_contract_prompt(normalized_targets),
            "## 输出时必须遵守的阶段信号白名单",
            _stage_evidence_signal_codebook(normalized_targets),
            *([s5_independence_review] if s5_independence_review else []),
            *([certification_policy] if certification_policy else []),
            s6_language_review,
            "## 已锁定 Canonical Stage1-A/C 事实（只读）",
            json.dumps(context, ensure_ascii=False, indent=2),
            "## 严格 JSON 输出",
            json.dumps(
                {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "stage_evidence_checks": output_stages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            f"必须恰好输出目标阶段 {target_text} 的 stage_evidence_checks，不得输出其他阶段。",
        ]
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是 Flayr Stage1 资格投影器。只输出严格 JSON，不要 Markdown。"},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
    }
    payload.update(
        {"max_completion_tokens": 8192}
        if str(model).lower().startswith("qwen3.6-plus")
        else {"max_tokens": 8192}
    )
    return payload


def build_video_fact_recovery_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    visual_inputs: list[dict[str, Any]],
    current_facts: dict[str, Any],
    target_stages: list[str],
    api_url: str = "",
    budget: ResourceBudget | None = None,
    media_windows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one bounded pre-lock re-observation request.

    Recovery can add candidate observations and stage qualifications, but it
    cannot rewrite existing facts.  The caller merges and re-normalizes the
    candidate output before Stage2 sees it.
    """
    payload = build_video_fact_payload(
        model,
        role,
        analysis,
        visual_inputs,
        api_url=api_url,
        budget=budget,
        include_standalone_audio=False,
    )
    normalized_targets = [
        code
        for code in stage_codes()
        if code in {str(stage).strip().upper()[:2] for stage in target_stages if str(stage).strip()}
    ]
    target_text = ", ".join(normalized_targets) or "S1-S6"
    evidence_prefix = "B" if role == "benchmark" else "C"
    target_set = set(normalized_targets)
    current_checks = {
        str(item.get("stage") or "").strip().upper()[:2]: item
        for item in current_facts.get("stage_evidence_checks") or []
        if isinstance(item, dict)
    }
    s6_current_status = str(current_checks.get("S6", {}).get("status") or "").strip().lower()
    s6_current_coverage = str(current_checks.get("S6", {}).get("coverage") or "").strip().lower()
    s6_explicitly_absent = "S6" in target_set and s6_current_status == "absent"
    candidate_observations = current_facts.get("candidate_observations_by_stage")
    candidate_observations = candidate_observations if isinstance(candidate_observations, dict) else {}
    candidate_observations = {
        str(stage).strip().upper()[:2]: [
            dict(item) for item in items if isinstance(item, dict)
        ]
        for stage, items in candidate_observations.items()
        if str(stage).strip().upper()[:2] in target_set and isinstance(items, list)
    }
    candidate_ids_by_stage = current_facts.get("candidate_evidence_ids_by_stage")
    candidate_ids_by_stage = candidate_ids_by_stage if isinstance(candidate_ids_by_stage, dict) else {}
    candidate_ids_by_stage = {
        str(stage).strip().upper()[:2]: [str(item).strip() for item in items if str(item).strip()]
        for stage, items in candidate_ids_by_stage.items()
        if str(stage).strip().upper()[:2] in target_set and isinstance(items, list)
    }
    locked_fact_summary = {
        "evidence_units": [
            dict(item)
            for item in current_facts.get("evidence_units") or []
            if isinstance(item, dict)
        ],
        "stage_evidence_checks": [
            dict(item)
            for item in current_facts.get("stage_evidence_checks") or []
            if isinstance(item, dict)
            and str(item.get("stage") or "").strip().upper()[:2] in target_set
        ],
    }
    recovery_candidate_summary = {
        # These observations are deliberately kept in a separate recovery lane.
        # They are not qualified facts, but hiding them from Stage1-C makes it
        # impossible for one bounded re-observation to close a missed stage.
        "candidate_evidence_ids_by_stage": candidate_ids_by_stage,
        "candidate_observations_by_stage": candidate_observations,
    }
    recovery_audio_rule = (
        "你可以直接感知本轮窗口音轨，但仍不得补全听不清的话术。"
        if can_analyze_native_audio(api_url, model)
        else "你不能直接理解视频音轨；口播语义只能来自本轮提供的窗口安全 Fun-ASR，缺失时保持 unknown，不得脑补。"
    )
    recovery_system = (
        "你是 Flayr Stage1-C 的定向视觉观察器。只输出严格 JSON。"
        "这是一次且仅一次的事实恢复，不得改写、删除或合并已有 evidence_units，"
        "只能补充当前视频中可直接观察到的新 candidate_evidence_units。"
        "目标阶段资格由后续判断模型的 Stage1-D 独立投影，你不得输出 stage_evidence_checks 或其他判断字段。"
        "候选观察是未资格化的恢复线索，不是事实；必须结合其内容、时间和本轮媒体独立核实，"
        "不得仅凭 functions、关键词或旧资格表把候选直接升级为证据。"
        "没有确认的新观察就写空数组，不得为了让阶段成立而推断。"
        + recovery_audio_rule
    )
    payload["messages"][0]["content"] = recovery_system
    original_content = payload["messages"][1].get("content")
    media = [
        item for item in original_content
        if isinstance(item, dict) and item.get("type") in {"image_url", "video_url", "input_audio"}
    ] if isinstance(original_content, list) else []
    media = _replace_recovery_full_media(
        media,
        analysis,
        role,
        target_stages,
        api_url=api_url,
        model=model,
        budget=budget,
        media_windows=media_windows,
        s6_tail_review=(
            "S6" in target_set
            and not (s6_current_status == "present" and s6_current_coverage == "complete")
        ),
    )
    if visual_inputs:
        labeled_media: list[dict[str, Any]] = []
        visual_index = 0
        for item in media:
            if item.get("type") != "image_url":
                labeled_media.append(item)
                continue
            if visual_index < len(visual_inputs):
                label = str(visual_inputs[visual_index].get("label") or "").strip()
                if label:
                    labeled_media.append({"type": "text", "text": f"图片：{label}"})
            labeled_media.append(item)
            visual_index += 1
        media = labeled_media
    s6_tail_review_block = (
        "## S6 完整上下文 CTA 定向复核\n"
        "当前 S6 资格未闭合（可能是 absent、unknown 或 conflict）。本轮对原始视频完整上下文做一次漏检复核；"
        "不要因为出现关键词就直接写成 CTA 结论，必须原样记录完整语句、说话对象、画面路径和真实时间。\n"
        "马来/东南亚电商口语可能用 beg kuning、bakul kuning、yellow bag/cart，或 tekan、klik、tap、beli、order、checkout、link 等表达；"
        "这些只是检索线索，不是自动等价规则。若观察中可能包含购买行动或路径，只追加一条保留原句和上下文的候选观察；"
        "如果窗口安全 Fun-ASR 补齐了会改变 CTA 解释的完整上下文，即使它与已有 evidence unit 时间重叠，也必须追加一条补充候选观察；"
        "这不属于重复事实。只有语义和上下文都已被已有 evidence unit 完整保留时，才不得重复追加。"
        "不得在本层输出 explicit_action、purchase_path、present、absent 或 coverage，最终语义由 Stage1-D 复核。"
    ) if (
        "S6" in target_set
        and not (s6_current_status == "present" and s6_current_coverage == "complete")
    ) else ""
    payload["messages"][1]["content"] = [
        {
            "type": "text",
            "text": "\n\n".join(
                [
                    "# Stage1-C 定向缺口补观察",
                    f"只复核这些阶段：{target_text}。只看目标阶段的 required_signals、非替代渠道和 disqualifiers。",
                    "目标阶段的 signal 名称只能来自下面的白名单；禁止把其他阶段的 signal 拿来填当前阶段，禁止使用 product_identity、product_features 等未注册名称。",
                    "## 目标阶段信号白名单",
                    _stage_evidence_signal_codebook(normalized_targets),
                    "这是一次追加观察，不是重新抽取整条视频；不得改写、删除或合并已有 evidence_units。",
                    "已有资格化事实只用于避免重复，不得把它们当成可修改的模型输出。候选观察位于恢复线索区，必须逐条核实；"
                    "如果没有可直接观察且与目标阶段相关的新内容，返回空 candidate_evidence_units，不得输出拒绝理由或资格字段。",
                    "你只记录观察，不判断 present/absent/unknown，也不得输出 coverage 或 stage_evidence_checks；"
                    "资格与覆盖由 Stage1-D 根据代码拥有的媒体范围投影。",
                    "即使目标阶段的完整证明链没有闭合，也要记录直接看到的真实执行子集；不要因为动作、结果呈现或阶段参与较弱就省略观察。"
                    "S4 中把使用后的目标对象或结果状态当作结果展示，可记录为 result_presentation 候选；静态产品展示或只有口播功效声称不属于 result_presentation。",
                    "S5 是可选的信任放大阶段，不得根据品类先验强行要求或关闭；品牌/logo/产品身份本身不等于 source_basis，"
                    "只有实际来源、报告、认证、用户原话或过程信息才可作为合格背书依据。",
                    "若候选观察可能承担 S6_cta，必须原样保留完整窗口口播、字幕、画面和时间范围，"
                    "不要在本层裁决它最终是否构成购买行动。",
                    "每个新 candidate_evidence_unit 必须填写 fact_quality 的六个观察轴；"
                    "completion 只记录关键动作过程是否完整可见，proof 只记录结果证明形态，causal_link 只记录动作与结果的可见连接。"
                    "direct_comparison 必须有画面直接对照/控制与差异；result_only 是只见结果、不见产品如何造成结果；"
                    "claim_only 是只有口播或字幕声称、没有可见结果；完整使用动作本身不等于 direct_comparison，"
                    "结果存在也不等于 causal_link=supported。无法判断时填 uncertain 或 not_applicable。",
                    recovery_audio_rule,
                    s6_tail_review_block,
                    "## 已锁定事实摘要（只读）",
                    json.dumps(locked_fact_summary, ensure_ascii=False, indent=2),
                    "## 未资格化恢复线索（只作核实提示，不是事实；不得仅凭这些线索输出 present）",
                    json.dumps(recovery_candidate_summary, ensure_ascii=False, indent=2),
                    "## 输出合同",
                    json.dumps(
                        {
                    "candidate_evidence_units": [
                        {
                            "id": f"新的唯一 ID，例如 {evidence_prefix}9；不能复用已有 ID；"
                            f"当前角色只能使用 {evidence_prefix} 前缀",
                            "time_range": "真实时间范围",
                            "information": "直接观察到的事实",
                            "voiceover": "仅窗口安全口播中的原句，没有则留空",
                            "voiceover_zh": "中文翻译，没有则留空",
                            "visual_fact": "直接看到的画面事实",
                            "subtitle_fact": "直接读到的字幕，没有则留空",
                            "audio_fact": (
                                "直接听到的音频事实，没有则写无"
                                if can_analyze_native_audio(api_url, model)
                                else "未直接感知音轨；口播只引用窗口安全 Fun-ASR"
                            ),
                            "evidence_strength": "direct|explicit|inferred|absent",
                            "fact_quality": {
                                "subject": "correct|incorrect|uncertain|not_applicable",
                                "visibility": "clear|partial|obscured|uncertain|not_applicable",
                                "composition": "central|supporting|weak|uncertain|not_applicable",
                                "completion": "complete|partial|none|uncertain|not_applicable",
                                "proof": "direct_comparison|result_only|claim_only|none|uncertain|not_applicable",
                                "causal_link": "supported|weak|unsupported|uncertain|not_applicable",
                            },
                            "functions": [],
                        }
                    ],
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ]
            ),
        },
        *media,
    ]
    return payload


def _stage_evidence_signal_codebook(stages: list[str] | tuple[str, ...] | None = None) -> str:
    """Render the registered stage signal vocabulary for model-facing prompts.

    The validator has always owned this vocabulary.  Printing the exact
    per-stage allowlist next to the output shape prevents a text-only
    qualification or focused recovery response from borrowing a plausible
    signal name from a neighboring stage.
    """
    requested = {str(stage).strip().upper()[:2] for stage in stages or stage_codes()}
    codebook: list[dict[str, Any]] = []
    for code in stage_codes():
        if code not in requested:
            continue
        contract = stage_evidence_contract(code)
        if contract is None:
            continue
        codebook.append(
            {
                "stage": contract.code,
                "required_signals": list(contract.required_signals),
                "required_signal_mode": contract.required_signal_mode,
                "participation_signals": list(
                    contract.participation_signals or contract.required_signals
                ),
                "optional_signals": list(contract.optional_signals),
                "allowed_signal_names": list(contract.allowed_signals),
                "disqualifiers": list(contract.disqualifiers),
                "partial_compatible_disqualifiers": list(
                    contract.partial_compatible_disqualifiers
                ),
                "channel_policy": contract.channel_policy,
                "non_substitutable_channels": list(contract.non_substitutable_channels),
            }
        )
    return json.dumps(codebook, ensure_ascii=False, indent=2)


def _stage_evidence_qualification_examples(
    stages: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build exact output examples without duplicating the stage registry."""
    requested = {str(stage).strip().upper()[:2] for stage in stages or stage_codes()}
    examples: list[dict[str, Any]] = []
    for code in stage_codes():
        if code not in requested:
            continue
        contract = stage_evidence_contract(code)
        if contract is None:
            continue
        examples.append(
            {
                "stage": code,
                "status": "present|partial|absent|unknown|conflict|not_applicable",
                "coverage": "complete|partial|unknown",
                "evidence_ids": [],
                "observed_signals": [],
                "missing_signals": [],
                "signal_bindings": {
                    signal: {
                        "status": "supported|missing|unknown",
                        "evidence_ids": [],
                        "reason": "只说明该 signal 是否被当前证据支持。",
                    }
                    for signal in dict.fromkeys(
                        (*contract.required_signals, *(contract.participation_signals or ()))
                    )
                },
                "invalid_signal_bindings": [],
                "observed_disqualifiers": [],
                "invalid_evidence_ids": [],
                "invalid_observed_signals": [],
                "invalid_missing_signals": [],
                "invalid_observed_disqualifiers": [],
                "evidence_strength": "direct|explicit|inferred|absent",
                "reason": "只说明该阶段资格事实，不写比较或严重度。",
            }
        )
    return examples


def _recovery_stage_windows(
    analysis: dict[str, Any],
    role: str,
    target_stages: list[str],
    *,
    s6_tail_review: bool = False,
) -> list[tuple[str, float, float]]:
    """Return one full-context window for a bounded recovery request.

    Stage1-C is an omission search, not a verification pass over known stage
    evidence.  Stage labels therefore identify retrieval targets only; they do
    not describe the time span represented by the media.
    """
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    raw_duration = info.get("duration_seconds")
    duration = None if isinstance(raw_duration, bool) else parse_timestamp_seconds(raw_duration)
    target_codes = _recovery_target_codes(target_stages)
    if duration is None or duration <= 0 or not target_codes:
        return []
    # ``s6_tail_review`` remains accepted for replay/caller compatibility, but
    # a recovery request always searches the same complete source context.
    _ = s6_tail_review
    return [("+".join(target_codes), 0.0, float(duration))]


def stage1_recovery_media_windows(
    analysis: dict[str, Any],
    role: str,
    target_stages: list[str],
    *,
    s6_tail_review: bool = False,
) -> list[dict[str, Any]]:
    """Return the code-owned full-context plan used for audit metadata."""
    return [
        {
            "role": role,
            "window_label": label,
            # Keep the exact source endpoint for native encoding, ASR, and
            # audit consumers.  Formatting belongs only in display text.
            "start_seconds": start,
            "end_seconds": end,
        }
        for label, start, end in _recovery_stage_windows(
            analysis,
            role,
            target_stages,
            s6_tail_review=s6_tail_review,
        )
    ]


def _recovery_target_codes(target_stages: list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize requested stage tokens in canonical stage order."""
    requested = {
        match.group(0)
        for value in target_stages or []
        if (match := re.search(r"\bS([1-6])\b", str(value).upper()))
    }
    return [code for code in stage_codes() if code in requested]


def _recovery_media_window_tuples(
    media_windows: list[dict[str, Any]],
    role: str,
    target_label: str,
) -> list[tuple[str, float, float]]:
    """Normalize one pipeline-owned recovery plan without changing endpoints."""
    normalized: list[tuple[str, float, float]] = []
    for item in media_windows:
        if not isinstance(item, dict):
            raise ValueError(
                "Stage1-C recovery media plan is invalid: "
                f"reason_code=stage1_recovery_media_window_invalid role={role}"
            )
        plan_role = str(item.get("role") or "").strip()
        label = str(item.get("window_label") or "").strip()
        raw_start = item.get("start_seconds")
        raw_end = item.get("end_seconds")
        start = parse_timestamp_seconds(raw_start)
        end = parse_timestamp_seconds(raw_end)
        if (
            plan_role != role
            or label != target_label
            or start is None
            or end is None
            or end <= start
        ):
            raise ValueError(
                "Stage1-C recovery media plan is invalid: "
                f"reason_code=stage1_recovery_media_window_invalid role={role} "
                f"plan_role={plan_role or '<missing>'} label={label or '<missing>'} "
                f"start={raw_start!r} end={raw_end!r}"
            )
        normalized.append((label, float(start), float(end)))
    return normalized


def _planned_recovery_windows(
    analysis: dict[str, Any],
    role: str,
    target_stages: list[str],
    media_windows: list[dict[str, Any]] | None,
) -> list[tuple[str, float, float]]:
    """Resolve the shared full-context plan and fail closed on bad duration."""
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    raw_duration = info.get("duration_seconds")
    duration = None if isinstance(raw_duration, bool) else parse_timestamp_seconds(raw_duration)
    target_codes = _recovery_target_codes(target_stages)
    if not target_codes:
        return []
    if duration is None or duration <= 0:
        raise ValueError(
            "Stage1-C recovery source duration is invalid: "
            f"reason_code=stage1_recovery_video_duration_invalid role={role} "
            f"duration={info.get('duration_seconds')!r}"
        )
    target_label = "+".join(target_codes)
    if media_windows is not None and not isinstance(media_windows, list):
        raise ValueError(
            "Stage1-C recovery media plan must be a list of window objects: "
            f"reason_code=stage1_recovery_media_window_invalid role={role}"
        )
    windows = (
        _recovery_stage_windows(analysis, role, target_stages)
        if media_windows is None
        else _recovery_media_window_tuples(media_windows, role, target_label)
    )
    if len(windows) != 1:
        raise ValueError(
            "Stage1-C recovery media plan must contain one full-context window: "
            f"reason_code=stage1_recovery_media_window_invalid role={role} "
            f"window_count={len(windows)}"
        )
    label, start, end = windows[0]
    if start != 0.0 or end != float(duration):
        raise ValueError(
            "Stage1-C recovery media plan must cover the full source video: "
            f"reason_code=stage1_recovery_media_window_invalid role={role} "
            f"label={label} start={start:g} end={end:g} duration={float(duration):g}"
        )
    return windows


def _validate_stage1_recovery_video_windows(
    windows: list[tuple[str, float, float]],
    role: str,
) -> None:
    """Reject native-video recovery batches containing a provider-invalid window."""
    minimum = MIN_NATIVE_VIDEO_WINDOW_SECONDS
    for label, start, end in windows:
        window_start = float(start)
        window_end = float(end)
        duration = window_end - window_start
        if duration < minimum:
            raise ValueError(
                "Stage1-C native video recovery window too short: "
                "reason_code=stage1_recovery_video_window_too_short "
                f"role={role} label={label} start={window_start:g} "
                f"end={window_end:g} min={minimum:g} duration={duration:g}"
            )


def _replace_recovery_full_media(
    media: list[dict[str, Any]],
    analysis: dict[str, Any],
    role: str,
    target_stages: list[str],
    *,
    api_url: str,
    model: str,
    budget: ResourceBudget | None,
    media_windows: list[dict[str, Any]] | None = None,
    s6_tail_review: bool = False,
) -> list[dict[str, Any]]:
    """Replace initial media with one full-context recovery request."""
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    role_dir = Path(str(info.get("work_dir") or ""))
    video_path = Path(str(info.get("path") or ""))
    audio_path = role_dir / "audio.wav"
    windows = _planned_recovery_windows(
        analysis,
        role,
        target_stages,
        media_windows,
    )
    _ = s6_tail_review
    native_video_capable = can_analyze_native_video(api_url, model)
    if native_video_capable and not video_path.is_file():
        raise ValueError(
            "Stage1-C native video recovery source is unavailable: "
            f"reason_code=stage1_recovery_video_source_missing role={role} "
            f"path={video_path}"
        )
    native_video = native_video_capable
    if native_video:
        _validate_stage1_recovery_video_windows(windows, role)
    retained = [
        item for item in media
        if item.get("type") not in {"video_url", "input_audio"}
    ]
    words = load_transcript_words(info)
    for label, start, end in windows:
        window_duration = max(0.1, end - start)
        transcript_text = transcript_text_for_range(words, start, end) if words else ""
        retained.append(
            {
                "type": "text",
                "text": (
                    f"Stage1-C 完整上下文 Fun-ASR 检索目标｜{role}｜{label}｜"
                    f"{format_seconds(start)} - {format_seconds(end)}\n"
                    + (
                        transcript_text
                        if transcript_text
                        else "（无词级窗口安全口播；不得使用粗粒度 SRT 推断本窗口口播）"
                    )
                ),
            }
        )
        if native_video:
            clip = video_to_data_url(
                video_path,
                start=start,
                duration=window_duration,
                budget=budget,
            )
            if not clip:
                raise ValueError(
                    "Stage1-C native video recovery encoding failed: "
                    f"reason_code=stage1_recovery_video_encoding_failed role={role} "
                    f"label={label} start={start:g} end={end:g} "
                    "video_to_data_url returned no data"
                )
            retained.extend(
                [
                    {
                        "type": "text",
                        "text": (
                            f"Stage1-C 完整上下文视频检索目标 {label}："
                            f"{format_seconds(start)} - {format_seconds(end)}"
                            "（阶段标签是检索目标，不是时间段语义标签）"
                        ),
                    },
                    {"type": "video_url", "video_url": {"url": clip}},
                ]
            )
            continue
        if can_analyze_native_audio(api_url, model) and can_send_standalone_audio(api_url, model):
            audio_clip = audio_to_mp3_data_url(
                audio_path,
                start=start,
                duration=window_duration,
                budget=budget,
            )
            if audio_clip:
                retained.extend(
                    [
                        {
                            "type": "text",
                            "text": (
                                f"Stage1-C 完整上下文音频检索目标 {label}："
                                f"{format_seconds(start)} - {format_seconds(end)}"
                                "（阶段标签是检索目标，不是时间段语义标签）"
                            ),
                        },
                        {"type": "input_audio", "input_audio": {"data": audio_clip, "format": "mp3"}},
                    ]
                )
    return retained


# Stage1 qualification and Stage2 judgment must share one grouping contract.
# Keep the historical export name for callers, but do not maintain a second
# literal that could drift from the canonical Stage1 definition.
STAGE_JUDGMENT_GROUPS: tuple[tuple[str, ...], ...] = STAGE1_QUALIFICATION_GROUPS


def _compact_stage_group_facts(
    facts: dict[str, Any],
    target_stages: list[str],
) -> dict[str, Any]:
    """Build the closed-world handoff consumed by one Stage2 group.

    The immutable Stage1 record stays outside the model request.  Only
    qualified observations for the target stages are exposed.  Readiness and
    missing requirements are included as diagnostics so the model can return
    ``uncertain`` instead of inventing a conclusion when the gate is blocked.
    """
    target = {str(value).strip().upper() for value in target_stages}
    view = stage_analysis_evidence_view(facts, target)
    compact: dict[str, Any] = {}
    for role in ("benchmark", "creator"):
        side = view.get(role) if isinstance(view.get(role), dict) else {}
        original = facts.get(role) if isinstance(facts.get(role), dict) else {}
        projection = stage1_qualification_projection(original, target)
        # The persisted handoff validates the complete immutable ledger. A
        # stage-scoped model request must not also carry that global digest:
        # otherwise an S6-only recovery changes the request identity for S1-S4
        # even when every fact visible to those groups is unchanged.
        projection.pop("ledger_hash", None)
        for stage_projection in (projection.get("stages") or {}).values():
            if isinstance(stage_projection, dict):
                stage_projection.pop("ledger_hash", None)
        stage_payload: dict[str, Any] = {}
        for stage in sorted(target):
            stage_units = (side.get("stage_evidence_units") or {}).get(stage) or []
            candidate_ids = (side.get("candidate_evidence_ids_by_stage") or {}).get(stage) or []
            stage_payload[stage] = {
                "readiness": (side.get("stage_evidence_readiness") or {}).get(stage, "unknown"),
                "qualified_evidence": [
                    {
                        "id": unit.get("id"),
                        "time_range": unit.get("time_range"),
                        "information": unit.get("information"),
                        "voiceover": unit.get("voiceover"),
                        "voiceover_zh": unit.get("voiceover_zh"),
                        "visual_fact": unit.get("visual_fact"),
                        "subtitle_fact": unit.get("subtitle_fact"),
                        "audio_fact": unit.get("audio_fact"),
                        "evidence_strength": unit.get("evidence_strength"),
                        "fact_quality": unit.get("fact_quality"),
                        "functions": unit.get("functions"),
                        "trust_source_signals": unit.get("trust_source_signals"),
                        "trust_source_reference": unit.get("trust_source_reference"),
                    }
                    for unit in stage_units
                    if isinstance(unit, dict)
                ],
                "projection": (projection.get("stages") or {}).get(stage) or {},
                "candidate_summary": {
                    "ids": [str(value).strip() for value in candidate_ids if str(value).strip()],
                    "count": len(candidate_ids),
                    "missing_requirements": ((projection.get("stages") or {}).get(stage) or {}).get("missing_requirements") or [],
                    "reason_code": ((projection.get("stages") or {}).get(stage) or {}).get("projection_reason_code") or "unknown",
                    "rule": "candidate observations are retained for recovery/audit and cannot support judgment",
                },
            }
        compact[role] = {
            "product_identity": side.get("product_identity") or {},
            "stages": stage_payload,
        }
    return compact


def _compact_stage_group_comparison_contract(
    value: Any,
    target_stages: list[str],
) -> dict[str, Any]:
    """Keep only comparison semantics that can affect this Stage2 group."""
    source = value if isinstance(value, dict) else {}
    target = {str(stage).strip().upper() for stage in target_stages}
    shared_job = source.get("shared_job") if isinstance(source.get("shared_job"), dict) else {}
    stage_eligibility = (
        source.get("stage_eligibility")
        if isinstance(source.get("stage_eligibility"), dict)
        else {}
    )
    return {
        "identity_relation": source.get("identity_relation"),
        "substitution_relation": source.get("substitution_relation"),
        "shared_job": {
            key: shared_job.get(key)
            for key in (
                "same_consumer_job",
                "same_target_object",
                "same_desired_outcome",
                "same_purchase_decision",
                "complement_or_dependency",
            )
        },
        "stage_eligibility": {
            stage: stage_eligibility.get(stage)
            for stage in sorted(target)
            if stage in stage_eligibility
        },
        "scope": source.get("scope"),
        "confidence": source.get("confidence"),
    }


def _stage_group_flag_contract(stage: str) -> str:
    """Return only the stage-specific structured facts for one small call."""
    contracts = {
        "S1": (
            '"creator_hook":{"exists":true,"type":"A-G|unknown","dims":{"camera":true,"copy":true,"sound":true,"rhythm":true},'
            '"hook_boundary_seconds":0,"hook_boundary_reason":"...","s2_start_signal":"...","landing_met":false,'
            '"landing_reason":"...","window_evidence":"...","landing_window_leak":false,"anchors_proposition":false,"evidence_ids":[]},'
            '"benchmark_hook":{...}。s2_start_signal 与 window_evidence 通常必须非空；仅当该侧 Stage1 已闭合为 absent、'
            'exists=false 且 evidence_ids=[] 时允许空字符串，不得编造转换信号或窗口证据。landing_met 只看 0 到 '
            'hook_boundary_seconds：对象明确、张力明确、可感知承诺/证据或具体未解问题三项必须齐全；答案可在 S2 承接，'
            '不得用边界后的产品解释补足，发生泄漏时 landing_window_leak=true 且 landing_met=false'
        ),
        "S2": (
            '"creator_s2":{"exists":true,"merged_with_s3":false,"module_type":"A-D|unknown","handoff_met":false,'
            '"s1_s2_compatible":false,"product_identity_clear":false,"product_role_clear":false,"excluded_or_risky_module":false,'
            '"start_seconds":0,"end_seconds":0,"handoff_reason":"...","evidence_ids":[]},"benchmark_s2":{...}'
        ),
        "S3": (
            '"creator_s3":{"exists":true,"module_type":"A-E|unknown","usage_evidence_state":"none|partial|complete|uncertain",'
            '"usage_process_visible":false,"result_only_without_process":false,"mouth_only_or_static":false,"real_usage_met":false,'
            '"core_selling_point_visible":false,"process_framing_met":false,"action_proof_met":false,"action_target_contact_met":false,'
            '"action_application_change_visible":false,"critical_action_continuity_met":false,"demonstrated_selling_points":[],'
            '"missing_selling_points":[],"scene_mode":"single_scene|multi_scene|multi_person|hybrid|unknown","usage_context_fit":false,'
            '"continuity_met":false,"richness_met":false,"single_scene_continuity_met":false,"single_scene_variation_met":false,'
            '"multi_scene_logic_met":false,"multi_scene_transition_met":false,"multi_scene_role_adaptation_met":false,"role_design_met":false,'
            '"role_interaction_met":false,"distinct_personas_met":false,"steps_clear_met":false,"pov_immersive_met":false,'
            '"presentation_overlays":[],"fake_or_staged":false,"start_seconds":0,"end_seconds":0,"usage_reason":"...","evidence_ids":[]},'
            '"benchmark_s3":{...}'
        ),
        "S4": (
            '"creator_s4":{"effect_type":"before_after|split_screen|person_vs_person|product_vs_alt|quantified_test|process_visualization|aesthetic_display|none",'
            '"effect_evidence_state":"none|result_only|verified|uncertain","effect_visible":false,"effect_salience":"none|subtle|clear|strong",'
            '"effect_proposition_matched":false,"comparison_control_met":false,"closeup_or_focus_met":false,"visual_difference_observed":false,'
            '"module_constraints_met":false,"effect_maximized":false,"requires_close_inspection":false,"effect_attribution_supported":false,'
            '"result_only_without_process":false,"process_linked_effect":false,"tamper_or_cut_risk":false,"effect_reason":"...","evidence_ids":[]},'
            '"benchmark_s4":{...}'
        ),
        "S5": (
            '"creator_s5":{"exists":false,"module_type":"A-E|unknown","trust_evidence_type":"hard|soft|mixed|none|unknown",'
            '"trust_basis":"authority|traceable_data|independent_user|social_consensus|process_transparency|product_claim|offer_or_spec|none|unknown",'
            '"trust_source_evidence_ids":[],"trust_source_visible":false,"trust_source_credible":false,"trust_claim_specific":false,'
            '"product_relevance_met":false,"independent_trust_purpose":false,"duplicates_other_stage":false,"voice_only":false,'
            '"risky_or_unsupported":false,"start_seconds":0,"end_seconds":0,"trust_reason":"...","evidence_ids":[]},"benchmark_s5":{...}'
        ),
        "S6": (
            '"creator_s6":{"exists":false,"module_type":"A-E|unknown","direct_order_met":false,"action_path_clear":false,'
            '"soft_purchase_invitation_met":false,"offer_or_incentive_clear":false,"price_anchor_met":false,"urgency_evidence_met":false,'
            '"gift_stack_met":false,"guarantee_clear_met":false,"urgency_met":false,"product_value_recalled":false,"module_fit_met":false,'
            '"ending_position_met":false,"depends_on_valid_s4":false,"compliance_risk":false,"start_seconds":0,"end_seconds":0,'
            '"cta_reason":"...","evidence_ids":[]},"benchmark_s6":{...}'
        ),
    }
    return contracts.get(stage, "")


def build_stage_group_judgment_payload(
    model: str,
    _analysis_input: str,
    facts: dict[str, Any],
    analysis: dict[str, Any],
    target_stages: list[str],
    api_url: str = "",
    budget: ResourceBudget | None = None,
) -> dict[str, Any]:
    """Build one bounded Stage2 judgment request.

    Stage2 is deliberately text-only.  The model must judge the locked,
    stage-scoped handoff rather than silently re-reading raw video and creating
    facts that cannot be traced back to the Stage1 ledger.
    """
    targets = [str(value).strip().upper() for value in target_stages if str(value).strip().upper() in stage_codes()]
    targets = list(dict.fromkeys(targets))
    if not targets:
        raise ValueError("stage group must contain at least one known stage")
    scoped_facts = _compact_stage_group_facts(facts, targets)
    has_positive_certification = contains_certification(
        json.dumps(scoped_facts, ensure_ascii=False)
    )
    eligibility = _compact_stage_group_comparison_contract(
        analysis.get("comparison_contract") or analysis.get("comparison_eligibility") or {},
        targets,
    )
    foundation = analysis.get("product_foundation") or {}
    flag_contract = "\n".join(f"{stage}: {_stage_group_flag_contract(stage)}" for stage in targets)
    if "S5" in targets:
        stage_ownership_contract = (
            "S5 是可选的信任放大阶段，不代表达人必须完成背书，也不能用品类先验否定任一侧实际出现的信任事实。"
            + CERTIFICATION_OWNERSHIP_PROMPT
        )
    elif has_positive_certification:
        stage_ownership_contract = (
            CERTIFICATION_OWNERSHIP_PROMPT
            + "\n当前阶段组不拥有第三方认证归属；认证不是非 S5 阶段的合法判断信号，"
            "不得写入 judgment_reason、任何阶段专属字段，也不得用于 relation 或 model_gap_magnitude；"
            "认证事实只由 S5 处理。非 S5 不得为了声明排除而复述认证，直接只写非认证依据。"
        )
    else:
        # Preserve the existing request identity for groups whose locked facts
        # contain no positive certification claim.
        stage_ownership_contract = "（本阶段组不处理第三方认证归属）"
    text = "\n\n".join(
        [
            "# Flayr Stage2 小阶段组判断",
            f"目标阶段：{', '.join(targets)}",
            "你只负责目标阶段的语义判断。先读取每侧该阶段的 qualified_evidence，再写阶段事实状态、relation、model_gap_magnitude 和理由。",
            "candidate summary 和 readiness 只能说明为什么未知，不能被升级为正式证据。不得新增事实、不得跨阶段或跨角色引用。",
            "relation 只能是 creator_better|benchmark_better|equivalent|uncertain；model_gap_magnitude 只能是 none|small|medium|large|uncertain。",
            "model_gap_magnitude 不是最终 severity；最终 severity 由代码 resolver 处理。不得输出 stage_evidence_links、improvements、commercial_priority、完整报告或其他阶段字段。",
            "每个阶段必须先完成 stage_state，再给 relation、model_gap_magnitude 和 judgment_reason；stage_state 只能是 completed|unknown|conflict|blocked；reason 只能引用该阶段实际 qualified evidence IDs 和代码已闭合的 readiness=absent 状态。",
            "readiness=partial 是可引用的真实阶段执行/尝试，不等于完整证明；必须结合其缺失信号与另一侧事实完成比较，不能把 partial 当 unknown，也不能把它写成 present/verified。readiness=absent 是已闭合的负向事实，不是采集缺失；单侧 absent、另一侧 present/partial 时仍要完成判断，relation 必须指向有证据的一侧，model_gap_magnitude 必须是 small、medium 或 large。双侧均 absent 时由代码收口为 not_applicable，不进入本阶段判断。只有 unknown/conflict，或比较合同未闭合的 not_applicable，才必须返回 uncertain。",
            "## 全阶段统一差距语义（结构库单一来源）",
            structure_library_gap_semantics(),
            "## 目标阶段模块判断视图（结构库单一来源）",
            structure_library_judgment_view(targets),
            "## 阶段归属规则",
            stage_ownership_contract,
            "## 商品与比较合同",
            json.dumps({"product_foundation": foundation, "comparison_contract": eligibility}, ensure_ascii=False, indent=2),
            "## 目标阶段证据交接（只读）",
            json.dumps(scoped_facts, ensure_ascii=False, indent=2),
            "每个阶段的正式引用必须从该阶段 qualified_evidence 的 id 中选择；如果 readiness=present/partial，至少引用能支撑该阶段判断的一个 ID。readiness=absent 的一侧必须保持 evidence_ids 为空，并在理由中明确它是代码已闭合的负向事实；不得把 candidate_summary 的 ID 当成正式引用，也不得只在理由文字中提到 ID。",
            "不得用固定条数截断 qualified_evidence、阶段引用或能证明因果链的事实；不得为凑数重复拆分。",
            "## 阶段字段合同",
            "每个 stage 对象只负责：stage、stage_state、relation、model_gap_magnitude、benchmark_evidence_ids、creator_evidence_ids、judgment_reason，以及该阶段专属结构化字段。不得输出 benchmark_summary、creator_summary、quote、time_range、gap、improvements、commercial_priority 或其他报告字段；这些字段由代码从锁定证据和阶段结果机械生成。stage_state 是必填语义字段；无法完成该阶段判断时填 unknown/conflict/blocked，不得省略后让代码猜测。",
            "仅在能够完整填写且引用合法证据时输出该阶段专属结构化字段；字段不完整就省略该字段，代码会将其视为 unknown，不会补写语义。",
            flag_contract,
            "## 输出严格 JSON",
            json.dumps({
                "stage_group": targets,
                "stages": [{
                    "stage": targets[0],
                    "stage_state": "completed|unknown|conflict|blocked",
                    "relation": "creator_better|benchmark_better|equivalent|uncertain",
                    "model_gap_magnitude": "none|small|medium|large|uncertain",
                    "benchmark_evidence_ids": ["只能引用本阶段 benchmark qualified_evidence ID"],
                    "creator_evidence_ids": ["只能引用本阶段 creator qualified_evidence ID"],
                    "judgment_reason": "只引用本阶段已锁定 evidence ID 的一句话理由",
                }],
            }, ensure_ascii=False, indent=2),
            "stages 必须恰好覆盖目标阶段，顺序与目标阶段相同。",
        ]
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是 Flayr 的小阶段判断器。只输出严格 JSON，不要 Markdown。"},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
    }
    payload.update({"max_completion_tokens": 8192} if str(model).lower().startswith("qwen3.6-plus") else {"max_tokens": 8192})
    return payload


def build_stage_synthesis_payload(
    model: str,
    analysis_input: str,
    facts: dict[str, Any],
    stage_results: list[dict[str, Any]],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Build the read-only Stage3 synthesis request."""
    text = "\n\n".join(
        [
            "# Flayr Stage3 只读综合",
            "只基于已经锁定的六个阶段判断生成全局摘要和改进建议。不得改变任何 stage 的 relation、model_gap_magnitude、stage_state、evidence_ids 或 severity。",
            "建议必须引用已有阶段证据，不得新造事实；如果阶段 unknown，只能明确写待复核，不能当作达人缺陷。",
            "输出 one_line_verdict、one_line_summary、executive_summary、holistic_assessment、key_conclusions、loop_closure、s3_s4_relationship、promise_chain、improvements。",
            "improvements 每项只输出 title,target_stage,problem,suggestion,actions,gmv_reason,gmv_impact；target_stage 必须是 S1-S6。代码会从已锁定阶段结果补齐 gap_type、时间范围、证据 ID、evidence 和 priority，模型不得填写这些机械字段。",
            "## 产品与比较合同",
            json.dumps({"product": analysis.get("product") or {}, "foundation": analysis.get("product_foundation") or {}, "comparison_contract": analysis.get("comparison_contract") or {}}, ensure_ascii=False, indent=2),
            "## 已锁定阶段判断",
            json.dumps(stage_results, ensure_ascii=False, indent=2),
            "## 输出 JSON",
            json.dumps({
                "one_line_verdict": "...",
                "one_line_summary": "...",
                "executive_summary": "...",
                "holistic_assessment": {},
                "key_conclusions": [],
                "loop_closure": {},
                "s3_s4_relationship": {},
                "promise_chain": {},
                "improvements": [],
            }, ensure_ascii=False, indent=2),
        ]
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是只读的 Stage3 综合器。只输出严格 JSON，不要 Markdown。"},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
    }
    payload.update(
        {"max_completion_tokens": 8192}
        if str(model).lower().startswith("qwen3.6-plus")
        else {"max_tokens": 8192}
    )
    return payload


def build_absolute_execution_shadow_payload(
    model: str,
    role: str,
    facts: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """构造单侧绝对执行质量审计请求。

    该请求故意不带另一侧视频、对比结果或 severity。它只消费锁定的单视频事实，
    用于检测成对分析造成的执行分锚定漂移；shadow 结果尚不参与主链评分。
    """
    foundation = analysis.get("product_foundation") if isinstance(analysis.get("product_foundation"), dict) else {}
    product_profile = foundation.get("product_profile") if isinstance(foundation.get("product_profile"), dict) else {}
    category_profile = foundation.get("category_profile") if isinstance(foundation.get("category_profile"), dict) else {}
    brand = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    contract = build_product_proposition_contract(foundation, brand)
    fact_payload = facts.get(role) if isinstance(facts.get(role), dict) else {}
    text = "\n\n".join(
        [
            f"# 单视频绝对执行质量审计：{role}",
            "你只评当前这一条视频。你不知道也不得推测另一条视频、标杆、差距或 severity。",
            "所有结论只能引用下方锁定事实的 evidence id；没有证据就给低分或 low confidence，不能脑补。",
            "## 本品地基（固定尺子）",
            json.dumps(
                {
                    "category_profile": category_profile,
                    "product_profile": product_profile,
                    "proposition_contract": contract,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## 结构库判断视图",
            structure_library_judgment_view(),
            "## 锁定单视频事实（唯一证据来源）",
            json.dumps(fact_payload, ensure_ascii=False, indent=2),
            "## 评分口径",
            "只输出 S1-S4，每个阶段独立给 score=0|0.5|1|2：0=阶段不存在或无效；0.5=有形式但核心功能基本无效、无法有效接收或仅口头宣称；1=阶段目标完成且证据清楚，但呈现基础；2=核心机制围绕本品命题被清楚、聚焦且充分地做出来。2 不能仅因形式存在或卖点数量多获得。",
            "S1：要看开头是否形成具体对象/张力/承诺，并与本品 hook 命题相连。泛泛产品露出或无可感知承诺最高 0.5。",
            "S2：要看产品身份、解决方案角色及与 S1 的自然承接。只报产品名或只报参数最高 0.5。",
            "S3：要看真实使用动作是否让核心卖点被看见、观众是否能接收关键过程。场景多、人物多或 ASMR 只在核心证明成立后才加分。",
            "S4：只看本品 short_video_proof_plan 选定的视觉证明目标是否按对应 S4 类型有效呈现。仅有过程、口播或字幕而没有效果证据不得高分；效果要清楚、聚焦且不靠观众细看猜测才可给 2。",
            "## 输出严格 JSON",
            json.dumps(
                {
                    "role": role,
                    "stage_execution": [
                        {
                            "stage": "S1",
                            "score": 0,
                            "status": "missing|weak|competent|strong",
                            "reason": "一句话，仅说明本侧证据如何支持该分数",
                            "evidence_ids": ["B1"],
                            "proposition_ids": ["hook.1"],
                            "confidence": "high|medium|low",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            "stage_execution 必须恰好四项且顺序为 S1,S2,S3,S4。不得输出比较、差距、另一侧、severity 或建议。",
        ]
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是单视频绝对执行质量审计器。只输出严格 JSON，不要 Markdown。"
                    "必须完全忽略任何未提供的对比对象；不可把品类常识写成视频事实。"
                ),
            },
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
        "max_tokens": 4096,
    }


def build_video_identity_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    visual_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """为 scope 预检提取最小产品身份，不替代完整单视频事实抽取。"""
    info = analysis.get("videos", {}).get(role, {})
    role_dir = Path(str(info.get("work_dir") or ""))
    text = "\n\n".join(
        [
            f"# 单视频产品身份提取：{role}",
            f"- 运营声明产品：{analysis.get('product', {}).get('name') or '未填写'}（只作核对线索）",
            "## 紧凑转写",
            read_text_if_exists(_video_evidence_path(info, "transcript_pack_path")),
            "## OCR 字幕轨",
            read_track_markdown(role_dir / "subtitle_track.json", render_subtitle_track_markdown, "（未生成 OCR 字幕轨）"),
            "## 输出 JSON",
            json.dumps(
                {
                    "product_identity": {
                        "brand_or_product_name": "视频中实际看见、读到或听到的品牌/产品名；无法确认留空。",
                        "brand": "可确认的品牌；无法确认留空。",
                        "product_line": "可确认的产品线/系列；无法确认留空。",
                        "product_category": "视频实际展示的品类；无法确认留空。",
                        "functional_form": "影响使用机制的功能形态，如压粉、散粉、液体胶、卷材胶带；包装颜色和盒子圆方不写这里。",
                        "variant_attributes": ["包装颜色、外壳、色号、容量、套装等不改变核心使用机制的 SKU 属性。"],
                        "core_job": "消费者用它完成的核心任务；无法确认留空。",
                        "target_object": "产品作用对象；无法确认留空。",
                        "use_mechanism": "实际使用或作用机制；无法确认留空。",
                        "desired_outcome": "消费者追求的最终结果；无法确认留空。",
                        "identity_basis": "visible|spoken|subtitle|mixed|unknown",
                        "confidence": "high|medium|low",
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for item in visual_inputs:
        content.extend(
            [
                {"type": "text", "text": f"代表帧：{item['label']}"},
                {"type": "image_url", "image_url": {"url": item["data_url"], "detail": "low"}},
            ]
        )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是视频产品身份提取器。只输出严格 JSON，不要 Markdown；不得用运营声明或常识补全视频中没有的身份；包装颜色、盒子圆方和外壳造型不是功能形态。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
    }


def structure_library_gap_semantics() -> str:
    """Return the canonical pair-level gap semantics used by every stage."""
    path = ROOT / "structure_library_full.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"(### 既有视频 S1-S6 的统一差距语义.*?)(?=\n---|\n## 二、)",
        text,
        flags=re.S,
    )
    return match.group(1).strip() if match else ""


def structure_library_judgment_view(target_stages: list[str] | None = None) -> str:
    """从 structure_library_full.md 抽"判断视图"——每模块只留 编号+名称+一句话功能+【适配条件】，
    扔掉【镜头】【文案】【声音】【节奏】【降级规则】制作规格（那些服务样片生成；喂进判断会诱导
    模型"看模式"扣分，违"看功能不看模式"宪法）。运行时从 full 文档单一来源抽取，不另维护副本。
    用途：补进阶段2 判断上下文，让模型判 module_id/适配时有客观结构骨架可依（此前被砍、锚到空气）。"""
    path = ROOT / "structure_library_full.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    targets = {
        str(stage).strip().upper()
        for stage in (target_stages or [])
        if str(stage).strip().upper() in stage_codes()
    }
    blocks = re.split(r"\n(?=###\s+S[1-6]-[A-Z][:：])", text)
    lines: list[str] = []
    for blk in blocks:
        m = re.match(r"###\s+(S[1-6]-[A-Z])[:：]\s*(.+)", blk.strip())
        if not m:
            continue
        mid, name = m.group(1), m.group(2).strip()
        if targets and mid[:2] not in targets:
            continue
        pre_code = blk[m.end():].split("```", 1)[0]
        func_lines = [ln.strip() for ln in pre_code.splitlines() if ln.strip()]
        func = func_lines[0] if func_lines else ""
        cm = re.search(r"【适配条件】\s*(.*?)(?=\n\s*【|\n```|\Z)", blk, flags=re.S)
        fit = " ".join(ln.strip() for ln in cm.group(1).splitlines() if ln.strip()) if cm else ""
        lines.append(f"- {mid} {name}：{func}｜适配：{fit}")
    return "\n".join(lines)


_BRAND_PAIR_SUFFIX_RE = re.compile(r"-[bc]\d+$")


def resolve_brand_key(run_dir_name: str) -> str:
    """从 run 目录名解析【品】键：去已约定运行前缀和配对后缀；榨汁机族归 juicer。"""
    s = run_dir_name.removeprefix("sample-")
    for prefix in ("validation-", "scope-probe-"):
        s = s.removeprefix(prefix)
    if s.startswith(("wukoubo", "youkoubo")):
        return "juicer"
    return _BRAND_PAIR_SUFFIX_RE.sub("", s)


def load_brand_proposition(
    run_dir: Path | None = None,
    proposition_key: str = "",
) -> dict[str, Any] | None:
    """读冻结的 S1 命题尺子 references/brand_propositions.json，按【品】返回 {propositions, painpoints}。
    显式 proposition_key 是业务身份，适用于线上 UUID/租户目录；目录名只为历史本地 run 兼容回退。
    文件缺失/无该品条目/解析失败 → None（pipeline 据此降级回 Step-0 命题，hook flag 仍会输出）。"""
    path = ROOT / "references" / "brand_propositions.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    entry = None
    if isinstance(data, dict):
        keys: list[str] = []
        explicit_key = str(proposition_key or "").strip()
        if explicit_key:
            keys.append(explicit_key)
        elif run_dir is not None:
            # 兼容已存在的本地 runs；新任务不得把实例目录当业务身份。
            keys.extend(resolve_brand_key(dirname) for dirname in (run_dir.name, run_dir.parent.name))
        for key in keys:
            candidate = data.get(key)
            if isinstance(candidate, dict):
                entry = candidate
                break
    if not isinstance(entry, dict):
        return None
    props = [str(p) for p in entry.get("propositions") or [] if str(p).strip()]
    pains = [str(p) for p in entry.get("painpoints") or [] if str(p).strip()]
    if not props and not pains:
        return None
    return {"propositions": props, "painpoints": pains}
