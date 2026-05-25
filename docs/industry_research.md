# EcomHighlightSkill 行业调研与方案对比

## 1. 行业定位

电商短视频剪辑位于三个方向的交叉点：

- 传统视频摘要：从长视频中挑出重要片段。
- Query-based Highlight Detection：根据自然语言目标定位相关高光片段。
- 电商内容生产：从商品视频中提炼最能促进理解和转化的素材。

EcomHighlightSkill 不做任意视频摘要，而是聚焦商品开箱、展示、测评、种草、带货视频。这样高光定义更稳定：商品可见、卖点清晰、动作完整、画面可用、适合短视频传播。

## 2. 主流方案对比

| 类型 | 代表方案 | 优点 | 局限 | 本项目取舍 |
|---|---|---|---|---|
| 传统视频摘要 | 镜头切分、运动强度、视觉显著性 | 快、便宜、可解释 | 不理解电商卖点 | 用作候选片段生成和兜底 |
| 开源视频工具链 | FFmpeg、PySceneDetect | 工程稳定，适合批处理 | 不负责语义判断 | 用于元信息、切分、裁剪、拼接 |
| Query-based 高光检测 | QVHighlights / Moment-DETR | 支持文本 query 和时间定位 | 训练和部署成本较高 | 借鉴 Temporal IoU、Recall@K 等评测方式 |
| 多模态大模型 | 视频理解模型、关键帧 caption、字幕理解 | 泛化强，可解释性较好 | 时间边界不稳定、成本高 | 作为 Ark/方舟可选重排序后端 |
| 商业剪辑产品 | 剪映、CapCut、Runway、Descript | 交互成熟、视觉效果强 | OpenClaw 集成和自动评测弱 | 本项目强调可编排、可复现、可量化 |
| 电商素材生产工具 | 商品主图/短视频智能剪辑工具 | 场景贴近业务 | 黑盒能力较多 | 本项目输出评分明细、时间戳和评测报告 |

## 3. 论文和数据集脉络

### TVSum

TVSum 是视频摘要经典数据集，包含 50 个视频和 shot-level importance scores，适合说明“视频片段重要性评分”这一任务不是临时拍脑袋。

参考：

- https://openaccess.thecvf.com/content_cvpr_2015/html/Song_TVSum_Summarizing_Web_2015_CVPR_paper.html
- https://github.com/yalesong/tvsum

### SumMe

SumMe 是视频摘要方向常见基准，强调生成短摘要并与人工摘要比较。它支撑本项目中 Temporal Precision / Recall / F1 的评测思路。

参考：

- https://gyglim.github.io/me/vsum/index.html

### QVHighlights / Moment-DETR

QVHighlights 将自然语言 query、相关片段定位和高光显著性评分结合起来。EcomHighlightSkill 的“用户指令 + 候选片段 + 时间戳评测”与该方向相似，但业务约束更明确。

参考：

- https://proceedings.neurips.cc/paper/2021/hash/62e0973455fd26eb03e91d5741a4a3bb-Abstract.html
- https://github.com/jayleicn/moment_detr

### PHD2 / YouTube Highlights

这类 human-centric / web-video highlights 数据集说明高光检测具有公开研究基础，但其高光定义偏泛娱乐。本项目进一步收窄到电商短视频，减少主观性。

参考：

- https://arxiv.org/html/2110.01774v2

### Video-LLM / Video-MME

Video-MME 和视频大模型综述说明长视频理解仍涉及时间建模、空间感知、事件定位等挑战。因此本项目不让大模型直接输出时间戳，而是先程序化生成候选片段，再让模型或规则打分。

参考：

- https://arxiv.org/abs/2405.21075
- https://arxiv.org/html/2312.17432v5

## 4. 技术路线选择

EcomHighlightSkill 采用“程序化候选片段 + 电商评分 + 可选模型重排序 + 自动评测”的路线：

1. 使用 `ffprobe` 获取视频元信息。
2. 使用 `ffmpeg` scene filter 或固定窗口生成候选片段。
3. 使用字幕/ASR 文本补充语义线索。
4. 按电商高光评分公式排序。
5. 使用 Ark/方舟模型作为可选语义重排序器。
6. 使用 `ffmpeg` 进行裁剪、拼接和比例转换。
7. 使用 Ground Truth 自动评测时间定位、卖点覆盖、低质片段避让和 100 分综合分。

这种设计避免了“直接让大模型看完整长视频并猜时间戳”的不稳定问题，也符合 OpenClaw Skill 的工程闭环要求。

