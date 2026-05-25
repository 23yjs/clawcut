#!/usr/bin/env python3
"""Run EcomHighlightSkill over a JSONL evaluation suite and aggregate metrics."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from evaluate_highlights import evaluate, write_json, write_markdown


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return cases


def run_command(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def case_output_dir(root: Path, case_id: str) -> Path:
    return root / case_id


def run_case(case: Dict[str, Any], output_root: Path, skip_pipeline: bool, no_render: bool) -> Dict[str, Any]:
    case_id = case["case_id"]
    output_dir = case_output_dir(output_root, case_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    prediction = output_dir / "segments.json"

    if not skip_pipeline:
        cmd = [
            sys.executable,
            "skills/ecom-highlight-skill/scripts/highlight_pipeline.py",
            "--input",
            case["video"],
            "--instruction",
            case["instruction"],
            "--target-duration",
            str(case.get("target_duration", 60)),
            "--output-dir",
            str(output_dir),
            "--task-id",
            case_id,
            "--target-platform",
            case.get("target_platform", "xiaohongshu"),
            "--style",
            case.get("style", "种草"),
            "--aspect-ratio",
            case.get("aspect_ratio", "source"),
        ]
        if case.get("transcript"):
            cmd.extend(["--transcript", case["transcript"]])
        if case.get("no_audio"):
            cmd.append("--no-audio")
        if no_render:
            cmd.append("--no-render")
        proc = run_command(cmd)
        if proc.returncode != 0:
            return {
                "case_id": case_id,
                "ok": False,
                "stage": "pipeline",
                "domain": case.get("domain"),
                "difficulty": case.get("difficulty"),
                "error": proc.stderr.strip() or proc.stdout.strip(),
            }

    if not prediction.exists():
        return {
            "case_id": case_id,
            "ok": False,
            "stage": "prediction_missing",
            "domain": case.get("domain"),
            "difficulty": case.get("difficulty"),
            "error": f"Prediction file not found: {prediction}",
        }

    try:
        result = evaluate(prediction, Path(case["ground_truth"]))
    except Exception as exc:
        return {
            "case_id": case_id,
            "ok": False,
            "stage": "evaluation",
            "domain": case.get("domain"),
            "difficulty": case.get("difficulty"),
            "error": str(exc),
        }

    eval_json = output_dir / "eval_result.json"
    eval_md = output_dir / "eval_report.md"
    write_json(eval_json, result)
    write_markdown(eval_md, result)
    return {
        "case_id": case_id,
        "ok": True,
        "domain": case.get("domain"),
        "difficulty": case.get("difficulty"),
        "video": case.get("video"),
        "ground_truth": case.get("ground_truth"),
        "target_platform": case.get("target_platform", "xiaohongshu"),
        "style": case.get("style", "种草"),
        "prediction": str(prediction),
        "eval_result": str(eval_json),
        "eval_report": str(eval_md),
        "metrics": result["metrics"],
        "grade": result["grade"],
    }


def average_metric(rows: List[Dict[str, Any]], key: str) -> float:
    values = [row["metrics"][key] for row in rows if row.get("ok") and key in row.get("metrics", {})]
    return round(sum(values) / len(values), 4) if values else 0.0


def aggregate(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok_rows = [row for row in results if row.get("ok")]
    metric_keys = [
        "mean_best_iou",
        "recall_at_1_iou_0_3",
        "recall_at_3_iou_0_3",
        "recall_at_1_iou_0_5",
        "recall_at_3_iou_0_5",
        "coverage",
        "required_point_coverage",
        "temporal_f1",
        "duration_error",
        "composite_score",
        "ecommerce_score_100",
    ]
    summary = {
        "total_cases": len(results),
        "passed_cases": len(ok_rows),
        "failed_cases": len(results) - len(ok_rows),
        "metrics": {key: average_metric(results, key) for key in metric_keys},
        "by_difficulty": {},
        "by_domain": {},
    }
    for group_key, target in [("difficulty", "by_difficulty"), ("domain", "by_domain")]:
        groups = sorted({row.get(group_key) or "unknown" for row in results})
        for group in groups:
            rows = [row for row in results if (row.get(group_key) or "unknown") == group]
            summary[target][group] = {
                "total_cases": len(rows),
                "passed_cases": len([row for row in rows if row.get("ok")]),
                "composite_score": average_metric(rows, "composite_score"),
                "ecommerce_score_100": average_metric(rows, "ecommerce_score_100"),
                "mean_best_iou": average_metric(rows, "mean_best_iou"),
                "coverage": average_metric(rows, "coverage"),
            }
    return summary


def write_suite_markdown(path: Path, payload: Dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# EcomHighlightSkill 批量评测报告",
        "",
        "## 总览",
        "",
        f"- 用例总数：{summary['total_cases']}",
        f"- 成功评测：{summary['passed_cases']}",
        f"- 失败用例：{summary['failed_cases']}",
        "",
        "## 平均指标",
        "",
        "| 指标 | 均值 |",
        "|---|---:|",
    ]
    for key, value in summary["metrics"].items():
        lines.append(f"| {key} | {value} |")

    lines.extend(["", "## 用例明细", "", "| Case | Domain | Difficulty | Grade | 100分 | Composite | F1 | Coverage | Status |", "|---|---|---|---|---:|---:|---:|---:|---|"])
    for row in payload["results"]:
        if row.get("ok"):
            metrics = row["metrics"]
            lines.append(
                f"| {row['case_id']} | {row.get('domain', '')} | {row.get('difficulty', '')} | {row['grade']} | "
                f"{metrics['ecommerce_score_100']} | {metrics['composite_score']} | {metrics['temporal_f1']} | {metrics['coverage']} | ok |"
            )
        else:
            lines.append(
                f"| {row['case_id']} | {row.get('domain', '')} | {row.get('difficulty', '')} | - | 0 | 0 | 0 | 0 | "
                f"failed: {row.get('stage')} |"
            )

    lines.extend(["", "## 失败用例", ""])
    failed = [row for row in payload["results"] if not row.get("ok")]
    if not failed:
        lines.append("无。")
    else:
        for row in failed:
            lines.append(f"- `{row['case_id']}`：{row.get('stage')}，{row.get('error', '')[:300]}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run EcomHighlightSkill evaluation suite.")
    parser.add_argument("--cases", required=True, help="JSONL evaluation cases.")
    parser.add_argument("--output-dir", required=True, help="Per-case output root.")
    parser.add_argument("--report-json", required=True, help="Aggregated JSON report.")
    parser.add_argument("--report-md", required=True, help="Aggregated Markdown report.")
    parser.add_argument("--skip-pipeline", action="store_true", help="Only evaluate existing predictions.")
    parser.add_argument("--no-render", action="store_true", help="Do not render highlight.mp4 during suite runs.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cases = load_cases(Path(args.cases))
    results = [run_case(case, Path(args.output_dir), args.skip_pipeline, args.no_render) for case in cases]
    payload = {"summary": aggregate(results), "results": results}
    write_json(Path(args.report_json), payload)
    write_suite_markdown(Path(args.report_md), payload)
    print(json.dumps({"ok": True, "summary": payload["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
