"""flayr_core.postprocess.health_rewrite：健康品类（维生素 / 营养补充 / 儿童牙膏）合规重写专项。

⚠️ 仅适用于健康品类。本模块包含两部分：
   1. 两个 validate_* 函数（validate_recommendation_safety / validate_creator_script_language）
      **会抛 SystemExit** 触发 pipeline 走 repair payload 重跑，调用方必须感知。
      它们和下面的 sanitize_* 共用同一组健康关键词，强耦合，所以放在一起。
   2. sanitize_* 系列对健康品类提升点做合规重写（按 hook / cta / 信任 / 使用等位置分类）。

未来若新增其他敏感品类（如美妆功效、母婴用品等）合规重写，请新增 cosmetics_rewrite.py /
baby_rewrite.py 等平级文件，不要往本模块塞。

依赖：仅依赖外部 (re)，与 postprocess 包内其他模块完全解耦。
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# validate（会抛 SystemExit）
# ---------------------------------------------------------------------------

def validate_recommendation_safety(result: dict[str, Any], analysis_input: str) -> None:
    """健康品类（维生素 / 营养补充）建议中禁止医疗承诺与虚构优惠。

    ⚠️ 触发条件命中时抛 SystemExit，调用方需感知。
    """
    health_product_markers = ("维生素", "营养补充", "supplement", "vitamin")
    if not any(marker.lower() in analysis_input.lower() for marker in health_product_markers):
        return
    prohibited_patterns = [
        r"激素",
        r"hormon",
        r"月经",
        r"menstru",
        r"period",
        r"血块",
        r"darah",
        r"治疗",
        r"治愈",
        r"cure",
        r"treat",
        r"改善.{0,6}(症状|皮肤|月经)",
        r"diskon",
        r"折扣",
    ]
    violations: list[str] = []
    fields = ("suggestion", "creator_script", "creator_script_zh", "aigc_prompt")
    for index, item in enumerate(result.get("improvements", []), start=1):
        text = "\n".join(str(item.get(field) or "") for field in fields)
        matched = [
            pattern
            for pattern in prohibited_patterns
            if re.search(pattern, text, flags=re.IGNORECASE)
        ]
        if matched:
            violations.append(f"提升点 {index} 含高风险或未证实承诺：{', '.join(matched)}")
    if violations:
        raise SystemExit(
            "健康品类建议不合规。"
            + "；".join(violations)
            + "。请保留对标杆风险的分析，但重写达人建议为合规表达：只谈日常营养补充、产品展示、成分信息需以包装可见内容为准，以及明确但不虚构优惠的购买引导。"
        )
    if "儿童牙膏" in analysis_input or "toothpaste" in analysis_input.lower():
        oral_care_patterns = [
            r"terbaik",
            r"最好的",
            r"2\s*(?:hingga|-|到)\s*12",
            r"anti.?car",
            r"防蛀",
            r"闻香",
            r"香味",
            r"品尝",
            r"可吞咽",
            r"孩子.{0,12}(喜欢|反应|出镜)",
            r"anak[-\\s]*anak.{0,24}suka",
            r"confirm.{0,24}anak",
            r"wangi",
            r"bau buah",
        ]
        for index, item in enumerate(result.get("improvements", []), start=1):
            text = "\n".join(str(item.get(field) or "") for field in fields)
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in oral_care_patterns):
                raise SystemExit(f"提升点 {index} 的儿童牙膏建议含未核验的年龄、绝对化或防蛀表述，请仅保留按压演示、包装展示和商品信息引导。")


def validate_creator_script_language(result: dict[str, Any], analysis_input: str) -> None:
    """达人话术必须用达人口播语言（ms/id/th），不能写成中文。

    ⚠️ 触发条件命中时抛 SystemExit，调用方需感知。
    """
    match = re.search(r"## 达人视频.*?检测语言：([a-z]{2})", analysis_input, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return
    language = match.group(1).lower()
    if language not in {"ms", "id", "th"}:
        return
    violations: list[str] = []
    for index, item in enumerate(result.get("improvements", []), start=1):
        script = str(item.get("creator_script") or "").strip()
        if not script:
            continue
        if language in {"ms", "id"} and re.search(r"[一-鿿]", script):
            violations.append(f"提升点 {index} 的 creator_script 使用了中文而不是目标市场语言 {language}")
        if language == "th" and not re.search(r"[฀-๿]", script):
            violations.append(f"提升点 {index} 的 creator_script 未使用泰语")
    if violations:
        raise SystemExit("；".join(violations) + "。请将 creator_script 改为检测到的本地语言，并仅将中文写入 creator_script_zh。")


# ---------------------------------------------------------------------------
# sanitize（修改 data 不抛异常）
# ---------------------------------------------------------------------------

def sanitize_child_toothpaste_recommendations(result: dict[str, Any], analysis_input: str) -> None:
    """儿童牙膏品类的提升点：仅对真正含违规词的单条 improvement 覆盖为合规模板。

    违规判定与 validate_recommendation_safety 的 oral_care_patterns 一致；
    未命中违规的 improvement 保留 LLM 原文，避免覆盖 LLM 给出的精准建议（如 KKM 认证、限时优惠）。
    """
    if "儿童牙膏" not in analysis_input and "toothpaste" not in analysis_input.lower():
        return
    if "检测语言：th" in analysis_input:
        return

    sanitize_child_toothpaste_conclusions(result)

    # 与 validate_recommendation_safety 里 oral_care_patterns 同源：未核验的年龄、绝对化、防蛀表述。
    # 额外拦截会引入新孩子演员/品尝动作/“孩子一定喜欢”的建议；这些不适合在现有达人素材上直接生成。
    oral_care_patterns = [
        r"terbaik",
        r"最好的",
        r"2\s*(?:hingga|-|到)\s*12",
        r"anti.?car",
        r"防蛀",
        r"品尝",
        r"尝一",
        r"可吞咽",
        r"让孩子",
        r"孩子.{0,12}(喜欢|反应)",
        r"孩子.{0,8}出镜",
        r"小孩.{0,8}出镜",
        r"anak.{0,16}mesti.{0,8}suka",
        r"anak.{0,16}pun.{0,8}suka",
        r"anak[-\s]*anak.{0,24}suka",
        r"confirm.{0,24}anak",
        r"闻香",
        r"香味",
        r"wangi",
        r"bau buah",
    ]
    fields = ("title", "suggestion", "creator_script", "creator_script_zh", "aigc_prompt")

    for item in result.get("improvements", []):
        text = "\n".join(str(item.get(field) or "") for field in fields)
        if not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in oral_care_patterns):
            # 这条 improvement 没有违规词，保留 LLM 原文
            continue

        # 违规命中，按位置覆盖为合规模板
        title = str(item.get("title") or "").lower()
        if "hook" in title or "开头" in title or "吸引" in title:
            replace_health_action(
                item,
                "开头直接展示按压式牙膏和泵头，用一句简短马来语说明这是儿童刷牙场景的易用产品；不加入年龄、功效或绝对化承诺。",
                "Ubat gigi pump anak yang senang digunakan, tengok cara pakainya.",
                "容易使用的儿童按压牙膏，看看怎么用。",
                "基于达人真实手持牙膏画面，将产品和泵头置于视觉中心，保留家庭实拍质感；留出字幕区域，不新增年龄、功效、认证或绝对化文案。",
            )
        elif "S6" in str(item.get("target_stage") or "") or "cta" in title or "下单" in title or "购物车" in title:
            item["title"] = "补清结尾购物车提示，降低下单流失"
            replace_health_action(
                item,
                "结尾保留产品特写并加入购物车方向提示，引导用户查看商品信息后再选择，不虚构功效或优惠。",
                "Nak lihat ubat gigi pump ini? Klik troli untuk semak maklumat produk.",
                "想看看这款按压牙膏？点击购物车查看商品信息。",
                "基于达人结尾手持产品画面，突出真实牙膏包装并加入购物车方向提示；不新增功效、年龄、认证、价格或优惠文案。",
            )
        else:
            target_stage = str(item.get("target_stage") or "")
            if "S4" in target_stage or "效果" in title or "香" in title or "wangi" in text.lower():
                item["title"] = "强化按压结果展示，承接前面的产品演示"
            elif "S5" in target_stage or "信任" in title:
                item["title"] = "补可验证的包装信息，避免只靠口播建立信任"
            else:
                item["title"] = "突出按压演示，让使用方法一眼可懂"
            replace_health_action(
                item,
                "放大泵头操作，展示按压和挤出步骤；字幕只描述使用动作，包装信息以实拍可读内容为准。",
                "Tekan pam sekali dan lihat cara ubat gigi ini digunakan untuk rutin berus gigi anak.",
                "按压一次，看看这款牙膏如何用于孩子的日常刷牙。",
                "基于达人真实按压泵头画面，突出按压动作和挤出步骤；字幕仅写操作说明，不新增年龄、功效、认证或比较结论。",
            )


def sanitize_child_toothpaste_conclusions(result: dict[str, Any]) -> None:
    """儿童牙膏结论优先级：功能卖点和 CTA 优势优先，感官体验只能辅助。"""
    stages = {
        str(stage.get("stage") or "")[:2]: stage
        for stage in result.get("stage_analysis", [])
        if isinstance(stage, dict)
    }
    s6_small = str(stages.get("S6", {}).get("severity") or "") == "small"
    sanitized: list[dict[str, str]] = [
        {
            "conclusion": "达人把按压泵控制用量、减少浪费的核心价值讲得更清楚，这是比标杆更直接的购买理由。",
            "gmv_impact": "high",
        },
        {
            "conclusion": "达人已有结尾购买指令，CTA 不弱于标杆；后续只需要让购物车提示更清晰，而不是重做促单逻辑。",
            "gmv_impact": "small" if s6_small else "medium",
        },
        {
            "conclusion": "可优化点应围绕现有产品和泵头画面，把按压结果拍得更近、更清楚；香味和孩子喜欢只能作为辅助体验，不能压过功能卖点。",
            "gmv_impact": "medium",
        },
    ]
    result["key_conclusions"] = sanitized
    summary = str(result.get("one_line_summary") or result.get("executive_summary") or "")
    if re.search(r"香味|感官|孩子.{0,8}喜欢|购买指令.*缺失|CTA.*缺失", summary):
        result["one_line_summary"] = "达人清晰传达按压泵防浪费的核心卖点，并已有结尾购买指令；后续重点是把按压结果和购物车提示拍得更清楚。"
        result["executive_summary"] = result["one_line_summary"]


def sanitize_health_recommendations(result: dict[str, Any], analysis_input: str) -> None:
    """维生素/营养补充品类（马来语市场）的提升点：仅对真正含违规词的单条 improvement 覆盖。

    违规判定与 validate_recommendation_safety 的 prohibited_patterns 一致；
    未命中违规的 improvement 保留 LLM 原文。
    """
    health_product_markers = ("维生素", "营养补充", "supplement", "vitamin")
    if not any(marker.lower() in analysis_input.lower() for marker in health_product_markers):
        return
    target_language = "ms" if "检测语言：ms" in analysis_input else ""
    if target_language != "ms":
        return

    # 与 validate_recommendation_safety 同源的违规关键词
    prohibited_patterns = [
        r"激素", r"hormon", r"月经", r"menstru", r"period", r"血块", r"darah",
        r"治疗", r"治愈", r"cure", r"treat",
        r"改善.{0,6}(症状|皮肤|月经)", r"diskon", r"折扣",
    ]
    fields = ("title", "suggestion", "creator_script", "creator_script_zh", "aigc_prompt")

    for item in result.get("improvements", []):
        text = "\n".join(str(item.get(field) or "") for field in fields)
        if not any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in prohibited_patterns):
            # 这条 improvement 没有违规词，保留 LLM 原文
            continue

        title = str(item.get("title") or "").lower()
        if "hook" in title or "开头" in title or "痛点" in title:
            replace_health_action(
                item,
                "开头使用可用的产品露出画面，并叠加“Rutin nutrisi harian wanita”文字；若当前素材没有清晰产品画面则补拍；口播只说明女性日常营养补充场景。",
                "Untuk wanita yang mahu melengkapkan rutin nutrisi harian, produk ini mudah dimasukkan dalam rutin harian anda.",
                "对于希望完善日常营养补充习惯的女性，这款产品可以轻松加入你的每日习惯。",
                "基于达人实际可用的产品包装画面，将产品置于画面中心并加清晰文字“Rutin nutrisi harian wanita”；若无清晰包装画面需先补拍，不添加医疗症状或功效承诺。",
            )
        elif "cta" in title or "下单" in title or "购物车" in title:
            replace_health_action(
                item,
                "结尾保留产品特写和购物车指引，只提示用户查看商品详情与当前价格，不虚构优惠或效果理由。",
                "Klik troli kuning untuk lihat maklumat produk dan harga semasa sebelum membuat pilihan.",
                "点击黄色购物车，在选择前查看产品信息和当前价格。",
                "基于达人实际存在的结尾产品画面，保留真实包装，加入黄色购物车方向提示与文字“Semak maklumat produk”；若无产品结尾画面需补拍，不添加优惠或功效承诺。",
            )
        elif "信任" in title or "认证" in title:
            replace_health_action(
                item,
                "信任环节展示包装标签和可读信息，只有画面中明确可见的认证或成分才能写入字幕。",
                "Semak label dan maklumat pada bungkusan sebelum membuat pilihan anda.",
                "作出选择前，请查看包装上的标签和产品信息。",
                "基于达人手持包装的真实画面，放大包装标签区域并保持文字可读；仅突出画面中真实可见的信息，不新增认证或功效文案。",
            )
        elif "使用" in title or "效果" in title or "产品" in title or "引出" in title:
            replace_health_action(
                item,
                "展示包装、开盖和日常携带或服用动作；卖点仅使用包装可见的成分/食用信息，不展示健康结果对比。",
                "Ini suplemen nutrisi wanita. Lihat label bungkusan untuk maklumat nutrien dan cara pengambilan.",
                "这是一款女性营养补充品。请查看包装标签了解营养信息和食用方法。",
                "基于达人真实产品画面，增加开盖与查看标签的动作特写；字幕写“Semak label nutrisi”，不添加症状改善或效果对比。",
            )
        else:
            replace_health_action(
                item,
                "围绕真实包装或日常营养补充场景重拍该段；所有信息以包装可见内容为准，不添加功效、症状或优惠承诺。",
                "Semak label produk ini dan pilih mengikut keperluan rutin nutrisi harian anda.",
                "请查看这款产品的标签，并根据你的日常营养补充需求作出选择。",
                "基于达人实际存在的产品包装画面，强化包装可读性和产品主体；若无清晰素材则补拍，不添加功效或优惠文字。",
            )


def replace_health_action(
    item: dict[str, Any],
    suggestion: str,
    local_script: str,
    translated_script: str,
    image_prompt: str,
) -> None:
    item["problem"] = "当前画面对产品使用结果展示不够集中，用户需要更快看清按压、用量和包装信息。"
    item["suggestion"] = suggestion
    item["actions"] = [suggestion]
    item["gmv_impact"] = "中"
    item["gmv_reason"] = "通过更清晰、可验证的画面信息降低理解成本，提升继续观看和点击查看商品的意愿。"
    item["base_frame_reason"] = "仅使用达人已有真实画面作为基底；缺少的评价、背书或包装细节需补拍或人工核验。"
    item["creator_script"] = local_script
    item["creator_script_zh"] = translated_script
    item["aigc_prompt"] = image_prompt
    item["expected_effect"] = "让用户更快看懂产品用法和商品信息，提升继续观看和点击查看的意愿。"
