# 课题 FAQ 与 EcomHighlightSkill 对齐说明

## 1. 技术栈必须严格遵守吗？

课题要求 OpenClaw 为必选项，其他 LLM、存储方案和框架可自由选择。

EcomHighlightSkill 的对应方案：

- 使用 OpenClaw 调度 `skills/ecom-highlight-skill/SKILL.md`。
- 使用 Python 编写 Skill 执行脚本，便于处理视频、JSON、评测和模型调用。
- 使用 `ffmpeg` / `ffprobe` 完成稳定的视频处理。
- Ark/方舟模型作为可选电商语义评分后端；无模型配置时使用启发式评分兜底。

## 2. 评测报告至少需要多大规模的数据集？

课题建议至少 20-30 组不同难度样本。

本项目的对应方案：

- `eval/cases.example.jsonl` 定义评测集结构。
- 正式交付时扩展到 20-30 条电商短视频样本。
- 样本分为 easy / medium / hard，覆盖商品主体清晰、口播卖点、多镜头、背景杂乱、多个商品等情况。
- 使用 `run_eval_suite.py` 一键跑批量评测并生成汇总报告。

## 3. 测试数据去哪里找？可以使用公司内部或真实个人数据吗？

课题要求遵守数据安全底线，不使用未授权内部数据或真实个人隐私数据。

本项目的对应方案：

- 优先使用自己拍摄的商品展示视频、公开可用 stock video、公开开箱/展示素材。
- 不使用公司内部素材、客户数据、真实个人隐私视频。
- Ground Truth 只记录时间戳、卖点标签和失败原因，不记录敏感个人信息。
- 使用公开素材时逐条检查许可证。

## 4. 开发语言和框架有限制吗？

课题不限制语言和框架，但建议 Python。

本项目的对应方案：

- 使用 Python 标准库优先实现，减少环境安装阻力。
- 关键外部依赖只有 `ffmpeg`。
- 后续可逐步引入 PySceneDetect、Whisper、OCR、关键帧 caption、多模态模型。

## 5. 加分项如何体现？

### 深度行业视野

材料位置：

- `docs/industry_research.md`

覆盖内容：

- 传统视频摘要、Query-based Highlight Detection、视频大模型、电商内容生产工具的对比。
- TVSum、SumMe、QVHighlights、PHD2、Video-MME 等论文/数据集。
- 为什么收窄到电商短视频而不是做泛视频摘要。

### 严谨量化体系

材料位置：

- `reports/evaluation_plan.md`
- `skills/ecom-highlight-skill/scripts/evaluate_highlights.py`

覆盖内容：

- Temporal IoU。
- Temporal Precision / Recall / F1。
- 必选卖点覆盖率。
- 低质片段命中率。
- Duration Error。
- 100 分制电商剪辑质量分。
- LLM-as-a-Judge Prompt。

### 自动化评测能力

材料位置：

- `skills/ecom-highlight-skill/scripts/run_eval_suite.py`

覆盖内容：

- 从 `eval/cases.example.jsonl` 读取评测用例。
- 自动运行 EcomHighlightSkill 主流程。
- 自动调用评测脚本。
- 输出单样本报告和汇总报告。

