"""flayr_core.llm.payload：LLM 请求 payload 构造。

每个 build_*_payload 都返回 OpenAI 兼容的 chat completions 请求体。
不调用 LLM、不解析响应，纯粹组装文本 + 图片 + system prompt。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds, parse_time_range_seconds
from ..shot_track import render_shot_track_markdown
from ..speech_mode import speech_mode_prompt
from ..subtitle_track import render_subtitle_track_markdown
from ..video_evidence import parse_srt_segments
from .api import audio_to_mp3_data_url, video_to_data_url
from .media import build_evidence_sensory_inputs

ROOT = Path(__file__).resolve().parents[3]


def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip() if path.is_file() else "（缺失）"


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


def build_product_foundation_payload(model: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Step-0 品的商业地基：看视频前，据产品事实 + 品类世界知识确立 category_profile(特征) +
    product_profile(命题)，作为下游 S1-S6 判断的独立尺子。纯文本不附视频——地基独立于任一条
    视频，避免'阶段2 现编标尺又当场自评'的循环。运营未给的字段用品类世界知识补全。"""
    p = analysis.get("product") or {}
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
            f"- 目标用户/痛点：{p.get('target_user') or '未填写（按品类推目标人群与核心痛点）'}",
            f"- 购买动机：{p.get('purchase_motivation') or '未填写（按品类推）'}",
            f"- 目标市场：{p.get('target_market') or 'auto'}",
            f"- 备注：{p.get('notes') or '无'}",
            "## 输出严格 JSON（两个对象）",
            "category_profile（品类特征，只报事实+世界知识，不做权重判断）：category_name、price_tier(low|mid|high)、"
            "decision_threshold(impulse 冲动可买|considered 需被说服)、drive_type(emotional|functional|mixed)、"
            "painpoints（该品类目标消费者最在意的决策因素，每词中文+本地语放同一数组，6-16 个）。",
            "product_profile（产品商业 DNA，S1-S6 打分的尺子）：visualizable(yes|no 核心价值能否视觉化)、"
            "physical_task（解决的最直观尴尬）、hook_proposition（S1 钩子命题，类型取决于本品、不限痛点——"
            "可痛点/承诺/反差/情绪/向往/视觉吸引/身份代入/场景还原，见 structure_library S1 七型）、"
            "core_selling_points（S3 主轴：使用过程要演示传递的核心卖点，1-6 个）、"
            "usage_context（S3 场景层：本品典型使用场景=卖点演示的舞台）、"
            "core_visual_proposition（S4 决定性视觉瞬间=到位效果标准，按本品现推、别套通用 before/after）、"
            "visual_diff_dimensions（before/after 应变化的视觉维度，1-3 个）、"
            "trust_multipliers（建立专业度/信任的元素，3-6 个）、shooting_requirement（卖点显现所需拍摄条件）、"
            "confidence(high|low，小众或本地新奇特品标 low)。",
            "只报产品事实与品类世界知识，不臆造具体功效数据/检测数字/价格优惠。",
        ]
    )
    system_prompt = (
        "你是产品商业分析师。只输出严格 JSON（含 category_profile 与 product_profile 两个对象），不要 Markdown。"
        "基于产品事实 + 品类世界知识确立商业地基，运营未给的字段据品类世界知识补全，不臆造具体功效数据。"
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [{"type": "text", "text": text}]},
        ],
        "temperature": 0.0,
    }


def build_video_fact_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    visual_inputs: list[dict[str, str]],
) -> dict[str, Any]:
    """单视频事实抽取请求 payload。

    主路径（omni + ffmpeg 可用）：把原生视频（重编码 fps=3 + 降分辨率，含完整音轨）
    直接喂给模型，让它像人一样看连续画面 + 听声音，自定位变化点。
    降级路径（无 ffmpeg 或视频转码失败）：回退到关键帧抽帧 + 完整音频，
    沿用 visual_inputs（由 select_role_visual_inputs 提供）。
    """
    info = analysis.get("videos", {}).get(role, {})
    code = "B" if role == "benchmark" else "C"
    role_dir = Path(str(info.get("work_dir") or ""))
    mode_prompt = speech_mode_prompt(info.get("speech_mode") if isinstance(info.get("speech_mode"), dict) else {})

    # 优先走原生视频；失败则降级为抽帧。
    video_path = Path(str(info.get("path") or ""))
    video_data_url = video_to_data_url(video_path) if video_path.is_file() else None
    native_video = video_data_url is not None

    visual_source_hint = (
        "随请求附带本视频的原生画面（已抽帧为连续序列）、完整音轨，以及 Hook/CTA 时间线证据图。"
        if native_video
        else "随请求附带本视频的若干关键帧/时间线证据图和完整音频。"
    )

    # 品地基命题注入（Step-0 产出）：告诉事实抽取器该重点盯哪些证据，只导观察不下结论；无地基则退回通用抽取。
    fnd = (analysis.get("product_foundation") or {}).get("product_profile") or {}
    obs_hint = ""
    if fnd:
        csp = "、".join(fnd.get("core_selling_points") or []) or "（无）"
        vdd = "、".join(fnd.get("visual_diff_dimensions") or []) or "（无）"
        obs_hint = "\n".join(
            [
                "## 本品重点观察线索（据产品地基，帮你定位该盯什么；只记客观证据、不下结论）",
                f"- 核心视觉命题：{fnd.get('core_visual_proposition') or '（无）'}——留意画面有没有出现/拍清这个决定性瞬间，按实记拍到与否、是否清晰。",
                f"- before/after 应变化的视觉维度：{vdd}——重点观察这些维度的画面证据。",
                f"- 核心卖点：{csp}——留意使用过程中这些卖点有没有被动作演示出来。",
                f"- 典型使用场景：{fnd.get('usage_context') or '（无）'}。",
                "命题相关证据尤其别漏；但不要为凑命题臆造没拍到的东西。",
            ]
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
            "## 本地语言转写",
            read_text_if_exists(role_dir / "transcript.txt"),
            "",
            "## 紧凑口播索引（先按这个理解口播顺序；逐字引用仍以 transcript.srt 为准）",
            read_text_if_exists(role_dir / "transcript_packed.md"),
            "",
            "## 带时间戳口播分段（口播时间归因的权威依据）",
            read_text_if_exists(role_dir / "transcript.srt"),
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
                    "evidence_units": [
                        {
                            "id": f"{code}1",
                            "time_range": "0.0s - 3.0s",
                            "information": "该变化点实际传递的信息，不做 S1-S6 阶段推断。",
                            "voiceover": "只能摘录本视频 transcript.srt 中真实出现的原句；没有则留空。",
                            "voiceover_zh": "中文翻译；没有则留空。",
                            "visual_fact": "该时刻画面中实际可见的事实：主体、动作、表情变化、字幕叠字、特效。",
                            "subtitle_fact": "可读字幕；没有则留空。",
                            "audio_fact": "该时刻的 BGM（有/无、风格情绪）、口播语气（热情/平淡/亲和）、特殊音效；无则写无。",
                            "product_visible": True,
                            "product_coverage": "该时段产品在画面里的视觉占比：none｜low｜medium｜high。看不到产品写 none。",
                            "endorsement_verbal": False,
                            "endorsement_visual": False,
                            "functions": ["S3_usage", "S4_effect"],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if native_video:
        content.append(
            {"type": "video_url", "video_url": {"url": video_data_url}}
        )
        for item in visual_inputs:
            if "timeline" not in str(item.get("label") or "").lower():
                continue
            content.extend(
                [
                    {"type": "text", "text": f"时间线证据图：{item['label']}，本地路径：{item['path']}"},
                    {"type": "image_url", "image_url": {"url": item["data_url"], "detail": "low"}},
                ]
            )
    else:
        for item in visual_inputs:
            content.extend(
                [
                    {"type": "text", "text": f"图片：{item['label']}，本地路径：{item['path']}"},
                    {"type": "image_url", "image_url": {"url": item["data_url"], "detail": "low"}},
                ]
            )
        audio_data_url = audio_to_mp3_data_url(role_dir / "audio.wav")
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
        "你能同时看到连续画面、听到声音。严格按用户消息中『观察方法』一节的全部维度逐项观察、不漏项"
        "（含镜头语言/取景完整性、遮挡与 UI 危险区、画中画小窗、拍摄视角、口播与画面对齐、四轨对齐），"
        "必须先读取用户消息中的 speech_mode/证据组织模式，并按其证据优先级组织事实："
        "spoken 以口播时间线为骨架；subtitle_driven 以 OCR 字幕轨为文案骨架；visual_driven 以画面变化和镜头轨为骨架；"
        "music_driven 以画面变化、BGM/节奏/音效为骨架。无有效口播时 voiceover 与 voiceover_zh 必须留空，"
        "不得把屏幕字幕、画面文案或你对画面的理解伪装成口播。"
        "按带货短视频的天然结构（钩子→产品引出→使用过程→效果呈现→信任放大→促单）找证据切分 evidence_units，"
        "目标是抽出对分析带货视频有价值的事实，而非随意找转折点；输出 4 到 8 条，沿时间线排列，id 必须使用指定前缀，"
        "time_range 用真实时间（如 2.5s - 4.0s）。"
        "把各维度观察到的画面事实记入 visual_fact、声音事实记入 audio_fact（BGM 在场与类型/语气/音效）、"
        "口播与画面的对齐关系（同步/提前/滞后/无关）记入 information；按实记录，不做评价；"
        "每条还要标 product_visible（该时段画面里能否看到产品本体，true/false）与 product_coverage"
        "（产品视觉占比 none｜low｜medium｜high，看不到写 none）：这两项用于确定性统计产品出镜，"
        "据画面如实标，产品被手遮住或只露局部按真实可见程度给 low；"
        "再标 endorsement_verbal 与 endorsement_visual（各 true/false，纯观察、不判断算不算有效背书——有效性归后续打分）："
        "endorsement_verbal＝该时段口播/字幕里有没有【出现】halal/KKM/认证/证书/检测/临床/医生/皮肤科/专家/机构/FDA/GMP/SIRIM/BPOM/GMP/certified 等硬来源词（只看词出没出现，不判断是否构成援引背书）；"
        "endorsement_visual＝该时段画面里有没有【出现】独立的硬背书视觉证据（证书/检测报告文件/机构认证标识被画面清晰呈现）——产品瓶身上的印刷小标不算，口播说了但画面没出现也不算（口播归 endorsement_verbal，别把听到的脑补成画面）；"
        "每条还要标 functions（list，多选）：这段画面支撑哪些带货功能，枚举 S1_hook/S2_intro/S3_usage/S4_effect/S5_trust/S6_cta，"
        "按信息功能判断、信道无关（口播/字幕/画面/特效综合看，无口播也能判），一段可同时支撑多个"
        "（手在操作+效果出来 → [S3_usage,S4_effect]）；这是描述这段在带货结构里干什么、不是评价好坏，没有对应功能就不标；"
        "voiceover 必须逐字来自当前视频 transcript.srt，画面看不清的时段在 visual_fact 写画面证据不足待复核；"
        "无 BGM 或无明显音效时 audio_fact 写无，不要臆造。"
        "不得臆造牙齿前后对比、用户评论、证书、检测报告、认证、价格、优惠或功效。"
    )

    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
    }


def structure_library_judgment_view() -> str:
    """从 structure_library_full.md 抽"判断视图"——每模块只留 编号+名称+一句话功能+【适配条件】，
    扔掉【镜头】【文案】【声音】【节奏】【降级规则】制作规格（那些服务样片生成；喂进判断会诱导
    模型"看模式"扣分，违"看功能不看模式"宪法）。运行时从 full 文档单一来源抽取，不另维护副本。
    用途：补进阶段2 判断上下文，让模型判 module_id/适配时有客观结构骨架可依（此前被砍、锚到空气）。"""
    path = ROOT / "structure_library_full.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n(?=###\s+S[1-6]-[A-Z][:：])", text)
    lines: list[str] = []
    for blk in blocks:
        m = re.match(r"###\s+(S[1-6]-[A-Z])[:：]\s*(.+)", blk.strip())
        if not m:
            continue
        mid, name = m.group(1), m.group(2).strip()
        pre_code = blk[m.end():].split("```", 1)[0]
        func_lines = [ln.strip() for ln in pre_code.splitlines() if ln.strip()]
        func = func_lines[0] if func_lines else ""
        cm = re.search(r"【适配条件】\s*(.*?)(?=\n\s*【|\n```|\Z)", blk, flags=re.S)
        fit = " ".join(ln.strip() for ln in cm.group(1).splitlines() if ln.strip()) if cm else ""
        lines.append(f"- {mid} {name}：{func}｜适配：{fit}")
    return "\n".join(lines)


_BRAND_PAIR_SUFFIX_RE = re.compile(r"-[bc]\d+$")


def resolve_brand_key(run_dir_name: str) -> str:
    """从 run 目录名解析【品】键：去 sample- 前缀、去 -b0/-c1 标杆/达人配对后缀；榨汁机族（wukoubo/youkoubo）归 juicer。"""
    s = run_dir_name.removeprefix("sample-")
    if s.startswith(("wukoubo", "youkoubo")):
        return "juicer"
    return _BRAND_PAIR_SUFFIX_RE.sub("", s)


def load_brand_proposition(run_dir: Path) -> dict[str, Any] | None:
    """读冻结的 S1 命题尺子 references/brand_propositions.json，按【品】返回 {propositions, painpoints}。
    文件缺失/无该品条目/解析失败 → None（pipeline 据此降级回 Step-0 命题，hook flag 仍会输出）。"""
    path = ROOT / "references" / "brand_propositions.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    entry = data.get(resolve_brand_key(run_dir.name)) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    props = [str(p) for p in entry.get("propositions") or [] if str(p).strip()]
    pains = [str(p) for p in entry.get("painpoints") or [] if str(p).strip()]
    if not props and not pains:
        return None
    return {"propositions": props, "painpoints": pains}


S2_START_CUES = [
    "能解决",
    "解决",
    "能拯救",
    "拯救",
    "救",
    "直到我发现",
    "我发现",
    "我用的是",
    "我用的就是",
    "答案",
    "秘密",
    "就是它",
    "这个产品",
    "这款产品",
    "这个是",
    "这款是",
    "认证",
    "成分",
    "价格",
    "优惠",
    "推荐",
    "朋友推荐",
    "医生",
]


def build_s1_boundary_hint_block(analysis: dict[str, Any] | None, facts: dict[str, Any]) -> str:
    """用 SRT 句段给 Stage2 一个 S1/S2 边界候选，避免粗 evidence 单元把 Hook 和产品引出焊死。"""
    if not analysis:
        return ""
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    lines = [
        "## S1/S2 边界候选（代码从 transcript.srt + facts 提取，仅辅助裁边界）",
        "按 structure_library_full.md：S1 是抢夺注意力，S2 是从 Hook 自然过渡到产品。",
        "若候选处下一句已经开始承接/揭晓/否定转正/第三方推荐，即使产品实物或产品名还没出现，也优先视为 S2 起点。",
    ]
    wrote_any = False
    for role in ("creator", "benchmark"):
        info = videos.get(role) if isinstance(videos, dict) else None
        if not isinstance(info, dict):
            continue
        role_dir = Path(str(info.get("work_dir") or ""))
        segments = parse_srt_segments(role_dir / "transcript.srt")[:4]
        if not segments:
            continue
        candidate = infer_s1_boundary_candidate(role, segments, facts)
        lines.append("")
        lines.append(f"- {role}:")
        if candidate:
            lines.append(f"  - candidate_hook_boundary_seconds: {candidate['seconds']:.2f}")
            lines.append(f"  - candidate_reason: {candidate['reason']}")
        else:
            lines.append("  - candidate_hook_boundary_seconds: 未自动识别；仍按下方 SRT 句段自行按功能裁边界。")
        lines.append("  - early_srt:")
        for segment in segments:
            lines.append(
                f"    [{segment['start_seconds']:.2f}-{segment['end_seconds']:.2f}] {segment['text']}"
            )
        wrote_any = True
    return "\n".join(lines) if wrote_any else ""


def infer_s1_boundary_candidate(
    role: str,
    segments: list[dict[str, Any]],
    facts: dict[str, Any],
) -> dict[str, Any] | None:
    if len(segments) < 2:
        return None
    first = segments[0]
    second = segments[1]
    start = float(second.get("start_seconds") or 0.0)
    if start <= 0 or start > 12:
        return None

    first_fact = find_early_evidence_for_role(role, facts)
    voice_zh = str(first_fact.get("voiceover_zh") or "")
    info = str(first_fact.get("information") or "")
    second_text = str(second.get("text") or "")
    cue = find_s2_start_cue(" ".join([voice_zh, second_text, info]))
    if cue:
        return {
            "seconds": start,
            "reason": (
                f"SRT 第一句 {first['start_seconds']:.2f}-{first['end_seconds']:.2f}s 更像 S1 留人；"
                f"第二句从 {start:.2f}s 开始出现“{cue}”类承接/解决方案信号，按 S2-A/S2-B/S2-C/S2-D 功能可能已进入 S2。"
            ),
        }

    evidence_candidate = infer_boundary_from_evidence(role, facts)
    if evidence_candidate:
        return evidence_candidate
    return None


def infer_boundary_from_evidence(role: str, facts: dict[str, Any]) -> dict[str, Any] | None:
    units = get_role_evidence_units(role, facts)
    if len(units) < 2:
        return None
    previous = units[0]
    for current in units[1:4]:
        current_start = parse_evidence_start(current.get("time_range"))
        if current_start <= 0 or current_start > 12:
            continue
        prev_functions = {str(item) for item in previous.get("functions") or []}
        current_functions = {str(item) for item in current.get("functions") or []}
        current_text = " ".join(
            str(current.get(key) or "")
            for key in ("information", "voiceover_zh", "visual_fact", "subtitle_fact")
        )
        if "S1_hook" in prev_functions and (
            "S2_intro" in current_functions or find_s2_start_cue(current_text)
        ):
            return {
                "seconds": current_start,
                "reason": (
                    f"facts 中前一单元 {previous.get('id')} 主功能为 S1_hook，"
                    f"{current.get('id')} 从 {current_start:.2f}s 开始进入 S2_intro/产品承接信号。"
                ),
            }
        previous = current
    return None


def find_early_evidence_for_role(role: str, facts: dict[str, Any]) -> dict[str, Any]:
    role_units = get_role_evidence_units(role, facts)
    if not role_units:
        return {}
    return min(role_units, key=lambda unit: parse_evidence_start(unit.get("time_range")))


def get_role_evidence_units(role: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    prefix = "C" if role == "creator" else "B"
    units = facts.get("evidence_units") if isinstance(facts.get("evidence_units"), list) else []
    role_units = [unit for unit in units if isinstance(unit, dict) and str(unit.get("id") or "").startswith(prefix)]
    if not role_units:
        direct = facts.get(role) if isinstance(facts.get(role), dict) else {}
        role_units = direct.get("evidence_units") if isinstance(direct.get("evidence_units"), list) else []
    if not role_units:
        videos = facts.get("videos") if isinstance(facts.get("videos"), dict) else {}
        nested = videos.get(role) if isinstance(videos.get(role), dict) else {}
        role_units = nested.get("evidence_units") if isinstance(nested.get("evidence_units"), list) else []
    if not role_units:
        return []
    return [unit for unit in role_units if isinstance(unit, dict)]


def parse_evidence_start(value: Any) -> float:
    start, _ = parse_time_range_seconds(str(value or ""), None)
    return start


def find_s2_start_cue(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    for cue in S2_START_CUES:
        if cue in compact:
            return cue
    return ""


def hook_anchor_terms(bp: dict[str, Any], foundation: dict[str, Any]) -> tuple[list[str], list[str], str]:
    """Resolve S1 anchor terms for hook flags.

    Frozen human-curated terms are strongest. Step-0 product/category profiles are
    the fallback so hook flags remain active for new products and normal timestamp
    run directories.
    """
    props = [str(p).strip() for p in bp.get("propositions") or [] if str(p).strip()]
    pains = [str(p).strip() for p in bp.get("painpoints") or [] if str(p).strip()]
    if props or pains:
        return props, pains, "冻结·人工策展"

    product_profile = foundation.get("product_profile") if isinstance(foundation.get("product_profile"), dict) else {}
    category_profile = foundation.get("category_profile") if isinstance(foundation.get("category_profile"), dict) else {}
    fallback_props = []
    for key in ("hook_proposition", "physical_task"):
        value = str(product_profile.get(key) or "").strip()
        if value and value not in fallback_props:
            fallback_props.append(value)
    fallback_pains = [
        str(item).strip()
        for item in category_profile.get("painpoints") or []
        if str(item).strip()
    ][:12]
    source = "Step-0 产品地基回退" if fallback_props or fallback_pains else "无冻结尺子，按当轮 product_profile/category_profile 判断"
    return fallback_props, fallback_pains, source


def build_llm_comparison_payload(
    model: str,
    analysis_input: str,
    facts: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """基于已校验的单视频事实清单做对比分析的 payload。

    Phase B：facts 文字仍是唯一事实源（防串供基线），但额外附上每条 evidence 的
    关键帧 + 切片音频，让判断环节能"看着证据、听着声音"评估声画质感与情绪强度，
    从而给出更准的 severity 和对比结论。感官素材不可用于新增/改写事实。
    """
    context = extract_comparison_context(analysis_input)
    commercial_framework = read_text_if_exists(ROOT / "references" / "commercial-judgement-framework.md")
    market_knowledge = read_text_if_exists(ROOT / "references" / "market-knowledge-my.md")
    qa_rules = read_text_if_exists(ROOT / "QA-RULES.md")
    speech_mode_block = render_speech_mode_block(analysis or {})
    s1_boundary_hint_block = build_s1_boundary_hint_block(analysis, facts)
    # Step-0 品地基注入：已确立则作为 S1-S6 判断的尺子直接采用，模型不再另起炉灶现编（防"现编标尺又自评"）。
    fnd = (analysis or {}).get("product_foundation") or {}
    foundation_block = ""
    if fnd.get("category_profile") or fnd.get("product_profile"):
        foundation_block = (
            "## 本品商业地基（Step-0 已确立，作为 S1-S6 判断的尺子，直接采用）\n"
            "以下 category_profile（特征）与 product_profile（命题）已在看视频前据产品事实+品类世界知识确立。"
            "S1-S6 的锚点（hook_proposition/core_selling_points/usage_context/core_visual_proposition/"
            "trust_multipliers/decision_threshold 等）一律以此为准，直接用它判断达人/标杆，不要另起炉灶重推；"
            "你输出的 category_profile/product_profile 必须原样回填这套地基。\n"
            + json.dumps(fnd, ensure_ascii=False, indent=2)
        )
    # S1 钩子命题尺子：优先用冻结·人工策展；没有则回退 Step-0 地基。无命题也仍强制输出 hook flags，
    # 因为 dims/landing 是通用钩子质量事实，不应依赖人工品库是否覆盖。
    bp = (analysis or {}).get("brand_proposition") or {}
    props, pains, anchor_source = hook_anchor_terms(bp, fnd)
    hook_flag_block = (
        f"## 本品 S1 钩子命题尺子（{anchor_source}，judge anchors_proposition 用）\n"
        "propositions（能做钩子主张的概念）：" + (" / ".join(props) if props else "（未提供；按 product_profile.hook_proposition 自行判）") + "\n"
        "painpoints（开头核心痛点）：" + (" / ".join(pains) if pains else "（未提供；按 category_profile.painpoints 自行判）") + "\n"
        "S1 阶段（且仅 S1）每侧【必须】输出 creator_hook 与 benchmark_hook 两个对象，形如：\n"
        '{"exists": bool（该侧前段是否存在抢注意力的 Hook，非直接进产品介绍）, '
        '"type": "A"~"G" 或 "unknown"（按 structure_library S1 七型判该侧钩子属哪型。判定铁律：'
        '①证据优先级 voiceover_zh / on_screen_text / 画面帧事实 ＞ information；information 只是索引、严禁作判定依据，'
        '尤其不得因某 evidence 的 information 写了"痛点场景/油光/出油"就判 A；'
        '②只取最早 hook 窗口的主导机制定 type——优先看 0-3s，不足扩到 0-5s，若 voiceover_zh 第一完整句跨到约 6.8s 可用这一整句；'
        '严禁用该句之后才出现的痛点跟进/产品引出反推 type；'
        '③若第一句口播是"没抱期待但结果超出预期"这类低期待→高结果表述，必须判反差 B、不得判痛点 A；判不出填 unknown，不影响其余字段）, '
        '"dims": {"camera": bool, "copy": bool, "sound": bool, "rhythm": bool}'
        '（该侧钩子在所选 type 下，镜头/文案/声音/节奏四维是否做到结构库给的【必需】结构件——'
        '做到结构库示例即 true、缺了即 false；只判"做到没"不判"好不好"，不评创新加分；type=unknown 时四维按通用做到度判）, '
        '"hook_boundary_seconds": number（该侧 S1 Hook 的结束秒点，不是固定 3/5/6 秒。按 structure_library_full.md 的槽位职责判：'
        'S1=抢夺注意力，让用户留下；S2=从 Hook 自然过渡到产品。边界就是主导信息从"留人机制"切到"产品/解决方案引出/解决方案承接"的第一个时刻。'
        '判定优先级：口播语义切换 > 字幕/贴纸信息切换 > 画面主体/产品功能切换 > 场景/镜头功能切换。'
        'S2 起点信号包括：开始回答 Hook、开始说"能解决/能拯救/直到我发现/我用的是/答案是/就是它"这类承接话术、'
        '产品名/品类名/解决方案/卖点/认证/成分/价格/购买理由开始出现，或产品从道具变为解决方案主角/揭晓对象/推荐对象。'
        '重要：S2-A 承接式引出可以早于产品实物出镜或产品名出现；不要等到产品画面/产品名才切 S2。'
        '产品在 S1 画面里出现不自动算 S2；但一旦口播/字幕开始把某个东西作为解决方案承接 Hook，即使还没露出包装，也已经是 S2。'
        '若第一句完整 Hook 跨过 5 秒，可取第一句结束；但不能把后续产品解释并入 S1）, '
        '"hook_boundary_reason": "一句话说明为什么这里是 S1/S2 边界：S1 主导信息是什么，S2 起点信号是什么", '
        '"s2_start_signal": "边界后第一个 S2 信号，如产品名/解决方案承接/产品揭晓/认证卖点/产品成为画面主角", '
        '"landing_met": bool（钩子有没有"打穿"，【与 type 无关】。判 true 当且仅当：0 到 hook_boundary_seconds 内用户能 get 到一个【可停留的理由】，'
        '且【同时】满足三件——①对象明确：在说谁的问题/谁的场景/谁会关心（油皮、脱妆、经期痛、孩子抗拒刷牙）；'
        '②张力明确：为什么要继续看（痛点/反差/结果/悬念/场景共鸣/身份代入/认知颠覆 任一）；'
        '③承诺或证据明确：后面要证明什么（油光变哑光、补妆不花、刷头不用手碰、孩子能接受）。三件缺任一即 false——'
        '不是没 hook 元素，是 hook 没闭环。【铁律：严禁因为后续 S2/S3 产品介绍补足了逻辑就把 S1 landing 判 true，'
        '只看 0 到 hook_boundary_seconds 本身闭没闭环、不跟后段走】）, '
        '"landing_reason": "一句话说清 landing 为何 true/false，必须只引用 0 到 hook_boundary_seconds 内的具体证据（时间戳+原话/画面），'
        '如 0-6.8s 仅口播\'结果超预期\'但没说超预期的结果是什么→承诺不明确→false；严禁引用 hook_boundary_seconds 之后的产品/卖点/认证来补足", '
        '"window_evidence": "0 到 hook_boundary_seconds 内实际出现了什么（带时间戳），作为 type 判断依据，'
        '如 0-4.5s 近脸/指脸/拿产品但未建立使用前后强对比", '
        '"landing_window_leak": bool（landing_reason 或 landing_met 是否借用了 hook_boundary_seconds 之后的 S2/S3 材料。若引用边界后的产品名/卖点/认证/解决方案补足三件套，必须 true 且 landing_met=false）, '
        '"anchors_proposition": bool（该侧钩子内容是否触及上面任一 proposition、painpoint，或 product_profile.hook_proposition/category_profile.painpoints 概念）}。'
    )
    # 强制字段要求（放在末尾"输出要求"区，模型严格跟这块走；前面的尺子块只给结构定义）
    hook_field_req = (
        "S1 强制：stage_analysis 第 1 项（S1 Hook）必须再含 creator_hook 与 benchmark_hook 两个对象"
        "（结构见上方：exists/type/dims{camera,copy,sound,rhythm}/hook_boundary_seconds/hook_boundary_reason/s2_start_signal/landing_met/landing_reason/window_evidence/landing_window_leak/anchors_proposition）。"
        "type 为描述字段（按最早窗口主导机制判、不进 severity）；landing_met 是 type 无关的三件套二元判（进 severity）；"
        "hook_boundary_seconds 必须按 structure_library 的 S1 留人机制→S2 产品引出/解决方案承接功能切换来判，不得写死固定秒数；"
        "S2-A 承接式引出可早于产品实物或产品名出现，不能等产品画面才切 S2；缺失视为违规输出。S2-S6 不含这两个字段。"
    )
    s2_flag_block = (
        "## S2 产品引出契约 flag（只判 S1→S2 衔接，不做四维打分）\n"
        "S2 阶段（且仅 S2）每侧【必须】输出 creator_s2 与 benchmark_s2 两个对象，形如：\n"
        '{"exists": bool（该侧是否存在产品引出功能；≤15s 且 S2/S3 合并也算存在）, '
        '"merged_with_s3": bool（成片≤15s 或产品引出与使用演示不可分时 true；true 时不因缺独立 S2 扣分）, '
        '"module_type": "A"~"D" 或 "unknown"（按 structure_library S2 四型判：A承接式/B解谜式/C对比式/D第三方式）, '
        '"handoff_met": bool（是否自然承接该侧 S1 抛出的痛点/悬念/结果/场景；不是单纯产品露出）, '
        '"s1_s2_compatible": bool（按 structure_library 的 S1→S2 兼容矩阵判模块组合是否兼容）, '
        '"product_identity_clear": bool（用户是否知道这是什么产品/品类/品牌之一，不能只看到模糊道具）, '
        '"product_role_clear": bool（产品是否成为解决方案/答案/推荐对象/对比胜出者，而非背景道具）, '
        '"excluded_or_risky_module": bool（是否用了结构库对该品类排除或高合规风险的引出方式，如保健/美妆用否定竞品式 S2-C）, '
        '"start_seconds": number, "end_seconds": number, '
        '"handoff_reason": "一句话说明 S1 提了什么、S2 如何接住；若没接住要直说", '
        '"evidence_ids": ["C1"]}。\n'
        "S2 铁律：产品露出≠产品引出完成；讲卖点细节/成分/选购建议不归 S2，归 S3/S4/S5；"
        "S2 只判三件事——承接 S1、说清产品身份、让产品成为答案/解决方案。"
    )
    s2_field_req = (
        "S2 强制：stage_analysis 第 2 项（S2 产品引出）必须再含 creator_s2 与 benchmark_s2 两个对象"
        "（结构见上方：exists/merged_with_s3/module_type/handoff_met/s1_s2_compatible/product_identity_clear/product_role_clear/"
        "excluded_or_risky_module/start_seconds/end_seconds/handoff_reason/evidence_ids）。"
        "S2 flag 只服务衔接契约，不评卖点细节；S1 提过的钩子关键词不得在 S2 重复分析，S2 已分析的引出方式不得在 S3 重复。"
    )
    s3_flag_block = (
        "## S3 使用过程 flag（只判真实使用中核心卖点是否被动作演示出来）\n"
        "S3 阶段（且仅 S3）每侧【必须】输出 creator_s3 与 benchmark_s3 两个对象，形如：\n"
        '{"exists": bool（该侧是否存在使用过程功能；S2/S3 合并时也算存在）, '
        '"module_type": "A"~"E" 或 "unknown"（按 structure_library S3 五型判：A单场景全流程/B多场景拼接/C多人像使用/D步骤拆解/E沉浸第一视角）, '
        '"real_usage_met": bool（是否是真实使用动作，而非静态展示、摆拍假用、只拿着产品说）, '
        '"core_selling_point_visible": bool（product_profile.core_selling_points 中至少一个核心卖点是否在使用动作里被看见；只口播不算）, '
        '"usage_context_fit": bool（使用场景是否给核心卖点提供合适舞台，如去污在脏污面、控油在出油/补妆场景）, '
        '"continuity_met": bool（过程是否连贯且符合真实用法，不是无关镜头拼贴或错误用法）, '
        '"richness_met": bool（多场景覆盖多卖点，或单场景做厚：多角度、多步骤、多卖点完整过程；必须在 core_selling_point_visible=true 后才有意义）, '
        '"fake_or_staged": bool（显假摆拍/错误使用/画面无法相信时 true）, '
        '"start_seconds": number, "end_seconds": number, '
        '"usage_reason": "一句话说明实际使用了什么、哪个核心卖点被动作证明；没证明要直说", '
        '"evidence_ids": ["C1"]}。\n'
        "S3 铁律：演示即证据。口播/字幕说卖点但画面没做出来，不算 core_selling_point_visible；"
        "场景丰富、人物多、步骤多都不能补偿核心卖点没落地；独立效果结果归 S4，背书归 S5。"
    )
    s3_field_req = (
        "S3 强制：stage_analysis 第 3 项（S3 使用过程）必须再含 creator_s3 与 benchmark_s3 两个对象"
        "（结构见上方：exists/module_type/real_usage_met/core_selling_point_visible/usage_context_fit/continuity_met/"
        "richness_met/fake_or_staged/start_seconds/end_seconds/usage_reason/evidence_ids）。"
        "S3 flag 只服务真实使用过程判断，不评效果结果，不把 S4/S5 内容回填到 S3。"
    )
    user_text = "\n\n".join(
        [
            context,
            foundation_block,
            hook_flag_block,
            s2_flag_block,
            s3_flag_block,
            s1_boundary_hint_block,
            "## S1-S6 模块结构库（判断视图：客观类型 + 适配条件，判 module_id 与类型对本品适配用；这是结构层、非判断层，不讲好坏）",
            structure_library_judgment_view(),
            "## 商业评判框架（判断差距权重的方法）",
            commercial_framework,
            "## 目标市场知识库（仅作判断依据，不在报告呈现）",
            (
                "目标市场未确认时，以下 SEA/MY seed 仅作文化视角和误判防护提示；"
                "发现明确马来语或马来市场信号时可提高权重，但不得当作已确认事实。\n\n"
                + market_knowledge
            ),
            "## QA-RULES.md 自检契约（输出前必须自检）",
            qa_rules,
            "## 已校验单视频事实清单（唯一事实来源）",
            json.dumps(facts, ensure_ascii=False, indent=2),
            "## 各视频证据组织模式（判断时必须尊重）",
            speech_mode_block,
            "## 输出要求",
            "只输出严格 JSON，不要 Markdown。字段必须使用 references/analysis-output-schema.json 的字段名。",
            "必须输出：one_line_verdict, one_line_summary, executive_summary, holistic_assessment（每维独立）, key_conclusions（1-5 条消费者视角）, product_visibility, category_profile, product_profile, loop_closure, video_understanding, stage_analysis[6], improvements（1-5 条，按 GMV 杠杆排序）。",
            "stage_analysis 每项必须含：stage, time_range, benchmark_time_range, creator_time_range, core_question, creator_module_id, benchmark_module_id, module_fit, module_fit_reason, task_completion, gap_type, gap_summary, voice_performance, benchmark_summary, benchmark_key_message, benchmark_evidence_ids, benchmark_visual_evidence, benchmark_support_status, benchmark_has_effect_demo, benchmark_has_usage_demo, benchmark_quote, benchmark_quote_zh, creator_summary, creator_key_message, creator_evidence_ids, creator_visual_evidence, creator_support_status, creator_has_effect_demo, creator_has_usage_demo, creator_quote, creator_quote_zh, gap, evidence, severity, creator_execution, benchmark_execution, painpoint_relevance, stage_standard_delivery。",
            hook_field_req,
            s2_field_req,
            s3_field_req,
            "task_completion 只能取 complete、partial、missing 三选一（达人侧该阶段功能完成度），禁止 both_complete、no_gap 等任何其他词；标杆侧完成情况写在 benchmark_summary。",
            "creator_execution 与 benchmark_execution 取值只能是 0、0.5、1、2 四个数字：0=未执行该阶段功能；0.5=做了但对该阶段核心功能基本无效——敷衍、平庸无感、几乎不起作用（如一句轻带的 CTA、平铺直叙毫无抓力的开场、仅口头承诺没有任何验证支撑）；1=执行合格（功能完成且对观众有效）；2=执行出色（可视化演示/铺垫到位/感染力强）。两侧按该阶段功能定义各自独立打分，先打分再对比，禁止因对比结果回调任何一侧分数。",
            "效果呈现阶段（S4）执行分以 product_profile.core_visual_proposition（本品核心视觉命题）为评判锚点，不套通用 before/after：先判该侧有没有拍出本品的决定性瞬间（定妆粉饼=粉底油光→哑光对比、面膜=逐日变化+敷后效果），并满足 product_profile.shooting_requirement（效果细微的品需正面强光+面部特写才算拍到）。拍出命题且拍摄到位才给 2；只完成动作（揭膜/擦粉/口头带过）未体现命题、或拍摄条件不支撑（暗光/无特写/wide shot 看不出效果）按敷衍计最高 0.5；做了但缺命题对比的'呈现单薄'最高 1。过长全程记录不加分（标尺是命题覆盖非完整性）。两侧各自独立打分，禁止因对比回调。",
            "S4 给执行分前必须做一次闭环核验：回到该侧关键帧，对照 core_visual_proposition 与 visual_diff_dimensions，在画面上实际确认那个视觉对比肉眼可见——'存在 before/after 结构'不等于'对比拍出来了'。若该侧前后帧在指定维度上看不出明显差异（如油光帧与哑光帧看起来差不多、敷膜前后肤质无变化），即命题未被有效呈现，该侧执行分最高 1（只完成动作未呈现效果）；几乎完全无差异则 0.5。这是把你自己定的命题当检查清单逐帧核对，不许凭结构臆断。两侧同此核验。",
            "S4 执行分主轴只有一个：core_visual_proposition（核心命题）的有效呈现。trust_multipliers（防水/防汗测试、美容仪、周期记录、专业手法等）是加分项，只能在核心命题已有效呈现（该侧≥1）时把分抬向 2；不能替代、也不能补偿弱核心命题。若某侧核心命题没拍出来（对比弱/不可见），哪怕它有很强的次要演示，该侧执行分仍封顶 1——严禁用次要演示把分顶上去。判分先看核心命题达没达到，再决定加分项加不加。",
            "S4 还要逐侧输出布尔字段 benchmark_has_effect_demo / creator_has_effect_demo（非 S4 阶段填 null）——针对本卖点，该侧视频里有没有出现『效果呈现』。"
            "判 true 当且仅当满足结构库 S4-A~F 任意一种：①前后状态对比（同机位/分屏/左右 before/after，S4-A/B）；②人物差异对比（用了的人 vs 没用的人，视觉差可见，S4-C）；"
            "③本品 vs 替代方案效果对比（结果侧有差异，S4-D）；④借物量化（硬币/纸巾/水珠等参照物展示可量化效果，S4-E）；"
            "⑤过程可视化（特写/慢镜让肉眼不易察觉的效果成为画面——粉质覆盖/精华渗透/泡沫溶解/拉丝/吸水/去渍过程，S4-F）。"
            "判 false 当以下单独出现：纯使用动作（涂抹/按压/拆装步骤，无效果变化可见）→ 属 S3；产品出镜无人操作；口头/字幕描述效果但画面无对应效果画面；只展示外观/材质/包装。"
            "注意：不要求效果『非常明显』，观众能看见变化/差异/量化结果即计 true。这是一个干净的结构化判断（看见效果呈现=true，只看见动作/口播=false），别用自由文本描述替代。",
            "S3 还要逐侧输出布尔字段 benchmark_has_usage_demo / creator_has_usage_demo（非 S3 阶段填 null）——本侧有没有在真实使用过程中把核心卖点『演示出来被看见』。"
            "判 true 当满足结构库 S3-A~E 任意一种真实使用演示：①单场景全流程（开箱到使用完整展示，S3-A）；②多场景拼接（多场景覆盖卖点，S3-B）；"
            "③多人像使用（不同人演示，S3-C）；④步骤拆解式（分步操作演示，S3-D）；⑤沉浸第一视角（第一人称实操，S3-E）——关键是核心卖点在使用动作里被看见（清洁机吸力/干湿分离在动作里可见、涂抹推开过程可见），演示即证据。"
            "判 false 当以下单独出现：只口播/字幕描述怎么用但画面没演（嘴上讲）；产品静态展示/外观包装无实操；显假摆拍非真实使用。"
            "注意：这是『有没有演示使用过程』的存在性判断，不评教学清晰度、不评场景丰富度（那些进执行分）。看见真实使用演示=true，只看见口播/静态=false。",
            "0.5 档同样适用于'内容存在但消费者无法有效接收'：看不清（虚焦/过曝/遮挡/一闪而过/画面晃动到观众抓不住重点）、听不清（吞字/被 BGM 压制）、读不完（字幕停留过短）——物理存在不等于有效传递，晃动按观众可看性判而非镜头美学。S5 背书孤证规则：仅口播提及背书而画面无任何佐证、或背书标志一闪而过无法辨认，执行分最高 0.5（高决策门槛品类口头孤证视为无效背书）。",
            "painpoint_relevance 只能取 benchmark_only、creator_only、both、none 四选一：该阶段双方内容是否命中 category_profile.painpoints 中的核心决策因素——只有标杆命中/只有达人命中/双方都命中/双方都未命中。按内容功能判断（讲没讲到、演没演到核心痛点），不要求字面用词一致。",
            "category_profile 必须含：category_name（品类名）, price_tier（low|mid|high 客单价档）, decision_threshold（impulse|considered）, drive_type（emotional|functional|mixed）, painpoints（该品类目标消费者最在意的决策因素关键词，每个痛点同时给中文和本地语两种表述放进同一数组，共 6-16 个词条）。只报品类事实与世界知识，不做权重判断。",
            "打分前必须先输出 product_profile 产品商业 DNA（这是 S1-S6 打分的尺子，先立尺再量）：visualizable（yes|no，核心价值能否视觉化）、physical_task（解决的最直观尴尬）、hook_proposition（本品对目标人群最有拦截力的点=钩子命题，类型取决于本品、不限痛点——可痛点/承诺/反差/情绪/向往/视觉吸引/身份代入/场景还原等，见 structure_library S1 七型，模型按品类+视频推、运营可覆盖）、core_visual_proposition（决定性视觉瞬间=本品到位效果展示的标准，按本品现推，别套通用 before/after）、visual_diff_dimensions（本品 before/after 应在哪些视觉维度变化，从 亮度反光/纹理毛孔/色泽均匀度/水润干燥/肿胀轮廓 中选或按品自命名如去污/拉丝，1-3 个，S4 核验对比只看这些维度）、trust_multipliers（建立专业度的元素如美容仪/周期记录/专业手法/第三方检测，3-6 个）、shooting_requirement（卖点显现所需拍摄条件）、confidence（high|low，小众或本地新奇特品标 low）。只报产品事实与品类世界知识。visualizable=no（香水/保健品/隐形矫正等效果拍不出）时，S4 不强求视觉命题，把判断重心放到 S5 信任放大与达人可信度。",
            "每阶段输出 stage_standard_delivery（benchmark_only|creator_only|both|none）：该阶段双方是否有效达到本阶段的『本品到位标准』（见下条对照表锚点）。做到/展示到才算，仅口头讲到不算。先作为事实输出，暂不参与推导。",
            "S1-S6 执行分统一三层判：阶段目标(core_question) → 用了什么做法(module_id/module_fit) → 该做法在【本品】上到位没(execution)。'到位'按阶段查本品锚点、核心目标为主轴次要元素不补偿弱核心；本轮已接入的阶段锚点——S4 效果呈现→锚 core_visual_proposition（详见前述演示锚点+闭环核验+核心主轴三条）；S5 信任放大→锚 trust_multipliers：硬信任（第三方认证/检测/临床/仪器实测/官方背书）有效呈现可达 2，软信任（真实好评/社会认同/向往式对比/使用记录/达人自用）算信任但封顶 1（软不如硬），自述功效/纯参数不算；位置优先——视频开头的此类背书内容算 S1 钩子（留人）、结尾算 S6 CTA，不要按语义把开头/结尾的背书塞进 S5；判'用没用且呈现有效'非'口头说没说'，口播孤证或标志一闪而过最高 0.5；S6 促单→到位=把 structure_library S6 五型各自【适配条件】（含排除项，如价格锚定/赠品堆叠排除情感满足品=category_profile.drive_type=emotional）套上本品特征 category_profile（decision_threshold/drive_type/品类）+命题 product_profile，判达人/标杆选的 CTA 类型适配与否＋执行到位与否，gap=适配×执行差距；决策类型（冲动/高决策）是输入之一非唯一轴——冲动品需清晰指令+紧迫感、高决策品需先消顾虑再 CTA，但哪型 CTA 好仍由(五型适配条件×本品命题)结合得出。S1 钩子→到位=把 structure_library S1 七型各自【适配条件】（按品类/购买动机匹配）套上本品特征 category_profile（品类/drive_type/decision_threshold）+命题 product_profile（hook_proposition），判达人/标杆选的钩子类型适配本品与否＋执行到位与否，gap=适配×执行差距；不预设某根轴（痛点/视觉冲击/悬念）通用为好，好坏由(类型适配条件×本品命题)结合得出（潮玩配场景还原/反差、儿童牙膏配身份代入/场景还原、榨汁机配反差/场景还原）；开头的背书/认证类内容按钩子算（见位置宪法）；S2 产品引出→到位=引出自然 + 承接 S1 钩子（冲着钩子抛出的那个点去承接，痛点钩→引出冲着解痛点）+ 引出产品身份（这是什么品）；S2 只判这两件事，不判卖点本身、也不判卖点细节/选购指导/适配人群/参数/信息完整度（这些归 S3/S4）——标杆比达人多讲分肤质版/选购建议/卖点细节，不构成 S2 差距，只要达人自然引出+点明产品身份即同等到位；锚 hook_proposition 承接；S3 使用过程→主轴锚 core_selling_points（卖点传递有效性）+ 场景层 usage_context：到位=真实使用过程中把核心卖点'演示出来'被看见（清洁机吸力强/干湿分离/易倒垃圾在动作里可见，不是嘴上讲——演示即证据=打开水箱看见分层）；按 14b5 这是不可补偿主轴，场景再丰富人员再多样、卖点没在过程落地仍判弱；前置门槛真实感（显假/摆拍直接封顶低分）；S3 不评教学清晰度；场景层三看——适配度（场景给没给卖点舞台，地毯/沙发高、光洁瓷砖低）+丰富性（多场景覆盖多卖点 或 单场景做厚=多角度多卖点完整过程非一个动作反复，二选一）+连贯一致（拧成同一产品叙事且与真实用法一致，非拼贴非演错用法）；人员看配置是否强化说服非人数（单/多人/单人用+多人体验皆可，多人须带'大家都有好体验'社会化证据）；独立背书归 S5。",
            "improvements 每项必须含：title,target_stage,gmv_impact,gap_type,time_range,creator_time_range,benchmark_time_range,problem,benchmark_reference,benchmark_evidence_ids,suggestion,actions,gmv_reason,evidence,creator_script,creator_script_zh,base_frame_suitability,best_base_frame_time,base_frame_evidence_id,base_frame_reason,aigc_prompt,aigc_image_path,expected_effect,priority。",
            "可额外输出 top-level low_confidence_stages，数组元素只能是 S1-S6；只有当该阶段现有帧/音频不足以支撑 severity 时才填写，最多 2 个。",
            "除 stage_analysis、improvements、video_understanding.evidence_units、low_confidence_stages 和 category_profile.painpoints 外，所有数组最多 1 条。所有描述字段最多一句且不超过 40 个汉字。video_understanding 必须原样使用事实清单，不得新增、改写或跨视频移动 evidence_units。",
        ]
    )
    payload = build_llm_payload(model, user_text, [])
    # temperature=0：对比判断要可复现，消除 severity 在边界 case（如 S3）上的抖动。
    payload["temperature"] = 0.0
    # 16384：观察到 8192 在 qwen-vl-max-latest 下被截断（中文 schema + 完整 stage_analysis + 多条 improvements）。
    # qwen 支持到 16K-32K，提到 16K 留出余量。如还不够可继续调，或在 prompt 里强制精简。
    payload["max_tokens"] = 16384

    # Phase B：把每条 evidence 的关键帧 + 切片音频挂到 user message（增强判断的感官输入）。
    if analysis is not None:
        sensory = build_evidence_sensory_inputs(analysis, facts)
        if sensory:
            user_msg = payload["messages"][1]
            base_text = user_msg["content"] if isinstance(user_msg["content"], str) else ""
            user_msg["content"] = [
                {"type": "text", "text": base_text},
                {
                    "type": "text",
                    "text": (
                        "## 各 evidence 对应的画面帧与切片音频（仅辅助判断声画质感，不可据此新增或改写事实）\n"
                        "下面按 role 和 evidence id 附上对应时段的关键帧与音频。"
                        "请按 S1-S6 功能阶段自行对齐两条视频的 evidence 做横向对比。"
                    ),
                },
                *sensory,
            ]

    payload["messages"][0]["content"] += (
        "本次对比分析的唯一事实来源是用户提供的已校验单视频事实清单；"
        "不得新增 evidence_unit，不得改写口播，不得把 benchmark 与 creator 的口播或画面互换。"
        "\n\n## 感官素材使用规则（Phase B）\n"
        "随请求附带了每条 evidence 对应时段的关键帧和切片音频。"
        "帧和音频仅用于评估已有 evidence 的声画质感、强度与情绪，以支撑 severity 和对比结论；"
        "不得据此新增或改写 facts 的事实单元；"
        "当帧/音频与 facts 文字描述冲突时以 facts 为准，可在该处判断理由中标注'此处存在感知歧义'。"
        "请按 S1-S6 功能阶段对齐两条视频的 evidence 再做横向对比，不要按绝对时间对齐。"
        "判断口播表现时必须尊重 speech_mode：spoken 视频评口播骨架；subtitle_driven 视频评字幕文案轨；"
        "visual_driven/music_driven 视频不得因 voiceover 为空而直接扣分，必须看画面变化、OCR、BGM/节奏是否完成同一阶段功能。"
        "\n\n## 低置信阶段声明（Phase C）\n"
        "如果某个阶段的 severity 需要观察连续动作、效果瞬间或声画关系，而当前 evidence 代表帧/切片音频不足，"
        "可在 top-level low_confidence_stages 写入该阶段代码（如 [\"S4\"]），最多 2 个。"
        "普通商业判断困难、双方都缺内容、或 facts 已足够判断时不要标低置信。"
        "\n\n## QA 自检规则\n"
        "输出前必须按 QA-RULES.md 检查：module_id 必须来自结构库官方编号；"
        "stage evidence_ids 必须存在且与阶段时间相交；product_visibility 数值必须自洽；"
        "若发现会违反 QA 的内容，先自行修正再输出 JSON。"
    )
    return payload


def render_speech_mode_block(analysis: dict[str, Any]) -> str:
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    lines = []
    for role in ("benchmark", "creator"):
        info = videos.get(role) if isinstance(videos, dict) else None
        if not isinstance(info, dict):
            continue
        mode = info.get("speech_mode") if isinstance(info.get("speech_mode"), dict) else {}
        lines.append(f"- {role}: {speech_mode_prompt(mode)}")
    return "\n".join(lines) if lines else "（未分类，按事实清单和现有素材判断）"


def build_stage_review_payload(
    model: str,
    analysis: dict[str, Any],
    facts: dict[str, Any],
    current_result: dict[str, Any],
    stage_codes: list[str],
) -> dict[str, Any]:
    """Phase C：对低置信阶段切原生视频片段，只重判这些阶段。

    这是一次性回看，不允许模型继续索要素材；事实清单仍是唯一事实源。
    """
    target_codes = normalize_stage_codes(stage_codes)[:2]
    target_stages = [
        stage for stage in current_result.get("stage_analysis", [])
        if stage_code(stage.get("stage")) in target_codes
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
    }
    s1_contract = ""
    s2_contract = ""
    s3_contract = ""
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
            "landing_reason": "只引用 0 到 hook_boundary_seconds 内的时间戳+原话/画面，说明对象/张力/承诺或证据是否齐全",
            "window_evidence": "0.0s 到 hook_boundary_seconds 内实际出现的画面/口播/字幕",
            "landing_window_leak": False,
            "anchors_proposition": True,
        }
        stage_update_example["creator_hook"] = hook_example
        stage_update_example["benchmark_hook"] = hook_example
        s1_contract = (
            "目标阶段包含 S1 时，stage_update 必须同时重判 creator_hook 与 benchmark_hook；"
            "不得沿用当前阶段判断里的旧 hook。先按 structure_library_full.md 判 S1/S2 边界："
            "S1=抢夺注意力，S2=从 Hook 自然过渡到产品；开始回答 Hook、解决方案承接、产品名/卖点/认证/产品成为主角通常是 S2 起点。"
            "S2-A 承接式引出可早于产品实物或产品名出现，不能等产品画面才切 S2。"
            "landing_met 只能按 0 到 hook_boundary_seconds 内的三件套判：对象明确 + 张力明确 + 承诺或证据明确，"
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
        }
        stage_update_example["creator_s2"] = s2_example
        stage_update_example["benchmark_s2"] = s2_example
        s2_contract = (
            "目标阶段包含 S2 时，stage_update 必须同时重判 creator_s2 与 benchmark_s2；"
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
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": False,
            "fake_or_staged": False,
            "start_seconds": 8.0,
            "end_seconds": 18.0,
            "usage_reason": "真实使用动作中能看见核心卖点如何发生；若只口播卖点则写未被动作证明",
            "evidence_ids": ["C1"],
        }
        stage_update_example["creator_s3"] = s3_example
        stage_update_example["benchmark_s3"] = s3_example
        s3_contract = (
            "目标阶段包含 S3 时，stage_update 必须同时重判 creator_s3 与 benchmark_s3；"
            "S3 只判真实使用过程：是否真实操作、核心卖点是否在动作里可见、场景是否适配、过程是否连贯、素材是否足够丰富。"
            "只口播/字幕说卖点但画面没演，不算 core_selling_point_visible；场景丰富不能补偿核心卖点没落地。"
            "效果结果归 S4，背书归 S5，不要回填到 S3。"
        )
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "\n\n".join(
                [
                    "# Phase C 低置信阶段回看",
                    "你将看到低置信阶段对应的原生视频切片（含画面和声音）。",
                    "切片边界可能有 ±2 秒误差，可能混入相邻阶段内容；判断按功能归属，不要把相邻阶段内容算进本阶段。",
                    "只重判 target_stages 中列出的阶段；不要改写 video_understanding，不要新增 evidence_unit。",
                    "必须先在 gap 字段写清判断依据（达人做了什么→标杆做了什么→对购买意愿影响），再给 severity。",
                    "回看后必须按主分析同一标尺重打 creator_execution 与 benchmark_execution（0=未执行；0.5=做了但基本无效/敷衍/无法有效接收；1=合格有效；2=出色。两侧独立打分，先打分再对比）和 painpoint_relevance——系统将据这些事实重推导差距等级；severity 仍需填写但仅作参考。",
                    s1_contract,
                    s2_contract,
                    s3_contract,
                    "只输出严格 JSON，不要 Markdown。",
                    "输出格式：",
                    json.dumps(
                        {
                            "stage_updates": [
                                stage_update_example
                            ],
                            "review_notes": ["为什么回看后这样判断"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "## 目标阶段",
                    json.dumps(target_codes, ensure_ascii=False),
                    "## 当前阶段判断",
                    json.dumps(target_stages, ensure_ascii=False, indent=2),
                    "## 已校验单视频事实清单（唯一事实来源）",
                    json.dumps(facts, ensure_ascii=False, indent=2),
                ]
            ),
        }
    ]
    content.extend(build_stage_review_video_inputs(analysis, target_stages))
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr 的低置信阶段复核器。只输出严格 JSON。"
                    "本轮只能基于用户给出的 facts 和原生视频切片，重判指定 S1-S6 阶段。"
                    "不得新增、删除或改写 evidence_units；可修正该阶段的 gap、severity、support_status、summary、quote 和 evidence 引用。"
                    "如果目标阶段包含 S1，必须重新输出 creator_hook 与 benchmark_hook，不得复用旧 hook 判断。"
                    "severity 仍按购买意愿影响定级：large=直接影响购买意愿的硬伤；medium=削弱说服力但不致命；small=细节瑕疵或达人持平/更优。"
                    # 接地约束：禁止从含糊音频脑补话术（kakwan S6 幻觉教训）；不预设判断方向。
                    "判断只能基于切片中真实听到/看到的内容：引用口播必须能对上切片音频，"
                    "听不清就写听不清并标 voice_only，禁止推断或补全未听清的话术。"
                    "达人持平或更优时如实给 small，达人明显缺失时如实给 large，不预设任何方向。"
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
) -> list[dict[str, Any]]:
    """为 Phase C 低置信阶段附上对应时间窗的原生视频切片。"""
    content: list[dict[str, Any]] = []
    videos = analysis.get("videos", {})
    for stage in target_stages:
        code = stage_code(stage.get("stage"))
        for role in ("benchmark", "creator"):
            info = videos.get(role) or {}
            video_path = Path(str(info.get("path") or ""))
            if not video_path.is_file():
                continue
            time_range = str(stage.get(f"{role}_time_range") or stage.get("time_range") or "")
            start, end = parse_time_range_seconds(time_range, info.get("duration_seconds"))
            # ±2s 缓冲：阶段 time_range 是模型估计，边界偏差常见；prompt 已告知按功能归属判断。
            padded_start = max(0.0, start - 2.0)
            padded_end = min(float(info.get("duration_seconds") or end), end + 2.0)
            data_url = video_to_data_url(
                video_path,
                fps=3.0,
                max_width=480,
                start=padded_start,
                duration=max(0.5, padded_end - padded_start),
            )
            if data_url is None:
                continue
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"【Phase C 回看视频｜{role}｜{code}｜"
                        f"{format_seconds(padded_start)} - {format_seconds(padded_end)}】"
                    ),
                }
            )
            content.append({"type": "video_url", "video_url": {"url": data_url}})
    return content


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


def extract_comparison_context(analysis_input: str) -> str:
    """从 analysis_input.md 中提取"分析等级"和"产品信息"两段。"""
    sections = []
    for heading in ("## 分析等级与结论边界", "## 产品信息"):
        pattern = rf"{re.escape(heading)}\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, analysis_input, flags=re.S)
        if match:
            sections.append(f"{heading}\n{match.group(1).strip()}")
    return "\n\n".join(sections) or "## 产品信息\n（缺失）"


def build_llm_payload(
    model: str,
    analysis_input: str,
    visual_inputs: list[dict[str, str]] | None = None,
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
                    "分析必须严格遵循输入中的 ANALYSIS-PROMPT.md：第一步，整体感知并输出 one_line_verdict、holistic_assessment，不引用具体证据；"
                    "第二步，输出 product_visibility，并将事实证据映射到 structure_library_full.md 的 S1-S6 语义阶段、官方模块编号、模块适配性和真实时间边界；"
                    "第三步，输出 loop_closure，并基于被引用证据比较 gap_type 和提升点。"
                    "输出必须精炼：每个视频列出 3 到 6 个关键 evidence_units；任何 evidence、visual_evidence、gap_summary 或 actions 数组最多 3 条；"
                    "每个描述字段最多一句，improvements 按 GMV 杠杆排序输出 1-5 条；视频值得改的点确实多就给 3-5 条，确实只有 1-2 个 GMV 杠杆点就给 1-2 条，不要为凑数编造。"
                    "禁止重复同一判断，禁止为了描述缺失而枚举不存在的音效、卡点、镜头或功能；缺失内容用一句“未发现对应证据”概括。"
                    "不要把 0~3s、3~6s 等参考时间当作固定切片。"
                    "stage_analysis 必须固定输出六项且顺序为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA；每个阶段都必须分别写 benchmark_time_range 和 creator_time_range，并写 creator_module_id、benchmark_module_id、module_fit、module_fit_reason、task_completion、gap_type、gap_summary 和 voice_performance。"
                    "有有效口播时，benchmark_key_message/creator_key_message 必须以该段实际口播传递的信息为核心，再选择确实支持该信息的画面证据。"
                    "没有有效口播时，必须以可见画面与字幕为核心，不得把音乐或推测写成信息。"
                    "每个阶段必须引用 video_understanding 中的 evidence_ids，并写 visual_evidence 和 support_status："
                    "口播与画面共同支持为 supported；口播提及但画面不能验证为 voice_only；仅画面/字幕承载信息为 visual_only；两者矛盾为 conflict。"
                    "阶段引用的事实时间必须与该阶段时间相交；若某阶段确实不存在独立内容，仍应建立该时间段的 evidence_unit，明确说明未发现对应口播或画面，而不是借用其他阶段事实。"
                    "输入中如提供 transcript.srt，其时间戳是口播归因的权威依据；口播不在阶段时间内时必须调整阶段边界或不得引用。"
                    "不得写某张画面展示了认证、成分或效果，除非附带关键帧中实际可见。"
                    "只可把请求中实际附带的关键帧视为已观察画面；未被附图覆盖的时段不得臆造镜头内容，应写为画面证据不足待复核。"
                    "同一关键信息只归入一个最主要阶段，禁止在多个阶段重复作为表现依据。"
                    "认证或审批信息，例如 KKM、KKMA、认证，不属于 Hook。若其与首次产品卖点一起被口播引出，应只归入 S2 作为产品信任支撑；"
                    "认证与营养成分、产品名称等产品属性连续表达时，即使发生在 10 秒之后也仍属于 S2，应延长 S2 的真实边界，不得塞入 S3/S4。"
                    "只有出现独立证明环节时才归入 S5。若口播说到 KKM 但画面不显示认证标记，必须标记 voice_only，并表述为口播声称、画面未验证。"
                    "每个阶段都应从转写中摘录对应本地语言口播到 benchmark_quote/creator_quote，并附中文翻译；没有明确口播时留空。"
                    "每个阶段和提升点都必须写 evidence，引用时间段、画面或口播证据。"
                    "提升点按 GMV 杠杆排序，不按 S1-S6 顺序凑数：CTA 与 Hook 的大差距优先于中等信息传递差距。"
                    "每个提升点必须先抽象标杆功能意图，再结合产品决策权重和达人现有拍法生成原创可执行建议；不得把标杆卖点、原句或动作机械搬给达人。"
                    "涉及卖点时，必须使用第 0 步商业权重判断理性/感性哪个更能驱动该品类，而不是硬凑两者或照搬标杆。"
                    "儿童牙膏等两极产品逻辑品类：若达人已讲清按压、用量、减少浪费等功能痛点，标杆香味/口味/调性只能作辅助体验，不得自动排到功能卖点前，不得作为 Top 1 提升点；不得建议新增孩子演员、品尝动作、闻香镜头或“孩子一定喜欢”等不可验证表达。"
                    "suggestion 必须优先在达人已有素材和拍摄方式内改造；只有 no_suitable_frame 时才建议补拍或补素材。"
                    "达人建议话术必须使用达人口播语言，creator_script_zh 只放中文翻译。"
                    "如果达人没有有效口播或语言识别不可靠，则根据标杆视频语言/目标市场语言撰写全新的本地语言建议话术，不得把音乐、噪音或无关字幕当作话术。"
                    "达人执行话术必须是针对达人素材重新设计的原创表达，不得抄写或轻微改写标杆口播。"
                    "每个提升点必须输出 base_frame_suitability。只有达人现有画面确实适合作为目标改造基底时，才可写 usable 和 best_base_frame_time；"
                    "如达人素材缺少目标所需的人物、产品或场景，必须写 no_suitable_frame，best_base_frame_time 留空，并在建议中明确需补拍或补素材。"
                    "每项提升点还必须输出 benchmark_evidence_ids 与 base_frame_evidence_id；前者只可指向所属阶段的标杆事实证据，后者必须指向基底帧所在的达人事实证据。"
                    "base_frame_reason 只能描述该达人证据中真实可见的素材。aigc_prompt 只能基于真实存在的达人原始画面写具体改造提示词，不得把不存在的人物、口播或场景说成已有素材。aigc_image_path 留空。"
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
                    "双方都没有独立设计的阶段（如 S5），key_message 写'均未设计该环节'。"
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "temperature": 0.2,
        # 16384：Qwen 默认 max_tokens 偏低（2048-4096），完整 stage_analysis + improvements 需要 12K+ tokens。
        "max_tokens": 16384,
    }


def build_llm_repair_payload(
    model: str,
    raw_result_text: str,
    error_message: str,
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """JSON 修复请求 payload。校验失败时由 pipeline 触发。

    设 max_tokens=16384 与 build_llm_comparison_payload 一致；
    否则 qwen 等 provider 默认 max_tokens 偏低，重新输出完整结构会被截断成残缺 JSON。
    """
    locked_facts_block = ""
    if locked_video_understanding:
        locked_facts_block = json.dumps(locked_video_understanding, ensure_ascii=False, indent=2)
    return {
        "model": model,
        "max_tokens": 16384,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr JSON 修复器。只输出严格 JSON，不要 Markdown，不要解释。"
                    "必须符合 references/analysis-output-schema.json：保留 one_line_verdict、holistic_assessment（每维独立评估）、key_conclusions（1-5 条消费者视角）、product_visibility、loop_closure，6 个 stage_analysis，1-5 个 improvements（按 GMV 杠杆排序）。"
                    "如果原始输出缺少 improvements（如 JSON 被截断），必须基于 stage_analysis 的差距分析补充 1-5 条。"
                    "severity 必须差异化：功能没完成=large，有短板=medium，做到位或持平=small。"
                    "必须保留 video_understanding 证据事实清单。stage_analysis 必须严格按 S1、S2、S3、S4、S5、S6 顺序输出六项；阶段必须保留 benchmark_time_range、creator_time_range、证据引用、核心信息、画面证据和 support_status；达人话术必须保留本地语言和中文翻译。"
                    "每个阶段引用的事实单元时间必须与阶段时间相交；缺少独立内容的阶段也要提供该时段的无对应内容事实单元。"
                    "提供了 transcript.srt 时，以其时间戳重新校对口播对应阶段；认证与产品属性连续表达时只归入 S2。"
                    "一条事实只归属一个主要阶段；KKM 等认证信息不能写入 Hook，与产品引出同段出现时仅归入 S2；口播提及但画面不可见时标记 voice_only。"
                    "S1 Hook 必须补齐 creator_hook 与 benchmark_hook 两个对象，字段为 exists(bool)、type(A-G 或 unknown)、dims{camera,copy,sound,rhythm}(bool)、hook_boundary_seconds(number)、hook_boundary_reason(非空)、s2_start_signal(非空)、landing_met(bool)、landing_reason(非空)、window_evidence(非空)、landing_window_leak(bool)、anchors_proposition(bool)。"
                    "hook_boundary_seconds 按 structure_library_full.md 的 S1 留人机制→S2 产品引出/解决方案承接功能切换判断，不得写死固定秒数；S2-A 承接式引出可早于产品实物或产品名出现，不能等产品画面才切 S2。"
                    "landing_met 按 type 无关三件套判断：0 到 hook_boundary_seconds 内对象明确、张力明确、承诺或证据明确，缺一即 false；不得用后续 S2/S3 产品介绍补足 S1 landing。若引用边界后材料，landing_window_leak=true 且 landing_met=false。"
                    "S2 产品引出必须补齐 creator_s2 与 benchmark_s2 两个对象，字段为 exists(bool)、merged_with_s3(bool)、module_type(A-D或unknown)、handoff_met(bool)、s1_s2_compatible(bool)、product_identity_clear(bool)、product_role_clear(bool)、excluded_or_risky_module(bool)、start_seconds(number)、end_seconds(number)、handoff_reason(非空)、evidence_ids(非空数组)。"
                    "S3 使用过程必须补齐 creator_s3 与 benchmark_s3 两个对象，字段为 exists(bool)、module_type(A-E或unknown)、real_usage_met(bool)、core_selling_point_visible(bool)、usage_context_fit(bool)、continuity_met(bool)、richness_met(bool)、fake_or_staged(bool)、start_seconds(number)、end_seconds(number)、usage_reason(非空)、evidence_ids(非空数组)。"
                    "提升点必须保留 benchmark_evidence_ids、base_frame_suitability、best_base_frame_time、base_frame_evidence_id、base_frame_reason 和 aigc_prompt；无可用达人素材时写 no_suitable_frame 且时间与 base_frame_evidence_id 留空。aigc_image_path 留空。"
                    "修复 improvements 时也必须遵循达人框架约束、卖点适配权重和标杆功能意图转译，不得把 benchmark_reference 直接改写成 suggestion。"
                    "健康品类建议不得声称调节激素、改善月经、治疗症状或虚构优惠。建议话术必须重新设计，不得复制标杆原句。"
                    "输出必须精炼，每个描述字段最多一句，improvements 按 GMV 杠杆排序保留 1-5 条；不要为凑数编造。"
                    "任何列表最多 3 条；不要枚举或重复不存在的音效、镜头或功能，缺失证据只写一句概括。"
                    "保留原分析含义，但补齐缺失字段、修正字段类型和 JSON 语法。"
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
                        "已锁定单视频事实清单（唯一事实源，补字段只能引用这里，不得新增/改写 evidence_units）：",
                        locked_facts_block[:24000] if locked_facts_block else "（未提供 locked facts；只能修 JSON 结构，不得补事实依据）",
                        "模型原始输出：",
                        raw_result_text[:12000],
                    ]
                ),
            },
        ],
        "temperature": 0.0,
    }
