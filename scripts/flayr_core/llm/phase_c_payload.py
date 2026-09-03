"""Phase C payload builders."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    parse_time_range_seconds,
    parse_timestamp_seconds,
    resolve_artifact_path,
)
from ..multimodal import multimodal_output_example
from ..resources import ResourceBudget
from ..stage_evidence_contracts import (
    stage_analysis_evidence_view,
    stage_analysis_stage_context,
)
from ..stage_ownership import (
    CERTIFICATION_OWNERSHIP_PROMPT,
    CERTIFICATION_POSITION_EXCEPTION_PROMPT,
)
from ..transcript import load_transcript_words, transcript_text_for_range
from ..video_evidence import build_timeline_view_for_range
from .api import can_analyze_native_video, image_to_data_url, video_to_data_url
from .stage_review_contract import patch_fields_for_stage

PHASE_C_WINDOW_PADDING_SECONDS = 2.0
PHASE_C_REVIEW_FPS = 3.0
PHASE_C_REVIEW_MAX_WIDTH = 480


def build_stage_review_payload(
    model: str,
    analysis: dict[str, Any],
    facts: dict[str, Any],
    current_result: dict[str, Any],
    stage_codes: list[str],
    budget: ResourceBudget | None = None,
    api_url: str = "",
) -> dict[str, Any]:
    """Phase C：对低置信阶段切原生视频片段，只返回受限事实补丁。

    这是一次性回看，不允许模型继续索要素材；事实清单仍是唯一事实源。
    """
    target_codes = normalize_stage_codes(stage_codes)[:2]
    analysis_facts = stage_analysis_evidence_view(facts, target_codes)
    target_stages = [
        stage_analysis_stage_context(stage, facts, stage_code(stage.get("stage")))
        for stage in current_result.get("stage_analysis", [])
        if isinstance(stage, dict) and stage_code(stage.get("stage")) in target_codes
    ]
    stage_update_example: dict[str, Any] = {
        "stage": "S4 效果呈现",
        "time_range": "标杆真实时间 / 达人真实时间",
        "benchmark_time_range": "0.0s - 0.0s",
        "creator_time_range": "0.0s - 0.0s",
        "core_question": "用户能不能看见价值",
        "creator_module_id": "unknown",
        "benchmark_module_id": "unknown",
        "module_fit": "fit | degraded | unfit | unknown",
        "module_fit_reason": "一句话",
        "task_completion": "complete | partial | missing",
        "gap_type": "structural | execution | resource",
        "gap_summary": ["一句话"],
        "voice_performance": {
            "pace": "语速判断",
            "energy": "情绪判断",
            "key_pause": False,
            "note": "一句话",
        },
        "benchmark_summary": "一句话",
        "benchmark_key_message": "一句话",
        "benchmark_evidence_ids": ["B1"],
        "benchmark_visual_evidence": ["一句话"],
        "benchmark_support_status": "supported | voice_only | visual_only | conflict",
        "benchmark_quote": "本地语言口播；没有留空",
        "benchmark_quote_zh": "中文翻译；没有留空",
        "creator_summary": "一句话",
        "creator_key_message": "一句话",
        "creator_evidence_ids": ["C1"],
        "creator_visual_evidence": ["一句话"],
        "creator_support_status": "supported | voice_only | visual_only | conflict",
        "creator_quote": "本地语言口播；没有留空",
        "creator_quote_zh": "中文翻译；没有留空",
        "gap": "达人做了什么→标杆做了什么→对购买意愿影响。",
        "evidence": ["引用时间段、画面或口播证据"],
        "severity": "large | medium | small",
        "creator_execution": "0 | 0.5 | 1 | 2",
        "benchmark_execution": "0 | 0.5 | 1 | 2",
        "painpoint_relevance": "benchmark_only | creator_only | both | none",
        "creator_multimodal": multimodal_output_example(),
        "benchmark_multimodal": multimodal_output_example(),
    }
    stage_patch_examples: list[dict[str, Any]] = []

    def add_stage_patch_example(code: str) -> None:
        stage_patch_examples.append(
            {
                "stage": code,
                "fields": {
                    field: json.loads(json.dumps(stage_update_example[field], ensure_ascii=False))
                    for field in patch_fields_for_stage(code)
                    if field in stage_update_example
                },
            }
        )
    s1_contract = ""
    s2_contract = ""
    s3_contract = ""
    s4_contract = ""
    s5_contract = ""
    s6_contract = ""
    if "S1" in target_codes:
        stage_update_example["stage"] = "S1 Hook"
        stage_update_example["core_question"] = "用户凭什么停下来"
        hook_example = {
            "exists": True,
            "type": "A-G 或 unknown",
            "dims": {"camera": True, "copy": True, "sound": True, "rhythm": True},
            "hook_boundary_seconds": 4.5,
            "hook_boundary_reason": "S1 是痛点/反差/悬念留人，S2 从解决方案承接/产品引出/产品揭晓开始",
            "s2_start_signal": "开始回答 Hook 或把某个东西作为解决方案承接，即使产品尚未出镜",
            "landing_met": True,
            "landing_reason": "只引用 0 到 hook_boundary_seconds 内的时间戳+原话/画面，说明对象/张力/收益方向（承诺、证据或具体未解问题）是否齐全",
            "window_evidence": "0.0s 到 hook_boundary_seconds 内实际出现的画面/口播/字幕",
            "landing_window_leak": False,
            "anchors_proposition": True,
            "proposition_ids": ["hook.1"],
        }
        stage_update_example["creator_hook"] = hook_example
        stage_update_example["benchmark_hook"] = hook_example
        add_stage_patch_example("S1")
        s1_contract = (
            "目标阶段包含 S1 时，stage patch 必须同时包含 creator_hook 与 benchmark_hook；"
            "不得沿用当前阶段判断里的旧 hook。先按 structure_library_full.md 判 S1/S2 边界："
            "S1=抢夺注意力，S2=从 Hook 自然过渡到产品；开始回答 Hook、解决方案承接、产品名/卖点或产品成为主角通常是 S2 起点。"
            "exists 只判是否做了留人尝试，不等于 landing：直接产品介绍中若已有具体用户问题、可感知收益、结果承诺、反常识反差或熟悉场景，应填 exists=true，即使 landing_met=false；只有产品名/规格/泛卖点且没有面向用户的具体问题或承诺，才填 exists=false。"
            "S2-A 承接式引出可早于产品实物或产品名出现，不能等产品画面才切 S2。"
            "landing_met 只能按 0 到 hook_boundary_seconds 内的三件套判：对象明确 + 张力明确 + 可感知承诺/证据或具体未解问题。"
            "痛点提问的答案可以在 S2 承接，不要求 S1 先说出产品；泛泛好评不算具体未解问题。"
            "缺一即 false，禁止用后续 S2/S3 补足；若 landing_reason 引用边界后内容，landing_window_leak=true 且 landing_met=false。"
        )
    if "S2" in target_codes:
        stage_update_example["stage"] = "S2 产品引出"
        stage_update_example["core_question"] = "Hook 如何自然过渡到产品"
        s2_example = {
            "exists": True,
            "merged_with_s3": False,
            "module_type": "A-D 或 unknown",
            "handoff_met": True,
            "s1_s2_compatible": True,
            "product_identity_clear": True,
            "product_role_clear": True,
            "excluded_or_risky_module": False,
            "start_seconds": 4.5,
            "end_seconds": 8.0,
            "handoff_reason": "S1 提出痛点/悬念/结果，S2 用产品身份和解决方案自然接住",
            "evidence_ids": ["C1"],
            "proposition_ids": ["role.1"],
        }
        stage_update_example["creator_s2"] = s2_example
        stage_update_example["benchmark_s2"] = s2_example
        add_stage_patch_example("S2")
        s2_contract = (
            "目标阶段包含 S2 时，stage patch 必须同时包含 creator_s2 与 benchmark_s2；"
            "S2 只判 S1→S2 衔接契约：是否承接 S1、产品身份是否清楚、产品是否成为解决方案/答案。"
            "产品露出不等于产品引出完成；卖点细节/成分/认证/选购建议不要当作 S2 加分，归 S3/S4/S5。"
            "≤15s 且 S2/S3 不可分时 merged_with_s3=true，不因没有独立 S2 扣分。"
        )
    if "S3" in target_codes:
        stage_update_example["stage"] = "S3 使用过程"
        stage_update_example["core_question"] = "用户能不能看见产品如何使用并理解核心卖点"
        s3_example = {
            "exists": True,
            "module_type": "A-E 或 unknown",
            "usage_process_visible": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "process_framing_met": True,
            "action_proof_met": True,
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
            "demonstrated_selling_points": ["动作里实际证明的核心卖点"],
            "missing_selling_points": [],
            "scene_mode": "single_scene|multi_scene|multi_person|hybrid|unknown",
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": False,
            "single_scene_continuity_met": True,
            "single_scene_variation_met": False,
            "multi_scene_logic_met": False,
            "multi_scene_transition_met": False,
            "multi_scene_role_adaptation_met": False,
            "role_design_met": False,
            "role_interaction_met": False,
            "distinct_personas_met": False,
            "steps_clear_met": False,
            "pov_immersive_met": False,
            "presentation_overlays": ["step_breakdown"],
            "fake_or_staged": False,
            "start_seconds": 8.0,
            "end_seconds": 18.0,
            "usage_reason": "真实使用动作中能看见核心卖点如何发生；若只口播卖点则写未被动作证明",
            "evidence_ids": ["C1"],
            "proposition_ids": ["selling.1"],
        }
        stage_update_example["creator_s3"] = s3_example
        stage_update_example["benchmark_s3"] = s3_example
        add_stage_patch_example("S3")
        s3_contract = (
            "目标阶段包含 S3 时，stage patch 必须同时包含 creator_s3 与 benchmark_s3；"
            "S3 只判真实使用过程：有没有使用过程、是否只有结果无过程、是否只口播静态、核心卖点是否在动作里可见、"
            "使用过程证据是否可接收、动作是否在同一窗口形成可复核卖点证明、产品是否实际作用于目标对象、动作是否新施加/位移/激活材料或改变目标状态、关键动作是否能追到目标状态、场景是单场景/多场景/多人/混合、场景组织是否服务卖点。"
            "只口播/字幕说卖点但画面没演，不算 core_selling_point_visible；只有结果没有过程，S3 最高只能算弱；"
            "process_framing_met 只判证据接收质量，合理局部特写不扣分；看不清对象/动作/证明区域时为 false。"
            "action_proof_met 不要求最终效果，但要求产品动作、作用对象、卖点的即时可观察证据同窗出现；不能靠后续效果或口播补足。"
            "action_target_contact_met 要求产品/材料实际作用到目标对象；action_application_change_visible 要求看见动作新施加/位移/激活材料或改变目标状态，不能把触碰已有材料/结果当过程；critical_action_continuity_met 要求看见关键作用动作并能追到目标状态，"
            "准备镜头跳到成品、空中比划、只拿产品都必须为 false。"
            "单场景连续展示只算合格，不能自动判出色；只有核心卖点清楚可见、证据可接收且过程被做厚时才给高执行。"
            "多人使用时记录角色是否清楚、互动是否服务卖点、人物是否有可辨识差异；步骤/第一视角只记录实际做到了什么。"
            "场景丰富、ASMR、第一视角、步骤拆解都不能补偿核心卖点没落地。效果结果归 S4，背书归 S5，不要回填到 S3。"
        )
    if "S4" in target_codes:
        stage_update_example["stage"] = "S4 效果呈现"
        stage_update_example["core_question"] = "用户能不能看见效果并相信效果由产品造成"
        s4_example = {
            "effect_type": "before_after|split_screen|person_vs_person|product_vs_alt|quantified_test|process_visualization|aesthetic_display|none",
            "effect_evidence_state": "none|result_only|verified|uncertain",
            "effect_visible": True,
            "effect_salience": "strong",
            "effect_proposition_matched": True,
            "comparison_control_met": True,
            "closeup_or_focus_met": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_maximized": True,
            "requires_close_inspection": False,
            "effect_attribution_supported": True,
            "result_only_without_process": False,
            "process_linked_effect": True,
            "tamper_or_cut_risk": False,
            "effect_reason": "画面能看见产品使用动作与结果变化之间的可信连接；若只有结果没过程要直说",
            "evidence_ids": ["C1"],
            "proposition_ids": ["proof.1"],
        }
        stage_update_example["creator_s4"] = s4_example
        stage_update_example["benchmark_s4"] = s4_example
        add_stage_patch_example("S4")
        s4_contract = (
            "目标阶段包含 S4 时，stage patch 必须同时包含 creator_s4 与 benchmark_s4；"
            "两侧都必须输出 effect_evidence_state=none/result_only/verified/uncertain（none=没有效果证据，result_only=只有结果图或结果叙述没有因果桥，verified=效果可见且归因/过程可信，uncertain=证据冲突或不足；result_only 不得写成 verified）；"
            "S4 只判效果是否可见、效果是否显著、是否命中核心视觉命题、是否可信地由产品造成。"
            "只有结果没有过程不能直接高分；需要仔细看才有变化时 requires_close_inspection=true 且 effect_salience=subtle；"
            "没有因果桥时 effect_attribution_supported=false，有跳剪/换物/光线变化风险时 tamper_or_cut_risk=true。"
            "必须按 structure_library_full.md 的 S4-A~F 硬约束判 module_constraints_met：A/B 要同对象同光线同构图或同细节区域，"
            "C 要两组人物条件可比，D 要本品与替代方案对照，E 要有日常参照物量化，F 要用特写/慢镜/微距把过程可视化。"
            "必须对照 product_profile.visual_diff_dimensions 判 visual_difference_observed；只看到结构/动作/字幕/口播、但看不出指定维度变化时为 false。"
        )
    if "S5" in target_codes:
        stage_update_example["stage"] = "S5 信任放大"
        stage_update_example["core_question"] = "用户凭什么相信"
        s5_example = {
            "exists": True,
            "module_type": "A-E 或 unknown",
            "trust_evidence_type": "hard|soft|mixed|none|unknown",
            "trust_basis": "authority|traceable_data|independent_user|social_consensus|process_transparency|product_claim|offer_or_spec|none|unknown",
            "trust_source_evidence_ids": ["C1"],
            "trust_source_visible": True,
            "trust_source_credible": True,
            "trust_claim_specific": True,
            "product_relevance_met": True,
            "independent_trust_purpose": True,
            "duplicates_other_stage": False,
            "voice_only": False,
            "risky_or_unsupported": False,
            "start_seconds": 20.0,
            "end_seconds": 24.0,
            "trust_reason": "画面/口播中出现了可验证信任材料；若只是口播孤证要直说",
            "evidence_ids": ["C1"],
            "proposition_ids": ["selling.1"],
        }
        stage_update_example["creator_s5"] = s5_example
        stage_update_example["benchmark_s5"] = s5_example
        add_stage_patch_example("S5")
        s5_contract = (
            "目标阶段包含 S5 时，stage patch 必须同时包含 creator_s5 与 benchmark_s5；"
            "S5 只判独立信任材料：数据背书、权威背书、用户证言、场景广度、过程透明。"
            "硬信任可到 2，软信任封顶 1，口播孤证封顶 0.5；"
            + CERTIFICATION_OWNERSHIP_PROMPT
            + CERTIFICATION_POSITION_EXCEPTION_PROMPT
            + "产品数量、使用时长、参数、价格、赠品、套餐不是独立信任；没有外部来源时填 product_claim 或 offer_or_spec，且 exists=false。"
            + "social_consensus 必须同时有明确目标群体/社区和该群体已表达的共同看法；耐用、数量、时长、价格或泛泛“大家会喜欢”均不得填 social_consensus。"
            + "traceable_data 必须带报告编号、官方/平台页面、可辨识认证或来源截图；产品包装或达人自行报出的数字不算。S5-D 必须真实列出至少两类不同人群/场景来证明适用范围。"
            + "S5-C 开头评论/粉丝问答归 S1；S5-D 不得重复 S3/S4 多场景；S5-E 只认探厂/原料/生产/质检/供应链。"
            + "保健/美妆等高风险品类不得把无来源疗效承诺判为可信信任。"
        )
    if "S6" in target_codes:
        stage_update_example["stage"] = "S6 CTA"
        stage_update_example["core_question"] = "用户为什么现在下单"
        s6_example = {
            "exists": True,
            "module_type": "A-E 或 unknown",
            "direct_order_met": True,
            "action_path_clear": True,
            "soft_purchase_invitation_met": False,
            "offer_or_incentive_clear": True,
            "price_anchor_met": False,
            "urgency_evidence_met": True,
            "gift_stack_met": False,
            "guarantee_clear_met": False,
            "urgency_met": True,
            "product_value_recalled": True,
            "module_fit_met": True,
            "ending_position_met": True,
            "depends_on_valid_s4": True,
            "compliance_risk": False,
            "start_seconds": 25.0,
            "end_seconds": 30.0,
            "cta_reason": "明确购买指令、行动路径和利益点；没有 CTA 要直说",
            "evidence_ids": ["C1"],
            "proposition_ids": ["selling.1"],
        }
        stage_update_example["creator_s6"] = s6_example
        stage_update_example["benchmark_s6"] = s6_example
        add_stage_patch_example("S6")
        s6_contract = (
            "目标阶段包含 S6 时，stage patch 必须同时包含 creator_s6 与 benchmark_s6；"
            "S6 只判购买动作：是否明确下单/点链接/进购物车，路径是否清楚，利益/紧迫/保障是否适配本品。"
            "没有明确路径时，若结尾同时有面向观众的购买邀请和具体利益点，soft_purchase_invitation_met=true，属于软促单而非无 CTA；仅播报促销/价格而未邀请用户行动，仍为 false。"
            "S6-A 记录明确价格锚定，S6-B 记录可核验限时/限量/库存，S6-C 记录具体赠品或组合利益，S6-E 记录清楚保障；"
            "不要把 S4 效果或 S5 信任回填成 CTA；达人 CTA 强于标杆时必须如实记为达人亮点。"
            "价格/优惠出现在开头时归 S1，不算 S6；S6-D 效果总结必须依赖有效 S4 输出。"
        )
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "\n\n".join(
                [
                    "# Phase C 低置信阶段回看",
                    "你将看到低置信阶段对应的 focused window 原生视频画面，以及代码按同一时间窗裁剪的 Fun-ASR 文本。",
                    "当前视觉模型不能直接理解视频音轨；口播语义只能来自标为 window-safe 的 ASR，不得声称听到了音轨。",
                    f"detail_mode=focused_window：每个目标阶段只附阶段时间窗±{PHASE_C_WINDOW_PADDING_SECONDS:g}s 的片段，采样约 {PHASE_C_REVIEW_FPS:g}fps、宽度≤{PHASE_C_REVIEW_MAX_WIDTH}px。",
                    "切片边界可能有缓冲误差，可能混入相邻阶段内容；判断按功能归属，不要把相邻阶段内容算进本阶段。",
                    "若切片内证据不足、画面过稀或关键动作跨出窗口，必须在 review_notes 写明 sparse_window，而不是用主分析旧结论或邻近阶段补证。",
                    "只处理 target_stages 中列出的阶段；不要改写 video_understanding，不要新增 evidence_unit。",
                    "你只能输出事实与证据引用补丁：不得输出或修改 severity、gap、summary、quote、执行分、痛点相关性、improvements 或 multimodal 结论。",
                    "每个补丁必须同时给出双方的 stage evidence_ids 和双方结构化 stage flag；不得只更新一侧，也不得遗漏任一允许字段。",
                    "结构化 stage flag 必须保留 proposition_ids，并只引用下方合同中该阶段 allowed_ids；没有实际命中则填空数组。",
                    s1_contract,
                    s2_contract,
                    s3_contract,
                    s4_contract,
                    s5_contract,
                    s6_contract,
                    "只输出严格 JSON，不要 Markdown。",
                    "输出格式：",
                    json.dumps(
                        {
                            "stage_patches": [
                                *stage_patch_examples
                            ],
                            "review_notes": ["仅描述证据补丁的依据"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "## 目标阶段",
                    json.dumps(target_codes, ensure_ascii=False),
                    "## 本品命题引用合同",
                    json.dumps(current_result.get("product_proposition_contract") or {}, ensure_ascii=False, indent=2),
                    "## 当前阶段判断",
                    json.dumps(target_stages, ensure_ascii=False, indent=2),
                    "## 已校验单视频事实清单（唯一事实来源）",
                    json.dumps(analysis_facts, ensure_ascii=False, indent=2),
                ]
            ),
        }
    ]
    content.extend(
        build_stage_review_video_inputs(
            analysis,
            target_stages,
            model=model,
            api_url=api_url,
            budget=budget,
        )
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr 的低置信阶段复核器。只输出严格 JSON。"
                    "本轮只能基于用户给出的 facts 和原生视频切片，为指定 S1-S6 阶段输出允许的事实与证据引用补丁。"
                    "不得新增、删除或改写 evidence_units；不得输出或改写 severity、gap、summary、quote、support_status、执行分、痛点相关性、improvements 或 multimodal 结论。"
                    "如果目标阶段包含 S1，补丁必须同时包含 creator_hook 与 benchmark_hook，不得复用旧 hook 判断。"
                    # 接地约束：禁止从不可感知音轨脑补话术（kakwan S6 幻觉教训）；不预设判断方向。
                    "视觉判断只能基于切片中真实看到的内容；口播引用只能来自随窗口提供的 Fun-ASR 文本。"
                    "ASR 缺失或时间粒度不足时必须保持 unknown/voice_only，禁止推断或补全话术。"
                    "不要继续要求更多素材。"
                ),
            },
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
        "max_tokens": 8192,
    }


def build_stage_review_video_inputs(
    analysis: dict[str, Any],
    target_stages: list[dict[str, Any]],
    *,
    model: str = "",
    api_url: str = "",
    budget: ResourceBudget | None = None,
) -> list[dict[str, Any]]:
    """为 Phase C 低置信阶段附上对应时间窗的原生视频切片。"""
    if not can_analyze_native_video(api_url, model):
        return []
    content: list[dict[str, Any]] = []
    videos = analysis.get("videos", {})
    for window in stage_review_media_windows(analysis, target_stages):
        code = str(window["stage"])
        role = str(window["role"])
        padded_start = float(window["start_seconds"])
        padded_end = float(window["end_seconds"])
        info = videos.get(role) or {}
        if isinstance(info, dict):
            video_path = Path(str(info.get("path") or ""))
            if not video_path.is_file():
                continue
            artifact_dir = Path(str(info.get("work_dir") or "")).expanduser()
            if not artifact_dir.is_dir():
                continue
            words = load_transcript_words(info)
            transcript_text = transcript_text_for_range(words, padded_start, padded_end) if words else ""
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"【Phase C 窗口安全 Fun-ASR｜{role}｜{code}｜"
                        f"{format_seconds(padded_start)} - {format_seconds(padded_end)}】\n"
                        + (
                            transcript_text
                            if transcript_text
                            else "（无词级窗口安全口播；不得使用粗粒度 SRT 推断本窗口口播）"
                        )
                    ),
                }
            )
            timeline_view = build_timeline_view_for_range(
                artifact_dir,
                info,
                f"phase_c_{code}_{role}",
                padded_start,
                padded_end,
            )
            timeline_path = resolve_artifact_path(
                info,
                timeline_view.get("path") if isinstance(timeline_view, dict) else "",
                require_file=True,
                require_root=True,
            )
            if timeline_view and timeline_path is not None:
                evidence_views = analysis.setdefault("phase_c_evidence_views", [])
                if isinstance(evidence_views, list):
                    if not any(
                        isinstance(item, dict)
                        and item.get("path") == timeline_view.get("path")
                        for item in evidence_views
                    ):
                        evidence_views.append({**timeline_view, "stage": code, "role": role})
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"【Phase C 证据时间线｜{role}｜{code}｜"
                            f"{format_seconds(padded_start)} - {format_seconds(padded_end)}】"
                        ),
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(timeline_path), "detail": "low"},
                    }
                )
            data_url = video_to_data_url(
                video_path,
                fps=PHASE_C_REVIEW_FPS,
                max_width=PHASE_C_REVIEW_MAX_WIDTH,
                start=padded_start,
                duration=max(0.5, padded_end - padded_start),
                budget=budget,
            )
            if data_url is None:
                continue
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"【Phase C 回看视频｜{role}｜{code}｜"
                        f"{format_seconds(padded_start)} - {format_seconds(padded_end)}｜"
                        f"detail=focused_window｜fps≈{PHASE_C_REVIEW_FPS:g}｜max_width={PHASE_C_REVIEW_MAX_WIDTH}】"
                    ),
                }
            )
            content.append({"type": "video_url", "video_url": {"url": data_url}})
    return content


def stage_review_media_windows(
    analysis: dict[str, Any],
    target_stages: list[dict[str, Any]],
    facts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every focused Phase C window before media encoding."""
    windows: list[dict[str, Any]] = []
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    for stage in target_stages:
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage.get("stage"))
        if not code:
            continue
        stage_context = stage_analysis_stage_context(stage, facts, code) if facts is not None else stage
        for role in ("benchmark", "creator"):
            info = videos.get(role) if isinstance(videos.get(role), dict) else {}
            parsed = parse_time_range_seconds(
                stage_context.get(f"{role}_time_range"),
                info.get("duration_seconds"),
            )
            if parsed is None:
                continue
            start, end = parsed
            raw_duration = info.get("duration_seconds")
            duration_value = parse_timestamp_seconds(raw_duration)
            if raw_duration is not None and str(raw_duration).strip() and duration_value is None:
                continue
            duration_value = end if duration_value is None else duration_value
            windows.append(
                {
                    "stage": code,
                    "role": role,
                    "source_start_seconds": round(start, 3),
                    "source_end_seconds": round(end, 3),
                    "start_seconds": round(max(0.0, start - PHASE_C_WINDOW_PADDING_SECONDS), 3),
                    "end_seconds": round(min(duration_value, end + PHASE_C_WINDOW_PADDING_SECONDS), 3),
                }
            )
    return windows


def normalize_stage_codes(values: list[str]) -> list[str]:
    codes: list[str] = []
    for value in values:
        code = stage_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


def stage_code(value: Any) -> str:
    match = re.search(r"S[1-6]", str(value or "").upper())
    return match.group(0) if match else ""
