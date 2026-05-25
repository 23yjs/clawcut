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
- `min_segment_duration`：候选片段最小时长，默认 `4` 秒。
- `max_segment_duration`：候选片段最大时长，默认 `20` 秒。

## 执行链路

1. 校验视频路径、目标时长、输出目录。
2. 使用 `ffprobe` 读取视频元信息。
3. 使用 `ffmpeg` 镜头变化检测生成候选片段；失败时退化为固定窗口切分。
4. 读取字幕或 ASR 转写，将文本对齐到候选片段。
5. 对候选片段进行电商高光评分：
   - 商品可见度。
   - 卖点相关性。
   - 画面质量。
   - 动作/事件完整性。
   - 音频/字幕信息量。
   - 指令匹配度。
   - 节奏适配度。
6. 可选调用 Ark/方舟模型对片段进行语义重排序。
7. 在目标时长约束下选择最优片段组合。
8. 使用 `ffmpeg` 裁剪、拼接，并按需转换输出比例。
9. 输出高光视频、时间线报告、片段 JSON 和评测报告。

## 单条任务命令

```bash
python3 skills/ecom-highlight-skill/scripts/highlight_pipeline.py \
  --input <video_path> \
  --instruction "<剪辑目标>" \
  --target-duration <秒数> \
  --target-platform xiaohongshu \
  --style 种草 \
  --aspect-ratio 9:16 \
  --output-dir <output_dir>
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
- `segments.json`：选中片段、候选片段、评分明细、运行 warning。
- `result.json`：结构化运行结果。
- `timeline_report.md`：时间戳、片段说明、未选片段原因。
- `run_report.md`：与 `timeline_report.md` 同步的人类可读报告。
- `eval_result.json`：自动评测 JSON。
- `eval_report.md`：自动评测 Markdown。
- `failure.json`：失败时的错误类型、错误信息和输入参数。

当 Ark/方舟模型参与评分时，片段 `reason` 应输出中文理由，便于直接进入中文评测报告和答辩材料。

## 失败处理

- 输入视频不存在：直接失败并写入 `failure.json`。
- 缺少 `ffmpeg` / `ffprobe`：提示安装 `ffmpeg`。
- 镜头切分失败：退化为固定窗口候选片段。
- Ark/方舟模型失败：退化为启发式评分。
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
