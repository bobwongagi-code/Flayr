#!/usr/bin/env python3
"""重抽单视频事实（Phase A），验证观察维度增量（4g③ 镜头语言/遮挡/UI 危险区）。

背景：门禁 runner 只重跑 stage2，video_facts_{role}.json 是缓存——Phase A 的 prompt
改动（如 visual_fact 新观察维度）不重抽就永远测不到（round4 实证：kakwan S3 修复未被测到）。

行为：旧 facts 归档到 run_dir/facts_backup_<时间戳>/（不删除），新 facts 落原位
供 dev_run_gate.sh / dev_test_stage2 直接消费。每样本花费 2 次 LLM 调用。

用法：python3 scripts/dev_refresh_facts.py runs/sample-kakwanreview
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from flayr_core.llm.api import read_llm_api_key
from flayr_core.llm.pipeline import run_video_fact_extraction

# 与 dev_test_stage2.py 同源的连接常量
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.5-omni-plus"
KEYCHAIN_SERVICE = "VidLingo.Qwen"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", help="已有 run 目录，含 analysis.json 与预处理产物")
    parser.add_argument("--image-limit", type=int, default=12)
    args_cli = parser.parse_args()
    run = Path(args_cli.run_dir)
    analysis = json.loads((run / "analysis.json").read_text(encoding="utf-8"))

    args = SimpleNamespace(
        llm_model=MODEL,
        llm_api_url=API_URL,
        llm_image_limit=args_cli.image_limit,
        llm_include_images=True,
        llm_dry_run=False,
        llm_api_key_env="OPENAI_API_KEY",
        llm_api_key_keychain_service=KEYCHAIN_SERVICE,
        llm_api_key_keychain_account="API_KEY",
    )
    api_key = read_llm_api_key(args).strip()
    if not api_key:
        print("缺 API key（OPENAI_API_KEY 或 Keychain VidLingo.Qwen）", flush=True)
        return 1

    # 归档旧 facts（不删除）
    backup = run / f"facts_backup_{time.strftime('%H%M%S')}"
    backup.mkdir(exist_ok=True)
    for role in ("benchmark", "creator"):
        src = run / f"video_facts_{role}.json"
        if src.is_file():
            shutil.copy2(src, backup / src.name)
    print(f"旧 facts 已归档: {backup}", flush=True)

    facts = run_video_fact_extraction(args, analysis, run, api_key)
    for role, fact in facts.items():
        units = fact.get("evidence_units", [])
        print(f"{role}: {len(units)} 条 evidence_units 重抽完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
