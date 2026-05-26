---
name: ecom-highlight-skill
description: 面向电商种草、开箱、带货视频的高光片段识别、自动剪辑与评测 Skill。
---

# EcomHighlightSkill：电商短视频高光剪辑 Skill

当用户需要从商品展示、开箱、种草、带货或测评视频中剪出最有营销价值的高光短视频时，使用本 Skill。

本项目中的“高光”不是泛娱乐意义上的精彩片段，而是电商短视频中最能促进商品理解和转化的片段。优先选择：

- 商品主体清晰出现的片段。
- 商品外观、开箱、上手、功能演示、使用场景等卖点片段。
- 画面质量稳定、无黑屏、无严重模糊、无明显抖动的片段。
- 语义完整、动作完整、适合剪成 10-20 秒短视频的片段。
- 符合用户指定平台、风格、目标时长和输出比例的片段。

## 输入参数

必填参数：

- `input`：原始视频文件路径，建议为 30 秒到 3 分钟商品视频。
- `instruction`：自然语言剪辑需求。
- `target_duration`：目标高光视频总时长，建议 10-20 秒。
- `output_dir`：结果输出目录。

可选参数：

- `task_id`：任务 ID，用于复现实验和报告归档。
- `transcript`：字幕或 ASR 转写文件路径，支持 `.srt`、`.vtt`、`.txt`、`.json`。
- `target_platform`：目标平台，例如 `xiaohongshu`、`douyin`、`taobao`。
- `style`：剪辑风格，例如 `种草`、`带货`、`开箱`、`测评`。
- `aspect_ratio`：输出比例，支持 `source`、`9:16`、`16:9`、`1:1`。
- `ground_truth`：人工标注文件路径，用于自动评测。
- `scene_threshold`：镜头切分阈值，默认 `0.30`。
- `candidate_mode`：候选生成模式，支持 `scene`、`window`、`hybrid`、`llm_hybrid`，默认 `llm_hybrid`。
- `scoring_mode`：候选评分模式，支持 `model`、`fallback`、`heuristic`，默认 `fallback`。
- `assembly_mode`：拼接规划模式，支持 `model`、`fallback`、`heuristic`，默认 `fallback`。
- `planner_frame_interval`：Candidate Planner 稀疏抽帧间隔，默认 `2.0` 秒。
- `max_planner_frames`：Candidate Planner 最大抽帧数量，默认 `48`。
- `keyframes_per_segment`：每个候选片段关键帧数量，默认 `3`。
- `min_candidate_duration`：候选片段最小时长，默认 `4` 秒。
- `max_candidate_duration`：候选片段最大时长，默认 `12` 秒。
- `dump_candidates_only`：只生成候选和候选证据，不做评分、不渲染。

## OpenClaw Docker 路径约定

OpenClaw 本地版通常在 Linux Docker 容器中执行 Skill，不能直接访问 macOS 宿主机路径，例如 `/Users/df/Documents/clawcut/...`。

在 OpenClaw 调度中请优先使用当前工作区内的相对路径：

- 输入视频：`data/input/ecom_cup_demo.mp4`
- 输出目录：`outputs/openclaw_ecom_cup_demo_30s`

如果需要绝对路径，请使用容器内路径：

- 输入视频：`/home/node/.openclaw/workspace/data/input/ecom_cup_demo.mp4`
- 输出目录：`/home/node/.openclaw/workspace/outputs/openclaw_ecom_cup_demo_30s`

不要在 OpenClaw 任务中传入 `/Users/...`、`/Volumes/...` 等宿主机路径；如已传入，请先把文件复制到 OpenClaw 工作区的 `data/input/` 下。

## 执行链路

1. 校验视频路径、目标时长、输出目录。
2. 使用 `ffprobe` 读取视频元信息，使用 `ffmpeg` 进行 scene detection、稀疏抽帧和候选关键帧抽取。
3. Candidate Planner：大模型基于用户指令、视频信息、scene boundaries、稀疏帧和字幕规划语义候选切片，不输出 score。
4. 融合 `ffmpeg` scene/window 候选与大模型候选边界，生成 `candidates.json` 和 `candidate_evidence.json`。
5. Candidate Judge：大模型对每个候选切片独立评分，输出 `score`、`score_breakdown`、`covered_points`、`quality_issues` 和中文 `reason`。
6. Assembly Planner：大模型基于候选评分、目标时长、平台风格和卖点覆盖生成 `assembly_plan.json`。
7. 校验拼接方案；若模型失败或输出不合法，根据模式退回启发式 fallback，并在 `pipeline` / `fallback` metadata 中记录原因。
8. `ffmpeg` 只按 `assembly_plan.json` 执行裁剪、转码、拼接和比例转换，不参与语义评分。
9. 输出高光视频、时间线报告、片段 JSON、模型中间产物和评测兼容字段。

## 单条任务命令

```bash
python3 skills/ecom-highlight-skill/scripts/highlight_pipeline.py \
  --input <video_path> \
  --instruction "<剪辑目标>" \
  --target-duration <秒数> \
  --target-platform xiaohongshu \
  --style 种草 \
  --aspect-ratio 9:16 \
  --output-dir <output_dir> \
  --candidate-mode llm_hybrid \
  --scoring-mode fallback \
  --assembly-mode fallback
```

如有字幕文件：

```bash
  --transcript <transcript_path>
```

## 自动评测命令

```bash
python3 skills/ecom-highlight-skill/scripts/evaluate_highlights.py \
  --prediction <output_dir>/segments.json \
  --ground-truth <ground_truth_path> \
  --output <output_dir>/eval_result.json \
  --markdown <output_dir>/eval_report.md
```

## 批量评测命令

```bash
python3 skills/ecom-highlight-skill/scripts/run_eval_suite.py \
  --cases eval/cases.example.jsonl \
  --output-dir outputs/eval_suite \
  --report-json reports/eval_suite_summary.json \
  --report-md reports/eval_suite_summary.md
```

## Ground Truth 标注格式

```json
{
  "case_id": "ecom_cup_001",
  "video": "data/input/ecom_cup_001.mp4",
  "instruction": "剪出 15 秒小红书种草高光，突出外观、开箱和保温卖点",
  "target_duration": 15,
  "difficulty": "medium",
  "segments": [
    {
      "start": 3.2,
      "end": 7.8,
      "label": "开箱展示，商品首次完整出现"
    },
    {
      "start": 18.5,
      "end": 24.0,
      "label": "旋转展示商品外观"
    }
  ],
  "must_cover_points": ["商品外观", "保温卖点", "使用场景"],
  "avoid_segments": [
    {
      "start": 60.0,
      "end": 70.0,
      "reason": "画面模糊，商品不可见"
    }
  ]
}
```

## 输出产物

- `highlight.mp4`：最终高光视频。
- `video_info.json`：视频时长、分辨率、fps、格式和编码。
- `ffmpeg_scenes.json`：scene detection 边界。
- `timeline_evidence.json`：Candidate Planner 输入证据。
- `planner_frames/`：稀疏采样帧。
- `llm_candidate_plan.json`：Candidate Planner 输出。
- `candidates.json`：融合后的候选切片。
- `candidate_evidence.json`：Candidate Judge 输入证据。
- `candidate_stats.json`：候选统计。
- `frames/`：候选片段关键帧。
- `segment_scores.json`：Candidate Judge 或 fallback 评分。
- `assembly_plan.json`：最终拼接方案。
- `assembly_validation.json`：拼接方案校验结果。
- `segments.json`：选中片段、候选片段、评分明细、pipeline/fallback metadata、运行 warning。
- `result.json`：结构化运行结果。
- `timeline_report.md`：时间戳、片段说明、未选片段原因。
- `run_report.md`：与 `timeline_report.md` 同步的人类可读报告。
- `eval_result.json`：自动评测 JSON。
- `eval_report.md`：自动评测 Markdown。
- `failure.json`：失败时的错误类型、错误信息和输入参数。

当 Ark/方舟模型参与 Planner、Judge 或 Assembly 时，所有 `reason` 应输出中文理由，便于直接进入中文评测报告和答辩材料。

## 失败处理

- 输入视频不存在：直接失败并写入 `failure.json`。
- 缺少 `ffmpeg` / `ffprobe`：提示安装 `ffmpeg`。
- Candidate Planner 失败：`llm_hybrid` 下退化为 `hybrid` 候选生成，或在严格 model 模式下失败。
- Candidate Judge 失败：`fallback` 模式退化为启发式评分，严格 `model` 模式失败。
- Assembly Planner 失败或输出不合法：`fallback` 模式退化为约束贪心拼接，严格 `model` 模式失败。
- 候选片段不足：输出明确 warning，保证失败可定位。

## 验收指标

最终报告至少包含：

- 20-30 组电商视频评测样本。
- Temporal IoU、Precision、Recall、F1、Duration Error。
- 必选卖点覆盖率 `required_point_coverage`。
- 避免低质片段命中率 `avoid_segment_hit_rate`。
- 100 分制电商剪辑质量分 `ecommerce_score_100`。
- LLM-as-a-Judge 主观质量评分。
- 典型成功案例、失败案例和优化结论。
