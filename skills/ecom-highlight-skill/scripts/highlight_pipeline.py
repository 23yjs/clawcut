#!/usr/bin/env python3
"""E-commerce short-video highlight clipping pipeline.

OpenClaw entrypoint for EcomHighlightSkill. The normal path is model-led:

1. ffmpeg/ffprobe extracts deterministic video evidence.
2. Ark Candidate Planner proposes semantic candidate boundaries.
3. Ark Candidate Judge scores candidates independently.
4. Ark Assembly Planner produces the final trim/concat plan.
5. ffmpeg only executes the validated assembly plan.

Heuristics are kept only for smoke tests and fallback.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
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


MACOS_HOST_PATH_PREFIXES = ("/Users/", "/Volumes/", "/Applications/")
OPENCLAW_WORKSPACE = "/home/node/.openclaw/workspace"
ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
SEVERE_QUALITY_ISSUES = {"black_screen", "severe_blur", "heavy_shaking", "product_not_visible", "irrelevant"}


@dataclass
class CandidateSegment:
    index: int
    segment_id: str
    start: float
    end: float
    source: str
    parent_start: Optional[float] = None
    parent_end: Optional[float] = None
    planner_reason: str = ""
    transcript: str = ""
    keyframes: List[str] = field(default_factory=list)
    clip_path: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SegmentJudgeResult:
    segment_id: str
    score: float
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    covered_points: List[str] = field(default_factory=list)
    quality_issues: List[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class AssemblySegment:
    segment_id: str
    output_order: int
    trim_start: float
    trim_end: float
    role: str = "highlight"
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.trim_end - self.trim_start)


@dataclass
class AssemblyPlan:
    assembly_strategy: str
    target_duration: float
    selected_segments: List[AssemblySegment]
    covered_points: List[str] = field(default_factory=list)
    total_duration: float = 0.0
    transition_style: str = "hard_cut"
    comment: str = ""


def run_command(cmd: List[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def file_to_data_url(path: Path, mime_type: Optional[str] = None) -> str:
    if not path.exists():
        raise PipelineError(f"Visual input file not found: {path}")
    guessed = mime_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{guessed};base64,{data}"


def build_multimodal_content(
    text: str,
    video_paths: Optional[List[Path]] = None,
    image_paths: Optional[List[Path]] = None,
) -> Tuple[List[Dict[str, Any]], str, int, int]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    video_count = 0
    image_count = 0
    for path in video_paths or []:
        content.append({"type": "video_url", "video_url": {"url": file_to_data_url(path, "video/mp4")}})
        video_count += 1
    for path in image_paths or []:
        mime_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        content.append({"type": "image_url", "image_url": {"url": file_to_data_url(path, mime_type)}})
        image_count += 1
    if video_count:
        visual_type = "video_data_url"
    elif image_count:
        visual_type = "image_data_url"
    else:
        visual_type = "text_only"
    return content, visual_type, video_count, image_count


def make_segment_id(index: int) -> str:
    return f"seg_{index:04d}"


def rounded_time(value: float) -> float:
    return round(max(0.0, value), 3)


def clamp(value: float, left: float, right: float) -> float:
    return max(left, min(value, right))


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


def running_in_openclaw_container() -> bool:
    cwd = str(Path.cwd())
    home = os.environ.get("HOME", "")
    return cwd.startswith(OPENCLAW_WORKSPACE) or home == "/home/node"


def openclaw_container_path_hint(raw_path: str, param_name: str) -> Optional[str]:
    if not running_in_openclaw_container() or not raw_path.startswith(MACOS_HOST_PATH_PREFIXES):
        return None
    return (
        f"`{param_name}` 当前传入的是 macOS 宿主机路径 `{raw_path}`，但 OpenClaw Skill "
        "运行在 Linux Docker 容器内，不能直接访问 `/Users/...`。请先把视频放到 "
        f"`{OPENCLAW_WORKSPACE}/data/input/`，然后在 OpenClaw 任务中使用相对路径，"
        "例如 `data/input/ecom_demo.mp4`；输出目录建议使用 `outputs/openclaw_ecom_demo`。"
    )


def ensure_openclaw_paths_are_container_visible(input_raw: str, output_raw: str) -> None:
    hints = [
        hint
        for hint in (
            openclaw_container_path_hint(input_raw, "input"),
            openclaw_container_path_hint(output_raw, "output_dir"),
        )
        if hint
    ]
    if hints:
        raise PipelineError(" ".join(hints))


def require_tool(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise PipelineError(f"Missing required tool `{name}`. Install ffmpeg first, for example: brew install ffmpeg")
    return resolved


def resolve_input_path(raw_path: str, warnings: List[str]) -> Path:
    path = Path(raw_path).expanduser()
    if path.exists():
        return path
    if path.name == "ecom_demo.mp4":
        fallback = path.with_name("ecom_cup_demo.MP4")
        if fallback.exists():
            warnings.append(f"`{raw_path}` not found; using demo fallback `{fallback}`.")
            return fallback
    raise PipelineError(f"Input video not found: {path}")


def parse_fraction(value: str) -> float:
    if "/" in value:
        left, right = value.split("/", 1)
        denominator = float(right)
        return float(left) / denominator if denominator else 0.0
    return float(value)


def probe_video_info(video_path: Path) -> Dict[str, Any]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate",
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
        video_stream = next(stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video")
        fps = parse_fraction(str(video_stream.get("r_frame_rate", "0/1")))
        return {
            "duration": round(duration, 3),
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "fps": round(fps, 3),
            "format_name": payload.get("format", {}).get("format_name", ""),
            "codec_name": video_stream.get("codec_name", ""),
        }
    except (StopIteration, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Unable to parse ffprobe video info: {exc}") from exc


def detect_scene_boundaries(video_path: Path, threshold: float, duration: float, min_scene_gap: float = 1.5) -> List[float]:
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
        return [0.0, rounded_time(duration)]

    times = [float(match) for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", f"{proc.stdout}\n{proc.stderr}")]
    boundaries = [0.0]
    for time_value in sorted(t for t in times if 0 < t < duration):
        if time_value - boundaries[-1] >= min_scene_gap:
            boundaries.append(rounded_time(time_value))
    if duration - boundaries[-1] >= 0.2:
        boundaries.append(rounded_time(duration))
    return boundaries


def sample_times(duration: float, interval: float, max_frames: int) -> List[float]:
    if duration <= 0 or max_frames <= 0:
        return []
    dense = [round(t, 3) for t in frange(0.0, duration, max(interval, 0.5))]
    if dense and dense[-1] < duration - 0.5:
        dense.append(round(duration, 3))
    if len(dense) <= max_frames:
        return dense
    step = (duration / max(max_frames - 1, 1)) if max_frames > 1 else duration / 2
    return [round(min(duration, i * step), 3) for i in range(max_frames)]


def frange(start: float, stop: float, step: float) -> Iterable[float]:
    cursor = start
    while cursor < stop:
        yield cursor
        cursor += step


def extract_frame(video_path: Path, timestamp: float, output_path: Path) -> Optional[str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(output_path),
    ]
    proc = run_command(cmd, timeout=120)
    return str(output_path) if proc.returncode == 0 and output_path.exists() else None


def create_planner_video(video_path: Path, output_dir: Path, args: argparse.Namespace) -> Path:
    output_path = output_dir / "planner_video.mp4"
    max_seconds = max(1.0, float(args.planner_video_max_seconds))
    vf = f"fps={args.planner_video_fps},scale={args.planner_video_width}:-2"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-t",
        f"{max_seconds:.3f}",
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30",
        str(output_path),
    ]
    proc = run_command(cmd, timeout=900)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown ffmpeg error"
        raise PipelineError(f"ffmpeg failed while creating planner_video.mp4: {detail}")
    return output_path


def create_candidate_clips(
    video_path: Path,
    candidates: List[CandidateSegment],
    output_dir: Path,
    args: argparse.Namespace,
) -> List[CandidateSegment]:
    clips_dir = output_dir / "candidate_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        clip_path = clips_dir / f"{candidate.segment_id}.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{candidate.start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{candidate.duration:.3f}",
            "-vf",
            f"scale={args.candidate_clip_width}:-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-an",
            str(clip_path),
        ]
        proc = run_command(cmd, timeout=300)
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown ffmpeg error"
            raise PipelineError(f"ffmpeg failed while creating candidate clip {candidate.segment_id}: {detail}")
        candidate.clip_path = str(clip_path)
    return candidates


def extract_planner_frames(video_path: Path, output_dir: Path, interval: float, max_frames: int) -> List[Dict[str, Any]]:
    duration = probe_video_info(video_path)["duration"]
    frames_dir = output_dir / "planner_frames"
    rows: List[Dict[str, Any]] = []
    for index, timestamp in enumerate(sample_times(duration, interval, max_frames)):
        frame_path = frames_dir / f"planner_{index:04d}_{timestamp:.2f}.jpg"
        path = extract_frame(video_path, timestamp, frame_path)
        if path:
            rows.append({"time": timestamp, "path": path})
    return rows


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
        return [
            {
                "start": float(row.get("start", 0)),
                "end": float(row.get("end", row.get("start", 0))),
                "text": str(row.get("text", row.get("content", ""))).strip(),
            }
            for row in rows
        ]
    if suffix in {".srt", ".vtt"}:
        parsed = []
        for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
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


def transcript_summary(transcript: List[Dict[str, Any]]) -> Dict[str, Any]:
    text = " ".join(str(row.get("text", "")).strip() for row in transcript if row.get("text"))
    return {"rows": transcript[:80], "text_preview": text[:3000], "row_count": len(transcript)}


def build_timeline_evidence(
    video_info: Dict[str, Any],
    scene_boundaries: List[float],
    planner_frames: List[Dict[str, Any]],
    transcript: List[Dict[str, Any]],
    args: argparse.Namespace,
    planner_video_path: Optional[Path] = None,
) -> Dict[str, Any]:
    return {
        "video_info": video_info,
        "planner_video": str(planner_video_path) if planner_video_path else None,
        "user_task": {
            "instruction": args.instruction,
            "target_duration": args.target_duration,
            "target_platform": args.target_platform,
            "style": args.style,
            "aspect_ratio": args.aspect_ratio,
        },
        "ffmpeg_scene_boundaries": scene_boundaries,
        "sampled_frames": planner_frames,
        "transcript": transcript_summary(transcript),
        "model_input_mode": args.visual_input_mode,
    }


def extract_json_object(text: str) -> Dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(text[start : end + 1])


def ark_chat_json(
    system_prompt: str,
    user_payload: Dict[str, Any],
    video_paths: Optional[List[Path]] = None,
    image_paths: Optional[List[Path]] = None,
    temperature: float = 0.1,
    timeout: int = 180,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    api_key = os.getenv("ARK_API_KEY")
    model = os.getenv("ARK_MODEL")
    base_url = os.getenv("ARK_BASE_URL", ARK_DEFAULT_BASE_URL).rstrip("/")
    metadata = {
        "called": False,
        "enabled": bool(api_key and model),
        "model": model,
        "base_url": base_url,
        "actual_visual_input_type": "text_only",
        "video_count": 0,
        "image_count": 0,
        "payload_size_bytes": 0,
        "raw_response_preview": None,
        "error": None,
    }
    if not api_key or not model:
        metadata["error"] = "ARK_API_KEY or ARK_MODEL is not set."
        return None, metadata["error"], metadata

    user_text = json.dumps(user_payload, ensure_ascii=False)
    try:
        if video_paths or image_paths:
            user_content, visual_type, video_count, image_count = build_multimodal_content(user_text, video_paths, image_paths)
        else:
            user_content, visual_type, video_count, image_count = user_text, "text_only", 0, 0
        body = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": temperature,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    except Exception as exc:
        metadata["actual_visual_input_type"] = "unsupported"
        metadata["error"] = str(exc)
        return None, str(exc), metadata

    metadata.update(
        {
            "actual_visual_input_type": visual_type,
            "video_count": video_count,
            "image_count": image_count,
            "payload_size_bytes": len(body),
        }
    )
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        metadata["called"] = True
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        metadata["raw_response_preview"] = content[:500]
        return extract_json_object(content), None, metadata
    except (urllib.error.URLError, OSError, KeyError, ValueError, json.JSONDecodeError, TimeoutError) as exc:
        metadata["error"] = str(exc)
        return None, str(exc), metadata


def ark_candidate_planner(
    timeline_evidence: Dict[str, Any],
    planner_video_path: Optional[Path],
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    if args.visual_input_mode in {"video", "clip"} and (not planner_video_path or not planner_video_path.exists()):
        return None, "planner_video.mp4 is required for Ark Candidate Planner.", {
            "called": False,
            "enabled": bool(os.getenv("ARK_API_KEY") and os.getenv("ARK_MODEL")),
            "model": os.getenv("ARK_MODEL"),
            "base_url": os.getenv("ARK_BASE_URL", ARK_DEFAULT_BASE_URL).rstrip("/"),
            "actual_visual_input_type": "unsupported",
            "video_count": 0,
            "image_count": 0,
            "payload_size_bytes": 0,
            "error": "planner_video.mp4 is missing",
        }
    system = (
        "你是电商短视频候选切片规划模型。你会看到原始视频的压缩预览版，"
        "请基于真实视频内容、用户指令、scene boundaries 和字幕规划语义候选边界。"
        "不要给分，不要决定最终剪辑方案。必须只输出 JSON。"
    )
    user = {
        "timeline_evidence": timeline_evidence,
        "output_schema": {
            "semantic_events": [
                {
                    "start": 0.0,
                    "end": 0.0,
                    "event_type": "unboxing | product_showcase | function_demo | usage_scene | selling_point | irrelevant | transition | other",
                    "description": "中文描述",
                    "candidate_priority": "high | medium | low",
                }
            ],
            "candidate_boundary_suggestions": [
                {"start": 0.0, "end": 0.0, "source": "llm_semantic_event", "reason": "中文理由"}
            ],
            "avoid_ranges": [{"start": 0.0, "end": 0.0, "reason": "中文理由"}],
        },
        "rules": ["不要输出 score", "不要输出最终 selected_segments", "时间戳必须在视频范围内", "reason 必须使用中文"],
    }
    video_paths = [planner_video_path] if planner_video_path and args.visual_input_mode in {"video", "clip"} else None
    image_paths = [Path(row["path"]) for row in timeline_evidence.get("sampled_frames", [])] if args.visual_input_mode == "keyframes" else None
    data, warning, meta = ark_chat_json(system, user, video_paths=video_paths, image_paths=image_paths)
    if warning:
        return None, f"Candidate Planner failed or unavailable: {warning}", meta
    return data, None, meta


def scene_ranges(boundaries: List[float]) -> List[Tuple[float, float]]:
    return [(left, right) for left, right in zip(boundaries, boundaries[1:]) if right > left]


def window_ranges(duration: float, window_size: float, window_stride: float) -> List[Tuple[float, float]]:
    rows = []
    cursor = 0.0
    window_size = max(1.0, window_size)
    window_stride = max(0.5, window_stride)
    while cursor < duration:
        end = min(duration, cursor + window_size)
        if end - cursor >= 1.0:
            rows.append((rounded_time(cursor), rounded_time(end)))
        if end >= duration:
            break
        cursor += window_stride
    return rows


def split_range(start: float, end: float, max_duration: float, stride: Optional[float] = None) -> Iterable[Tuple[float, float]]:
    if end - start <= max_duration:
        yield start, end
        return
    cursor = start
    step = stride or max_duration
    while cursor < end:
        chunk_end = min(end, cursor + max_duration)
        if chunk_end - cursor >= 1.0:
            yield cursor, chunk_end
        if chunk_end >= end:
            break
        cursor += step


def candidate_from_range(
    rows: List[Dict[str, Any]],
    start: float,
    end: float,
    source: str,
    duration: float,
    args: argparse.Namespace,
    reason: str,
    parent: Optional[Tuple[float, float]] = None,
) -> None:
    padded_start = clamp(start - args.boundary_padding, 0.0, duration)
    padded_end = clamp(end + args.boundary_padding, 0.0, duration)
    if padded_end - padded_start < args.min_candidate_duration:
        return
    for sub_start, sub_end in split_range(padded_start, padded_end, args.max_candidate_duration, args.window_stride):
        if sub_end - sub_start >= args.min_candidate_duration:
            rows.append(
                {
                    "start": rounded_time(sub_start),
                    "end": rounded_time(sub_end),
                    "source": source,
                    "planner_reason": reason,
                    "parent_start": rounded_time(parent[0]) if parent else None,
                    "parent_end": rounded_time(parent[1]) if parent else None,
                }
            )


def dedupe_candidate_rows(rows: List[Dict[str, Any]], max_candidates: int) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item["start"], item["end"], item["source"])):
        interval = (row["start"], row["end"])
        duplicate = False
        for existing in unique:
            existing_interval = (existing["start"], existing["end"])
            overlap = max(0.0, min(interval[1], existing_interval[1]) - max(interval[0], existing_interval[0]))
            union = max(interval[1], existing_interval[1]) - min(interval[0], existing_interval[0])
            if union > 0 and overlap / union >= 0.85:
                duplicate = True
                break
        if not duplicate:
            unique.append(row)
        if len(unique) >= max_candidates:
            break
    return unique


def generate_candidates(
    video_path: Path,
    args: argparse.Namespace,
    video_info: Dict[str, Any],
    scene_boundaries: List[float],
    llm_plan: Optional[Dict[str, Any]],
    transcript: List[Dict[str, Any]],
) -> List[CandidateSegment]:
    del video_path
    duration = float(video_info["duration"])
    rows: List[Dict[str, Any]] = []

    if args.candidate_mode in {"scene", "hybrid", "llm_hybrid"}:
        for start, end in scene_ranges(scene_boundaries):
            candidate_from_range(rows, start, end, "ffmpeg_scene", duration, args, "ffmpeg scene boundary", (start, end))

    if args.candidate_mode in {"window", "hybrid"}:
        for start, end in window_ranges(duration, args.window_size, args.window_stride):
            candidate_from_range(rows, start, end, "sliding_window", duration, args, "sliding window", (start, end))

    if args.candidate_mode == "llm_hybrid" and llm_plan:
        for item in llm_plan.get("candidate_boundary_suggestions", []):
            try:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", start))
            except (TypeError, ValueError):
                continue
            candidate_from_range(
                rows,
                start,
                end,
                str(item.get("source") or "llm_semantic_event"),
                duration,
                args,
                str(item.get("reason") or "LLM semantic candidate"),
                (start, end),
            )

    if not rows:
        for start, end in window_ranges(duration, args.window_size, args.window_stride):
            candidate_from_range(rows, start, end, "window_fallback", duration, args, "window fallback", (start, end))

    candidates = []
    for index, row in enumerate(dedupe_candidate_rows(rows, args.max_candidates)):
        candidates.append(
            CandidateSegment(
                index=index,
                segment_id=make_segment_id(index),
                start=row["start"],
                end=row["end"],
                source=row["source"],
                parent_start=row.get("parent_start"),
                parent_end=row.get("parent_end"),
                planner_reason=row.get("planner_reason", ""),
                transcript=transcript_for_segment(transcript, row["start"], row["end"]),
            )
        )
    return candidates


def keyframe_times(candidate: CandidateSegment, count: int) -> List[float]:
    if count <= 1:
        return [rounded_time((candidate.start + candidate.end) / 2)]
    base = [candidate.start + 0.3, (candidate.start + candidate.end) / 2, candidate.end - 0.3]
    if count <= 3:
        return [rounded_time(clamp(value, candidate.start, candidate.end)) for value in base[:count]]
    step = candidate.duration / max(count - 1, 1)
    return [rounded_time(clamp(candidate.start + i * step, candidate.start, candidate.end)) for i in range(count)]


def extract_candidate_keyframes(
    video_path: Path,
    candidates: List[CandidateSegment],
    frames_dir: Path,
    keyframes_per_segment: int = 3,
) -> List[CandidateSegment]:
    for candidate in candidates:
        candidate.keyframes = []
        for frame_index, timestamp in enumerate(keyframe_times(candidate, keyframes_per_segment)):
            frame_path = frames_dir / f"{candidate.segment_id}_{frame_index}_{timestamp:.2f}.jpg"
            path = extract_frame(video_path, timestamp, frame_path)
            if path:
                candidate.keyframes.append(path)
    return candidates


def candidate_to_dict(candidate: CandidateSegment) -> Dict[str, Any]:
    row = asdict(candidate)
    row["duration"] = round(candidate.duration, 3)
    return row


def build_candidate_evidence(candidates: List[CandidateSegment], args: argparse.Namespace, video_info: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user_task": {
            "instruction": args.instruction,
            "target_duration": args.target_duration,
            "target_platform": args.target_platform,
            "style": args.style,
            "aspect_ratio": args.aspect_ratio,
        },
        "video_info": video_info,
        "candidates": [candidate_to_dict(candidate) for candidate in candidates],
        "rules": [
            "候选证据不包含 heuristic initial_score、tags 或 score_breakdown",
            "Candidate Judge 必须独立判断每个片段",
        ],
    }


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


def heuristic_score(candidate: CandidateSegment, instruction: str, total_duration: float) -> SegmentJudgeResult:
    del instruction
    evidence_text = f"{candidate.transcript} {candidate.planner_reason} {candidate.source}".lower()
    keyword_hits = sum(1 for term in ECOMMERCE_TERMS if term.lower() in evidence_text)
    quality_penalty = min(sum(1 for term in QUALITY_NEGATIVE_TERMS if term in evidence_text) * 0.18, 0.45)
    transcript_density = min(len(candidate.transcript) / max(candidate.duration * 28, 1), 1.0)
    length_fit = 1.0 - min(abs(candidate.duration - 6.0) / 12.0, 1.0)
    center = (candidate.start + candidate.end) / 2.0
    early_middle_bias = 1.0 - min(abs(center / max(total_duration, 1.0) - 0.38) * 1.7, 1.0)
    source_bonus = 0.18 if candidate.source.startswith("llm") else 0.08 if "scene" in candidate.source else 0.0

    product_visibility = min(0.38 + keyword_hits * 0.08 + source_bonus, 1.0)
    selling_point_relevance = min(keyword_hits / 5.0 + source_bonus, 1.0)
    visual_quality = max(0.45 + length_fit * 0.30 + source_bonus - quality_penalty, 0.0)
    action_completeness = min(length_fit * 0.65 + 0.25 + source_bonus, 1.0)
    instruction_match = min((keyword_hits + (1 if candidate.source.startswith("llm") else 0)) / 5.0, 1.0)
    platform_fit = min(length_fit * 0.7 + early_middle_bias * 0.3, 1.0)
    breakdown = {
        "product_visibility": round(product_visibility, 4),
        "selling_point_relevance": round(selling_point_relevance, 4),
        "visual_quality": round(visual_quality, 4),
        "action_completeness": round(action_completeness, 4),
        "instruction_match": round(instruction_match, 4),
        "platform_fit": round(platform_fit, 4),
    }
    score = (
        product_visibility * 0.25
        + selling_point_relevance * 0.20
        + visual_quality * 0.20
        + action_completeness * 0.15
        + instruction_match * 0.10
        + platform_fit * 0.10
    )
    covered = [term for term in ["商品外观", "开箱", "功能演示", "使用场景", "卖点"] if term[:2] in evidence_text or term in evidence_text]
    issues = [term for term in QUALITY_NEGATIVE_TERMS if term in evidence_text]
    return SegmentJudgeResult(
        segment_id=candidate.segment_id,
        score=round(min(score, 1.0), 4),
        score_breakdown=breakdown,
        covered_points=covered,
        quality_issues=issues,
        reason="heuristic_fallback: 基于候选来源、字幕密度、片段时长和候选描述的兜底评分",
    )


def ark_candidate_judge(
    candidate_evidence: Dict[str, Any],
    candidates: List[CandidateSegment],
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    system = (
        "你是严格的电商短视频 Candidate Judge。你必须独立对每个候选切片评分，"
        "你会看到每个候选片段对应的真实短视频 clip。请基于 clip 内容独立评分，"
        "不要根据文件名或路径猜测，不要依赖 heuristic。必须只输出 JSON。"
    )
    metadata: Dict[str, Any] = {
        "called": False,
        "enabled": bool(os.getenv("ARK_API_KEY") and os.getenv("ARK_MODEL")),
        "model": os.getenv("ARK_MODEL"),
        "base_url": os.getenv("ARK_BASE_URL", ARK_DEFAULT_BASE_URL).rstrip("/"),
        "actual_visual_input_type": "video_data_url" if args.visual_input_mode in {"video", "clip"} else "image_data_url",
        "video_count": 0,
        "image_count": 0,
        "batch_count": 0,
        "payload_size_bytes": 0,
        "raw_response_preview": "",
        "error": None,
    }
    all_segments: List[Dict[str, Any]] = []
    by_id = {candidate.segment_id: candidate for candidate in candidates}
    evidence_rows = candidate_evidence.get("candidates", [])
    batch_size = max(1, int(args.judge_batch_size))
    for batch_start in range(0, len(evidence_rows), batch_size):
        batch = evidence_rows[batch_start : batch_start + batch_size]
        batch_ids = [str(row["segment_id"]) for row in batch]
        batch_candidates = [by_id[segment_id] for segment_id in batch_ids if segment_id in by_id]
        if args.visual_input_mode in {"video", "clip"}:
            video_paths = [Path(candidate.clip_path) for candidate in batch_candidates if candidate.clip_path]
            image_paths = None
            if len(video_paths) != len(batch_candidates):
                return None, "Candidate Judge requires candidate clip files, but at least one clip is missing.", metadata
        else:
            video_paths = None
            image_paths = [Path(path) for candidate in batch_candidates for path in candidate.keyframes]
        user = {
            "user_task": candidate_evidence["user_task"],
            "video_info": candidate_evidence["video_info"],
            "candidates": batch,
            "output_schema": {
                "segments": [
                    {
                        "segment_id": "seg_0001",
                        "score": 0.0,
                        "score_breakdown": {
                            "product_visibility": 0.0,
                            "selling_point_relevance": 0.0,
                            "visual_quality": 0.0,
                            "action_completeness": 0.0,
                            "instruction_match": 0.0,
                            "platform_fit": 0.0,
                        },
                        "covered_points": ["商品外观"],
                        "quality_issues": [],
                        "reason": "中文理由",
                    }
                ]
            },
            "rules": ["score 必须在 0 到 1", "只返回本 batch 中的 segment_id", "reason 必须中文"],
        }
        data, warning, batch_meta = ark_chat_json(system, user, video_paths=video_paths, image_paths=image_paths)
        metadata["called"] = metadata["called"] or bool(batch_meta.get("called"))
        metadata["video_count"] += int(batch_meta.get("video_count", 0))
        metadata["image_count"] += int(batch_meta.get("image_count", 0))
        metadata["batch_count"] += 1
        metadata["payload_size_bytes"] += int(batch_meta.get("payload_size_bytes", 0))
        metadata["raw_response_preview"] = batch_meta.get("raw_response_preview") or metadata["raw_response_preview"]
        if batch_meta.get("actual_visual_input_type") != "text_only":
            metadata["actual_visual_input_type"] = str(batch_meta.get("actual_visual_input_type"))
        if warning:
            metadata["error"] = warning
            return None, f"Candidate Judge failed or unavailable: {warning}", metadata
        all_segments.extend((data or {}).get("segments", []))
    return {"segments": all_segments}, None, metadata


def parse_judge_results(payload: Optional[Dict[str, Any]], candidates: List[CandidateSegment]) -> List[SegmentJudgeResult]:
    valid_ids = {candidate.segment_id for candidate in candidates}
    rows: List[SegmentJudgeResult] = []
    for item in (payload or {}).get("segments", []):
        segment_id = str(item.get("segment_id", ""))
        if segment_id not in valid_ids:
            continue
        try:
            score = clamp(float(item.get("score", 0.0)), 0.0, 1.0)
        except (TypeError, ValueError):
            score = 0.0
        rows.append(
            SegmentJudgeResult(
                segment_id=segment_id,
                score=round(score, 4),
                score_breakdown={
                    key: round(clamp(float(value), 0.0, 1.0), 4)
                    for key, value in (item.get("score_breakdown") or {}).items()
                    if isinstance(value, (int, float))
                },
                covered_points=[str(value) for value in item.get("covered_points", [])],
                quality_issues=[str(value) for value in item.get("quality_issues", [])],
                reason=str(item.get("reason", "")),
            )
        )
    return rows


def ark_assembly_planner(
    candidate_evidence: Dict[str, Any],
    judge_results: List[SegmentJudgeResult],
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    system = (
        "你是电商短视频 Assembly Planner。你根据候选片段、评分、目标时长和平台风格决定最终拼接方案。"
        "你只能选择已有候选片段范围内的 trim_start/trim_end。必须只输出 JSON。"
    )
    user = {
        "user_task": candidate_evidence["user_task"],
        "target_duration": args.target_duration,
        "target_platform": args.target_platform,
        "style": args.style,
        "candidates": candidate_evidence["candidates"],
        "judge_results": [asdict(result) for result in judge_results],
        "output_schema": {
            "assembly_strategy": "chronological_storyline",
            "target_duration": args.target_duration,
            "selected_segments": [
                {
                    "segment_id": "seg_0001",
                    "output_order": 1,
                    "trim_start": 0.0,
                    "trim_end": 0.0,
                    "role": "opening | unboxing | product_showcase | function_demo | usage_scene | selling_point | closing",
                    "reason": "中文理由",
                }
            ],
            "covered_points": ["商品外观", "功能演示"],
            "total_duration": 0.0,
            "transition_style": "hard_cut",
            "comment": "中文说明",
        },
        "rules": ["尽量接近 target_duration", "避免严重 quality_issues", "避免重复片段", "所有 reason 必须中文"],
    }
    data, warning, meta = ark_chat_json(system, user)
    if warning:
        return None, f"Assembly Planner failed or unavailable: {warning}", meta
    return data, None, meta


def judge_by_id(judge_results: List[SegmentJudgeResult]) -> Dict[str, SegmentJudgeResult]:
    return {result.segment_id: result for result in judge_results}


def validate_assembly_plan(
    plan: Optional[Dict[str, Any]],
    candidates: List[CandidateSegment],
    judge_results: List[SegmentJudgeResult],
    video_duration: float,
    target_duration: float,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    warnings: List[str] = []
    candidate_map = {candidate.segment_id: candidate for candidate in candidates}
    judge_map = judge_by_id(judge_results)
    normalized: Dict[str, Any] = {
        "assembly_strategy": str((plan or {}).get("assembly_strategy") or "model_plan"),
        "target_duration": target_duration,
        "selected_segments": [],
        "covered_points": list((plan or {}).get("covered_points") or []),
        "total_duration": 0.0,
        "transition_style": str((plan or {}).get("transition_style") or "hard_cut"),
        "comment": str((plan or {}).get("comment") or ""),
    }
    seen_orders = set()
    for index, item in enumerate((plan or {}).get("selected_segments", []), start=1):
        segment_id = str(item.get("segment_id", ""))
        candidate = candidate_map.get(segment_id)
        if not candidate:
            warnings.append(f"Unknown segment_id skipped: {segment_id}")
            continue
        try:
            order = int(item.get("output_order", index))
            trim_start = float(item.get("trim_start", candidate.start))
            trim_end = float(item.get("trim_end", candidate.end))
        except (TypeError, ValueError):
            warnings.append(f"Invalid trim fields skipped: {segment_id}")
            continue
        if order in seen_orders:
            warnings.append(f"Duplicate output_order normalized: {order}")
            order = index
        seen_orders.add(order)
        trim_start = clamp(trim_start, candidate.start, candidate.end)
        trim_end = clamp(trim_end, candidate.start, candidate.end)
        trim_start = clamp(trim_start, 0.0, video_duration)
        trim_end = clamp(trim_end, 0.0, video_duration)
        if trim_end - trim_start < 1.0:
            warnings.append(f"Segment too short skipped: {segment_id}")
            continue
        judge = judge_map.get(segment_id)
        if judge and any(issue in SEVERE_QUALITY_ISSUES for issue in judge.quality_issues):
            warnings.append(f"Selected segment has severe quality issue: {segment_id}")
        normalized["selected_segments"].append(
            {
                "segment_id": segment_id,
                "output_order": order,
                "trim_start": rounded_time(trim_start),
                "trim_end": rounded_time(trim_end),
                "role": str(item.get("role") or "highlight"),
                "reason": str(item.get("reason") or ""),
            }
        )

    normalized["selected_segments"] = sorted(normalized["selected_segments"], key=lambda item: item["output_order"])
    normalized["total_duration"] = round(sum(item["trim_end"] - item["trim_start"] for item in normalized["selected_segments"]), 3)
    if not normalized["selected_segments"]:
        warnings.append("Assembly plan has no usable selected_segments.")
        return False, warnings, normalized
    if target_duration > 0 and abs(normalized["total_duration"] - target_duration) / target_duration > 0.25:
        warnings.append("Assembly total duration differs from target duration by more than 25%.")
    return True, warnings, normalized


def ranges_overlap(left: Tuple[float, float], right: Tuple[float, float]) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def constrained_greedy_selector(
    candidates: List[CandidateSegment],
    judge_results: List[SegmentJudgeResult],
    target_duration: float,
) -> Dict[str, Any]:
    candidate_map = {candidate.segment_id: candidate for candidate in candidates}
    selected: List[Dict[str, Any]] = []
    used_ranges: List[Tuple[float, float]] = []
    total = 0.0
    ranked = sorted(judge_results, key=lambda result: result.score, reverse=True)
    for result in ranked:
        candidate = candidate_map.get(result.segment_id)
        if not candidate:
            continue
        if any(ranges_overlap((candidate.start, candidate.end), used) for used in used_ranges):
            continue
        trim_start = candidate.start
        trim_end = candidate.end
        if total + (trim_end - trim_start) > target_duration and target_duration - total >= 1.5:
            trim_end = trim_start + (target_duration - total)
        if trim_end - trim_start < 1.0:
            continue
        selected.append(
            {
                "segment_id": candidate.segment_id,
                "output_order": len(selected) + 1,
                "trim_start": rounded_time(trim_start),
                "trim_end": rounded_time(trim_end),
                "role": "highlight",
                "reason": f"heuristic_fallback: score={result.score:.3f}",
            }
        )
        used_ranges.append((candidate.start, candidate.end))
        total += trim_end - trim_start
        if total >= target_duration:
            break
    if not selected and candidates:
        first = candidates[0]
        selected.append(
            {
                "segment_id": first.segment_id,
                "output_order": 1,
                "trim_start": first.start,
                "trim_end": min(first.end, first.start + target_duration),
                "role": "highlight",
                "reason": "heuristic_fallback: first available candidate",
            }
        )
    return {
        "assembly_strategy": "constrained_greedy_selector",
        "target_duration": target_duration,
        "selected_segments": selected,
        "covered_points": sorted({point for result in judge_results for point in result.covered_points}),
        "total_duration": round(sum(item["trim_end"] - item["trim_start"] for item in selected), 3),
        "transition_style": "hard_cut",
        "comment": "程序兜底选择：按候选评分、重叠约束和目标时长贪心选择。",
    }


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


def render_from_assembly_plan(
    video_path: Path,
    assembly_plan: Dict[str, Any],
    output_dir: Path,
    aspect_ratio: str,
    keep_audio: bool,
) -> Path:
    selected = sorted(assembly_plan.get("selected_segments", []), key=lambda item: int(item.get("output_order", 0)))
    if not selected:
        raise PipelineError("No selected segments to render.")
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: List[Path] = []
    for order, segment in enumerate(selected, start=1):
        trim_start = float(segment["trim_start"])
        duration = float(segment["trim_end"]) - trim_start
        clip_path = clips_dir / f"clip_{order:03d}.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{trim_start:.3f}",
            "-i",
            str(video_path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
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
    proc = run_command(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-c", "copy", str(output_video)], timeout=900)
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown ffmpeg concat error"
        raise PipelineError(f"ffmpeg failed while concatenating clips: {detail}")
    return output_video


def selected_segments_for_output(
    assembly_plan: Dict[str, Any],
    candidates: List[CandidateSegment],
    judge_results: List[SegmentJudgeResult],
) -> List[Dict[str, Any]]:
    candidate_map = {candidate.segment_id: candidate for candidate in candidates}
    judge_map = judge_by_id(judge_results)
    rows = []
    for item in sorted(assembly_plan.get("selected_segments", []), key=lambda row: int(row.get("output_order", 0))):
        candidate = candidate_map.get(str(item.get("segment_id")))
        judge = judge_map.get(str(item.get("segment_id")))
        rows.append(
            {
                "index": candidate.index if candidate else None,
                "segment_id": item.get("segment_id"),
                "start": float(item["trim_start"]),
                "end": float(item["trim_end"]),
                "source": candidate.source if candidate else "",
                "score": judge.score if judge else 0.0,
                "score_breakdown": judge.score_breakdown if judge else {},
                "covered_points": judge.covered_points if judge else [],
                "quality_issues": judge.quality_issues if judge else [],
                "tags": judge.covered_points if judge else [],
                "reason": item.get("reason") or (judge.reason if judge else ""),
                "role": item.get("role", "highlight"),
                "planner_reason": candidate.planner_reason if candidate else "",
                "transcript": candidate.transcript if candidate else "",
            }
        )
    return rows


def write_markdown_report(
    output_dir: Path,
    task_id: str,
    input_path: Path,
    args: argparse.Namespace,
    output_video: Optional[Path],
    selected_segments: List[Dict[str, Any]],
    candidates: List[CandidateSegment],
    judge_results: List[SegmentJudgeResult],
    assembly_plan: Dict[str, Any],
    warnings: List[str],
    pipeline_meta: Dict[str, str],
    fallback_meta: Dict[str, Any],
) -> None:
    judge_map = judge_by_id(judge_results)
    lines = [
        f"# EcomHighlightSkill 时间线报告：{task_id}",
        "",
        f"- 原视频：`{input_path}`",
        f"- 剪辑指令：{args.instruction}",
        f"- 目标时长：{args.target_duration:.1f}s",
        f"- 目标平台：{args.target_platform}",
        f"- 剪辑风格：{args.style}",
        f"- 输出比例：{args.aspect_ratio}",
        f"- 输出视频：`{output_video}`",
        f"- 候选生成：{pipeline_meta.get('candidate_generation')}",
        f"- 候选评分：{pipeline_meta.get('candidate_scoring')}",
        f"- 拼接规划：{pipeline_meta.get('assembly_planning')}",
        f"- Fallback：{fallback_meta.get('used')}",
        "",
        "## 拼接策略",
        "",
        assembly_plan.get("comment", ""),
        "",
        "## 选中片段",
        "",
        "| # | 开始 | 结束 | 时长 | 分数 | 角色 | 选择理由 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for order, row in enumerate(selected_segments, start=1):
        lines.append(
            f"| {order} | {row['start']:.2f} | {row['end']:.2f} | {row['end'] - row['start']:.2f} | "
            f"{float(row.get('score', 0.0)):.3f} | {row.get('role', '')} | {row.get('reason', '')} |"
        )
    lines.extend(["", "## 未选择候选片段 Top 5", "", "| Segment | 开始 | 结束 | 分数 | 来源 | 原因 |", "|---|---:|---:|---:|---|---|"])
    selected_ids = {str(row.get("segment_id")) for row in selected_segments}
    rejected = [candidate for candidate in candidates if candidate.segment_id not in selected_ids]
    rejected = sorted(rejected, key=lambda candidate: judge_map.get(candidate.segment_id, SegmentJudgeResult(candidate.segment_id, 0.0)).score, reverse=True)[:5]
    for candidate in rejected:
        judge = judge_map.get(candidate.segment_id)
        lines.append(
            f"| {candidate.segment_id} | {candidate.start:.2f} | {candidate.end:.2f} | "
            f"{(judge.score if judge else 0.0):.3f} | {candidate.source} | {judge.reason if judge else candidate.planner_reason} |"
        )
    if warnings:
        lines.extend(["", "## 运行警告", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    report_text = "\n".join(lines) + "\n"
    (output_dir / "run_report.md").write_text(report_text, encoding="utf-8")
    (output_dir / "timeline_report.md").write_text(report_text, encoding="utf-8")


def candidate_stats(candidates: List[CandidateSegment]) -> Dict[str, Any]:
    by_source: Dict[str, int] = {}
    for candidate in candidates:
        by_source[candidate.source] = by_source.get(candidate.source, 0) + 1
    return {
        "total_candidates": len(candidates),
        "by_source": by_source,
        "avg_duration": round(sum(candidate.duration for candidate in candidates) / len(candidates), 3) if candidates else 0.0,
    }


def fallback_allowed(args: argparse.Namespace) -> bool:
    return args.run_mode == "fallback" or args.allow_fallback


def smoke_mode(args: argparse.Namespace) -> bool:
    return args.run_mode == "smoke"


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
    parser.add_argument("--run-mode", default="model", choices=["model", "fallback", "smoke"], help="model requires Ark; fallback allows heuristic fallback; smoke skips Ark.")
    parser.add_argument("--allow-fallback", action="store_true", help="Allow heuristic fallback when Ark fails.")
    parser.add_argument("--visual-input-mode", default="video", choices=["video", "clip", "keyframes"])
    parser.add_argument("--candidate-mode", default="llm_hybrid", choices=["scene", "window", "hybrid", "llm_hybrid"])
    parser.add_argument("--scoring-mode", default="model", choices=["model", "fallback", "heuristic"])
    parser.add_argument("--assembly-mode", default="model", choices=["model", "fallback", "heuristic"])
    parser.add_argument("--planner-video-max-seconds", type=float, default=180.0)
    parser.add_argument("--planner-video-fps", type=float, default=1.0)
    parser.add_argument("--planner-video-width", type=int, default=512)
    parser.add_argument("--candidate-clip-width", type=int, default=512)
    parser.add_argument("--judge-batch-size", type=int, default=2)
    parser.add_argument("--planner-frame-interval", type=float, default=2.0)
    parser.add_argument("--max-planner-frames", type=int, default=48)
    parser.add_argument("--keyframes-per-segment", type=int, default=3)
    parser.add_argument("--window-size", type=float, default=8.0)
    parser.add_argument("--window-stride", type=float, default=4.0)
    parser.add_argument("--boundary-padding", type=float, default=0.5)
    parser.add_argument("--min-candidate-duration", "--min-segment-duration", dest="min_candidate_duration", type=float, default=4.0)
    parser.add_argument("--max-candidate-duration", "--max-segment-duration", dest="max_candidate_duration", type=float, default=12.0)
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--scene-threshold", type=float, default=0.30, help="ffmpeg scene threshold.")
    parser.add_argument("--add-subtitles", action="store_true", help="Accepted but subtitle rendering is not implemented in MVP.")
    parser.add_argument("--no-audio", action="store_true", help="Remove audio from output video.")
    parser.add_argument("--dump-candidates-only", action="store_true", help="Stop after candidate evidence generation.")
    parser.add_argument("--no-render", action="store_true", help="Only produce metadata, not highlight.mp4.")
    return parser


def pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = time.time()
    load_env_file(Path(".env"))
    task_id = args.task_id or f"ecom_highlight_{uuid.uuid4().hex[:8]}"
    ensure_openclaw_paths_are_container_visible(args.input, args.output_dir)
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []
    fallback_meta: Dict[str, Any] = {"used": False, "stages": [], "reasons": []}
    pipeline_meta = {
        "candidate_generation": "",
        "candidate_scoring": "",
        "assembly_planning": "",
        "rendering": "ffmpeg",
    }

    input_path = resolve_input_path(args.input, warnings)
    if args.target_duration <= 0:
        raise PipelineError("--target-duration must be greater than 0.")
    if args.min_candidate_duration <= 0 or args.max_candidate_duration <= args.min_candidate_duration:
        raise PipelineError("Candidate duration bounds are invalid.")
    if args.add_subtitles:
        warnings.append("--add-subtitles is accepted but subtitle rendering is not implemented in MVP.")

    require_tool("ffmpeg")
    require_tool("ffprobe")

    transcript = load_transcript(Path(args.transcript).expanduser() if args.transcript else None)
    video_info = probe_video_info(input_path)
    scene_boundaries = detect_scene_boundaries(input_path, args.scene_threshold, float(video_info["duration"]))
    planner_video_path = create_planner_video(input_path, output_dir, args)
    planner_video_info = probe_video_info(planner_video_path)
    planner_frames = extract_planner_frames(input_path, output_dir, args.planner_frame_interval, args.max_planner_frames)
    timeline_evidence = build_timeline_evidence(video_info, scene_boundaries, planner_frames, transcript, args, planner_video_path)
    write_json(output_dir / "video_info.json", video_info)
    write_json(output_dir / "planner_video_info.json", planner_video_info)
    write_json(output_dir / "ffmpeg_scenes.json", {"boundaries": scene_boundaries, "threshold": args.scene_threshold})
    write_json(output_dir / "timeline_evidence.json", timeline_evidence)

    llm_plan: Optional[Dict[str, Any]] = None
    ark_metadata: Dict[str, Any] = {}
    if args.candidate_mode == "llm_hybrid" and not smoke_mode(args):
        llm_plan, planner_warning, planner_meta = ark_candidate_planner(timeline_evidence, planner_video_path, args)
        ark_metadata["candidate_planner"] = planner_meta
        if planner_warning:
            if not fallback_allowed(args):
                raise PipelineError(planner_warning)
            warnings.append(f"{planner_warning}; falling back to hybrid candidate generation.")
            fallback_meta["used"] = True
            fallback_meta["stages"].append("candidate_generation")
            fallback_meta["reasons"].append(planner_warning)
            pipeline_meta["candidate_generation"] = "ffmpeg_hybrid_fallback"
        else:
            pipeline_meta["candidate_generation"] = "ark_video_candidate_planner"
    else:
        if args.candidate_mode == "llm_hybrid" and smoke_mode(args):
            pipeline_meta["candidate_generation"] = "smoke_ffmpeg_hybrid"
        else:
            pipeline_meta["candidate_generation"] = f"ffmpeg_{args.candidate_mode}"
    write_json(output_dir / "llm_candidate_plan.json", llm_plan or {"semantic_events": [], "candidate_boundary_suggestions": [], "avoid_ranges": []})

    effective_candidate_mode = "hybrid" if args.candidate_mode == "llm_hybrid" and (not llm_plan or smoke_mode(args)) else args.candidate_mode
    original_candidate_mode = args.candidate_mode
    args.candidate_mode = effective_candidate_mode
    candidates = generate_candidates(input_path, args, video_info, scene_boundaries, llm_plan, transcript)
    args.candidate_mode = original_candidate_mode
    candidates = create_candidate_clips(input_path, candidates, output_dir, args)
    candidates = extract_candidate_keyframes(input_path, candidates, output_dir / "frames", args.keyframes_per_segment)
    candidate_evidence = build_candidate_evidence(candidates, args, video_info)
    write_json(output_dir / "candidates.json", {"candidates": [candidate_to_dict(candidate) for candidate in candidates]})
    write_json(output_dir / "candidate_evidence.json", candidate_evidence)
    write_json(output_dir / "candidate_stats.json", candidate_stats(candidates))

    if args.dump_candidates_only:
        payload = {
            "ok": True,
            "task_id": task_id,
            "skill": "EcomHighlightSkill",
            "input": str(input_path),
            "pipeline": pipeline_meta,
            "fallback": fallback_meta,
            "candidate_segments": [candidate_to_dict(candidate) for candidate in candidates],
            "warnings": warnings,
            "elapsed_seconds": round(time.time() - started_at, 3),
        }
        write_json(output_dir / "segments.json", payload)
        write_json(output_dir / "result.json", payload)
        return payload

    if smoke_mode(args) or args.scoring_mode == "heuristic":
        judge_results = [heuristic_score(candidate, args.instruction, float(video_info["duration"])) for candidate in candidates]
        pipeline_meta["candidate_scoring"] = "heuristic_smoke" if smoke_mode(args) else "heuristic"
    else:
        judge_payload, judge_warning, judge_meta = ark_candidate_judge(candidate_evidence, candidates, args)
        ark_metadata["candidate_judge"] = judge_meta
        judge_results = parse_judge_results(judge_payload, candidates)
        if judge_warning or len(judge_results) < len(candidates):
            reason = judge_warning or "Candidate Judge returned incomplete usable scores."
            if not fallback_allowed(args):
                raise PipelineError(reason)
            warnings.append(f"{reason}; using heuristic scoring fallback.")
            fallback_meta["used"] = True
            fallback_meta["stages"].append("candidate_scoring")
            fallback_meta["reasons"].append(reason)
            judge_results = [heuristic_score(candidate, args.instruction, float(video_info["duration"])) for candidate in candidates]
            pipeline_meta["candidate_scoring"] = "heuristic_fallback"
        else:
            pipeline_meta["candidate_scoring"] = "ark_video_candidate_judge"
    write_json(output_dir / "segment_scores.json", {"segments": [asdict(result) for result in judge_results]})

    if smoke_mode(args) or args.assembly_mode == "heuristic":
        assembly_plan = constrained_greedy_selector(candidates, judge_results, args.target_duration)
        pipeline_meta["assembly_planning"] = "heuristic_smoke" if smoke_mode(args) else "heuristic"
    else:
        assembly_payload, assembly_warning, assembly_meta = ark_assembly_planner(candidate_evidence, judge_results, args)
        ark_metadata["assembly_planner"] = assembly_meta
        ok, validation_warnings, normalized_plan = validate_assembly_plan(
            assembly_payload, candidates, judge_results, float(video_info["duration"]), args.target_duration
        )
        if assembly_warning or not ok:
            reason = assembly_warning or "; ".join(validation_warnings) or "Assembly plan is invalid."
            if not fallback_allowed(args):
                raise PipelineError(reason)
            warnings.append(f"{reason}; using constrained greedy assembly fallback.")
            fallback_meta["used"] = True
            fallback_meta["stages"].append("assembly_planning")
            fallback_meta["reasons"].append(reason)
            assembly_plan = constrained_greedy_selector(candidates, judge_results, args.target_duration)
            pipeline_meta["assembly_planning"] = "heuristic_fallback"
        else:
            warnings.extend(validation_warnings)
            assembly_plan = normalized_plan
            pipeline_meta["assembly_planning"] = "ark_assembly_planner"

    ok, validation_warnings, assembly_plan = validate_assembly_plan(
        assembly_plan, candidates, judge_results, float(video_info["duration"]), args.target_duration
    )
    if validation_warnings:
        warnings.extend(validation_warnings)
    if not ok:
        raise PipelineError("Validated assembly plan has no usable selected segments.")
    write_json(output_dir / "assembly_plan.json", assembly_plan)
    write_json(output_dir / "assembly_validation.json", {"ok": ok, "warnings": validation_warnings, "assembly_plan": assembly_plan})

    output_video: Optional[Path] = output_dir / "highlight.mp4"
    if not args.no_render:
        output_video = render_from_assembly_plan(input_path, assembly_plan, output_dir, args.aspect_ratio, keep_audio=not args.no_audio)
    else:
        output_video = None

    selected_segments = selected_segments_for_output(assembly_plan, candidates, judge_results)
    payload = {
        "ok": True,
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
        "source_duration": video_info["duration"],
        "pipeline": pipeline_meta,
        "fallback": fallback_meta,
        "scoring": pipeline_meta["candidate_scoring"],
        "ark": ark_metadata,
        "output_video": str(output_video) if output_video else None,
        "selected_segments": selected_segments,
        "candidate_segments": [candidate_to_dict(candidate) for candidate in candidates],
        "judge_results": [asdict(result) for result in judge_results],
        "assembly_plan": assembly_plan,
        "warnings": warnings,
        "elapsed_seconds": round(time.time() - started_at, 3),
    }
    write_json(output_dir / "segments.json", payload)
    write_json(output_dir / "result.json", payload)
    write_markdown_report(
        output_dir,
        task_id,
        input_path,
        args,
        output_video,
        selected_segments,
        candidates,
        judge_results,
        assembly_plan,
        warnings,
        pipeline_meta,
        fallback_meta,
    )
    return payload


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser()
    try:
        result = pipeline(args)
    except Exception as exc:
        failure = {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "message": str(exc),
            "input": args.input,
            "instruction": args.instruction,
            "target_duration": args.target_duration,
        }
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            failure_path = output_dir / "failure.json"
        except Exception:
            failure_path = Path("outputs") / "failures" / f"failure_{uuid.uuid4().hex[:8]}.json"
            failure["fallback_failure_path"] = str(failure_path)
        write_json(failure_path, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": str(output_dir / "result.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
