"""Full-analysis and legacy-repair LLM payload constructors."""

from __future__ import annotations

import json
from typing import Any

from ..proposition_contract import build_product_proposition_contract
from ..multimodal import render_multimodal_prompt_contract
from ..stage_evidence_contracts import stage_analysis_evidence_view
from ..stage_ownership import CERTIFICATION_OWNERSHIP_PROMPT

QWEN36_PLUS_MODEL_PREFIX = "qwen3.6-plus"
GENERIC_FULL_ANALYSIS_OUTPUT_BUDGET = 32768
QWEN36_PLUS_FULL_ANALYSIS_OUTPUT_BUDGET = 65536


def _uses_qwen36_plus_completion_budget(model: str) -> bool:
    return str(model or "").strip().lower().startswith(QWEN36_PLUS_MODEL_PREFIX)


def full_analysis_output_budget(model: str) -> int:
    """Output budget for Flayr's full six-stage JSON contract."""
    # Qwen3.6 Plus supports a 64K max completion budget.  Its thinking content
    # is included in that budget, so the full contract must use the provider's
    # completion-token field rather than the deprecated answer-only max_tokens.
    if _uses_qwen36_plus_completion_budget(model):
        return QWEN36_PLUS_FULL_ANALYSIS_OUTPUT_BUDGET
    # Preserve the existing generic-provider ceiling.
    return GENERIC_FULL_ANALYSIS_OUTPUT_BUDGET


def full_analysis_output_fields(model: str) -> dict[str, int]:
    """Return the provider-appropriate output-limit fields for full analysis."""
    budget = full_analysis_output_budget(model)
    if _uses_qwen36_plus_completion_budget(model):
        return {"max_completion_tokens": budget}
    return {"max_tokens": budget}


def build_llm_payload(
    model: str,
    analysis_input: str,
    visual_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """通用对比分析 payload。"""
    user_content: str | list[dict[str, Any]]
    if visual_inputs:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{analysis_input}\n\n"
                    "## 随请求附带的关键帧\n\n"
                    "以下图片覆盖爆款/达人视频的全片时间线，并额外包含 Hook/CTA 加密关键帧。"
                    "必须先浏览全片时间线，再识别每个视频自己的 S1-S6 阶段边界。"
                    "不要因为参考结构里的常见秒数，把长视频的中后段误判成早期阶段。"
                ),
            }
        ]
        for item in visual_inputs:
            content.extend(
                [
                    {"type": "text", "text": f"图片：{item['label']}，本地路径：{item['path']}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": item["data_url"],
                            "detail": "low",
                        },
                    },
                ]
            )
        user_content = content
    else:
        user_content = analysis_input

    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr 的 TikTok Shop 带货短视频分析器。"
                    "只输出严格 JSON，不要 Markdown，不要解释。"
                    "建议必须围绕 GMV、停留、信任、下单行动。"
                    "分析必须严格遵循输入中的三步分析流程：第一步，整体感知并输出 one_line_verdict、holistic_assessment，不引用具体证据；"
                    "第二步，输出 product_visibility，并将事实证据映射到 structure_library_full.md 的 S1-S6 语义阶段、官方模块编号、模块适配性和真实时间边界；"
                    "第三步，输出 loop_closure，并基于被引用证据比较 gap_type 和提升点。"
                    "输出必须精炼，但不得用固定条数截断 Stage1 evidence_units、阶段引用或能证明因果链的事实；"
                    "各数组遵循各自字段合同的明确上限，没有明确上限时按覆盖完整性输出，不为凑数重复拆分；"
                    "每个描述字段最多一句，improvements 按 GMV 杠杆排序输出 1-5 条；视频值得改的点确实多就给 3-5 条，确实只有 1-2 个 GMV 杠杆点就给 1-2 条，不要为凑数编造。"
                    "禁止重复同一判断，禁止为了描述缺失而枚举不存在的音效、卡点、镜头或功能；缺失内容用一句“未发现对应证据”概括。"
                    "不要把 0~3s、3~6s 等参考时间当作固定切片。"
                    "stage_analysis 必须固定输出六项且顺序为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA；每个阶段都必须分别写 benchmark_time_range 和 creator_time_range，并写 creator_module_id、benchmark_module_id、module_fit、module_fit_reason、task_completion、gap_type、gap_summary 和 voice_performance。"
                    "有有效口播时，benchmark_key_message/creator_key_message 必须以该段实际口播传递的信息为核心，再选择确实支持该信息的画面证据。"
                    "没有有效口播时，必须以可见画面与字幕为核心，不得把音乐或推测写成信息。"
                    "每个阶段必须引用 video_understanding 中的 evidence_ids，并写 visual_evidence 和 support_status："
                    "口播与画面共同支持为 supported；口播提及但画面不能验证为 voice_only；仅画面/字幕承载信息为 visual_only；两者矛盾为 conflict。"
                    "阶段引用的事实时间必须与该阶段时间相交；若某阶段没有足够独立证据，必须保留该阶段为 unknown/待复核并留空引用，不能为了填满阶段创建占位事实，也不能借用其他阶段事实。"
                    "模型输入不包含原始 transcript.srt 或原始词级索引；口播窗口归因只能使用窗口安全口播时间线。没有词级时间戳时必须标记时间粒度不足或调整阶段边界。"
                    "不得写某张画面展示了认证、成分或效果，除非附带关键帧中实际可见。"
                    "只可把请求中实际附带的关键帧视为已观察画面；未被附图覆盖的时段不得臆造镜头内容，应写为画面证据不足待复核。"
                    "同一关键信息只归入一个最主要阶段，禁止在多个阶段重复作为表现依据。"
                    + CERTIFICATION_OWNERSHIP_PROMPT
                    + "每个阶段都应从转写中摘录对应本地语言口播到 benchmark_quote/creator_quote，并附中文翻译；没有明确口播时留空。"
                    + "每个阶段和提升点都必须写 evidence，引用时间段、画面或口播证据。"
                    + "提升点按 GMV 杠杆排序，不按 S1-S6 顺序凑数：CTA 与 Hook 的大差距优先于中等信息传递差距。"
                    + "每个提升点必须先抽象标杆功能意图，再结合产品决策权重和达人现有拍法生成原创可执行建议；不得把标杆卖点、原句或动作机械搬给达人。"
                    + "涉及卖点时，必须使用第 0 步商业权重判断理性/感性哪个更能驱动该品类，而不是硬凑两者或照搬标杆。"
                    + "儿童牙膏等两极产品逻辑品类：若达人已讲清按压、用量、减少浪费等功能痛点，标杆香味/口味/调性只能作辅助体验，不得自动排到功能卖点前，不得作为 Top 1 提升点；不得建议新增孩子演员、品尝动作、闻香镜头或“孩子一定喜欢”等不可验证表达。"
                    + "suggestion 必须优先在达人已有素材和拍摄方式内改造；只有 no_suitable_frame 时才建议补拍或补素材。"
                    + "达人建议话术必须使用达人口播语言，creator_script_zh 只放中文翻译。"
                    + "如果达人没有有效口播或语言识别不可靠，则根据标杆视频语言/目标市场语言撰写全新的本地语言建议话术，不得把音乐、噪音或无关字幕当作话术。"
                    "达人执行话术必须是针对达人素材重新设计的原创表达，不得抄写或轻微改写标杆口播。"
                    "每个提升点必须输出 base_frame_suitability。只有达人现有画面确实适合作为目标改造基底时，才可写 usable 和 best_base_frame_time；"
                    "如达人素材缺少目标所需的人物、产品或场景，必须写 no_suitable_frame，best_base_frame_time 留空，并在建议中明确需补拍或补素材。"
                    "每项提升点还必须输出 benchmark_evidence_ids 与 base_frame_evidence_id；前者只可指向所属阶段的标杆事实证据，后者必须指向基底帧所在的达人事实证据。"
                    "base_frame_reason 只能描述该达人证据中真实可见的素材，不得把不存在的人物、口播或场景说成已有素材。"
                    "严禁臆造品牌、型号、价格、优惠、参数或功效。"
                    "只有产品信息、转写或画面证据明确出现时才能写具体品牌；不确定时用用户提供的产品名或本地语言中的中性产品指代。"
                    "对于维生素、营养补充品等健康品类，不得在建议话术中声称治疗疾病、调节激素、改善月经、排出血块或保证效果；标杆中出现此类表达时只能作为合规风险指出。"
                    "\n\n## 关键质量约束（必须遵守）\n"
                    "1. holistic_assessment 六维必须独立评估：structure_integrity 回答'结构是否连贯'，selling_point_efficiency 回答'卖点讲清楚没'，"
                    "audience_resonance 回答'目标用户有没有代入感'，pace_and_emotion 回答'节奏让不让人想看下去'，"
                    "trust_and_purchase_impulse 回答'看完想不想买'，conversion_prediction 回答'购买意愿是立刻想买/犹豫/完全不想买'。"
                    "每维用不同措辞从不同角度写，禁止复制粘贴同一段话。\n"
                    "2. 必须输出 key_conclusions 数组（1-5 条）：完成 S1-S6 对比后，代入本地目标消费者视角，回答'为什么看完标杆想买、看完达人不想买'。"
                    "每条说：达人做了什么→标杆做了什么→对购买意愿的影响。可跨阶段，用消费者语言，不用技术术语。按 GMV 影响从大到小排列。\n"
                    "3. severity 评级（必须差异化，large/medium/small 至少出现 2 种）。判级前先在 gap 字段写清判断依据"
                    "（达人做了什么→标杆做了什么→对目标消费者购买意愿的影响），再据此给 severity，做到推理在前、结论在后。\n"
                    "   可操作判据（按对购买意愿的影响定级，而非按画面差异大小）：\n"
                    "   - large：直接影响购买意愿的硬伤——该环节功能缺失或严重跑偏，会让目标消费者明显更不想买（如 Hook 留不住人、核心卖点讲错、CTA 缺失）；\n"
                    "   - medium：削弱说服力但不致命——功能基本完成，但执行短板让消费者购买意愿打折扣（如卖点讲了但不突出、场景代入感不足）；\n"
                    "   - small：细节瑕疵或达人不输标杆——功能完成且到位，仅细微差距，或达人做得持平甚至更优。\n"
                    "   达人做到位或持平的阶段必须给 small；gap 判定'无明显差距'时 severity 必须是 small。\n"
                    "4. 商业权重必须按品类自适应：Hook 恒高权重；儿童牙膏这类低客单但需说服的功能理性品类，Hook、核心卖点、效果验证和清晰 CTA 优先于调性/BGM。"
                    "关键结论和 improvements 中，Hook/卖点/效果验证/CTA 不得被低权重调性问题排到后面。\n"
                    "5. 达人有效、标杆弱时要记为达人亮点，不判达人差距；例如达人有明确购买指令而标杆没有独立 CTA，S6 应判达人略优或 small，不得判差距中等。\n"
                    "6. S3 只判真实使用过程中核心卖点是否被动作演示出来；闻香、口味、质感等感官体验归 S4 效果验证。给理由归 S5，给下单指令归 S6。\n"
                    "7. 使用目标市场知识库做文化视角校准：马来/东南亚语境下，真实生活感、轻语气、本地口语、划算/省/方便、节日紧迫感等可能是正向信号；"
                    "但知识库只用于判断有效性，不得替代视频证据，不得在报告中直接展开。\n"
                    "8. gap_type 判断：模块不同=structural，模块同但执行差=execution，资源条件限制=resource。\n"
                    "9. 同一信息只归入功能最匹配的一个阶段，后续阶段不重复。S1 提过的关键词 S2 不再重复分析。"
                    "S5 是可选的信任放大环节：只有双方 Stage1 都已完整核验为 absent 时，key_message 才写'双方均未使用独立信任放大'；一侧有真实背书、另一侧没有时，必须保留 S5 比较并如实描述差距，不能用品类先验把标杆事实抹掉。"
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "temperature": 0.2,
        # 完整 stage_analysis + improvements 需要超过 16K tokens；
        # 与 full_analysis_output_budget 保持一致，避免进入昂贵的 repair 路径。
        **full_analysis_output_fields(model),
    }


def build_llm_repair_payload(
    model: str,
    raw_result_text: str,
    error_message: str,
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the compatibility repair payload for imported legacy results."""
    locked_facts_block = ""
    native_audio = bool(((analysis or {}).get("audio_assessment") or {}).get("native_audio_analysis", True))
    if locked_video_understanding:
        # The finalizer preserves the locked raw facts itself.  The repair
        # model only needs the qualification-filtered analysis view; exposing
        # the raw list here would let it reason from facts that the target
        # stage never qualified.
        locked_facts_block = json.dumps(
            stage_analysis_evidence_view(locked_video_understanding),
            ensure_ascii=False,
            indent=2,
        )
    foundation = (analysis or {}).get("product_foundation") or {}
    brand = (analysis or {}).get("brand_proposition") or {}
    repair_contract = build_product_proposition_contract(foundation, brand)
    repair_contract_block = json.dumps(repair_contract, ensure_ascii=False, indent=2)
    return {
        "model": model,
        **full_analysis_output_fields(model),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr JSON 修复器。只输出严格 JSON，不要 Markdown，不要解释。"
                    "必须符合 references/analysis-output-schema.json：保留 one_line_verdict、holistic_assessment（每维独立评估）、key_conclusions（1-5 条消费者视角）、product_visibility、loop_closure、s3_s4_relationship、promise_chain，6 个 stage_analysis，1-5 个 improvements（按 GMV 杠杆排序）。"
                    "如果原始输出缺少 improvements（如 JSON 被截断），必须基于 stage_analysis 的差距分析补充 1-5 条。"
                    "severity 是本轮模型参考判断，不要为了凑分布强行改写；只有显式、可追溯事实才能触发 resolver 的 floor/ceiling 约束，缺失、unknown 或 uncertain 不触发。"
                    "必须保留 video_understanding 证据事实清单。stage_analysis 必须严格按 S1、S2、S3、S4、S5、S6 顺序输出六项；阶段必须保留 benchmark_time_range、creator_time_range、证据引用、核心信息、画面证据和 support_status；达人话术必须保留本地语言和中文翻译。"
                    "stage_evidence_links 必须为每个阶段引用登记 stage_id、role、evidence_id、relation、linking_reason、confidence；只能链接已锁定 Stage1 事实，不得创建、改写或移动 evidence_unit。"
                    "每个阶段引用的事实单元时间必须与阶段时间相交；缺少独立内容的阶段保留 unknown/待复核并留空引用，不得创建新的 Stage1 事实单元。"
                    "修复 evidence_ids 时必须保持阶段归属：stage 的 benchmark/creator_evidence_ids，以及每个嵌套 stage flag 的 evidence_ids，"
                    "只能引用对应侧、时间与该阶段 time_range 相交的已锁定 evidence_unit；嵌套 flag 的 evidence_ids 必须是该阶段主 evidence_ids 的子集。"
                    "相邻阶段的事实不能为了支撑语义跨阶段借用，也不得移动阶段时间范围；如果相邻事实更符合语义，必须按当前阶段窗口内事实重判，不能引用相邻阶段 ID。"
                    "尤其 S4 不得把 S5 的用户评论、认证或反馈引用成效果证据；S4 窗口只有使用、成分或静态展示时，按该窗口事实判断，不得借邻段结果补足。"
                    "以窗口安全口播时间线校对口播对应阶段；不得用未随请求发送的原始转写整段补阶段；"
                    + CERTIFICATION_OWNERSHIP_PROMPT
                    + "一条事实只归属一个主要阶段；口播提及但画面不可见时标记 voice_only。"
                    + render_multimodal_prompt_contract(native_audio)
                    + "每个阶段必须补齐 creator_multimodal 与 benchmark_multimodal；只能引用该侧该阶段已有 evidence_ids，不得为补多模态字段新增事实。"
                    "S1 Hook 必须补齐 creator_hook 与 benchmark_hook 两个对象，字段为 exists(bool)、type(A-G 或 unknown)、dims{camera,copy,sound,rhythm}(bool)、hook_boundary_seconds(number)、hook_boundary_reason(非空)、s2_start_signal（通常非空）、landing_met(bool)、landing_reason(非空)、window_evidence（通常非空）、landing_window_leak(bool)、anchors_proposition(bool)、evidence_ids(数组)、proposition_ids(数组)。仅当该侧 Stage1 已闭合为 absent、exists=false 且 evidence_ids=[] 时，s2_start_signal 与 window_evidence 可为空，不得编造转换信号或窗口证据。exists 只判是否有具体面向用户的留人尝试；弱 Hook 可以 exists=true、landing_met=false，不能与完全无 Hook 混淆。"
                    "hook_boundary_seconds 按 structure_library_full.md 的 S1 留人机制→S2 产品引出/解决方案承接功能切换判断，不得写死固定秒数；S2-A 承接式引出可早于产品实物或产品名出现，不能等产品画面才切 S2。"
                    "landing_met 按 type 无关三件套判断：0 到 hook_boundary_seconds 内对象明确、张力明确、可感知承诺/证据或具体未解问题，缺一即 false；痛点提问的答案可以在 S2 承接，不要求 S1 先说出产品；不得用后续 S2/S3 产品介绍补足 S1 landing。若引用边界后材料，landing_window_leak=true 且 landing_met=false。"
                    + "S2 产品引出必须补齐 creator_s2 与 benchmark_s2 两个对象，字段为 exists(bool)、merged_with_s3(bool)、module_type(A-D或unknown)、handoff_met(bool)、s1_s2_compatible(bool)、product_identity_clear(bool)、product_role_clear(bool)、excluded_or_risky_module(bool)、start_seconds(number)、end_seconds(number)、handoff_reason(非空)、evidence_ids(非空数组)、proposition_ids(数组)。"
                    "S3 使用过程必须补齐 creator_s3 与 benchmark_s3 两个对象，字段为 exists(bool)、module_type(A-E或unknown)、usage_evidence_state(none|partial|complete|uncertain)、usage_process_visible(bool)、result_only_without_process(bool)、mouth_only_or_static(bool)、real_usage_met(bool)、core_selling_point_visible(bool)、process_framing_met(bool)、action_proof_met(bool)、action_target_contact_met(bool)、action_application_change_visible(bool)、critical_action_continuity_met(bool)、demonstrated_selling_points(数组)、missing_selling_points(数组)、scene_mode(single_scene/multi_scene/multi_person/hybrid/unknown)、usage_context_fit(bool)、continuity_met(bool)、richness_met(bool)、single_scene_continuity_met(bool)、single_scene_variation_met(bool)、multi_scene_logic_met(bool)、multi_scene_transition_met(bool)、multi_scene_role_adaptation_met(bool)、role_design_met(bool)、role_interaction_met(bool)、distinct_personas_met(bool)、steps_clear_met(bool)、pov_immersive_met(bool)、presentation_overlays(数组)、fake_or_staged(bool)、start_seconds(number)、end_seconds(number)、usage_reason(非空)、evidence_ids(数组；usage_evidence_state=none 且没有使用事实时可为空，否则必须非空)、proposition_ids(数组)。"
                    "S4 效果呈现必须补齐 creator_s4 与 benchmark_s4 两个对象，字段为 effect_type(before_after/split_screen/person_vs_person/product_vs_alt/quantified_test/process_visualization/aesthetic_display/none)、effect_evidence_state(none/result_only/verified/uncertain)、effect_visible(bool)、effect_salience(none/subtle/clear/strong)、effect_proposition_matched(bool)、comparison_control_met(bool)、closeup_or_focus_met(bool)、visual_difference_observed(bool)、module_constraints_met(bool)、effect_maximized(bool)、requires_close_inspection(bool)、effect_attribution_supported(bool)、result_only_without_process(bool)、process_linked_effect(bool)、tamper_or_cut_risk(bool)、effect_reason(非空)、evidence_ids(数组；effect_type=none 且 effect_visible=false 且 effect_evidence_state=none 时可为空，否则必须非空)、proposition_ids(数组)。"
                    "S5 信任放大必须补齐 creator_s5 与 benchmark_s5 两个对象，字段为 exists(bool)、module_type(A-E或unknown)、trust_evidence_type(hard/soft/mixed/none/unknown)、trust_basis(authority/traceable_data/independent_user/social_consensus/process_transparency/product_claim/offer_or_spec/none/unknown)、trust_source_evidence_ids(数组；只允许引用 Stage1 同类型且带来源说明的证据)、trust_source_visible(bool)、trust_source_credible(bool)、trust_claim_specific(bool)、product_relevance_met(bool)、independent_trust_purpose(bool)、duplicates_other_stage(bool)、voice_only(bool)、risky_or_unsupported(bool)、start_seconds(number)、end_seconds(number)、trust_reason(非空)、evidence_ids(数组；exists=false 或 trust_evidence_type=none/unknown 可为空)、proposition_ids(数组)。"
                    "S6 CTA 必须补齐 creator_s6 与 benchmark_s6 两个对象，字段为 exists(bool)、module_type(A-E或unknown)、direct_order_met(bool)、action_path_clear(bool)、soft_purchase_invitation_met(bool)、offer_or_incentive_clear(bool)、price_anchor_met(bool)、urgency_evidence_met(bool)、gift_stack_met(bool)、guarantee_clear_met(bool)、urgency_met(bool)、product_value_recalled(bool)、module_fit_met(bool)、ending_position_met(bool)、depends_on_valid_s4(bool)、compliance_risk(bool)、start_seconds(number)、end_seconds(number)、cta_reason(非空)、evidence_ids(数组；exists=false 可为空)、proposition_ids(数组)。"
                    "必须补齐 s3_s4_relationship 和 promise_chain；promise_chain.chain_closed 必须是 bool，broken_at 只能是 S2/S3/S4/none/unknown；promise_chain 只审计 S1-S4，不得把 S5/S6/CTA/促单/下单问题写成承诺链断点。"
                    "提升点必须保留 benchmark_evidence_ids、base_frame_suitability、best_base_frame_time、base_frame_evidence_id 和 base_frame_reason；无可用达人素材时写 no_suitable_frame 且时间与 base_frame_evidence_id 留空。"
                    "修复 improvements 时也必须遵循达人框架约束、卖点适配权重和标杆功能意图转译，不得把 benchmark_reference 直接改写成 suggestion。"
                    "健康品类建议不得声称调节激素、改善月经、治疗症状或虚构优惠。建议话术必须重新设计，不得复制标杆原句。"
                    "输出必须精炼，每个描述字段最多一句，improvements 按 GMV 杠杆排序保留 1-5 条；不要为凑数编造。"
                    "各列表遵循字段合同的明确上限；没有明确上限时不要为了简洁截断事实。"
                    "不要枚举或重复不存在的音效、镜头或功能，缺失证据只写一句概括。"
                    "保留原分析含义，但补齐缺失字段、修正字段类型和 JSON 语法。"
                    "S5 修复硬规则：若 trust_source_evidence_ids 为空，或 Stage1 没有带同类 trust_source_signals 与 trust_source_reference 的来源，不得保留 authority/traceable_data/independent_user/social_consensus/process_transparency 任何独立信任 basis；仅有产品/来源自述时改为 trust_basis=product_claim，否则改为 trust_basis=none 或 unknown，并同步 exists=false、independent_trust_purpose=false、trust_source_evidence_ids=[]、proposition_ids=[]。"
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        "原始分析输入摘要：",
                        analysis_input[:12000],
                        "校验错误：",
                        error_message,
                        "已锁定单视频事实清单的阶段分析视图（原始审计清单不向修复模型开放；补字段只能引用这里，不得新增/改写 evidence_units）：",
                        locked_facts_block[:24000] if locked_facts_block else "（未提供 locked facts；只能修 JSON 结构，不得补事实依据）",
                        "本品命题引用合同（proposition_ids 只能引用对应阶段 allowed_ids；合同为空时保留原引用或填空数组，不得新造 ID）：",
                        repair_contract_block,
                        "模型原始输出：",
                        raw_result_text[:12000],
                    ]
                ),
            },
        ],
        "temperature": 0.0,
    }
