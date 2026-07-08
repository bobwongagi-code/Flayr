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

import json
from pathlib import Path
from typing import Any

from .artifacts import (
    format_seconds,
    get_focus_frame_entries,
    get_frame_entries,
    sample_evenly,
)
from .shot_track import render_shot_track_markdown
from .speech_mode import speech_mode_prompt
from .subtitle_track import render_subtitle_track_markdown
from .utils import read_optional_text


ROOT = Path(__file__).resolve().parents[2]


def read_track_markdown(track_path: Path, renderer: Any, disabled_hint: str) -> str:
    """读取预处理轨 json 并渲染成 markdown；文件不存在或损坏时返回提示（未启用/未生成）。"""
    if not track_path.is_file():
        return disabled_hint
    try:
        track = json.loads(track_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return disabled_hint
    return renderer(track)


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
        "## QA-RULES.md 自检契约（输出前必须自检）",
        "",
        read_optional_text(ROOT / "QA-RULES.md"),
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
        "4. 同一关键信息只能归属于一个主要阶段。第三方机构背书（监管认证 KKM/Halal/SIRIM、行业协会、评测中心/实验室、高校研究、调研咨询、疾病防治中心等类型）功能是外部背书，按功能归入 S5 信任放大，不归 S2、更不是 Hook。判定背书的关键门槛：该机构的数据/实验/研究要在证明本产品价值才算背书；仅提到机构名字、赞助或合作 logo、而无证明本产品价值的数据，不算背书、也不归 S5。自述功效是卖点不算背书。口播提到但画面未显示时，必须标明口播声称、画面未验证。",
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
        "14a. 每阶段 task_completion 只能输出 complete、partial、missing 三选一，评估的是达人侧该阶段功能完成度（完成/部分完成/未做）。禁止 both_complete、completed、双侧组合词或任何自由文本；标杆侧完成情况写在 benchmark_summary，不写进此字段。",
        "14b. 每阶段必须输出 creator_execution 和 benchmark_execution 两个独立执行分，取值只能是 0、0.5、1、2 四个数字：0=未执行该阶段功能；0.5=做了但对核心功能基本无效——敷衍、平庸无感（如一句轻带的 CTA、平铺直叙的开场、仅口头承诺无验证）；1=执行合格（功能完成且有效）；2=执行出色。两侧各自按该阶段功能定义独立打分，先打分再对比，禁止因对比结果回调分数；这是系统推导差距等级的事实输入。",
        "14b1. 0.5 档同样适用于'内容存在但消费者无法有效接收'：看不清（虚焦/过曝/遮挡/一闪而过/画面晃动到观众抓不住重点）、听不清（吞字/被 BGM 压制）、读不完（字幕停留过短）——物理存在不等于有效传递。S5 背书孤证规则：仅口播提及背书而画面无佐证、或背书标志一闪而过无法辨认，执行分最高 0.5。",
        "14b3. 效果呈现阶段（S4）执行分以 product_profile.core_visual_proposition（本品核心视觉命题）为锚点，不套通用 before/after：先判该侧有没有拍出本品的决定性瞬间（定妆粉饼=油光→哑光对比、面膜=逐日变化+敷后效果），并满足 shooting_requirement（效果细微的品需正面强光+特写才算拍到）；拍出命题且拍摄到位才给 2，只完成动作（揭膜/擦粉/口头带过）未体现命题、或拍摄条件不支撑（暗光/无特写看不出效果）按敷衍计最高 0.5，做了但缺命题对比的'呈现单薄'最高 1。过长全程记录不加分（标尺是命题覆盖非完整性）。",
        "14b4. S4 给执行分前必须做一次闭环核验：回到该侧关键帧，对照 core_visual_proposition 与 visual_diff_dimensions，在画面上实际确认那个视觉对比肉眼可见——'存在 before/after 结构'不等于'对比拍出来了'。若该侧前后帧在指定维度上看不出明显差异（油光帧与哑光帧看起来差不多、敷膜前后肤质无变化），即命题未被有效呈现，该侧执行分最高 1（只完成动作未呈现效果），几乎完全无差异则 0.5。把你自己定的命题当检查清单逐帧核对，不许凭结构臆断。两侧同此核验。",
        "14b5. S4 执行分主轴只有一个：core_visual_proposition（核心命题）的有效呈现。trust_multipliers（防水/防汗测试、美容仪、周期记录、专业手法等）是加分项，只能在核心命题已有效呈现（该侧≥1）时把分抬向 2；不能替代、也不能补偿弱核心命题。若某侧核心命题没拍出来（对比弱/不可见），哪怕它有很强的次要演示，该侧执行分仍封顶 1——严禁用次要演示把分顶上去。先看核心命题达没达到，再决定加分项加不加。",
        "14b6. S3 执行分 2 不是『有真实使用过程』，而是『核心卖点在动作里清楚可见 + process_framing_met=true + 过程被做厚』。单场景连续展示只说明 S3-A 成立，通常最高 1；要到 2，必须通过多角度、多步骤、多卖点、多场景或角色互动把核心卖点证明得更充分。看不清对象/动作/证明区域时最高 0.5。",
        "14b2. 每阶段必须输出 painpoint_relevance（benchmark_only/creator_only/both/none 四选一）：该阶段双方内容是否命中 category_profile.painpoints 的核心决策因素，按内容功能判断（讲没讲到、演没演到核心痛点），不要求字面用词一致。",
        "14c. 顶层输出 category_profile 品类画像：category_name（品类名）、price_tier（low/mid/high 客单价档）、decision_threshold（impulse 冲动可买 / considered 需被说服）、drive_type（emotional/functional/mixed 驱动类型）、painpoints（该品类目标消费者最在意的决策因素关键词，每个痛点中文+本地语双语表述放同一数组，共 6-16 个词条）。只报品类事实与世界知识，不做权重判断。",
        "14d. 打分前先输出 product_profile 产品商业 DNA（这是 S1-S6 打分的尺子，先立尺再量）：visualizable（yes/no 核心价值能否视觉化）、physical_task（解决的最直观尴尬）、hook_proposition（本品对目标人群最有拦截力的点=钩子命题，类型取决于本品、不限痛点——可痛点/承诺/反差/情绪/向往/视觉吸引/身份代入/场景还原等，见 structure_library S1 七型，模型按品类+视频推、运营可覆盖）、core_visual_proposition（决定性视觉瞬间=本品到位效果展示的标准，按本品现推，别套通用 before/after）、visual_diff_dimensions（本品 before/after 应在哪些视觉维度变化，从 亮度反光/纹理毛孔/色泽均匀度/水润干燥/肿胀轮廓 中选或按品自命名如去污/拉丝，1-3 个，S4 核验对比只看这些维度）、trust_multipliers（建立专业度的元素如美容仪/周期记录/第三方检测，3-6 个）、shooting_requirement（卖点显现所需拍摄条件）、confidence（high/low，小众或本地新奇特品标 low）。只报产品事实与品类世界知识。visualizable=no（香水/保健品/隐形矫正等效果拍不出）时 S4 不强求视觉命题，把判断重心转到 S5 信任放大与达人可信度。",
        "14e. 每阶段输出 stage_standard_delivery（benchmark_only/creator_only/both/none）：该阶段双方是否有效达到本阶段的『本品到位标准』（见 14f 对照表锚点）。做到/展示到才算，仅口头讲到不算。先作为事实输出，暂不参与推导。",
        "14f. S1-S6 执行分统一三层判：阶段目标(core_question) → 用了什么做法(module_id/module_fit) → 该做法在【本品】上到位没(execution)。'到位'按阶段查本品锚点、核心目标为主轴次要元素不补偿弱核心；本轮已接入的阶段锚点——S4 效果呈现→锚 core_visual_proposition（详见 14b3/14b4/14b5）；S5 信任放大→锚 trust_multipliers：硬信任（第三方认证/检测/临床/仪器实测/官方背书）有效呈现可达 2，软信任（真实好评/社会认同/向往式对比/使用记录/达人自用）算信任但封顶 1（软不如硬），自述功效/纯参数不算；位置优先——视频开头的此类背书内容算 S1 钩子（留人）、结尾算 S6 CTA，不要按语义把开头/结尾的背书塞进 S5；判'用没用且呈现有效'非'口头说没说'，口播孤证或标志一闪而过最高 0.5（沿用 14b1 的 S5 背书门槛）；S6 促单→到位=把 structure_library S6 五型各自【适配条件】（含排除项，如价格锚定/赠品堆叠排除情感满足品=category_profile.drive_type=emotional）套上本品特征 category_profile（decision_threshold/drive_type/品类）+命题 product_profile，判达人/标杆选的 CTA 类型适配与否＋执行到位与否，gap=适配×执行差距；决策类型（冲动/高决策）是输入之一非唯一轴——冲动品需清晰指令+紧迫感、高决策品需先消顾虑再 CTA，但哪型 CTA 好仍由(五型适配条件×本品命题)结合得出。S1 钩子→到位=把 structure_library S1 七型各自【适配条件】（按品类/购买动机匹配）套上本品特征 category_profile（品类/drive_type/decision_threshold）+命题 product_profile（hook_proposition），判达人/标杆选的钩子类型适配本品与否＋执行到位与否，gap=适配×执行差距；不预设某根轴（痛点/视觉冲击/悬念）通用为好，好坏由(类型适配条件×本品命题)结合得出（潮玩配场景还原/反差、儿童牙膏配身份代入/场景还原、榨汁机配反差/场景还原）；开头的背书/认证类内容按钩子算（见 14b1 位置宪法）；S2 产品引出→到位=引出自然 + 承接 S1 钩子（冲着钩子抛出的那个点去承接，痛点钩→引出冲着解痛点）+ 引出产品身份（这是什么品）；S2 只判这两件事，不判卖点本身、也不判卖点细节/选购指导/适配人群/参数/信息完整度（这些归 S3/S4）——标杆比达人多讲分肤质版/选购建议/卖点细节，不构成 S2 差距，只要达人自然引出+点明产品身份即同等到位；锚 hook_proposition 承接；S3 使用过程→主轴锚 core_selling_points（卖点传递有效性）+ 场景层 usage_context：到位=真实使用过程中把核心卖点'演示出来'被看见（清洁机吸力强/干湿分离/易倒垃圾在动作里可见，不是嘴上讲——演示即证据=打开水箱看见分层）；按 14b5 这是不可补偿主轴，场景再丰富人员再多样、卖点没在过程落地仍判弱；前置门槛真实感（显假/摆拍直接封顶低分）；S3 不评教学清晰度；场景层三看——适配度（场景给没给卖点舞台，地毯/沙发高、光洁瓷砖低）+丰富性（多场景覆盖多卖点 或 单场景做厚=多角度多卖点完整过程非一个动作反复，二选一）+连贯一致（拧成同一产品叙事且与真实用法一致，非拼贴非演错用法）；人员看配置是否强化说服非人数（单/多人/单人用+多人体验皆可，多人须带'大家都有好体验'社会化证据）；独立背书归 S5。",
        "15. JSON 输出保持简洁：每个视频列出 3~6 个关键 evidence_units；任何差距、证据或动作列表最多 3 条；每个描述字段最多一句；禁止重复列举未出现的音效、镜头或功能。",
        "16. 输出前按 QA-RULES.md 自检：证据引用必须存在且与阶段时间相交，module_id 必须来自 structure_library_full.md，product_visibility 数值必须自洽。",
        "17. 如果需要写回系统，请只输出符合 references/analysis-output-schema.json 的 JSON。",
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
                f"- 证据组织模式：{speech_mode_label(info)}",
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
                "### 二级视频证据视图（复核用，不是评分字段）",
                "",
                render_video_evidence_markdown(role_dir, info),
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
                "### 权威字幕轨（OCR 识别，卖点/价格/年龄段叠字以此为准，胜过画面认字）",
                "",
                read_track_markdown(
                    role_dir / "subtitle_track.json",
                    render_subtitle_track_markdown,
                    "（未启用 OCR 字幕轨；字幕以画面识别为准）",
                ),
                "",
                "### 镜头切分轨（精确镜头边界，定阶段起止时参考它，别切在镜头中间）",
                "",
                read_track_markdown(
                    role_dir / "shot_track.json",
                    render_shot_track_markdown,
                    "（未生成镜头轨）",
                ),
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


def speech_mode_label(info: dict[str, Any]) -> str:
    mode = info.get("speech_mode") if isinstance(info.get("speech_mode"), dict) else {}
    if not mode:
        return "未分类"
    return speech_mode_prompt(mode)


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


def render_video_evidence_markdown(role_dir: Path, info: dict[str, Any]) -> str:
    evidence = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    selection_report_path = Path(str(evidence.get("frame_selection_report_path") or role_dir / "frames" / "selection_report.json"))
    dedup_count = evidence.get("dedup_kept_frame_count")
    if dedup_count is None and selection_report_path.is_file():
        try:
            report = json.loads(selection_report_path.read_text(encoding="utf-8"))
            dedup_count = report.get("kept_count")
        except (json.JSONDecodeError, OSError):
            dedup_count = "未知"
    lines = [
        f"- 帧去重审计：{selection_report_path}",
        f"- 帧去重审计 HTML：{evidence.get('frame_selection_report_html_path') or role_dir / 'frames' / 'selection_report.html'}",
        f"- 去重后变化帧：{dedup_count if dedup_count is not None else '未知'} / 原始 {info.get('frame_count', 0)}",
        f"- 顺序联系表目录：{evidence.get('contact_sheets_dir') or role_dir / 'contact_sheets'}",
        f"- 时间线证据图目录：{evidence.get('timeline_views_dir') or role_dir / 'timeline_views'}",
        f"- 证据视图自检：{evidence.get('audit_path') or role_dir / 'video_evidence_audit.json'}",
    ]
    views = evidence.get("timeline_views") if isinstance(evidence, dict) else None
    if isinstance(views, list) and views:
        lines.append("- 时间线证据图：")
        for item in views:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or "timeline"
            start = item.get("start_seconds")
            end = item.get("end_seconds")
            path = item.get("path") or ""
            lines.append(f"  - {label}: {start}s-{end}s {path}")
    packed = evidence.get("transcript_pack_path") if isinstance(evidence, dict) else None
    packed_path = Path(str(packed or role_dir / "transcript_packed.md"))
    lines.extend(["", "#### 紧凑口播索引", ""])
    lines.append(read_optional_text(packed_path))
    return "\n".join(str(line) for line in lines)
