#!/usr/bin/env python3
"""Evaluate e-commerce highlight segments against Ground Truth labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


Interval = Tuple[float, float]


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_segments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("selected_segments") or payload.get("segments") or payload.get("gt_highlight_segments") or []
    normalized = []
    for row in rows:
        start = float(row["start"])
        end = float(row["end"])
        if end > start:
            normalized.append({**row, "start": start, "end": end})
    return normalized


def interval_iou(a: Interval, b: Interval) -> float:
    overlap = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return overlap / union if union > 0 else 0.0


def intersection_duration(a: Interval, b: Interval) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def best_iou(pred: Dict[str, Any], truth: List[Dict[str, Any]]) -> float:
    pred_interval = (pred["start"], pred["end"])
    return max((interval_iou(pred_interval, (gt["start"], gt["end"])) for gt in truth), default=0.0)


def mean_best_iou(predictions: List[Dict[str, Any]], truth: List[Dict[str, Any]]) -> float:
    if not predictions or not truth:
        return 0.0
    return sum(best_iou(pred, truth) for pred in predictions) / len(predictions)


def recall_at_k(
    predictions: List[Dict[str, Any]],
    truth: List[Dict[str, Any]],
    k: int,
    threshold: float,
) -> float:
    if not truth:
        return 0.0
    top_k = sorted(predictions, key=lambda row: float(row.get("score", 0.0)), reverse=True)[:k]
    hits = 0
    for gt in truth:
        gt_interval = (gt["start"], gt["end"])
        matched = any(interval_iou((pred["start"], pred["end"]), gt_interval) >= threshold for pred in top_k)
        hits += 1 if matched else 0
    return hits / len(truth)


def coverage(predictions: List[Dict[str, Any]], truth: List[Dict[str, Any]]) -> float:
    gt_total = sum(gt["end"] - gt["start"] for gt in truth)
    if gt_total <= 0:
        return 0.0
    covered = 0.0
    for gt in truth:
        gt_interval = (gt["start"], gt["end"])
        overlaps = [intersection_duration((pred["start"], pred["end"]), gt_interval) for pred in predictions]
        covered += min(sum(overlaps), gt["end"] - gt["start"])
    return covered / gt_total


def temporal_precision(predictions: List[Dict[str, Any]], truth: List[Dict[str, Any]]) -> float:
    pred_total = sum(pred["end"] - pred["start"] for pred in predictions)
    if pred_total <= 0:
        return 0.0
    overlap = 0.0
    for pred in predictions:
        pred_interval = (pred["start"], pred["end"])
        overlaps = [intersection_duration(pred_interval, (gt["start"], gt["end"])) for gt in truth]
        overlap += min(sum(overlaps), pred["end"] - pred["start"])
    return overlap / pred_total


def harmonic_mean(left: float, right: float) -> float:
    if left + right <= 0:
        return 0.0
    return 2 * left * right / (left + right)


def duration_error(predictions: List[Dict[str, Any]], target_duration: float) -> float:
    if target_duration <= 0:
        return 0.0
    predicted = sum(pred["end"] - pred["start"] for pred in predictions)
    return abs(predicted - target_duration) / target_duration


def point_text(row: Dict[str, Any]) -> str:
    tags = " ".join(str(item) for item in row.get("tags", []))
    return " ".join(
        [
            str(row.get("reason", "")),
            str(row.get("label", "")),
            str(row.get("content", "")),
            str(row.get("transcript", "")),
            tags,
        ]
    ).lower()


def required_point_coverage(predictions: List[Dict[str, Any]], required_points: List[str]) -> float:
    if not required_points:
        return 1.0
    texts = "\n".join(point_text(row) for row in predictions)
    covered = sum(1 for point in required_points if str(point).lower() in texts)
    return covered / len(required_points)


def avoid_segment_hit_rate(predictions: List[Dict[str, Any]], avoid_segments: List[Dict[str, Any]]) -> float:
    if not avoid_segments:
        return 0.0
    hits = 0
    for bad in avoid_segments:
        bad_interval = (float(bad["start"]), float(bad["end"]))
        if any(interval_iou((pred["start"], pred["end"]), bad_interval) > 0.1 for pred in predictions):
            hits += 1
    return hits / len(avoid_segments)


def average_breakdown(predictions: List[Dict[str, Any]], key: str, default: float = 0.7) -> float:
    values = []
    for row in predictions:
        breakdown = row.get("score_breakdown") or {}
        if key in breakdown:
            values.append(float(breakdown[key]))
    return sum(values) / len(values) if values else default


def order_coherence(predictions: List[Dict[str, Any]]) -> float:
    if len(predictions) <= 1:
        return 1.0
    sorted_count = sum(1 for left, right in zip(predictions, predictions[1:]) if left["end"] <= right["start"])
    return sorted_count / (len(predictions) - 1)


def composite_score(metrics: Dict[str, float]) -> float:
    duration_score = max(0.0, 1.0 - metrics["duration_error"])
    score = (
        metrics["temporal_f1"] * 0.30
        + metrics["required_point_coverage"] * 0.20
        + metrics["recall_at_3_iou_0_5"] * 0.15
        + metrics["coverage"] * 0.15
        + duration_score * 0.10
        + metrics["quality_proxy"] * 0.10
    )
    return round(score, 4)


def ecommerce_score_100(metrics: Dict[str, float]) -> float:
    duration_score = max(0.0, 1.0 - metrics["duration_error"])
    avoid_score = max(0.0, 1.0 - metrics["avoid_segment_hit_rate"])
    score = (
        metrics["temporal_f1"] * 25
        + metrics["required_point_coverage"] * 20
        + metrics["recall_at_3_iou_0_5"] * 15
        + metrics["quality_proxy"] * avoid_score * 15
        + metrics["order_coherence"] * 10
        + duration_score * 10
        + metrics["success_flag"] * 5
    )
    return round(score, 2)


def score_grade(score: float) -> str:
    if score >= 0.85:
        return "A"
    if score >= 0.70:
        return "B"
    if score >= 0.55:
        return "C"
    return "D"


def evaluate(prediction_path: Path, ground_truth_path: Path) -> Dict[str, Any]:
    prediction = load_json(prediction_path)
    ground_truth = load_json(ground_truth_path)
    predictions = normalize_segments(prediction)
    truth = normalize_segments(ground_truth)
    target_duration = float(
        prediction.get("target_duration")
        or ground_truth.get("target_duration")
        or ground_truth.get("expected_duration")
        or 0.0
    )
    required_points = ground_truth.get("must_cover_points", [])
    avoid_segments = ground_truth.get("avoid_segments", [])
    precision = temporal_precision(predictions, truth)
    recall = coverage(predictions, truth)

    metrics = {
        "mean_best_iou": round(mean_best_iou(predictions, truth), 4),
        "temporal_precision": round(precision, 4),
        "temporal_recall": round(recall, 4),
        "temporal_f1": round(harmonic_mean(precision, recall), 4),
        "recall_at_1_iou_0_3": round(recall_at_k(predictions, truth, 1, 0.3), 4),
        "recall_at_3_iou_0_3": round(recall_at_k(predictions, truth, 3, 0.3), 4),
        "recall_at_1_iou_0_5": round(recall_at_k(predictions, truth, 1, 0.5), 4),
        "recall_at_3_iou_0_5": round(recall_at_k(predictions, truth, 3, 0.5), 4),
        "coverage": round(recall, 4),
        "required_point_coverage": round(required_point_coverage(predictions, required_points), 4),
        "avoid_segment_hit_rate": round(avoid_segment_hit_rate(predictions, avoid_segments), 4),
        "quality_proxy": round(average_breakdown(predictions, "visual_quality"), 4),
        "order_coherence": round(order_coherence(predictions), 4),
        "success_flag": 1.0,
        "duration_error": round(duration_error(predictions, target_duration), 4),
        "predicted_duration": round(sum(pred["end"] - pred["start"] for pred in predictions), 3),
        "ground_truth_duration": round(sum(gt["end"] - gt["start"] for gt in truth), 3),
    }
    metrics["composite_score"] = composite_score(metrics)
    metrics["ecommerce_score_100"] = ecommerce_score_100(metrics)

    return {
        "case_id": prediction.get("task_id") or ground_truth.get("case_id"),
        "prediction": str(prediction_path),
        "ground_truth": str(ground_truth_path),
        "metrics": metrics,
        "grade": score_grade(metrics["composite_score"]),
        "must_cover_points": required_points,
        "avoid_segments": avoid_segments,
        "predicted_segments": predictions,
        "ground_truth_segments": truth,
    }


def write_markdown(path: Path, result: Dict[str, Any]) -> None:
    metrics = result["metrics"]
    lines = [
        f"# EcomHighlightSkill 评测报告：{result.get('case_id')}",
        "",
        "## 指标",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## 预测片段", "", "| # | Start | End | Score | Tags |", "|---|---:|---:|---:|---|"])
    for i, row in enumerate(result["predicted_segments"], start=1):
        tags = ", ".join(str(item) for item in row.get("tags", []))
        lines.append(f"| {i} | {row['start']:.2f} | {row['end']:.2f} | {float(row.get('score', 0.0)):.3f} | {tags} |")
    lines.extend(["", "## Ground Truth 片段", "", "| # | Start | End | Label |", "|---|---:|---:|---|"])
    for i, row in enumerate(result["ground_truth_segments"], start=1):
        lines.append(f"| {i} | {row['start']:.2f} | {row['end']:.2f} | {row.get('label', row.get('reason', ''))} |")
    if result.get("must_cover_points"):
        lines.extend(["", "## 必须覆盖卖点", ""])
        lines.extend(f"- {point}" for point in result["must_cover_points"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate e-commerce highlight clipping result.")
    parser.add_argument("--prediction", required=True, help="Path to segments.json or result.json.")
    parser.add_argument("--ground-truth", required=True, help="Path to Ground Truth JSON.")
    parser.add_argument("--output", required=True, help="Path to output JSON report.")
    parser.add_argument("--markdown", default=None, help="Optional Markdown report path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = evaluate(Path(args.prediction), Path(args.ground_truth))
    write_json(Path(args.output), result)
    if args.markdown:
        write_markdown(Path(args.markdown), result)
    print(json.dumps({"ok": True, "metrics": result["metrics"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
