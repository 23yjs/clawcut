# EcomHighlightSkill 技术设计

## 1. 设计原则

EcomHighlightSkill 采用“三阶段大模型协同 + ffmpeg 执行”的架构。大模型负责语义决策，`ffmpeg` / `ffprobe` 只负责确定性视频处理。

```text
ffmpeg/ffprobe 提取视频证据
  -> Candidate Planner 规划候选切片
  -> Candidate Judge 结构化评分
  -> Assembly Planner 生成拼接方案
  -> ffmpeg 按 assembly_plan.json 裁剪、转码、拼接
```

这避免了旧版本“启发式评分为主、Ark 可选重排序”的问题：正常路径下不再先计算 heuristic score，也不会把 `initial_score`、`tags`、`score_breakdown` 传给模型造成锚定。

## 2. 模块划分

Stage 1：视频证据提取

- `probe_video_info`：读取时长、分辨率、fps、格式和编码。
- `detect_scene_boundaries`：使用 `ffmpeg` scene detection 获取候选边界。
- `extract_planner_frames`：抽取稀疏时间轴帧，限制最大帧数。
- `extract_candidate_keyframes`：为每个候选片段抽 start/middle/end 关键帧。

Stage 2：Candidate Planner

- 输入用户指令、视频信息、scene boundaries、稀疏帧路径和可选字幕。
- 输出 `semantic_events`、`candidate_boundary_suggestions`、`avoid_ranges`。
- 不输出 score，不决定最终拼接。

Stage 3：Candidate Judge

- 输入融合后的候选片段、关键帧、字幕和用户指令。
- 输出 `score`、`score_breakdown`、`covered_points`、`quality_issues`、中文 `reason`。
- Judge 不接收 heuristic score，必须独立判断。

Stage 4：Assembly Planner

- 输入候选片段、Judge 评分、目标时长、平台和风格。
- 输出 `assembly_plan.json`，包含入选片段、顺序、`trim_start` / `trim_end`、角色和理由。
- `validate_assembly_plan` 校验时间戳、候选范围、时长和严重质量问题。

## 3. ffmpeg 职责边界

`ffmpeg` / `ffprobe` 负责：

- 读取视频信息。
- scene detection。
- 抽稀疏帧和候选关键帧。
- 按 `assembly_plan.json` 裁剪片段。
- 统一编码、转码、拼接和比例转换。

`ffmpeg` 不负责：

- 判断哪个片段是高光。
- 判断商品是否出现。
- 判断卖点是否覆盖。
- 给候选片段打分。
- 决定最终拼接顺序。

## 4. Fallback 策略

系统支持 `model`、`fallback`、`heuristic` 三种模式：

- `model`：模型阶段失败则任务失败并写入 `failure.json`。
- `fallback`：优先模型，失败时退回对应启发式兜底。
- `heuristic`：只使用程序兜底，用于本地 smoke test。

`segments.json` / `result.json` 中统一记录：

```json
{
  "pipeline": {
    "candidate_generation": "ffmpeg_plus_llm_candidate_planner | ffmpeg_hybrid_fallback | ffmpeg_hybrid",
    "candidate_scoring": "llm_candidate_judge | heuristic_fallback | heuristic",
    "assembly_planning": "llm_assembly_planner | heuristic_fallback | heuristic",
    "rendering": "ffmpeg"
  },
  "fallback": {
    "used": false,
    "stages": [],
    "reasons": []
  }
}
```

## 5. 输出产物

一次完整运行会输出：

- `video_info.json`
- `ffmpeg_scenes.json`
- `timeline_evidence.json`
- `planner_frames/`
- `llm_candidate_plan.json`
- `candidates.json`
- `candidate_evidence.json`
- `candidate_stats.json`
- `frames/`
- `segment_scores.json`
- `assembly_plan.json`
- `assembly_validation.json`
- `highlight.mp4`
- `segments.json`
- `result.json`
- `timeline_report.md`
- `run_report.md`

失败时输出 `failure.json`。

## 6. 验证方式

候选调试：

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

无模型 smoke test：

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
