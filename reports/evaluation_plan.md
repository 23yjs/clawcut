# EcomHighlightSkill 评测方案

## 1. 评测目标

评估 EcomHighlightSkill 是否能根据自然语言指令，从商品展示、开箱、种草、带货或测评视频中定位并导出高质量电商高光片段。

本项目的高光定义为：

```text
电商短视频中最能促进商品理解和转化的片段。
```

因此评测标准不是“画面是否好看”，而是：

- 商品主体是否清晰出现。
- 是否覆盖核心卖点。
- 是否包含开箱、外观展示、功能演示、使用场景等关键动作。
- 是否符合用户指定平台、风格、时长和比例。
- 是否避开黑屏、模糊、严重抖动、商品不可见、无关闲聊等低质内容。

## 2. 数据集设计

建议准备 20-30 条样本，聚焦电商短视频，不做泛视频混杂。

| 难度 | 数量 | 示例 | 主要验证点 |
|---|---:|---|---|
| easy | 6-8 | 商品主体清晰、卖点集中、视频较短 | 基础时间定位、裁剪导出 |
| medium | 8-10 | 有口播、多镜头、部分无关片段 | 字幕/语义、卖点覆盖、片段组合 |
| hard | 6-12 | 背景杂乱、镜头抖动、多个商品、卖点分散 | 失败兜底、低质片段过滤、案例分析 |

建议领域覆盖：

- 商品开箱。
- 商品外观展示。
- 功能演示。
- 使用场景种草。
- 带货口播。
- 多商品或干扰场景。

数据来源建议：

- 自己拍摄的无隐私商品展示视频。
- 公开视频素材，使用前检查授权。
- 可商用 stock video，例如 Pexels、Pixabay 等公开视频素材。

## 3. Ground Truth 标注规范

每个视频准备一个 JSON 标注文件：

```json
{
  "case_id": "ecom_cup_001",
  "video": "data/input/ecom_cup_001.mp4",
  "instruction": "剪出 15 秒小红书种草高光，突出外观和保温卖点",
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

标注要求：

- 起止时间精确到 0.1 秒。
- 每个高光片段必须有 `label` 或 `reason`。
- `must_cover_points` 用于衡量卖点覆盖。
- `avoid_segments` 用于衡量是否误选低质片段。
- 如果样本主观性强，需要在失败分析中解释标注依据。

## 4. 候选片段评分公式

候选片段高光分：

```text
HighlightScore =
0.25 * 商品可见度
+ 0.20 * 卖点相关性
+ 0.15 * 画面质量
+ 0.15 * 动作/事件完整性
+ 0.10 * 音频/字幕信息量
+ 0.10 * 指令匹配度
+ 0.05 * 节奏适配度
```

当前 MVP 使用字幕、指令关键词、镜头切分和片段时长作为可解释近似特征。后续可接入关键帧 caption、OCR、ASR 和视频多模态模型提升准确性。

## 5. 程序化指标

### 5.1 Temporal IoU

```text
Temporal IoU = 预测片段与 GT 片段交集时长 / 并集时长
```

### 5.2 Temporal Precision / Recall / F1

```text
Precision = 预测片段中落在 GT 高光区间的时长 / 预测总时长
Recall = GT 高光区间被预测覆盖的时长 / GT 总时长
F1 = 2 * Precision * Recall / (Precision + Recall)
```

### 5.3 Required Point Coverage

```text
CoverageScore = covered_required_points / total_required_points
```

衡量是否覆盖商品外观、核心功能、使用场景等必选卖点。

### 5.4 Avoid Segment Hit Rate

```text
AvoidHitRate = hit_avoid_segments / total_avoid_segments
```

该指标越低越好。若系统剪入黑屏、模糊、商品不可见片段，会在该指标中扣分。

### 5.5 Duration Error

```text
Duration Error = abs(output_duration - target_duration) / target_duration
```

## 6. 100 分制评分

| 一级指标 | 分值 | 程序化近似 |
|---|---:|---|
| 时间定位准确性 | 25 | Temporal F1 |
| 卖点覆盖度 | 20 | Required Point Coverage |
| 指令遵循度 | 15 | Recall@3@IoU0.5 |
| 视频质量 | 15 | Quality Proxy * (1 - AvoidHitRate) |
| 剪辑连贯性 | 10 | Order Coherence |
| 输出规范性 | 10 | DurationScore |
| 工程稳定性 | 5 | Success Flag |

```text
EcommerceScore100 =
25 * TemporalF1
+ 20 * RequiredPointCoverage
+ 15 * Recall@3@IoU0.5
+ 15 * QualityProxy * (1 - AvoidHitRate)
+ 10 * OrderCoherence
+ 10 * max(0, 1 - DurationError)
+ 5 * SuccessFlag
```

## 7. LLM-as-a-Judge Prompt

```text
你是电商短视频剪辑质量评估员。请根据原始视频摘要、用户剪辑指令、输出高光片段关键帧、时间戳和字幕内容，对结果进行评分。

评分维度：
1. product_visibility：商品主体是否清晰出现，1-5 分；
2. selling_point_coverage：是否覆盖核心卖点，1-5 分；
3. instruction_following：是否符合平台、风格、时长、主题，1-5 分；
4. editing_coherence：剪辑节奏和片段衔接是否自然，1-5 分；
5. quality_control：是否避开黑屏、模糊、抖动、无关内容，1-5 分。

请只输出 JSON：
{
  "product_visibility": 1,
  "selling_point_coverage": 1,
  "instruction_following": 1,
  "editing_coherence": 1,
  "quality_control": 1,
  "overall": 1,
  "failure_tags": ["..."],
  "comment": "..."
}
```

## 8. 自动化评测流水线

单条评测：

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

## 9. 成功/失败案例分析模板

成功案例：

- 视频类型：商品开箱 / 外观展示 / 功能演示。
- 用户指令。
- GT 片段。
- 系统输出片段。
- `ecommerce_score_100`、Temporal F1、卖点覆盖率。
- 成功原因：商品主体清晰、卖点词命中、片段边界自然。

失败案例：

- 失败类型：商品不可见、卖点漏剪、片段边界不准、输出时长异常、重复片段。
- 指标表现。
- 原因分析。
- 优化结论。

## 10. 验收标准

- 至少 20 条电商视频样本完成批量评测。
- 总体 `ecommerce_score_100 >= 70`。
- easy 样本 `Temporal F1 >= 0.75`。
- medium 样本 `Required Point Coverage >= 0.65`。
- hard 样本允许失败，但必须有失败案例分析。
- 所有失败样本必须能通过 `failure.json`、`warnings` 或指标定位原因。

