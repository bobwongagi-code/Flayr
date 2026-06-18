#!/usr/bin/env python3
"""盲化成对比较 minimal harness —— 比较 pivot 的第一块砖（2026-06-15）。

对一个 sample 的某阶段：取达人+标杆该阶段关键帧，盲化呈现为"视频A/视频B"（随机谁是A），
只在"本阶段产品到位标准"上问"A vs B 谁更好、差多少"，双序各跑一遍控位置偏置。
验证：独立打分会判反（C≥B 偏袒达人）的 case，盲化比较能不能一致地选标杆（=用户答案）。

用法：python3 scripts/dev_blind_compare.py sample-carslan-b0 S3 --src gate_group2_validation
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from flayr_core.llm.api import (  # noqa: E402
    call_llm_api,
    extract_chat_completion_text,
    image_to_data_url,
    read_llm_api_key,
)

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.5-omni-plus"
KEYCHAIN_SERVICE = "VidLingo.Qwen"

# 各阶段到位标准（对照表的盲化比较版，评判维度）
STAGE_STD = {
    "S1": "Hook：开头是否扣住本品最有拦截力的点（最尖锐痛点 或 最强承诺/反差）抓住目标人群，而非泛泛开场。",
    "S2": "产品引出：引出是否自然、且承接开头钩子（钩子抛痛点→引出冲着解痛点去）；不判卖点本身。",
    "S3": "使用过程·卖点的有效传递：核心卖点有没有被有效传递——① 画面里在使用动作中『演示出来』被看见（演示即证据）；② 口播把卖点讲到位、信息密度够。口播啰唆/密度低/绕半天没把卖点讲清 = 口播传递差（话多≠卖点传得好）。两者合看，判'卖点到底传到位没'，不评教学清晰度。",
    "S4": "效果呈现：有没有拍出本品的决定性视觉瞬间（如油光→哑光对比），且拍摄让效果肉眼可见。",
    "S5": "信任放大：有没有有效呈现该品类的信任工具（硬：认证/检测/仪器实测可高分；软：好评/社会认同封顶中等）；开头的背书算钩子、不算这里。",
    "S6": "促单：CTA 力度与时机是否匹配——清晰直接的购买指令+紧迫感（冲动品），或先消顾虑再 CTA（高决策品）。",
}


def stage_time_ranges(run: Path, src: str, stage: str) -> tuple[str, str]:
    """从现成 stage2 result 取该阶段的达人/标杆时间范围。"""
    base = run / src if src else run
    raw = json.loads((base / "dev_stage2_result_raw_01.json").read_text(encoding="utf-8"))
    s = next((x for x in raw.get("stage_analysis", []) if str(x.get("stage", "")).startswith(stage)), {})
    return s.get("creator_time_range") or "", s.get("benchmark_time_range") or ""


def stage_speech(run: Path, role: str, time_range: str, limit_chars: int = 700) -> str:
    """取该 role 在 stage 时间范围内的口播（从 transcript.srt 按时间戳截取）。"""
    srt = run / role / "transcript.srt"
    if not srt.is_file():
        return "（无口播文件）"
    nums = re.findall(r"([\d.]+)", time_range or "")
    lo, hi = (float(nums[0]), float(nums[1])) if len(nums) >= 2 else (0.0, 1e9)
    out = []
    for block in re.split(r"\n\n+", srt.read_text(encoding="utf-8", errors="ignore").strip()):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        tm = re.search(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->", lines[1])
        if not tm:
            continue
        start = int(tm[1]) * 3600 + int(tm[2]) * 60 + int(tm[3]) + int(tm[4]) / 1000
        if lo <= start <= hi:
            out.append(" ".join(lines[2:]).strip())
    s = " ".join(out).strip()
    return s[:limit_chars] if s else "（该时段无有效口播）"


def stage_frames(run: Path, role: str, time_range: str, limit: int = 6) -> list[str]:
    """取该 role 在 stage 时间范围内的帧 data_url（均匀采样，最多 limit 张）。"""
    pre = json.loads((run / role / "_preprocess.json").read_text(encoding="utf-8"))
    frames = pre.get("frames", [])
    nums = re.findall(r"([\d.]+)", time_range or "")
    lo, hi = (float(nums[0]), float(nums[1])) if len(nums) >= 2 else (0.0, 1e9)
    inrange = [f for f in frames if lo <= float(f.get("timestamp_seconds") or 0) <= hi] or frames
    if len(inrange) > limit:
        step = len(inrange) / limit
        inrange = [inrange[int(i * step)] for i in range(limit)]
    urls = []
    for f in inrange:
        p = Path(f.get("path", ""))
        if p.is_file():
            urls.append(image_to_data_url(p))
    return urls


def build_payload(std: str, frames_a: list[str], frames_b: list[str],
                  speech_a: str, speech_b: str) -> dict:
    content = [{"type": "text", "text": (
        "下面是两条带货短视频在同一阶段的内容：视频A 和 视频B，各给关键帧 + 该阶段口播。\n"
        f"评判标准（本阶段到位标准）：{std}\n\n"
        "只在这个标准上比较；不要因为谁更长、画质更好、制作更精致、或口播字数更多就判它更好——"
        "口播看的是把卖点讲到位、信息密度，不是话多。判断：在这个标准上，哪条做得更好、差多少？\n"
        "输出严格 JSON：{\"better\":\"A|B|tie\",\"gap\":\"none|small|medium|large\",\"reason\":\"一句话\"}。\n"
        "gap 语义：两条都达标只是风格/程度细微差异=none/small；一条达标另一条只部分达标=medium；"
        "一条达标另一条这一阶段直接没做到=large。"
    )}]
    content.append({"type": "text", "text": f"=== 视频A ===\n本阶段口播：{speech_a}"})
    content += [{"type": "image_url", "image_url": {"url": u}} for u in frames_a]
    content.append({"type": "text", "text": f"=== 视频B ===\n本阶段口播：{speech_b}"})
    content += [{"type": "image_url", "image_url": {"url": u}} for u in frames_b]
    return {"model": MODEL, "messages": [
        {"role": "system", "content": "你是带货短视频评审，只按给定标准做盲化成对比较，不被制作质感带偏。"},
        {"role": "user", "content": content},
    ]}


def call(payload: dict, run: Path, tag: str, key: str) -> dict:
    req = run / f"_blindcmp_{tag}_req.json"
    rawp = run / f"_blindcmp_{tag}_raw.json"
    req.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    raw = call_llm_api(API_URL, key, req, rawp)
    try:
        txt = extract_chat_completion_text(json.loads(raw))
    except Exception:
        txt = raw
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    return json.loads(m.group(0)) if m else {"better": "?", "gap": "?", "reason": txt[:100]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("stage")
    ap.add_argument("--src", default="", help="时间范围来源子目录，如 gate_group2_validation")
    args = ap.parse_args()
    name = args.sample if args.sample.startswith("sample-") else f"sample-{args.sample}"
    run = ROOT / "runs" / name
    stage = args.stage.upper()
    std = STAGE_STD[stage]

    c_tr, b_tr = stage_time_ranges(run, args.src, stage)
    creator_frames = stage_frames(run, "creator", c_tr)
    benchmark_frames = stage_frames(run, "benchmark", b_tr)
    creator_speech = stage_speech(run, "creator", c_tr)
    benchmark_speech = stage_speech(run, "benchmark", b_tr)
    print(f"== {name} {stage} 盲化比较（帧+口播）==")
    print(f"  达人帧 {len(creator_frames)}（{c_tr}）口播：{creator_speech[:55]}")
    print(f"  标杆帧 {len(benchmark_frames)}（{b_tr}）口播：{benchmark_speech[:55]}")
    print(f"  标准：{std[:50]}…\n")

    class _Args:
        llm_api_key_env = "OPENAI_API_KEY"
        llm_api_key_keychain_service = KEYCHAIN_SERVICE
        llm_api_key_keychain_account = "API_KEY"
    key = read_llm_api_key(_Args()).strip()
    if not key:
        print("❌ 无 API key"); return 1

    # 双序：序1 达人=A，序2 达人=B
    r1 = call(build_payload(std, creator_frames, benchmark_frames, creator_speech, benchmark_speech), run, f"{stage}_o1", key)
    who1 = {"A": "达人", "B": "标杆", "tie": "持平"}.get(r1.get("better"), "?")
    print(f"  序1(达人=A,标杆=B): better={r1.get('better')}→{who1} gap={r1.get('gap')} | {r1.get('reason','')[:70]}")
    r2 = call(build_payload(std, benchmark_frames, creator_frames, benchmark_speech, creator_speech), run, f"{stage}_o2", key)
    who2 = {"A": "标杆", "B": "达人", "tie": "持平"}.get(r2.get("better"), "?")
    print(f"  序2(标杆=A,达人=B): better={r2.get('better')}→{who2} gap={r2.get('gap')} | {r2.get('reason','')[:70]}")

    flip = who1 != who2 and "持平" not in (who1, who2)
    print(f"\n  → 盲化结论：{'⚠ 双序翻转(位置偏置,低置信)' if flip else f'一致选【{who1}】更好'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
