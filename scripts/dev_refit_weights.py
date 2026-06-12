#!/usr/bin/env python3
"""权重表离线重拟合（任务5 首跑）：对存量 facts 零 LLM 成本搜索 ARCHETYPE_W / 阈值。

数据：dev_score_gate.LABELS 全部样本（60 标签）× 各自最新 raw（原 3 样本为 round4/重抽
多次重复取众数，round5 为单跑）。坐标上升：逐原型逐阶段在候选值里取最优，固定其余；
末轮扫 TH_MEDIUM。目标 = 一致数最大，平手取 severe 更少。

⚠️ 拟合即在全部标签上做（用户原则：权重表靠数据积累渐进优化）；泛化要靠下一批新样本验证。
用法：python3 scripts/dev_refit_weights.py [--apply 不改文件，只打印建议值]
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

from dev_score_gate import LABELS, STAGES, load_raw, stage_map
from flayr_core.llm.parse import normalize_analysis_result
from flayr_core.postprocess import derive

ROOT = Path(__file__).resolve().parents[1]
W_CANDIDATES = [0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0]
TH_CANDIDATES = [2.2, 2.35, 2.5]


def load_corpus() -> dict[str, list[dict]]:
    """每样本读全部可用 raw 并归一（不推导），缓存为拟合语料。"""
    corpus: dict[str, list[dict]] = {}
    for name in LABELS:
        run = ROOT / "runs" / name
        normalized = []
        for i in range(1, 6):
            raw = load_raw(run, i)
            if raw:
                try:
                    normalized.append(normalize_analysis_result(raw))
                except SystemExit:
                    continue
        if normalized:
            corpus[name] = normalized
    return corpus


def evaluate(corpus: dict[str, list[dict]]) -> tuple[int, int]:
    """当前 derive 全局参数下：(一致数, severe 数)。每次评估用深拷贝避免污染缓存。"""
    agree = severe = 0
    for name, repeats in corpus.items():
        labels = LABELS[name]
        per_stage: dict[str, list[str]] = {s: [] for s in STAGES}
        for n in repeats:
            n2 = copy.deepcopy(n)
            derive.derive_severity_from_facts(n2)
            smap = stage_map(n2)
            for sid in STAGES:
                if sid in smap:
                    per_stage[sid].append(smap[sid]["severity"])
        for sid in STAGES:
            values = per_stage[sid]
            if not values:
                continue
            mode = Counter(values).most_common(1)[0][0]
            lab = labels[sid]
            agree += mode == lab
            severe += {mode, lab} == {"large", "small"}
    return agree, severe


def main() -> int:
    corpus = load_corpus()
    total = sum(len(LABELS[n]) for n in corpus)
    base = evaluate(corpus)
    print(f"语料: {len(corpus)} 样本 / {total} 标签 | 现行参数: 一致 {base[0]}/{total} severe {base[1]}")

    best = base
    # 坐标上升 ×2 轮：逐原型逐阶段
    for sweep in range(2):
        for arch in derive.ARCHETYPE_W:
            for sid in STAGES:
                if sid == "S1":
                    continue  # 框架红线：Hook 恒为高权重，任何品类不降权——S1 不进搜索空间
                current = derive.ARCHETYPE_W[arch][sid]
                best_val, best_score = current, best
                for cand in W_CANDIDATES:
                    if cand == current:
                        continue
                    derive.ARCHETYPE_W[arch][sid] = cand
                    score = evaluate(corpus)
                    if (score[0], -score[1]) > (best_score[0], -best_score[1]):
                        best_val, best_score = cand, score
                derive.ARCHETYPE_W[arch][sid] = best_val
                best = best_score
        print(f"sweep{sweep+1} 后: 一致 {best[0]}/{total} severe {best[1]}")

    # 阈值扫描
    for th in TH_CANDIDATES:
        old = derive.TH_MEDIUM
        derive.TH_MEDIUM = th
        score = evaluate(corpus)
        if (score[0], -score[1]) > (best[0], -best[1]):
            best = score
            print(f"TH_MEDIUM={th}: 一致 {score[0]}/{total} severe {score[1]} ← 采纳")
        else:
            derive.TH_MEDIUM = old

    print(f"\n最终: 一致 {best[0]}/{total} severe {best[1]}（基线 {base[0]}/{total}）")
    print(f"TH_MEDIUM = {derive.TH_MEDIUM}")
    for arch, table in derive.ARCHETYPE_W.items():
        print(f"  {arch}: {json.dumps(table)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
