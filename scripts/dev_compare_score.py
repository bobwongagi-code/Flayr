#!/usr/bin/env python3
"""比较式打分器 —— 把盲化双序成对比较跑满 S1-S6，映射成 severity，对账可信标签。

比较 pivot 的 reliability 验证（n=1 POC → n=多）：用"盲化+双序+锚到位标准+full content"
逐阶段比出差距，看它对人工可信标签的命中率，能不能赢过独立打分（48/60 那套）。
复用 dev_blind_compare 的取帧/取口播/盲化 payload/调用。

用法：python3 scripts/dev_compare_score.py sample-carslan-b0 --src gate_group2_validation
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from dev_blind_compare import (  # noqa: E402
    KEYCHAIN_SERVICE,
    STAGE_STD,
    build_payload,
    call,
    stage_frames,
    stage_speech,
    stage_time_ranges,
)
from flayr_core.llm.api import read_llm_api_key  # noqa: E402

# 今日 + round 可信标签（达人 vs 标杆差距）。? = 暂无干净标签
TRUSTED = {
    "sample-carslan-b0": {"S1": "large", "S2": "small", "S3": "large", "S4": "medium", "S5": "small", "S6": "small"},
    "sample-bluetoothwanju": {"S1": "small", "S2": "small", "S3": "medium", "S4": "?", "S5": "small", "S6": "medium"},
    "sample-mmx": {"S1": "small", "S2": "small", "S3": "medium", "S4": "?", "S5": "small", "S6": "small"},
}
GAP2SEV = {"none": "small", "small": "small", "medium": "medium", "large": "large"}


def compare_stage(run: Path, src: str, stage: str, key: str) -> dict:
    c_tr, b_tr = stage_time_ranges(run, src, stage)
    cf, bf = stage_frames(run, "creator", c_tr), stage_frames(run, "benchmark", b_tr)
    cs, bs = stage_speech(run, "creator", c_tr), stage_speech(run, "benchmark", b_tr)
    std = STAGE_STD[stage]
    r1 = call(build_payload(std, cf, bf, cs, bs), run, f"{stage}_o1", key)
    r2 = call(build_payload(std, bf, cf, bs, cs), run, f"{stage}_o2", key)
    who1 = {"A": "达人", "B": "标杆", "tie": "持平"}.get(r1.get("better"), "?")
    who2 = {"A": "标杆", "B": "达人", "tie": "持平"}.get(r2.get("better"), "?")
    flip = who1 != who2 and "持平" not in (who1, who2)
    # 映射 severity：标杆更好→差距=gap；达人更好/持平→small；翻转→取较轻+低置信
    def sev(who, gap):
        return GAP2SEV.get(gap, "medium") if who == "标杆" else "small"
    if flip:
        cands = [sev("标杆", r1.get("gap")) if who1 == "标杆" else "small",
                 sev("标杆", r2.get("gap")) if who2 == "标杆" else "small"]
        order = {"small": 0, "medium": 1, "large": 2}
        severity = min(cands, key=lambda x: order.get(x, 1))
        conf = "low(翻转)"
    else:
        severity = sev(who1, r1.get("gap"))
        conf = "high"
    return {"severity": severity, "conf": conf, "who1": who1, "who2": who2,
            "gap1": r1.get("gap"), "gap2": r2.get("gap")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("--src", default="")
    ap.add_argument("--stages", default="S1,S2,S3,S4,S5,S6")
    args = ap.parse_args()
    name = args.sample if args.sample.startswith("sample-") else f"sample-{args.sample}"
    run = ROOT / "runs" / name
    labels = TRUSTED.get(name, {})

    class _Args:
        llm_api_key_env = "OPENAI_API_KEY"
        llm_api_key_keychain_service = KEYCHAIN_SERVICE
        llm_api_key_keychain_account = "API_KEY"
    key = read_llm_api_key(_Args()).strip()
    if not key:
        print("❌ 无 API key"); return 1

    print(f"\n══ {name} 比较式打分 vs 可信标签 ══")
    print(f"{'阶段':<5}{'比较severity':<14}{'置信':<12}{'可信':<7}{'判':<4} 双序")
    hit = tot = 0
    for stage in args.stages.split(","):
        stage = stage.strip().upper()
        try:
            r = compare_stage(run, args.src, stage, key)
        except Exception as exc:  # noqa: BLE001
            print(f"{stage:<5}ERROR: {exc}")
            continue
        lab = labels.get(stage, "?")
        ok = lab != "?" and r["severity"] == lab
        if lab != "?":
            hit += ok; tot += 1
        mark = "✓" if ok else ("—" if lab == "?" else "✗")
        print(f"{stage:<5}{r['severity']:<14}{r['conf']:<12}{lab:<7}{mark:<4} {r['who1']}/{r['who2']} ({r['gap1']}/{r['gap2']})")
    print(f"\n  对可信标签：{hit}/{tot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
