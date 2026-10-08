#!/usr/bin/env python3
"""Score the tuning CSV with the request loaded by the local Blot scorer."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.app.ollaya_scoring import (
    OLLAYA_REQUEST_VERSION,
    SCORING_MODEL,
    LocalOllayaScorer,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "RAP_TTP_Procedure_Official_Closed_v1.0-draft09302100.docx.2a945e09.ollaya.csv"
EXPECTED_HEADER = [
    "word",
    "is_identifier_percent",
    "is_operationally_significant_percent",
    "is_common_word_percent",
    "target_is_identifier",
    "target_is_operationally_significant",
    "target_is_common_word",
]
SIGNALS = {
    "is_identifier": ("target_is_identifier", "isIdentifier"),
    "is_operationally_significant": (
        "target_is_operationally_significant",
        "isOperationallySignificant",
    ),
    "is_common_word": ("target_is_common_word", "isCommonWord"),
}
THRESHOLD = 0.5


def _probability_metrics(
    rows: list[dict[str, str]], predictions: list[dict[str, Any]], target_column: str,
    predicted_signal: str,
) -> dict[str, Any]:
    expected_tp = expected_fp = expected_fn = expected_tn = 0.0
    brier_sum = log_loss_sum = positive_mass = 0.0
    hard_tp = hard_fp = hard_fn = hard_tn = 0
    scored = 0
    for row, prediction in zip(rows, predictions, strict=True):
        probability = prediction.get(predicted_signal)
        if probability is None:
            continue
        target = float(row[target_column])
        predicted_positive = probability >= THRESHOLD
        positive_mass += target
        scored += 1
        if predicted_positive:
            expected_tp += target
            expected_fp += 1.0 - target
        else:
            expected_fn += target
            expected_tn += 1.0 - target
        hard_positive = target >= THRESHOLD
        if predicted_positive and hard_positive:
            hard_tp += 1
        elif predicted_positive:
            hard_fp += 1
        elif hard_positive:
            hard_fn += 1
        else:
            hard_tn += 1
        clipped = min(max(probability, 1e-15), 1.0 - 1e-15)
        brier_sum += (probability - target) ** 2
        log_loss_sum -= target * math.log(clipped) + (1.0 - target) * math.log(1.0 - clipped)

    negative_mass = scored - positive_mass
    precision = expected_tp / (expected_tp + expected_fp) if expected_tp + expected_fp else 0.0
    recall = expected_tp / positive_mass if positive_mass else 0.0
    specificity = expected_tn / negative_mass if negative_mass else 0.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "rows_total": len(rows),
        "rows_scored": scored,
        "rows_unscored": len(rows) - scored,
        "decision_threshold": THRESHOLD,
        "expected_confusion_matrix": {
            "true_positive": expected_tp,
            "false_positive": expected_fp,
            "false_negative": expected_fn,
            "true_negative": expected_tn,
        },
        "expected_support": {
            "positive_mass": positive_mass,
            "negative_mass": negative_mass,
        },
        "soft_metrics": {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "balanced_accuracy": (recall + specificity) / 2.0,
            "brier_score": brier_sum / scored if scored else None,
            "log_loss": log_loss_sum / scored if scored else None,
        },
        "diagnostic_hard_0_5_confusion_matrix": {
            "true_positive": hard_tp,
            "false_positive": hard_fp,
            "false_negative": hard_fn,
            "true_negative": hard_tn,
        },
        "diagnostic_hard_0_5_support": {
            "positive": hard_tp + hard_fn,
            "negative": hard_fp + hard_tn,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if OLLAYA_REQUEST_VERSION != args.expected_version:
        raise SystemExit(
            f"Loaded request {OLLAYA_REQUEST_VERSION}, expected {args.expected_version}; "
            "restart the scorer process before evaluation."
        )
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1.")
    if not args.csv.is_file():
        raise SystemExit(f"CSV not found: {args.csv}")
    csv_bytes = args.csv.read_bytes()
    csv_sha256 = hashlib.sha256(csv_bytes).hexdigest()
    with args.csv.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != EXPECTED_HEADER:
            raise SystemExit(f"Unexpected CSV header: {reader.fieldnames!r}")
        rows = list(reader)
    if not rows or any(not row.get("word", "").strip() for row in rows):
        raise SystemExit("CSV must contain rows with non-empty words.")
    words = [row["word"] for row in rows]
    duplicates = [word for word, count in Counter(words).items() if count > 1]
    if duplicates:
        raise SystemExit(f"CSV contains {len(duplicates)} duplicated word values.")
    target_distributions: dict[str, dict[str, Any]] = {}
    for target_column, _ in SIGNALS.values():
        values: list[float] = []
        for row in rows:
            raw = row.get(target_column, "").strip()
            if not raw:
                raise SystemExit(f"Missing target value in {target_column}.")
            value = float(raw)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise SystemExit(f"Invalid probability target in {target_column}: {raw!r}")
            values.append(value)
        target_distributions[target_column] = {
            "rows": len(values),
            "positive_mass": sum(values),
            "negative_mass": len(values) - sum(values),
            "exact_zero": sum(value == 0.0 for value in values),
            "exact_one": sum(value == 1.0 for value in values),
            "fractional": sum(value not in (0.0, 1.0) for value in values),
        }

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    stem = args.expected_version
    result_path = output_dir / f"{stem}.csv"
    partial_path = output_dir / f"{stem}.csv.partial"
    metrics_path = output_dir / f"{stem}.metrics.json"
    if result_path.exists() or metrics_path.exists():
        raise SystemExit(f"Refusing to overwrite an existing evaluation output in {output_dir}.")
    if partial_path.exists() != args.resume:
        raise SystemExit(
            "Use --resume only when a partial output exists; a new evaluation must not overwrite one."
        )

    extra_fields = ["row_index", "request_version", "scoring_model", "score_status"]
    for signal_name in SIGNALS:
        extra_fields.extend((f"{signal_name}_answer", f"{signal_name}_probability_yes"))
    predictions: list[dict[str, Any]] = []
    scorer = LocalOllayaScorer(model=SCORING_MODEL)
    logging.getLogger("backend.app.ollaya_scoring").disabled = True
    start_index = 0
    if args.resume:
        with partial_path.open(encoding="utf-8", newline="") as previous:
            prior_reader = csv.DictReader(previous)
            if prior_reader.fieldnames != EXPECTED_HEADER + extra_fields:
                raise SystemExit("Partial evaluation output has an unexpected header.")
            for index, record in enumerate(prior_reader, start=1):
                if index > len(rows) or any(record[column] != rows[index - 1][column] for column in EXPECTED_HEADER):
                    raise SystemExit("Partial output rows do not match the source CSV in order.")
                if (
                    record["row_index"] != str(index)
                    or record["request_version"] != OLLAYA_REQUEST_VERSION
                    or record["scoring_model"] != SCORING_MODEL
                ):
                    raise SystemExit("Partial output contains mismatched scorer metadata.")
                prediction: dict[str, Any] = {}
                for signal_name in SIGNALS:
                    raw_probability = record[f"{signal_name}_probability_yes"]
                    probability = float(raw_probability) if raw_probability else None
                    if probability is not None and (not math.isfinite(probability) or not 0.0 <= probability <= 1.0):
                        raise SystemExit("Partial output contains an invalid probability.")
                    prediction[signal_name] = probability
                predictions.append(prediction)
                start_index = index
        if start_index >= len(rows):
            raise SystemExit("Partial output already contains every source row; refusing duplicate metrics.")

    mode = "a" if args.resume else "x"
    with partial_path.open(mode, encoding="utf-8", newline="") as output:
        partial_path.chmod(0o600)
        writer = csv.DictWriter(output, fieldnames=EXPECTED_HEADER + extra_fields)
        if not args.resume:
            writer.writeheader()

        def score_row(item: tuple[int, dict[str, str]]) -> tuple[int, dict[str, str], dict[str, Any]]:
            index, row = item
            result = scorer.score_candidate({"candidate": row["word"]})
            return index, row, result

        pending_rows = enumerate(rows[start_index:], start=start_index + 1)
        if args.workers == 1:
            scored_rows = map(score_row, pending_rows)
        else:
            executor = ThreadPoolExecutor(max_workers=args.workers)
            scored_rows = executor.map(score_row, pending_rows)
        try:
            for index, row, result in scored_rows:
                if result.get("ollayaRequestVersion") != OLLAYA_REQUEST_VERSION:
                    raise SystemExit("Scorer returned a mismatched request version.")
                if result.get("scoringModel") != SCORING_MODEL:
                    raise SystemExit("Scorer returned a mismatched model tag.")
                signals = result.get("signals", {})
                prediction: dict[str, Any] = {}
                record: dict[str, Any] = {
                    **row,
                    "row_index": index,
                    "request_version": result.get("ollayaRequestVersion"),
                    "scoring_model": result.get("scoringModel"),
                    "score_status": result.get("scoreStatus"),
                }
                for signal_name, (_, public_name) in SIGNALS.items():
                    signal = signals.get(public_name, {})
                    probability = signal.get("probabilityYes")
                    record[f"{signal_name}_answer"] = signal.get("answer", "")
                    record[f"{signal_name}_probability_yes"] = probability if probability is not None else ""
                    prediction[signal_name] = probability
                predictions.append(prediction)
                writer.writerow(record)
                output.flush()
                if index % 100 == 0 or index == len(rows):
                    print(f"scored {index}/{len(rows)} rows", file=sys.stderr, flush=True)
        finally:
            if args.workers != 1:
                executor.shutdown(wait=True, cancel_futures=True)

    partial_path.replace(result_path)
    result_path.chmod(0o600)
    metrics = {
        "evaluation_date_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation_scope": "in-sample tuning data; not holdout or generalization evidence",
        "csv_filename": args.csv.name,
        "csv_sha256": csv_sha256,
        "row_count": len(rows),
        "unique_word_count": len(set(words)),
        "duplicate_word_count": 0,
        "target_semantics": "user-designated probability targets in [0,1]; no binary relabeling",
        "target_distributions": target_distributions,
        "scoring_model": SCORING_MODEL,
        "request_version": OLLAYA_REQUEST_VERSION,
        "request_questions": "loaded from the highest-numbered manifest at scorer import time",
        "decision_threshold": THRESHOLD,
        "probability_metrics_definition": (
            "Confusion entries are expected counts: for target probability y, a predicted Yes "
            "contributes y to TP and 1-y to FP; predicted No contributes y to FN and 1-y to TN. "
            "The separately labeled diagnostic matrix thresholds target and prediction at 0.5."
        ),
        "signals": {
            signal_name: _probability_metrics(rows, predictions, target_column, signal_name)
            for signal_name, (target_column, _) in SIGNALS.items()
        },
        "row_level_output": str(result_path),
    }
    with metrics_path.open("x", encoding="utf-8") as destination:
        metrics_path.chmod(0o600)
        json.dump(metrics, destination, indent=2)
        destination.write("\n")
    print(json.dumps({
        "request_version": OLLAYA_REQUEST_VERSION,
        "model": SCORING_MODEL,
        "rows": len(rows),
        "csv_sha256": csv_sha256,
        "row_level_output": str(result_path),
        "metrics_output": str(metrics_path),
        "metrics": metrics["signals"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
