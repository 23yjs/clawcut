# EcomHighlightSkill

面向电商种草、开箱、带货视频的多模态高光片段识别、自动剪辑与评测系统。项目基于 OpenClaw Skill 机制封装，目标是打通：

```text
商品短视频 + 自然语言剪辑需求
  -> OpenClaw 调度 EcomHighlightSkill
  -> ffmpeg/ffprobe 提取视频证据
  -> Candidate Planner 规划候选切片
  -> Candidate Judge 结构化评分
  -> Assembly Planner 生成拼接方案
  -> ffmpeg 按 assembly_plan.json 执行裁剪拼接
  -> 高光视频 + 时间戳说明 + 自动评测报告
```

## 业务场景

输入一段 30 秒到 3 分钟的商品展示、开箱、种草、带货或测评视频，系统根据用户指令自动识别最有营销价值的片段，剪辑成 10-20 秒高光短视频。

示例指令：

```text
请从这段 90 秒保温杯开箱视频中剪出 15 秒小红书种草高光视频。
要求突出商品外观、开箱过程和保温卖点；
节奏自然，不要有黑屏；
输出竖屏 9:16 视频，并给出每个片段的时间戳和选择理由。
```

## 当前状态

- OpenClaw Docker 容器已在本机运行，端口为 `18789-18790`。
- 代码采用 Python 标准库优先实现，方舟/Ark 模型主导候选规划、候选评分和拼接规划。
- 正式剪辑需要安装 `ffmpeg` / `ffprobe`。

## 目录结构

```text
.
├── skills/ecom-highlight-skill/
│   ├── SKILL.md
│   ├── examples/task.example.json
│   └── scripts/
│       ├── highlight_pipeline.py
│       ├── evaluate_highlights.py
│       └── run_eval_suite.py
├── data/
│   ├── input/
│   └── ground_truth/
├── eval/
│   └── cases.example.jsonl
├── docs/
│   ├── faq_alignment.md
│   ├── industry_research.md
│   ├── technical_design.md
│   └── failure_analysis.md
├── outputs/
└── reports/
    └── evaluation_plan.md
```

## 本地运行

安装依赖：

```bash
brew install ffmpeg
```

运行单个视频：

```bash
python3 skills/ecom-highlight-skill/scripts/highlight_pipeline.py \
  --input data/input/ecom_demo.mp4 \
  --instruction "剪出 15 秒小红书种草高光，突出商品外观、开箱过程和核心卖点" \
  --target-duration 15 \
  --target-platform xiaohongshu \
  --style 种草 \
  --aspect-ratio 9:16 \
  --output-dir outputs/ecom_demo \
  --candidate-mode llm_hybrid \
  --scoring-mode fallback \
  --assembly-mode fallback
```

无模型环境 smoke test：

```bash
python3 skills/ecom-highlight-skill/scripts/highlight_pipeline.py \
  --input data/input/ecom_demo.mp4 \
  --instruction "剪出 15 秒小红书种草高光，突出商品外观、开箱和保温卖点" \
  --target-duration 15 \
  --target-platform xiaohongshu \
  --style 种草 \
  --aspect-ratio 9:16 \
  --output-dir outputs/ecom_demo_smoke \
  --candidate-mode hybrid \
  --scoring-mode heuristic \
  --assembly-mode heuristic
```

只调试候选切片：

```bash
python3 skills/ecom-highlight-skill/scripts/highlight_pipeline.py \
  --input data/input/ecom_demo.mp4 \
  --instruction "剪出 15 秒小红书种草高光，突出商品外观、开箱和保温卖点" \
  --target-duration 15 \
  --target-platform xiaohongshu \
  --style 种草 \
  --output-dir outputs/ecom_demo_candidates \
  --candidate-mode llm_hybrid \
  --dump-candidates-only
```

如果有人工标注 Ground Truth：

```bash
python3 skills/ecom-highlight-skill/scripts/evaluate_highlights.py \
  --prediction outputs/ecom_demo/segments.json \
  --ground-truth data/ground_truth/ecom_demo.gt.json \
  --output reports/ecom_demo_eval.json \
  --markdown reports/ecom_demo_eval.md
```

批量评测：

```bash
python3 skills/ecom-highlight-skill/scripts/run_eval_suite.py \
  --cases eval/cases.example.jsonl \
  --output-dir outputs/eval_suite \
  --report-json reports/eval_suite_summary.json \
  --report-md reports/eval_suite_summary.md
```

## 接入 OpenClaw

在 OpenClaw 中添加本项目 Skill 时，引用：

```text
skills/ecom-highlight-skill/SKILL.md
```

建议给 OpenClaw 的任务输入使用 `skills/ecom-highlight-skill/examples/task.example.json` 的结构。Skill 会要求 Agent 调用 `highlight_pipeline.py` 完成端到端处理，并把产物保存到 `outputs/<task_id>/`。

## Ark / 方舟模型配置

默认脚本可在无模型情况下用启发式策略跑通。若要让三阶段模型协同成为主路径，可设置：

```bash
export ARK_API_KEY="your_api_key"
export ARK_MODEL="your_model_name"
export ARK_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
```

模型主路径包含：

1. Candidate Planner：基于视频信息、scene boundaries、稀疏帧和字幕规划语义候选切片。
2. Candidate Judge：对每个候选切片输出 `score`、`covered_points`、`quality_issues` 和中文 `reason`。
3. Assembly Planner：根据目标时长、平台风格和候选评分生成 `assembly_plan.json`。

`ffmpeg` / `ffprobe` 只负责确定性视频处理：读取元信息、scene detection、抽帧、按 `assembly_plan.json` 裁剪拼接和转码，不参与高光语义评分。

## 评测重点

EcomHighlightSkill 不用“好不好看”这种泛化标准评测，而是按电商短视频目标拆解：

1. 时间定位准确性：Temporal IoU、Precision、Recall、F1。
2. 卖点覆盖度：`must_cover_points` 是否被覆盖。
3. 指令遵循度：目标平台、风格、时长、比例是否满足。
4. 视频质量：是否避开黑屏、模糊、抖动、商品不可见片段。
5. 剪辑连贯性：片段顺序、动作完整性、节奏自然度。
6. 输出规范性：`highlight.mp4`、`segments.json`、`timeline_report.md`、`eval_result.json`。
7. 工程稳定性：失败兜底、warning、`failure.json`。

加分项对应材料：

- 行业调研：[docs/industry_research.md](docs/industry_research.md)
- FAQ 和课题要求对齐：[docs/faq_alignment.md](docs/faq_alignment.md)
- 技术设计：[docs/technical_design.md](docs/technical_design.md)
- 失败案例模板：[docs/failure_analysis.md](docs/failure_analysis.md)
- 自动评测方案：[reports/evaluation_plan.md](reports/evaluation_plan.md)
