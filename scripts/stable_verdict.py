"""stable_verdict.py：稳定口径计算器（BASELINE-PROTOCOL.md 铁律1）。

读一个目录下的 run_*_result.json（N 次重跑），对每阶段算 severity / B / C 的众数 + 一致度。
- severity 一致度 < 0.8（即 <4/5 或 <3/3）→ 🔴「不可信」（头条判定）。
- severity 碰巧稳但 B 或 C 在抖 → ⚠「侥幸稳」（底层赋分不稳，子② 修复时别漏）。
禁止手抄单跑值——任何 baseline/实验结论必须经本计算器产出。

用法：python3 scripts/stable_verdict.py <dir>     # dir 含 run_1_result.json ... run_N_result.json
"""
import sys
import json
import math
from collections import Counter
from pathlib import Path

STABLE_RATIO = 0.8  # mode_count/N >= 0.8 才算稳定（N=5→≥4，N=3→3/3）
STAGES = ["S1", "S2", "S3", "S4", "S5", "S6"]


def mode_agree(vals):
    """众数 + 该众数出现次数 k。None 也参与计数（缺字段本身是信号）。"""
    counter = Counter(vals)
    value, k = counter.most_common(1)[0]
    return value, k


def main(target):
    d = Path(target)
    files = sorted(d.glob("run_*_result.json"))
    if not files:
        sys.exit(f"无 run_*_result.json 于 {d}")
    runs = [json.loads(f.read_text(encoding="utf-8")).get("stage_analysis") or [] for f in files]
    n = len(runs)
    kmin = math.ceil(STABLE_RATIO * n)
    print(f"样本目录: {d}  | N={n}  | 稳定门槛 mode≥{kmin}/{n}")
    print(f"{'阶段':<6}{'severity(众/N)':<22}{'B(众/N)':<16}{'C(众/N)':<16}判定")
    n_red = n_warn = 0
    for i, stg in enumerate(STAGES):
        sevs = [r[i].get("severity") if i < len(r) else None for r in runs]
        b = [r[i].get("benchmark_execution") if i < len(r) else None for r in runs]
        c = [r[i].get("creator_execution") if i < len(r) else None for r in runs]
        sm, sk = mode_agree(sevs)
        bm, bk = mode_agree(b)
        cm, ck = mode_agree(c)
        sev_red = sk < kmin
        bc_shaky = (bk < kmin) or (ck < kmin)
        if sev_red:
            flag = "🔴不可信"
            n_red += 1
        elif bc_shaky:
            flag = "⚠侥幸稳(B/C抖)"
            n_warn += 1
        else:
            flag = "✓稳"
        print(f"{stg:<6}{f'{sm}({sk}/{n})':<22}{f'{bm}({bk}/{n})':<16}{f'{cm}({ck}/{n})':<16}{flag}")
    print()
    if n_red:
        print(f"结论：{n_red} 个🔴不可信阶段——该样本中段需子② 修复后复测（mode 口径盖不住，见协议）。")
    elif n_warn:
        print(f"结论：severity 全稳，但 {n_warn} 个阶段 B/C 在抖（侥幸稳）——子② 修复时一并盯。")
    else:
        print("结论：全阶段稳定。")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
