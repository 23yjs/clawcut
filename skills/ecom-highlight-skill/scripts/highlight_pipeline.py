#!/usr/bin/env python3
"""E-commerce short-video highlight clipping pipeline.

The script is intentionally dependency-light. It needs ffmpeg/ffprobe for real
video processing and can optionally use Ark's OpenAI-compatible chat endpoint
for e-commerce segment scoring.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class PipelineError(RuntimeError):
    """Expected pipeline failure with an actionable message."""


@dataclass
class CandidateSegment:
    index: int
    start: float
    end: float
    source: str
    score: float = 0.0
    reason: str = ""
    transcript: str = ""
    tags: List[str] = field(default_factory=list)
    score_breakdown: Dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def run_command(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_tool(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise PipelineError(
            f"Missing required tool `{name}`. Install ffmpeg first, for example: brew install ffmpeg"
        )
    return resolved


def probe_duration(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    proc = run_command(cmd, timeout=60)
    if proc.returncode != 0:
        raise PipelineError(f"ffprobe failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout)
        duration = float(payload["format"]["duration"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Unable to parse video duration from ffprobe output: {exc}") from exc
    if duration <= 0:
        raise PipelineError("Video duration is empty or invalid.")
    return duration


def split_long_segment(start: float, end: float, max_duration: float) -> Iterable[Tuple[float, float]]:
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + max_duration)
        if chunk_end - cursor > 0.1:
            yield cursor, chunk_end
        cursor = chunk_end


def normalize_segments(
    boundaries: List[float],
    duration: float,
    min_duration: float,
    max_duration: float,
    source: str,
) -> List[CandidateSegment]:
    cleaned = sorted({round(t, 3) for t in boundaries if 0 <= t <= duration})
    if not cleaned or cleaned[0] != 0:
        cleaned.insert(0, 0.0)
    if cleaned[-1] != round(duration, 3):
        cleaned.append(round(duration, 3))

    raw: List[Tuple[float, float]] = []
    for left, right in zip(cleaned, cleaned[1:]):
        if right - left >= min_duration:
            raw.extend(split_long_segment(left, right, max_duration))

    if not raw:
        return []

    merged: List[Tuple[float, float]] = []
    for start, end in raw:
        if not merged:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        if prev_end - prev_start < min_duration:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))

    return [
        CandidateSegment(index=i, start=round(s, 3), end=round(e, 3), source=source)
        for i, (s, e) in enumerate(merged)
        if e - s >= min_duration
    ]


def detect_scene_segments(
    video_path: Path,
    duration: float,
    threshold: float,
    min_duration: float,
    max_duration: float,
) -> Tuple[List[CandidateSegment], Optional[str]]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(video_path),
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    proc = run_command(cmd, timeout=900)
    if proc.returncode != 0:
        warning = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "scene detection failed"
        return [], f"Scene detection failed; falling back to fixed windows. Detail: {warning}"

    text = f"{proc.stdout}\n{proc.stderr}"
    times = [float(match) for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", text)]
    times = [t for t in times if 0 < t < duration]
    segments = normalize_segments(times, duration, min_duration, max_duration, "scene")
    if len(segments) < 2:
        return [], "Scene detection found too few boundaries; falling back to fixed windows."
    return segments, None


def fixed_window_segments(duration: float, min_duration: float, max_duration: float) -> List[CandidateSegment]:
    window = max(min_duration, min(max_duration, duration / 4 if duration > 60 else max_duration))
    boundaries = [0.0]
    cursor = 0.0
    while cursor < duration:
        cursor = min(duration, cursor + window)
        boundaries.append(cursor)
    return normalize_segments(boundaries, duration, min_duration, max_duration, "fixed_window")


def parse_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    return float(value)


def load_transcript(path: Optional[Path]) -> List[Dict[str, Any]]:
    if not path:
        return []
    if not path.exists():
        raise PipelineError(f"Transcript file not found: {path}")

    text = path.read_text(encoding="utf-8", errors="ignore")
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(text)
        rows = payload.get("segments", payload) if isinstance(payload, dict) else payload
        parsed = []
        for row in rows:
            parsed.append(
                {
                    "start": float(row.get("start", 0)),
                    "end": float(row.get("end", row.get("start", 0))),
                    "text": str(row.get("text", row.get("content", ""))).strip(),
                }
            )
        return parsed

    if suffix in {".srt", ".vtt"}:
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
        parsed = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            time_line = next((line for line in lines if "-->" in line), "")
            if not time_line:
                continue
            left, right = [item.strip().split()[0] for item in time_line.split("-->", 1)]
            body = " ".join(line for line in lines if line != time_line and not line.isdigit())
            parsed.append({"start": parse_timestamp(left), "end": parse_timestamp(right), "text": body})
        return parsed

    return [{"start": 0.0, "end": 10**9, "text": text.strip()}]


def transcript_for_segment(transcript: List[Dict[str, Any]], start: float, end: float) -> str:
    snippets = []
    for row in transcript:
        row_start = float(row.get("start", 0))
        row_end = float(row.get("end", row_start))
        if max(start, row_start) < min(end, row_end):
            snippets.append(str(row.get("text", "")).strip())
    return " ".join(item for item in snippets if item)[:1200]


ECOMMERCE_TERMS = [
    "商品",
    "产品",
    "外观",
    "开箱",
    "展示",
    "上手",
    "试用",
    "体验",
    "卖点",
    "功能",
    "材质",
    "质感",
    "保温",
    "防水",
    "便携",
    "容量",
    "价格",
    "优惠",
    "种草",
    "带货",
    "小红书",
    "抖音",
    "淘宝",
    "场景",
    "对比",
    "效果",
    "product",
    "unboxing",
    "showcase",
    "selling",
    "feature",
    "demo",
]


QUALITY_NEGATIVE_TERMS = ["黑屏", "模糊", "抖动", "杂音", "遮挡", "看不清", "无关", "重复"]


def instruction_terms(instruction: str) -> List[str]:
    domain_terms = [
        "高光",
        "精彩",
        "关键",
        "重点",
        "动作",
        "卖点",
        "产品",
        "展示",
        "情绪",
        "传播",
        "短视频",
        *ECOMMERCE_TERMS,
        "highlight",
        "key",
        "important",
        "action",
        "summary",
    ]
    alnum_terms = re.findall(r"[A-Za-z0-9_]{3,}", instruction.lower())
    return sorted(set([term for term in domain_terms if term in instruction] + alnum_terms))


def heuristic_score(
    segment: CandidateSegment,
    instruction: str,
    total_duration: float,
    total_segments: int,
) -> CandidateSegment:
    text = segment.transcript.lower()
    combined_text = f"{instruction} {segment.transcript}".lower()
    terms = instruction_terms(instruction)
    keyword_hits = sum(1 for term in terms if term.lower() in text)
    ecom_hits = sum(1 for term in ECOMMERCE_TERMS if term.lower() in combined_text)
    quality_penalty = min(sum(1 for term in QUALITY_NEGATIVE_TERMS if term in combined_text) * 0.18, 0.45)
    transcript_density = min(len(segment.transcript) / max(segment.duration * 28, 1), 1.0)
    length_fit = 1.0 - min(abs(segment.duration - 6.0) / 12.0, 1.0)
    center = (segment.start + segment.end) / 2.0
    early_middle_bias = 1.0 - min(abs(center / total_duration - 0.38) * 1.7, 1.0)
    scene_bonus = 0.12 if segment.source == "scene" else 0.0

    product_visibility = min(0.45 + ecom_hits * 0.08 + scene_bonus, 1.0)
    selling_point_relevance = min(keyword_hits / 4.0 + ecom_hits * 0.05, 1.0)
    visual_quality = max(0.35 + scene_bonus + length_fit * 0.35 - quality_penalty, 0.0)
    action_completeness = min(length_fit * 0.65 + scene_bonus + 0.20, 1.0)
    audio_text_info = transcript_density
    instruction_match = min((keyword_hits + ecom_hits) / 6.0, 1.0)
    pacing_fit = min(length_fit * 0.75 + early_middle_bias * 0.25, 1.0)

    breakdown = {
        "product_visibility": round(product_visibility, 4),
        "selling_point_relevance": round(selling_point_relevance, 4),
        "visual_quality": round(visual_quality, 4),
        "action_completeness": round(action_completeness, 4),
        "audio_text_information": round(audio_text_info, 4),
        "instruction_match": round(instruction_match, 4),
        "pacing_fit": round(pacing_fit, 4),
    }

    score = (
        product_visibility * 0.25
        + selling_point_relevance * 0.20
        + visual_quality * 0.15
        + action_completeness * 0.15
        + audio_text_info * 0.10
        + instruction_match * 0.10
        + pacing_fit * 0.05
    )
    segment.score = round(min(score, 1.0), 4)
    segment.score_breakdown = breakdown
    tags = []
    if product_visibility >= 0.6:
        tags.append("product_visible")
    if selling_point_relevance >= 0.5:
        tags.append("selling_point")
    if action_completeness >= 0.65:
        tags.append("complete_action")
    if visual_quality >= 0.65:
        tags.append("clean_frame")
    if audio_text_info >= 0.4:
        tags.append("informative_speech")
    segment.tags = tags

    reason_parts = ["电商启发式评分"]
    if keyword_hits:
        reason_parts.append(f"命中 {keyword_hits} 个指令关键词")
    if ecom_hits:
        reason_parts.append(f"命中 {ecom_hits} 个电商语义词")
    if segment.transcript:
        reason_parts.append("包含字幕/转写文本")
    if total_segments <= 1:
        reason_parts.append("唯一候选片段")
    segment.reason = "; ".join(reason_parts)
    return segment


def extract_json_object(text: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start : end + 1])


def ark_score_segments(
    candidates: List[CandidateSegment],
    instruction: str,
    target_duration: float,
    target_platform: str,
    style: str,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    api_key = os.getenv("ARK_API_KEY")
    model = os.getenv("ARK_MODEL")
    base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    if not api_key or not model:
        return False, "ARK_API_KEY or ARK_MODEL is not set; using heuristic scoring.", {
            "enabled": False,
            "updated_segments": 0,
        }

    compact_candidates = [
        {
            "index": seg.index,
            "start": seg.start,
            "end": seg.end,
            "duration": round(seg.duration, 3),
            "transcript": seg.transcript[:500],
            "initial_score": seg.score,
            "score_breakdown": seg.score_breakdown,
            "tags": seg.tags,
        }
        for seg in candidates
    ]
    system = (
        "你是严格的电商短视频高光片段评审模型。"
        "请根据商品种草、开箱、展示、测评、带货转化价值，对每个候选片段打 0 到 1 分。"
        "优先选择商品主体清晰、卖点明确、动作完整、节奏适合短视频传播的片段，"
        "避开黑屏、模糊、抖动、商品不可见、无关闲聊等低质片段。"
        "必须只输出 JSON，不要输出 Markdown。所有 reason 必须使用简体中文。"
    )
    user = {
        "instruction": instruction,
        "target_duration": target_duration,
        "target_platform": target_platform,
        "style": style,
        "candidates": compact_candidates,
        "output_schema": {
            "segments": [
                {
                    "index": 0,
                    "score": 0.0,
                    "reason": "中文理由，说明是否适合入选以及依据",
                }
            ]
        },
        "rules": [
            "reason 必须是中文",
            "score 必须是 0 到 1 的数字",
            "每个候选片段都要返回一个评分",
            "不要新增候选片段，不要修改 index",
        ],
    }
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        judged = extract_json_object(content)
        by_index = {int(item["index"]): item for item in judged.get("segments", [])}
        updated = 0
        for seg in candidates:
            item = by_index.get(seg.index)
            if not item:
                continue
            seg.score = round(float(item.get("score", seg.score)), 4)
            seg.reason = f"ark_judge: {item.get('reason', seg.reason)}"
            updated += 1
        metadata = {
            "enabled": True,
            "model": model,
            "base_url": base_url,
            "updated_segments": updated,
            "raw_response_preview": content[:500],
        }
        if updated == 0:
            return True, "Ark scoring returned no usable segment updates; keeping heuristic scores.", metadata
        return True, None, metadata
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        return False, f"Ark scoring failed; using heuristic scores. Detail: {exc}", {
            "enabled": True,
            "model": model,
            "base_url": base_url,
            "updated_segments": 0,
            "error": str(exc),
        }


def ranges_overlap(a: CandidateSegment, b: CandidateSegment) -> bool:
    return max(a.start, b.start) < min(a.end, b.end)


def select_segments(candidates: List[CandidateSegment], target_duration: float) -> List[CandidateSegment]:
    if not candidates:
        return []
    ranked = sorted(candidates, key=lambda item: (item.score, item.duration), reverse=True)
    selected: List[CandidateSegment] = []
    total = 0.0

    for seg in ranked:
        if any(ranges_overlap(seg, chosen) for chosen in selected):
            continue
        if total >= target_duration:
            break
        selected.append(seg)
        total += seg.duration

    if not selected:
        selected = [ranked[0]]

    selected = sorted(selected, key=lambda item: item.start)
    overflow = sum(seg.duration for seg in selected) - target_duration
    if overflow > 2.0 and selected[-1].duration - overflow >= 3.0:
        last = selected[-1]
        selected[-1] = CandidateSegment(
            index=last.index,
            start=last.start,
            end=round(last.end - overflow, 3),
            source=last.source,
            score=last.score,
            reason=f"{last.reason}; trimmed to target duration",
            transcript=last.transcript,
            tags=last.tags,
            score_breakdown=last.score_breakdown,
        )
    return selected


def video_filter_for_aspect_ratio(aspect_ratio: str) -> Optional[str]:
    presets = {
        "9:16": "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "16:9": "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "1:1": "scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2",
    }
    return presets.get(aspect_ratio)


def concat_file_line(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"


def render_highlight(
    video_path: Path,
    selected: List[CandidateSegment],
    output_dir: Path,
    aspect_ratio: str,
    keep_audio: bool,
) -> Path:
    if not selected:
        raise PipelineError("No selected segments to render.")
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: List[Path] = []

    for order, seg in enumerate(selected, start=1):
        clip_path = clips_dir / f"clip_{order:03d}.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{seg.start:.3f}",
            "-to",
            f"{seg.end:.3f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
        ]
        vf = video_filter_for_aspect_ratio(aspect_ratio)
        if vf:
            cmd.extend(["-vf", vf])
        if keep_audio:
            cmd.extend(["-map", "0:a:0?", "-c:a", "aac"])
        else:
            cmd.append("-an")
        cmd.append(str(clip_path))
        proc = run_command(cmd, timeout=900)
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown ffmpeg error"
            raise PipelineError(f"ffmpeg failed while rendering segment {order}: {detail}")
        clip_paths.append(clip_path)

    concat_path = clips_dir / "concat.txt"
    concat_path.write_text("\n".join(concat_file_line(path) for path in clip_paths), encoding="utf-8")
    output_video = output_dir / "highlight.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-c",
        "copy",
        str(output_video),
    ]
    proc = run_command(cmd, timeout=900)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown ffmpeg concat error"
        raise PipelineError(f"ffmpeg failed while concatenating clips: {detail}")
    return output_video


def write_markdown_report(
    output_dir: Path,
    task_id: str,
    input_path: Path,
    instruction: str,
    target_duration: float,
    target_platform: str,
    style: str,
    aspect_ratio: str,
    selected: List[CandidateSegment],
    candidates: List[CandidateSegment],
    output_video: Path,
    warnings: List[str],
) -> None:
    lines = [
        f"# EcomHighlightSkill 时间线报告：{task_id}",
        "",
        f"- 原视频：`{input_path}`",
        f"- 剪辑指令：{instruction}",
        f"- 目标时长：{target_duration:.1f}s",
        f"- 目标平台：{target_platform}",
        f"- 剪辑风格：{style}",
        f"- 输出比例：{aspect_ratio}",
        f"- 输出视频：`{output_video}`",
        "",
        "## 选中片段",
        "",
        "| # | 开始 | 结束 | 时长 | 分数 | 标签 | 选择理由 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for order, seg in enumerate(selected, start=1):
        lines.append(
            f"| {order} | {seg.start:.2f} | {seg.end:.2f} | {seg.duration:.2f} | {seg.score:.3f} | "
            f"{', '.join(seg.tags)} | {seg.reason} |"
        )
    lines.extend(["", "## 未选择候选片段 Top 5", "", "| # | 开始 | 结束 | 分数 | 原因 |", "|---|---:|---:|---:|---|"])
    selected_indexes = {seg.index for seg in selected}
    rejected = [seg for seg in sorted(candidates, key=lambda item: item.score, reverse=True) if seg.index not in selected_indexes][:5]
    for seg in rejected:
        lines.append(f"| {seg.index} | {seg.start:.2f} | {seg.end:.2f} | {seg.score:.3f} | 分数或时长约束未入选：{seg.reason} |")
    if warnings:
        lines.extend(["", "## 运行警告", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    report_text = "\n".join(lines) + "\n"
    (output_dir / "run_report.md").write_text(report_text, encoding="utf-8")
    (output_dir / "timeline_report.md").write_text(report_text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clip e-commerce highlight segments from a product video.")
    parser.add_argument("--input", required=True, help="Path to source video.")
    parser.add_argument("--instruction", required=True, help="Natural language clipping objective.")
    parser.add_argument("--target-duration", type=float, required=True, help="Desired highlight duration in seconds.")
    parser.add_argument("--output-dir", required=True, help="Directory for generated artifacts.")
    parser.add_argument("--task-id", default=None, help="Stable task id.")
    parser.add_argument("--transcript", default=None, help="Optional transcript path: json, srt, vtt, or txt.")
    parser.add_argument("--target-platform", default="xiaohongshu", help="Target platform: xiaohongshu, douyin, taobao, etc.")
    parser.add_argument("--style", default="种草", help="Editing style, e.g. 种草、带货、开箱、测评.")
    parser.add_argument("--aspect-ratio", default="source", choices=["source", "9:16", "16:9", "1:1"], help="Output aspect ratio.")
    parser.add_argument("--add-subtitles", action="store_true", help="Reserve option for subtitle rendering.")
    parser.add_argument("--no-audio", action="store_true", help="Remove audio from output video.")
    parser.add_argument("--scene-threshold", type=float, default=0.30, help="ffmpeg scene threshold.")
    parser.add_argument("--min-segment-duration", type=float, default=4.0, help="Minimum candidate duration.")
    parser.add_argument("--max-segment-duration", type=float, default=20.0, help="Maximum candidate duration.")
    parser.add_argument("--no-render", action="store_true", help="Only produce segment metadata, not highlight.mp4.")
    return parser


def pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = time.time()
    load_env_file(Path(".env"))
    task_id = args.task_id or f"ecom_highlight_{uuid.uuid4().hex[:8]}"
    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise PipelineError(f"Input video not found: {input_path}")
    if args.target_duration <= 0:
        raise PipelineError("--target-duration must be greater than 0.")
    if args.min_segment_duration <= 0 or args.max_segment_duration <= args.min_segment_duration:
        raise PipelineError("Segment duration bounds are invalid.")

    require_tool("ffmpeg")
    require_tool("ffprobe")

    warnings: List[str] = []
    duration = probe_duration(input_path)
    transcript = load_transcript(Path(args.transcript).expanduser() if args.transcript else None)

    candidates, warning = detect_scene_segments(
        input_path,
        duration,
        args.scene_threshold,
        args.min_segment_duration,
        args.max_segment_duration,
    )
    if warning:
        warnings.append(warning)
    if not candidates:
        candidates = fixed_window_segments(duration, args.min_segment_duration, args.max_segment_duration)

    for seg in candidates:
        seg.transcript = transcript_for_segment(transcript, seg.start, seg.end)
        heuristic_score(seg, args.instruction, duration, len(candidates))

    used_ark, ark_warning, ark_metadata = ark_score_segments(
        candidates,
        args.instruction,
        args.target_duration,
        args.target_platform,
        args.style,
    )
    if ark_warning:
        warnings.append(ark_warning)

    selected = select_segments(candidates, args.target_duration)
    output_video = output_dir / "highlight.mp4"
    if not args.no_render:
        output_video = render_highlight(
            input_path,
            selected,
            output_dir,
            args.aspect_ratio,
            keep_audio=not args.no_audio,
        )

    payload = {
        "task_id": task_id,
        "skill": "EcomHighlightSkill",
        "input": str(input_path),
        "instruction": args.instruction,
        "target_duration": args.target_duration,
        "target_platform": args.target_platform,
        "style": args.style,
        "aspect_ratio": args.aspect_ratio,
        "add_subtitles": args.add_subtitles,
        "keep_audio": not args.no_audio,
        "source_duration": duration,
        "scoring": "ark" if used_ark else "heuristic",
        "ark": ark_metadata,
        "output_video": str(output_video) if not args.no_render else None,
        "selected_segments": [asdict(seg) for seg in selected],
        "candidate_segments": [asdict(seg) for seg in candidates],
        "warnings": warnings,
        "elapsed_seconds": round(time.time() - started_at, 3),
    }
    write_json(output_dir / "segments.json", payload)
    write_json(output_dir / "result.json", payload)
    write_markdown_report(
        output_dir,
        task_id,
        input_path,
        args.instruction,
        args.target_duration,
        args.target_platform,
        args.style,
        args.aspect_ratio,
        selected,
        candidates,
        output_video,
        warnings,
    )
    return payload


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser()
    try:
        result = pipeline(args)
    except Exception as exc:
        output_dir.mkdir(parents=True, exist_ok=True)
        failure = {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "message": str(exc),
            "input": args.input,
            "instruction": args.instruction,
            "target_duration": args.target_duration,
        }
        write_json(output_dir / "failure.json", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": result["result_path"] if "result_path" in result else str(output_dir / "result.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
