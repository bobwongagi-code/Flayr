# 盲测人工 GT 待填表

填写规则：先完整填写每个样本的 S1-S6 差距等级，再运行任何 Flayr 模型调用。等级可填 `small`、`medium`、`large`；阶段不适用时填 `NA`，并在“备注”说明原因。`NA` 不参与该阶段准确率统计。若达人优于标杆，表格仍记录达人侧不足程度，并在备注中写明比较方向。

| 样本 | 标杆视频 | 达人视频 | S1 | S2 | S3 | S4 | S5 | S6 | 备注 |
|:--|---|---|---|---|---|---|---|---|---|
| colorblu-c0 | `Color blu/标杆1.3万单.mp4` | `Color blu/达人.mp4` | small | small | large | large | NA | small | S3 无使用过程；S4 无效果展示。S5 不涉及。 |
| colorblu-c1 | `Color blu/标杆1.3万单.mp4` | `Color blu/达人1.mp4` | medium | small | large | large | NA | small | S3 无使用过程；S4 仅给结果、缺过程佐证。S5 不涉及。 |
| carslan-powder-c0 | `CARSLAN/标杆粉饼.mp4` | `CARSLAN/达人粉饼.mp4` | medium | small | small | medium | small | small | S6 达人 CTA 更明显，达人优于标杆。麦克风贴近嘴边并晃动；黑/银版本并列介绍且无明确肤质选择，焦点分散。 |
| carslan-powder-c1 | `CARSLAN/标杆粉饼.mp4` | `CARSLAN/达人粉饼1.mp4` | medium | small | medium | large | small | NA | 清凉感、轻薄感缺少可视化证明空间，被错误放成主卖点。 |
| colorkey-lip-c0 | `COLORKEY/标杆口红.mp4` | `COLORKEY/达人口红1.mp4` | small | small | medium | large | NA | small | S3 达人完整但手指收边不如标杆小刷子精细（small/medium 边界，裁决为 medium）；S4 标杆的显色、多色号与高级感向往明显更强。S5 不涉及。 |
| colorkey-lip-c1 | `COLORKEY/标杆口红.mp4` | `COLORKEY/达人口红2.mp4` | 不适用 | 不适用 | 不适用 | 不适用 | 不适用 | 不适用 | 全片可行性盲测：人工先冻结为“不可拍”，原因是只展示本体和手部试色、缺少继续观看理由；允许 AI 独立给出 S1-S6 结果，但不计入阶段准确率。 |

产品元数据已登记在 `references/validation-inputs.json`：Color Blu 快干防水密封剂、CARSLAN 2.0 控油粉饼、COLORKEY Silky Creamy Matte Lip Mud 唇彩，目标市场均为泰国（`th`）。COLORKEY 面膜与该唇彩使用独立命题键，不可复用。
