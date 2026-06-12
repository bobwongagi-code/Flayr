#!/bin/bash
# 生死测量 runner：nohup 起一次（脱离 harness 不被 cull），断点续跑，进度看 runs/_gate/status.log。
# 用法：nohup scripts/dev_run_gate.sh >/dev/null 2>&1 &              # 全部 3 个样本 + 末尾打分
#       nohup scripts/dev_run_gate.sh are_xie >/dev/null 2>&1 &      # 指定样本（不打分，逐样本外部分析）
#       tail -f runs/_gate/status.log
set -u
# 自我保护：切断 stdin。后台作业的子进程（如 ffmpeg）读终端 stdin 会触发 SIGTTIN
# 把整个进程组挂起（2026-06-10 实证：kakwan payload 构建卡死 30 分钟）。
exec </dev/null
cd "$(dirname "$0")/.."
mkdir -p runs/_gate
LOG=runs/_gate/status.log
echo "===== gate run start $(date +%H:%M:%S) =====" >> "$LOG"

SAMPLES=("$@")
[ ${#SAMPLES[@]} -eq 0 ] && SAMPLES=(are_xie kakwanreview tashadiyana)
for name in "${SAMPLES[@]}"; do
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
  echo "[$(date +%H:%M:%S)] repeats x${REPEAT:-5} for $name (skip-existing 断点续跑)" >> "$LOG"
  # dev_test_stage2 自带的 PASS 是牙膏校准的，exit 2 不算失败；门禁判定在 scorer。
  python3 scripts/dev_test_stage2.py "$run" --repeat "${REPEAT:-5}" --skip-existing >> "$LOG" 2>&1 || true
done

# 指定样本模式不跑全量打分（其余样本无新结果，scorer 口径会失真），分析由外部逐样本做
if [ $# -eq 0 ]; then
  echo "[$(date +%H:%M:%S)] scoring gate (预注册阈值 T1-T7)" >> "$LOG"
  python3 scripts/dev_score_gate.py --repeat 5 >> "$LOG" 2>&1
fi
echo "===== gate run done $(date +%H:%M:%S) =====" >> "$LOG"
