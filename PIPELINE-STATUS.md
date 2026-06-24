# Flayr 全流程现状 · back-to-basic 2.0 工作记录

> 2026-06-23 代码级盘点。本轮从「阶段2 判断工程治不了根」回到「阶段1 事实覆盖是根」，
> 重新梳理整条流水线、定位两处红区、立总纲与清单化改造方向、上膛覆盖漂移探针。
> 配套：协议 [BASELINE-PROTOCOL.md](BASELINE-PROTOCOL.md)、探针 [scripts/probe_arms.py](scripts/probe_arms.py)。

## 全流程表

| 阶段 | 怎么处理 | 用到的模型 | 得到什么数据 | 品的地基 | 结构库的地基 | 备注 |
|---|---|---|---|---|---|---|
| **预处理** | 转码抽帧(fps3/480宽)、ASR转写、OCR字幕、镜头切分、晃动检测 | Whisper(ASR本地)、qwen-vl-ocr(字幕)、ffmpeg、光流(motion) | 帧序列+音频+转写(srt/中译)+OCR字幕轨+镜头轨+晃动指标 | 无（纯信号提取，不涉及） | 无 | 零大模型判断；晃动指标后面喂 derive 封顶 |
| **Step-0 品地基** | 据产品信息建命题地基 | qwen3.5-omni-plus | product_profile(核心视觉命题/卖点/visual_diff维度/使用场景)+category_profile(痛点等) | ✅**在这里产出** | 无（不读结构库） | 两地基的源头；后续阶段全靠它 |
| **阶段1 单视频事实抽取**（达人/标杆各一次） | 原生视频喂模型、按带货结构自由叙述切 evidence_units | qwen3.5-omni-plus | evidence_units(time/口播/视觉事实/音频事实/product_visible/coverage/背书/functions)+content_summary | ⚠️**软提示**(obs_hint，只注 product_profile，**痛点缺**) | ⚠️**只有粗 functions 标签**(S1-S6)，**事件目录(S4-A~F/S3-A~E)缺** | 🔴**覆盖漂移根在此**：软地基→重磅事件(泼水)随机漏。已帧级实证 |
| **阶段2 对比判断** | 看证据帧+音频做两侧对比打分 | qwen3.5-omni-plus | stage_analysis[6]：每阶段 B/C执行分(0/.5/1/2)+has_effect/usage/comparison布尔+painpoint_relevance+module_id+gap；improvements | ✅**注入**(命题/卖点写进 payload) | ✅S1-S6 + S3-A~E/S4-A~F **都在 payload** | 地基**到了模型**；判断漂移可治(改名+正负例已验 1.000) |
| **derive 后处理推导** | E=标杆−达人执行分 ×W品类权重 ×c痛点系数 +放大器+红线，**改写 severity** | 无（纯代码） | 每阶段 severity(small/medium/large)+溯源 | ⚠️**只用 painpoint_relevance**(粗信号) | ⚠️只用 has_effect/usage/comparison **布尔**(部分) | 🔴**孤儿病**：地基到模型、**没到 derive**；derive 只吃粗信号+硬编码正则 |
| **phase_c 复审** | 回看低置信阶段重核事实 | qwen3.5-omni-plus | 修订后的 stage_analysis | 继承阶段2 | 继承阶段2 | 临界分值触发，可选 |

## 两处红区（本轮挖出的根）

- 🔴 **阶段1 软地基 = 覆盖漂移根**：品只软提示（痛点没注）、结构库只粗标签（事件目录没给）→ 模型没有「必须逐项核查的硬清单」→ 真事件随机漏（carslan 泼水帧级实证：~34s 真实存在，3/5 跑漏掉）。清单化改造 = 把两地基在阶段1 打硬（**未落地**，探针上膛待验）。
- 🔴 **derive 孤儿病**：两地基到了阶段2 模型，但 derive（改写最终 severity）只吃粗信号。命题的丰富度只通过「执行分」这条脆弱通道间接进最终分。

## 两种漂移（阶段1 不稳分病理）

- **覆盖漂移 Coverage Drift**（真根）：重磅事件时有时无；注意力层、temp=0 压不住、伤下游；唯一修法 = 清单化。
- **措辞漂移 Lexical Drift**（无害）：同一事实换词；采样层、temp 能压、不伤下游；不用治，只是别拿文本去重当稳定指标。

## 总纲（设计层权威，高于一切清单规则）

```
命题（为什么有证据）      ── 品地基
  ↓ 由结构库翻译
结构事件（长什么样）      ── 结构库
  ↓ 由清单项稳定检测
yes / no（有没有）        ── 清单（执行层）
```

清单合法性来自总纲、不来自自己。命题相关性焊进锚定、不在运行时问。详见协议「总纲先于清单」节。

## 当前位置 & 下一步

- ✅ 已做：埋点①（阶段1 存 raw facts，run_batch）；总纲+清单原则+三个风险动作写进协议；D 项 worked example；三臂探针 harness（A 两步/B 直接/C 接地）+ 正负例坐实（carslan=yes / skincare=no）。
- ⏳ 卡点：网络（dashscope SSL 抽风）。探针 `python3 scripts/probe_arms.py` 上膛，网络回来即打。
- 📋 探针读法：正例稳命中 + 负例不 false-yes 才算赢；A/B/C 哪臂赢决定问法范式；都不稳 = 问题在视频理解/抽帧层。
- 📋 探针过后路线：清单本体定稿（7 构念，从 Stage2 字段倒推）→ 补品地基锚定字段 → 阶段1 prompt 改清单扫描 → derive 改读清单项（顺带治孤儿病）→ 全量上 κ/CI → 预测效度验证封锚。

## 待办：阶段2 旧账（本轮暂停、不删不跑）

- 闸门级联（derive 的 L1/L2/L3、has_comparison_structure）停在原型；将来 derive 改读清单项时一并收编。
- colorkey 补跑（路线一止损清关）从关键路径撤出，不再为阶段2 闸门投入。
