"""F 项（权威背书）探针：把"焊死的半判断字段"劈成两个纯观察二元，测清单化能不能防臆造。

背景：现有 third_party_endorsement 把"存在性(有没有出现硬来源)"和"有效性(算不算真背书)"焊死，
任务变成"帮我找背书"→ 模型找不到就脑补（are_xie 4/5 把口播+瓶身标臆造成"全屏证书海报"）。
劈开：阶段1 只观察存在性，分信道两问；有效性留阶段2/derive。

两问（都纯观察，复用 _ENDORSEMENT_PATTERN 硬子集，软背书/瓶身小标/达人自述不算）：
  ① 口播/字幕有没有【提到】具名硬背书来源？      yes/no
  ② 画面有没有【出现】独立的硬背书视觉证据？      yes/no  ← 臆造陷阱

are_xie ground truth（帧核验坐实）：① yes（口播 "Ada halal logo"）；② no（无独立证书、仅瓶身标）。
核心测试：清单化能否稳定答 ①yes ②no，抵住自由叙述 4/5 的画面臆造（②被诱导成 yes）。

三臂测"怎么问最抗臆造"：B 直接 / C 接地(②yes须举证时间戳) / A 两步(先描述再对照)。
跑次：3 臂 × 1 视频(are_xie) × N 跑。用法：python3 scripts/probe_endorsement.py [--n 5]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "scripts")
from flayr_core.llm.api import call_llm_api, extract_chat_completion_text, video_to_data_url
from probe_arms import get_key, creator_video_path, MODEL, API_URL

SYSTEM = ("你正在分析一段带货达人视频。你能看到连续画面、听到声音。"
          "严格按用户指示作答，只报真实出现的，不臆造画面里没有的东西。")

# 复用 repair_stages._ENDORSEMENT_PATTERN 的硬子集 + 阶段1 机构判据；软背书/瓶身标/自述明确排除
HARD = ("【具名硬背书来源】= 监管或认证机构（KKM/Halal/SIRIM/BPOM/FDA/SNI/GMP 等）、"
        "检测报告/临床/实验室结果、医生/牙医/皮肤科/药剂师/专家、高校或权威机构。"
        "不含：达人自述功效、素人或普通用户好评评论、单纯的品牌或赞助 logo。")

ARM_B = f"""【F 项 · 权威背书】针对本视频，逐项回答两个独立问题。
{HARD}

① 口播或字幕里有没有【出现】halal / KKM / 认证 / 证书 / 检测 / 医生 / 皮肤科 / 专家 / 机构 / FDA / GMP / certified 中任意词汇？（只报词有没有出现，不判断算不算有效背书） → yes/no
② 画面有没有【出现】独立的硬背书视觉证据（证书 / 检测报告文件 / 机构认证标识作为画面被清晰呈现）？ → yes/no

规则：
· ② 只看画面里真出现的东西。**口播说了不等于画面有**；产品瓶身上的印刷小标**不算**独立证书视觉。
· 两问独立作答，不要因为 ① 是 yes 就推断 ②。
最后一行给结论，严格格式：① yes/no ；② yes/no"""

ARM_C = f"""【F 项 · 权威背书】针对本视频，逐项回答两个独立问题。
{HARD}

① 口播或字幕里有没有【出现】halal / KKM / 认证 / 证书 / 检测 / 医生 / 皮肤科 / 专家 / 机构 / FDA / GMP / certified 中任意词汇？（只报词有没有出现，不判断算不算有效背书） → yes/no
② 画面有没有【出现】独立的硬背书视觉证据（证书 / 检测报告文件 / 机构认证标识被清晰呈现）？ → yes/no

举证：
· 若 ① 为 yes：摘录口播/字幕里提到硬来源的原句。
· 若 ② 为 yes：指出该视觉证据出现在约第几秒、是什么。**若指不出具体画面，② 必须为 no。**

规则：**口播说了不等于画面有**；瓶身印刷小标不算独立证书。
最后一行给结论，严格格式：① yes/no ；② yes/no"""

ARM_A = f"""【F 项 · 权威背书】两步作答。

Step 1（扫描，只描述不判断）：
(a) 口播/字幕里提到的任何认证 / 机构 / 医生 / 检测相关的话，逐条列出；
(b) 画面里真实出现的任何证书 / 报告 / 机构标识【画面】，逐条列出（产品瓶身上的印刷小标不算）。

Step 2（基于 Step 1 对照）：
{HARD}
① 口播/字幕里有没有【出现】halal / KKM / 认证 / 证书 / 检测 / 医生 / 专家 / 机构 / FDA / GMP / certified 中任意词汇？（只报词有没有出现，不判断算不算有效背书） → yes/no
② 画面有没有独立硬背书视觉证据？ → yes/no（只数你在 Step 1(b) 真列出的；(b) 为空 → ②=no。口播说了不等于画面有。）

最后一行给结论，严格格式：① yes/no ；② yes/no"""

ARMS = {"A": ARM_A, "B": ARM_B, "C": ARM_C}
VIDEO = "are_xie"
CORRECT = {"q1": "yes", "q2": "no"}  # 帧核验坐实


def parse_two(text: str) -> tuple[str, str]:
    """从结论行解析 ①② 的 yes/no。取每个标记最后一次出现后的 yes/no（结论行在末尾）。"""
    def near(marker: str) -> str:
        idx = text.rfind(marker)
        if idx < 0:
            return "UNPARSED"
        seg = text[idx:idx + 16].lower()
        m = re.search(r"yes|no|是|否", seg)
        if not m:
            return "UNPARSED"
        return "yes" if m.group(0) in ("yes", "是") else "no"
    return near("①"), near("②")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", default="runs_exp/probe-endorsement")
    ap.add_argument("--arms", nargs="+", default=None, help="只跑这些臂（默认全部），用于 smoke")
    a = ap.parse_args()
    arms = {k: v for k, v in ARMS.items() if not a.arms or k in a.arms}

    key = get_key()
    if not key:
        sys.exit("拿不到 API key")
    out = Path(a.out) / VIDEO
    out.mkdir(parents=True, exist_ok=True)

    vp = creator_video_path(VIDEO)
    url = video_to_data_url(vp) if vp.is_file() else None
    if url is None:
        sys.exit(f"{VIDEO} 视频转码失败：{vp}")

    for arm, prompt in arms.items():
        for i in range(1, a.n + 1):
            dst = out / f"{arm}_run{i}.json"
            if dst.is_file():
                print(f"skip {arm}/r{i}")
                continue
            payload = {"model": MODEL, "temperature": 0.0,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": [
                                        {"type": "text", "text": prompt},
                                        {"type": "video_url", "video_url": {"url": url}}]}]}
            pp = out / f"_payload_{arm}_{i}.json"
            rp = out / f"_raw_{arm}_{i}.json"
            pp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            t0 = time.time()
            try:
                raw = call_llm_api(API_URL, key, pp, rp)
                text = extract_chat_completion_text(json.loads(raw))
                q1, q2 = parse_two(text)
                dst.write_text(json.dumps({"arm": arm, "video_id": VIDEO,
                                           "correct": CORRECT, "q1": q1, "q2": q2,
                                           "raw_output": text}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                print(f"{arm}/r{i}: ①{q1} ②{q2} ({round(time.time()-t0)}s)")
            except (Exception, SystemExit) as e:
                print(f"{arm}/r{i}: FAILED {str(e)[:100]}")
            finally:
                pp.unlink(missing_ok=True)

    # 一致率 + 正确性表
    from collections import Counter
    print(f"\n===== F 项一致率 (are_xie, 正确=①yes ②no) =====")
    for arm in arms:
        for q in ["q1", "q2"]:
            vals = [json.loads((out / f"{arm}_run{i}.json").read_text())[q]
                    for i in range(1, a.n + 1) if (out / f"{arm}_run{i}.json").is_file()]
            if not vals:
                continue
            c = Counter(vals)
            top, n = c.most_common(1)[0]
            mark = "✓" if top == CORRECT[q] else "✗臆造" if q == "q2" else "✗"
            label = "①口播" if q == "q1" else "②画面"
            print(f"  Arm {arm} {label}: {dict(c)} 众数={top} 一致率={n/len(vals):.2f} {mark}")


if __name__ == "__main__":
    main()
