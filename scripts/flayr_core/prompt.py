"""flayr_core.prompt：analysis_input.md 装配。

把 harness（flayr.py）原先的"prompt 装配"职责整体迁出。本模块负责：
  - 把 analysis dict + 关键帧 manifest + 转写 + 翻译 + 结构库 + ANALYSIS-PROMPT
    拼接成给 LLM 的 analysis_input.md 输入包
  - 提供 speech_status / read_analysis_prompt / render_*_markdown 等辅助

迁出原因（详见 ARCHITECTURE.md 5.1）：
  - 凝聚度：prompt 装配是独立子系统，不应混在 CLI harness 里
  - 变更频率：prompt 内容每次 LLM 调优都要改，CLI 参数几乎不动

依赖：仅依赖 artifacts（帧 manifest 读取）和 utils（read_optional_text）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import (
    format_seconds,
    get_focus_frame_entries,
    get_frame_entries,
    get_stage_frame_entries,
    sample_evenly,
)
from .utils import read_optional_text


ROOT = Path(__file__).resolve().parents[2]


def write_analysis_input(run_dir: Path, analysis: dict[str, Any]) -> Path:
    """把 analysis dict 装配成 analysis_input.md 并写入 run_dir。"""
    market_knowledge = render_market_knowledge(analysis)
    lines = [
        "# Flayr 大模型分析输入包",
        "",
        "请先完整理解两条视频的口播、字幕与画面事实，再完成 S1-S6 阶段归因、爆款/达人对比、Top 3~5 提升点筛选。",
        "重点：只输出对 GMV 有帮助的具体差距，不要泛泛评价。",
        "",
        "## 分析等级与结论边界",
        "",
        f"- 当前等级：{analysis['analysis_scope']['label']}",
        f"- 缺失业务上下文：{'、'.join(analysis['analysis_scope']['missing_context']) or '无'}",
        f"- 结论边界：{analysis['analysis_scope']['boundary']}",
        "",
        "## 产品信息",
        "",
        f"- 产品名：{analysis['product']['name']}",
        f"- 品类：{analysis['product']['category'] or '缺失'}",
        f"- 价格：{analysis['product']['price']}",
        f"- 目标市场：{analysis['product'].get('target_market') or 'auto'}",
        f"- 核心卖点/差异化：{analysis['product']['core_selling_points'] or '缺失'}",
        f"- 目标用户/核心痛点：{analysis['product']['target_user'] or '缺失'}",
        f"- 购买动机：{analysis['product']['purchase_motivation'] or '缺失'}",
        f"- 达人账号背景：{analysis['product']['creator_profile'] or '未提供（可选）'}",
        f"- 备注：{analysis['product']['notes'] or '无'}",
        "",
        "## 视频观察指引（看视频的方法 - 优先级高于流程层与结构层）",
        "",
        read_optional_text(ROOT / "references" / "observation-guide.md"),
        "",
        "## 商业评判框架（判断差距权重的方法 - 优先级高于一般表达偏好）",
        "",
        read_optional_text(ROOT / "references" / "commercial-judgement-framework.md"),
        "",
        "## 目标市场知识库（仅作判断依据，不在报告呈现）",
        "",
        market_knowledge,
        "",
        "## 分析方法（必须严格遵循）",
        "",
        read_analysis_prompt(),
        "",
        "## structure_library_full.md 全量模块定义（模块识别与适配校验依据）",
        "",
        read_optional_text(ROOT / "structure_library_full.md"),
        "",
        "## 输出要求",
        "",
        "1. 严格按 ANALYSIS-PROMPT.md 的第一步、第二步、第三步输出结构化 JSON：第一步整体感知不得引用证据；第二、三步必须引用时间与证据。",
        "1a. 当当前等级为“视频证据分析”时，仍须完成结构、口播、字幕、画面证据与对标差距分析；不得假定未提供的真实卖点、价格策略、目标人群适配或最终 GMV 排序，相关判断必须表述为待确认。",
        "1b. 当当前等级为“策略增强分析”时，才可结合已确认的品类、价格、卖点、人群和购买动机输出完整成交诊断与 GMV 优先级。",
        "2. 先为爆款与达人分别输出 video_understanding：沿全片时间线列出 evidence_units，只记实际口播、可读字幕和可见画面事实，此步骤不要先套 S1-S6。",
        "3. 再将 evidence_units 归入 S1-S6。每段必须输出 structure_library_full.md 官方 module_id、module_fit、task_completion、gap_type、voice_performance；不得自创模块。",
        "4. 有有效口播时，阶段核心信息以口播实际传递的信息为主，再匹配支持它的画面；无有效口播时，以画面和字幕为核心。每个阶段写 evidence_ids、visual_evidence 与 support_status。",
        "3a. 阶段引用的 evidence_unit 时间必须与该阶段时间相交；若某阶段没有独立内容，也应建立该时段的“未发现对应内容”事实单元，不能挪用其他阶段证据。",
        "3b. `transcript.srt` 的时间戳是口播归因的权威依据；口播句不在阶段时间内时，必须调整阶段边界或停止引用该口播。",
        "4. 同一关键信息只能归属于一个主要阶段。KKM/认证/审批不是 Hook；若与产品卖点一起出现，归入 S2 作为信任支撑；只有独立证据段落才归入 S5。口播提到但画面未显示时，必须标明口播声称、画面未验证。",
        "5. 每个阶段从原始转写中摘录对应本地语言口播到 benchmark_quote/creator_quote，并附中文翻译；无明确口播则留空；不得将画面未显示的信息写成画面证据。",
        "6. 提升点输出 GMV 杠杆最高的 1-5 条，按优先级排序，不按阶段顺序凑数；CTA 与 Hook 的重大差距优先；必须具体到时间段、画面、话术或节奏。",
        "7. 每个提升点输出 base_frame_suitability。达人全片确有可改造真实基底时写 usable 与 best_base_frame_time；没有目标所需的人物/产品/场景时写 no_suitable_frame，时间留空并要求补拍/补素材。",
        "8. 每个提升点输出 benchmark_evidence_ids 与 base_frame_evidence_id。标杆参考只能引用所属阶段证据；AI 基底理由只能描述对应达人证据中真实可见的素材，不能把无口播画面写成主播表达。aigc_image_path 在分析阶段留空。",
        "9. 优先选择低成本、高 GMV 杠杆的改法。建议话术必须基于达人素材重新创作，不得复制或轻微改写标杆话术。",
        "10. 建议话术 creator_script 必须使用达人口播语言；creator_script_zh 只用于给中国运营理解。若达人未检测到有效口播或语言不可靠，用标杆语言/目标市场语言写新的达人话术，不要把音乐、噪声或无关字幕复制成建议。",
        "11. 不要臆造品牌、价格、优惠、型号或参数。只有当产品名/转写/画面中明确出现品牌时才能写品牌；不确定时使用产品名或本地语言中的中性产品指代。",
        "12. 健康品类不得建议疾病治疗、激素/月经调节、排出血块或保证效果等高风险话术；如标杆包含此类表达，应指出合规风险并给低风险替代。",
        "13. 必须输出 holistic_assessment（每维独立评估，禁止复制）、key_conclusions（1-5 条消费者视角关键结论）、product_visibility、loop_closure；产品可见度无法精确统计时要标明估算依据。",
        "14. severity 必须差异化：按 ANALYSIS-PROMPT.md 的标尺判断，large/medium/small 至少要出现 2 种。达人做到位或持平的阶段给 small，不能全给 medium。gap_summary 写'无明显差距'时 severity 必须是 small。",
        "15. JSON 输出保持简洁：每个视频列出 3~6 个关键 evidence_units；任何差距、证据或动作列表最多 3 条；每个描述字段最多一句；禁止重复列举未出现的音效、镜头或功能。",
        "16. 如果需要写回系统，请只输出符合 references/analysis-output-schema.json 的 JSON。",
        "",
        "## JSON 输出结构",
        "",
        read_optional_text(ROOT / "references" / "analysis-output-schema.json"),
        "",
    ]

    for role, label in (("benchmark", "爆款视频"), ("creator", "达人视频")):
        info = analysis["videos"].get(role)
        if not info:
            continue
        role_dir = Path(info["work_dir"])
        lines.extend(
            [
                f"## {label}",
                "",
                f"- 原视频：{info['path']}",
                f"- 时长：{format_seconds(info.get('duration_seconds'))}",
                f"- 检测语言：{info.get('detected_language') or info.get('transcription_language') or '未知'}",
                f"- 口播状态：{speech_status(role_dir, info)}",
                f"- 普通关键帧：{info.get('frame_count', 0)} 张，目录：{info['frames_dir']}",
                f"- 加密关键帧：{info.get('focus_frame_count', 0)} 张，目录：{info['focus_frames_dir']}",
                "",
                "### 全片时间线关键帧",
                "",
                render_timeline_frame_markdown(info),
                "",
                "### 加密关键帧时间戳",
                "",
                render_focus_frame_markdown(info),
                "",
                "### 本地语言转写",
                "",
                read_optional_text(role_dir / "transcript.txt"),
                "",
                "### 带时间戳口播分段",
                "",
                read_optional_text(role_dir / "transcript.srt"),
                "",
                "### 中文翻译",
                "",
                read_optional_text(role_dir / "transcript.zh.txt"),
                "",
            ]
        )

    path = run_dir / "analysis_input.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def render_market_knowledge(analysis: dict[str, Any]) -> str:
    """按目标市场加载知识库；auto 下仅作文化视角提示，不当作已确认事实。"""
    market = str(analysis.get("product", {}).get("target_market") or "auto").lower()
    text = read_optional_text(ROOT / "references" / "market-knowledge-my.md")
    if market == "my":
        return "目标市场已指定为马来西亚（my）。以下知识库可用于商业判断，但不得直接在报告中呈现。\n\n" + text
    if market == "sea":
        return "目标市场已指定为东南亚泛化（sea）。使用第一层东南亚共性知识；马来专属层仅作相似市场提示，不能当成确定事实。\n\n" + text
    return "目标市场未确认（auto）。以下 SEA/MY seed 仅作文化视角和误判防护提示；发现明确马来语或马来市场信号时可提高权重，但不得当作已确认事实。\n\n" + text


def speech_status(role_dir: Path, info: dict[str, Any]) -> str:
    """根据转写文本和语言置信度判断口播状态，仅供 prompt 装配显示用。"""
    transcript = read_optional_text(role_dir / "transcript.txt").strip()
    lowered = transcript.lower()
    non_speech_labels = {"*outro music*", "[music]", "(music)", "music", "（音乐渐弱）"}
    if lowered in non_speech_labels or transcript in non_speech_labels:
        return "未检测到有效口播，仅有音乐或环境声"
    confidence = info.get("detected_language_confidence")
    if isinstance(confidence, (int, float)) and confidence < 0.5:
        return "语言识别置信度较低，话术语言需结合标杆市场判断"
    if transcript in {"（缺失）", "（空）"}:
        return "未检测到有效口播"
    return "已检测到有效口播"


def read_analysis_prompt() -> str:
    return read_optional_text(ROOT / "ANALYSIS-PROMPT.md")


def render_focus_frame_markdown(info: dict[str, Any]) -> str:
    """加密关键帧（Hook / CTA）时间戳列表。"""
    entries = get_focus_frame_entries(info)
    if not entries:
        return "（无）"
    lines = []
    for item in entries:
        timestamp = format_seconds(item.get("timestamp_seconds"))
        label = item.get("label") or "frame"
        path = item.get("path") or ""
        lines.append(f"- {label} @ {timestamp}: {path}")
    return "\n".join(lines)


def render_timeline_frame_markdown(info: dict[str, Any]) -> str:
    """全片 1fps 关键帧均匀采样 24 张的时间线。"""
    entries = sample_evenly(get_frame_entries(info), 24)
    if not entries:
        return "（无）"
    lines = []
    for item in entries:
        timestamp = format_seconds(item.get("timestamp_seconds"))
        path = item.get("path") or ""
        lines.append(f"- {timestamp}: {path}")
    return "\n".join(lines)


def render_stage_frame_markdown(info: dict[str, Any]) -> str:
    """阶段代表帧列表。

    TODO: 当前 write_analysis_input 没有调用此函数（dead code 候选）。
          按用户拆分约束 #7 "按当前实际行为归类，不顺手删"，搬家时保留。
          若后续确认无用应整体删除（含 artifacts.get_stage_frame_entries 是否也只服务这一处）。
    """
    entries = get_stage_frame_entries(info)
    if not entries:
        return "（无）"
    lines = []
    for item in entries:
        timestamp = format_seconds(item.get("timestamp_seconds"))
        stage = item.get("stage") or item.get("label") or "stage"
        path = item.get("path") or ""
        lines.append(f"- {stage} @ {timestamp}: {path}")
    return "\n".join(lines)
