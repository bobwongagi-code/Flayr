# 要求与候选片段核对

2026-10-15，TwinClip 最小能力迁移。此入口只整理已有证据，不进行模型检索、视频重采集、S1-S6 判断或自动验收，不改变生产分析流程。

## 使用边界

- 一个核对包绑定一份明确要求、一个 Flayr 运行目录中的 creator 视频和一份人工提供的候选 ID 对照。多份返稿分别建包，不混用证据 ID。
- 要求来自用户明确清单或教学视频明确提出的点，必须有人核准，并附原文、页码或时间段。不能从分镜画面自行推导一条新的教学要求。原清单一对一保留，不自动拆分或合并。
- 代码检查来源文件、文本引用、ID、时间范围和文件哈希。页码/教学视频时间段是否确实表达这条要求，仍由核准人确认；格式合法不等于语义正确。
- 当前候选由操作人员依据已有账本提供，不是自动召回。账本描述仍可能错误。截图只因真实时间落在候选范围内而展示，不代表截图支持要求；保留范围内全部现有帧，不按“更好”自动排序。
- 没有候选只表示尚未提供候选，没有截图只表示此范围没有可用绑定截图；两者均不能推出没做。通道没有文本时也不补造事实。
- 人工意见单独追加，不改原分析、不写 GT。核对包没有自动“已完成”、改善程度、学习分或排名字段。人可以在备注中判断是否满足；这不会变成模型结论或触发报告发布。
- 一个命令展示整条视频的全部要求及候选，没有逐要求的模型调用或自动恢复。原来源门禁不通过则拒绝，不以空列表伪装成检索结果。

## 输入

`requirements.json`（相对来源路径按此文件所在目录解析）：

```json
{
  "id": "charger-revision",
  "version": "1",
  "approved_by": "运营核准人",
  "requirements": [
    {
      "id": "R1",
      "text": "近景展示接口插入手机的过程。",
      "source_type": "user_provided",
      "source_path": "修改意见.txt",
      "source_locator": {
        "kind": "quote",
        "value": "近景展示接口插入手机的过程。"
      }
    }
  ]
}
```

`source_type` 只接受 `user_provided` 或 `breakdown_explicit`。`source_locator` 也可以是 `{"kind":"page","value":2}` 或 `{"kind":"time_range","value":[12.5,18]}`。引用文本必须在源文件中实际存在；其他两种定位不宣称已经核实页面/画面含义。

`links.json`：

```json
{"R1": ["C3", "C4"]}
```

ID 必须来自本次 `analysis.json` 的 `video_understanding.creator.evidence_units`，时间直接取自该条目。不能输入替代时间或编造描述。空数组或省略某条要求表示未提供候选；未知要求、重复 ID、另一视频的 ID 均拒绝。

## 操作

在 Flayr 仓库执行，路径替换为实际已存在的文件：

```bash
python3 scripts/review_requirements.py init /path/to/existing-flayr-run \
  --requirements /path/to/requirements.json \
  --links /path/to/links.json \
  --output /path/to/revision-review.json

python3 scripts/review_requirements.py show /path/to/revision-review.json

python3 scripts/review_requirements.py note /path/to/revision-review.json \
  --requirement-id R1 --reviewer 运营核准人 \
  --note '候选片段找对了，但接口被手挡住，需要补拍。'
```

这三个命令均不调用模型，不生成对外 HTML，不修改原运行状态。`show` 输出视频、要求出处、候选时间、原始账本分通道文本、截图路径和人工历史。文字面向内部审核，不复用作者报告的比较措辞。

已有核对包不覆盖。要求文本/版本、来源文件、候选清单、视频、分析或已绑定截图变化后，旧核对包读取和记意见均拒绝，必须以新文件名重建；旧意见不自动继承。文件哈希用于防误用与漂移检查，不是对恶意修改所有本地文件的签名防护。

## 迁移对应与舍弃范围

来源版本：TwinClip `270b945fe7d35ba84ef366a9a06d0f848e4a1171`。

| TwinClip 来源 | Flayr 落点 |
|---|---|
| SKILL 的明确教学要点、source_locator、不得推断新增要求 | 核准要求清单及引用校验 |
| contracts.py 的参考资料身份绑定、semantic_run.py 的独立运行来源 | 核对包的 source_hashes，复用 Flayr load_analysis 来源门禁 |
| 候选证据与人工确认分离 | 只收集候选 ID、原始片段和单独 human_notes |
| 集中人工复核，不按每点拆调用 | 一份视频一份核对包，统一 show |

采用上述约束，不复制 TwinClip 评分编译器、L/S/T、选 lane、置信分、学习深度、权重、锚点排名、自动借鉴判断或飞书发布器。也不复制视频预处理、模型客户端或预算系统。完整旧源码、判例和安装副本先保留；历史报告不转算成新核对结论。

已迁入的是离线整理和人工核对能力，不是已验证的按要求检索能力。实际召回、定位准确性、人工耗时仍未验证，不能因此删除所有回退材料或宣布新产品验收完成。

## 初次工作区验收与回退（2026-10-15）

- `python3 -m unittest tests.test_requirement_review tests.test_human_review tests.test_creator_report -q`：77项通过，其中新核对流程23项。覆盖错侧/未知证据、重复ID、来源变化、候选篡改、越界时间、不制造不存在的截图、保留多张候选帧、来源门禁失败、人工记录分离和CLI往返。
- 复用了真实 `_SUCCESS` 来源校验的测试夹具；其余输入校验使用隔离临时文件。没有以这些测试宣称实际视频语义、检索召回或人工省时通过。
- `verify_semantic_baseline_freeze.py` 通过，旧生产冻结内容未改。新模块仍在未提交工作区，尚未做本次提交/重新冻结；该检查通过不代表新功能已经发布。
- Python 编译与已跟踪改动的 `git diff --check` 通过。未运行全仓发布流程，不提交、不推送、不调用模型、不重跑视频。
- TwinClip 原仓库保持不变，未移除旧安装入口。完整Git历史（含相对本地origin/main领先的14次提交）已归档，`git bundle verify` 确认完整且有效；已安装的不同版本另行保存。

本地回退目录：`/Users/wangbo5/Documents/VibeCoding/archives/TwinClip-migration-20261015-yH2AXn/`。

| 文件 | SHA-256 |
|---|---|
| TwinClip.bundle | a7544e4a7e2c30b2a5d252916f1274adae627cdfde9177f62ccaddbc8876a46a |
| installed-twinclip.tar.gz | ce72d62603201f3549a6d53b7f00f92e73e5949f0a15b24772321c514ffcf335 |

安装副本归档中的 SKILL.md 与原文件哈希一致。以后删除独立项目前，应另行核准入口退出；本次迁移不授权删除远端仓库。

## 发布与旧项目退出授权（2026-10-15）

用户后续授权本次迁移单独提交、必要的机械重新冻结、验证后推送，以及删除不再独立维护的 TwinClip。本节取代上节当时“不提交、不推送、保留旧入口”的待办状态，测试数字仍仅指初次工作区验收。

发布时在隔离检出上运行完整测试和质量门禁，不带入其他未提交诊断文档。
新入口只提供离线核对，不能宣称自动检索或业务省时已经通过。
旧 TwinClip 评分功能退出独立维护；归档恢复核验后移除本地项目目录与旧安装入口，
完整历史和安装副本继续保留在上列回退目录。远端 GitHub 仓库不在删除范围内。
