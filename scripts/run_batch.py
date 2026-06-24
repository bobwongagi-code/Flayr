"""并发 + 循环重试 批跑 runner。

设计（见讨论）：
- 样本级并发：不同样本并行（各写各自 runs/sample-X/ 目录，不撞）；同一样本 N 次串行（撞目录，禁并发）。
- 循环内重试：每个 run_i 失败当场重试（QA 校验是随机失败，重试常过）；首次重试无延迟、
  第二次起 5~10s 退避（防 rate-limit 边缘无间隔连打出 429）；连续 retries 次失败才放弃。
- resume-skip + JSON 完整性：已有有效结果跳过；半截损坏删了重跑。
- SystemExit 一并捕获：单条 QA 校验失败不拖垮整批。

**接口强制**：--scripts 与 --out 必填、无默认——默认值会在最不注意时走错路径静默污染
baseline（协议铁律2）。baseline 必须传 _pinned/scripts；functions 等实验传工作树 scripts。

用法：
  # functions 取证（工作树代码，独立实验目录）
  python3 scripts/run_batch.py --scripts scripts --out runs_exp/functions-gate \\
      --samples carslan-b0 wukoubo-c0 youkoubo-c2 --workers 3
  # baseline 补跑（pinned 旧代码）
  python3 scripts/run_batch.py --scripts runs_baseline_v3/_pinned/scripts --out runs_baseline_v3 \\
      --samples youkoubo-c2 --workers 3
"""
import argparse
import sys
import json
import time
import shutil
import threading
import random
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    # 强制传参、无默认：防止静默走错 scripts 路径污染 baseline
    ap.add_argument("--scripts", required=True,
                    help="flayr_core 所在 scripts 路径（必填无默认）：baseline 用 runs_baseline_v3/_pinned/scripts，实验用工作树 scripts")
    ap.add_argument("--out", required=True,
                    help="输出目录（必填无默认）：如 runs_baseline_v3 或 runs_exp/<实验名>")
    ap.add_argument("--samples", nargs="+", required=True, help="要跑的样本名（不含 sample- 前缀）")
    ap.add_argument("--n", type=int, default=5, help="每样本重跑次数（默认5）")
    ap.add_argument("--workers", type=int, default=3, help="并发样本数（默认3；持续429就调小）")
    ap.add_argument("--retries", type=int, default=3, help="单 run 连续失败几次才放弃（默认3）")
    a = ap.parse_args()

    if not Path(a.scripts, "flayr_core").is_dir():
        ap.error(f"--scripts={a.scripts} 下找不到 flayr_core，路径不对（防走错路径污染 baseline）")

    sys.path.insert(0, a.scripts)  # 必须在 import flayr_core 之前
    from types import SimpleNamespace
    from concurrent.futures import ThreadPoolExecutor
    from flayr_core.llm.pipeline import run_large_model_analysis
    from flayr_core.prompt import write_analysis_input

    run_args = SimpleNamespace(
        llm_model="qwen3.5-omni-plus",
        llm_api_url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        llm_dry_run=False, llm_image_limit=12, llm_include_images=True,
        llm_api_key_env="OPENAI_API_KEY", llm_api_key_keychain_service="VidLingo.Qwen",
        llm_api_key_keychain_account="API_KEY")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    st: dict[str, str] = {}
    lock = threading.Lock()

    def set_st(key, val):  # 状态写盘整体加锁，线程安全
        with lock:
            st[key] = val
            (out / "_status.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_one(s, i):
        rd = Path("runs") / f"sample-{s}"
        dst = out / s / f"run_{i}_result.json"
        if dst.is_file():  # resume：有效结果跳过；半截损坏删了重跑
            try:
                if json.loads(dst.read_text(encoding="utf-8")).get("stage_analysis"):
                    set_st(f"{s}/r{i}", "skip-done")
                    return
                dst.unlink()
            except Exception:
                dst.unlink()
        last = ""
        for attempt in range(a.retries):
            if attempt >= 2:  # 首次重试(attempt1)无延迟；第二次起(attempt>=2)退避5~10s
                time.sleep(random.uniform(5, 10))
            set_st(f"{s}/r{i}", f"running(try{attempt + 1})")
            t0 = time.time()
            try:
                an = json.loads((rd / "analysis.json").read_text(encoding="utf-8"))
                aip = write_analysis_input(rd, an)
                run_large_model_analysis(run_args, an, aip, rd)
                shutil.copy2(rd / "analysis_result.json", dst)
                # 埋点①：每跑存一份原始 Stage1 facts（自由文本输出），供跨跑覆盖漂移诊断。
                # rd 是同样本各跑共享目录、facts 每跑被覆盖；同样本串行，故此处即时拷出无竞态。
                for who in ("creator", "benchmark"):
                    fsrc = rd / f"video_facts_{who}.json"
                    if fsrc.is_file():
                        shutil.copy2(fsrc, out / s / f"video_facts_raw_{who}_{i}.json")
                set_st(f"{s}/r{i}", f"done {round((time.time() - t0) / 60, 1)}min(try{attempt + 1})")
                return
            except (Exception, SystemExit) as e:
                last = str(e)[:120]
        set_st(f"{s}/r{i}", f"failed×{a.retries} {last}")

    def do_sample(s):  # 一个样本=一个并发任务，内部 N 次串行（同样本不并发，防撞目录）
        (out / s).mkdir(parents=True, exist_ok=True)
        for i in range(1, a.n + 1):
            run_one(s, i)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(do_sample, a.samples))
    set_st("_DONE", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("DONE")


if __name__ == "__main__":
    main()
