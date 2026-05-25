# OpenClaw 调度验证说明

本项目必须证明不是只在终端里直接运行 Python，而是完成：

```text
输入数据 -> OpenClaw 调度 -> EcomHighlightSkill -> Python Runner -> 结果输出
```

## 1. 确认本地 OpenClaw

本地容器状态：

```bash
docker ps
```

当前 UI 入口：

```text
http://127.0.0.1:18789
```

## 2. 添加 Skill

在 OpenClaw 控制台中找到 Agent / Skills 相关配置，将本项目 Skill 路径加入：

```text
/Users/df/Documents/clawcut/skills/ecom-highlight-skill/SKILL.md
```

如果 UI 要求填写 Skill 目录，则填写：

```text
/Users/df/Documents/clawcut/skills/ecom-highlight-skill
```

## 3. OpenClaw 任务输入

可以在 OpenClaw 聊天或任务输入中粘贴：

```text
请使用 EcomHighlightSkill 处理本地视频：

input: /Users/df/Documents/clawcut/data/input/ecom_cup_demo.mp4
instruction: 请从这段商品视频中剪出 30 秒小红书种草高光，突出商品外观、开箱过程和核心卖点；节奏自然，不要有黑屏。
target_duration: 30
target_platform: xiaohongshu
style: 种草
aspect_ratio: 9:16
output_dir: /Users/df/Documents/clawcut/outputs/openclaw_ecom_cup_demo_30s

要求：
1. 调用 EcomHighlightSkill 的 Python Runner。
2. 输出 highlight.mp4、segments.json、timeline_report.md、result.json。
3. 返回选中片段时间戳、评分、选择理由和 warning。
4. 如果失败，请返回 failure.json 路径和错误原因。
```

## 4. 成功判断

调度成功后应出现：

```text
outputs/openclaw_ecom_cup_demo_30s/highlight.mp4
outputs/openclaw_ecom_cup_demo_30s/segments.json
outputs/openclaw_ecom_cup_demo_30s/timeline_report.md
outputs/openclaw_ecom_cup_demo_30s/result.json
```

检查 `result.json`：

```bash
python3 -c 'import json; d=json.load(open("outputs/openclaw_ecom_cup_demo_30s/result.json")); print(d["skill"]); print(d["scoring"]); print(d.get("ark")); print(d.get("warnings"))'
```

期望：

```text
EcomHighlightSkill
ark
updated_segments 大于 0
warnings 为空或只有可解释 warning
```

## 5. 报告可写结论

如果上述流程跑通，可在最终报告中写：

```text
本项目已完成 OpenClaw 调度链路验证。OpenClaw 读取 EcomHighlightSkill 的 SKILL.md 后，调用本地 Python Runner 执行电商视频高光剪辑任务，成功生成 highlight.mp4、segments.json、timeline_report.md 和 result.json，完成输入、调度、Skill 执行、结果输出的端到端闭环。
```

