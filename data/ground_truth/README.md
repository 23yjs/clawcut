# Ground Truth 标注说明

每个评测视频需要一个同名标注文件，例如：

```text
data/input/ecom_easy_001.mp4
data/ground_truth/ecom_easy_001.gt.json
```

标注格式参考 `ecom_cup_demo_001.gt.json`，必须包含：

- `case_id`
- `video`
- `instruction`
- `target_duration`
- `segments`
- `must_cover_points`
- `avoid_segments`

