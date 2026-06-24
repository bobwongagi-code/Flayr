"""D 项（性能压力测试）覆盖漂移探针：三臂问法 A/B/C 对比。

隔离条件（补充一）：temperature=0 三臂完全一致，唯一变量是问法；否则一致率差异会混入采样噪声。
存证（补充二）：每跑存 arm/video_id/correct/answer/raw_output——失败分析靠 raw_output，统计靠 answer。
对照（洞一）：carslan 达人 correct=yes（泼水实证在 ~34s）；skincare 达人 correct=no（四锚定全缺席，已验帧）。
读法：主指标 = answer 跨跑一致率，按 arm×video 分组；正例稳命中 + 负例不 false-yes 才算赢。
       Arm C 副指标 = 时间戳收敛性（注意：模型时序定位偏弱、实证偏 ~10s，分散≠运气一致）。

跑次：3 臂 × 2 视频 × N 跑（默认 5）= 30 次。resume-skip 续跑。网络回来直接执行。
用法：python3 scripts/probe_arms.py [--n 5]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "scripts")
from flayr_core.llm.api import call_llm_api, extract_chat_completion_text, video_to_data_url

MODEL = "qwen3.5-omni-plus"
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 系统提示三臂完全一致（隔离条件）：中性，只说看一条美妆视频、按指示作答、不臆造。
SYSTEM = ("你正在分析一段美妆达人视频。你能看到连续画面、听到声音。"
          "严格按用户消息中的指示作答，不臆造画面里没有出现的事件。")

# 三臂问法（用户定稿，摩擦二/三已在 prompt 层处理：scope 改物理可观察、B/D 边界写进规则）
ARM_A = """Step 1（扫描）：
描述这段视频中，有哪些画面是"对产品、脸部或身体施加了某种物理动作"的
（例如：涂抹、拍打、泼水、喷水、按压、擦拭、运动等）。
只描述画面中发生了什么动作，不评价效果好坏。分条列出，每条一句。

Step 2（对照清单）：
基于你在 Step 1 中的描述，判断以下清单项：

【D 项 · 性能压力测试】
视频中有没有出现真实场景压力测试？
判断标准（以下任一即为 yes）：
  · 泼水或喷水到脸部
  · 出汗 / 高温环境下持妆考验
  · 纸巾按压验证残妆
  · 运动后上妆状态对比

→ yes / no

规则：
· 只根据 Step 1 你列出的内容作答，不得推断或补充画面中未出现的事件
· Step 1 未提到上述任何形态 → 答 no
· 持妆计时数字（如屏幕显示"已持妆 12 小时"）归 B 项，不归本项，答 no"""

ARM_B = """【D 项 · 性能压力测试】
视频中有没有出现真实场景压力测试？
判断标准（以下任一即为 yes）：
  · 泼水或喷水到脸部
  · 出汗 / 高温环境下持妆考验
  · 纸巾按压验证残妆
  · 运动后上妆状态对比

→ yes / no

规则：
· 只根据视频画面中实际发生的事件作答
· 持妆计时数字（如屏幕显示"已持妆 12 小时"）不属于本项，答 no"""

ARM_C = """【D 项 · 性能压力测试】
视频中有没有出现真实场景压力测试？
判断标准（以下任一即为 yes）：
  · 泼水或喷水到脸部
  · 出汗 / 高温环境下持妆考验
  · 纸巾按压验证残妆
  · 运动后上妆状态对比

→ yes / no

如果答 yes：
请指出该事件大约发生在视频的哪个时间点。
格式："约第 X 秒" 或 "约 X:XX 处"。
若无法定位，写"无法定位"——不影响 yes/no 的判断。

规则：
· 只根据视频画面中实际发生的事件作答
· 答 yes 时必须附时间戳或"无法定位"，二者均为合法回答
· 持妆计时数字（如屏幕显示"已持妆 12 小时"）不属于本项，答 no"""

ARMS = {"A": ARM_A, "B": ARM_B, "C": ARM_C}
VIDEOS = [("carslan-b0", "yes"), ("skincare", "no")]  # (样本, 正确答案)


def get_key() -> str:
    env = os.environ.get("OPENAI_API_KEY", "").strip()
    if env:
        return env
    out = subprocess.run(["security", "find-generic-password", "-s", "VidLingo.Qwen",
                          "-a", "API_KEY", "-w"], capture_output=True, text=True)
    return out.stdout.strip()


def creator_video_path(sample: str) -> Path:
    """从 analysis.json 取达人原视频路径。"""
    an = json.loads((Path("runs") / f"sample-{sample}" / "analysis.json").read_text(encoding="utf-8"))
    return Path(str(an.get("videos", {}).get("creator", {}).get("path") or ""))


def parse_answer(text: str) -> str:
    """从模型输出解析 yes/no。取最后一个明确的 yes/no（A 臂 Step2 在后半）。解析不出记 UNPARSED，靠 raw_output 人工纠。"""
    hits = re.findall(r"\b(yes|no)\b|(是)\b|(否)\b", text.lower())
    if not hits:
        # 中文兜底
        if "→ 是" in text or "答 是" in text:
            return "yes"
        if "→ 否" in text or "答 否" in text:
            return "no"
        return "UNPARSED"
    last = hits[-1]
    token = last[0] or last[1] or last[2]
    return {"yes": "yes", "no": "no", "是": "yes", "否": "no"}.get(token, "UNPARSED")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", default="runs_exp/probe-d")
    # 分阶段冒烟：先 --samples carslan-b0 --arms A 打 5 个验 harness/raw_output，再无参跑满（resume-skip 续）
    ap.add_argument("--samples", nargs="+", default=None, help="只跑这些样本（默认全部）")
    ap.add_argument("--arms", nargs="+", default=None, help="只跑这些臂 A/B/C（默认全部）")
    a = ap.parse_args()

    videos = [(s, c) for s, c in VIDEOS if not a.samples or s in a.samples]
    arms = {k: v for k, v in ARMS.items() if not a.arms or k in a.arms}

    key = get_key()
    if not key:
        sys.exit("拿不到 API key（keychain VidLingo.Qwen / 环境 OPENAI_API_KEY 均空）")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # 每个视频只编码一次 data_url，多次调用复用（省重复转码）
    video_cache: dict[str, str] = {}
    for sample, _ in videos:
        vp = creator_video_path(sample)
        url = video_to_data_url(vp) if vp.is_file() else None
        if url is None:
            sys.exit(f"{sample} 达人视频转码失败/不存在：{vp}（需 ffmpeg）")
        video_cache[sample] = url

    for sample, correct in videos:
        (out / sample).mkdir(parents=True, exist_ok=True)
        for arm, prompt in arms.items():
            for i in range(1, a.n + 1):
                dst = out / sample / f"{arm}_run{i}.json"
                if dst.is_file():  # resume-skip
                    print(f"skip {sample}/{arm}/r{i}")
                    continue
                payload = {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "video_url", "video_url": {"url": video_cache[sample]}},
                        ]},
                    ],
                    "temperature": 0.0,  # 补充一：锁死，三臂一致
                }
                pp = out / sample / f"_payload_{arm}_{i}.json"
                rp = out / sample / f"_raw_{arm}_{i}.json"
                pp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                t0 = time.time()
                try:
                    raw = call_llm_api(API_URL, key, pp, rp)
                    text = extract_chat_completion_text(json.loads(raw))
                    ans = parse_answer(text)
                    dst.write_text(json.dumps({
                        "arm": arm, "video_id": sample, "correct": correct,
                        "answer": ans, "raw_output": text,  # 补充二：原始输出存盘供失败分析
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{sample}/{arm}/r{i}: {ans} ({round(time.time()-t0)}s)")
                except (Exception, SystemExit) as e:
                    print(f"{sample}/{arm}/r{i}: FAILED {str(e)[:100]}")
                finally:
                    pp.unlink(missing_ok=True)

    # 一致率表（arm × video）：负例先打印，防锚定（先排除 false-yes 的假抗臆造臂，再看正例存活臂）
    print("\n===== 一致率（answer 众数占比，arm × video）— 负例在前 =====")
    from collections import Counter
    for sample, correct in sorted(videos, key=lambda x: x[1] != "no"):
        for arm in arms:
            vals = []
            for i in range(1, a.n + 1):
                f = out / sample / f"{arm}_run{i}.json"
                if f.is_file():
                    vals.append(json.loads(f.read_text(encoding="utf-8"))["answer"])
            if not vals:
                continue
            c = Counter(vals)
            top, topn = c.most_common(1)[0]
            print(f"  {sample:11}(正确={correct:3}) Arm {arm}: {dict(c)}  众数={top} 一致率={topn/len(vals):.2f}")


if __name__ == "__main__":
    main()
