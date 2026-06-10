#!/bin/bash
# 生死测量 runner：nohup 起一次（脱离 harness 不被 cull），断点续跑，进度看 runs/_gate/status.log。
# 用法：nohup scripts/dev_run_gate.sh >/dev/null 2>&1 &
#       tail -f runs/_gate/status.log
set -u
cd "$(dirname "$0")/.."
mkdir -p runs/_gate
LOG=runs/_gate/status.log
echo "===== gate run start $(date +%H:%M:%S) =====" >> "$LOG"

for name in are_xie kakwanreview tashadiyana; do
  run="runs/sample-$name"
  # 关键：用当前代码重生成 analysis_input.md（含最新 prompt/framework/字幕轨/镜头轨），
  # 否则测的是旧 prompt 下的模型行为，门禁结论无效。
  echo "[$(date +%H:%M:%S)] regenerate analysis_input: $name" >> "$LOG"
  python3 - "$run" >> "$LOG" 2>&1 <<'PY'
import json, sys
sys.path.insert(0, "scripts")
from pathlib import Path
from flayr_core.prompt import write_analysis_input
run = Path(sys.argv[1])
analysis = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
write_analysis_input(run, analysis)
print(f"analysis_input regenerated: {run}")
PY
  echo "[$(date +%H:%M:%S)] repeats x5 for $name (skip-existing 断点续跑)" >> "$LOG"
  # dev_test_stage2 自带的 PASS 是牙膏校准的，exit 2 不算失败；门禁判定在 scorer。
  python3 scripts/dev_test_stage2.py "$run" --repeat 5 --skip-existing >> "$LOG" 2>&1 || true
done

echo "[$(date +%H:%M:%S)] scoring gate (预注册阈值 T1-T7)" >> "$LOG"
python3 scripts/dev_score_gate.py --repeat 5 >> "$LOG" 2>&1
echo "===== gate run done $(date +%H:%M:%S) =====" >> "$LOG"
