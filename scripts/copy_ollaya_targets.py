#!/usr/bin/env python3
"""Copy Ollaya target labels from an older CSV to a newer CSV by word."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLD = ROOT / "RAP_TTP_Procedure_Official_Closed_v1.0-draft09302100.docx.2a945e09.ollaya.csv"
DEFAULT_NEW = ROOT / "RAP_TTP_Procedure_Official_Closed_v1.0-draft09302100.docx.2a945e09.ollaya(2).csv"
TARGET_COLUMNS = (
    "target_is_identifier",
    "target_is_operationally_significant",
    "target_is_common_word",
)


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path} has duplicate column names")

        required = {"word", *TARGET_COLUMNS}
        missing = required.difference(reader.fieldnames)
        if missing:
            raise ValueError(f"{path} is missing required columns: {', '.join(sorted(missing))}")

        rows: list[dict[str, str]] = []
        for line_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path} has an inconsistent number of columns on line {line_number}")
            rows.append(row)

    return reader.fieldnames, rows


def copy_targets(old_path: Path, new_path: Path, check_only: bool = False) -> tuple[int, int]:
    if old_path.resolve() == new_path.resolve():
        raise ValueError("Old and new CSV paths must be different")

    old_fields, old_rows = read_rows(old_path)
    new_fields, new_rows = read_rows(new_path)

    old_by_word: dict[str, dict[str, str]] = {}
    old_by_casefold: dict[str, list[dict[str, str]]] = {}
    for line_number, row in enumerate(old_rows, start=2):
        word = row["word"]
        if word in old_by_word:
            raise ValueError(f"Duplicate word {word!r} in {old_path} (including line {line_number})")
        old_by_word[word] = row
        old_by_casefold.setdefault(word.casefold(), []).append(row)

    matched_rows = 0
    matched_old_words: set[str] = set()
    for row in new_rows:
        word = row["word"]
        old_row = old_by_word.get(word)
        if old_row is None:
            candidates = old_by_casefold.get(word.casefold(), [])
            if len(candidates) > 1:
                raise ValueError(f"Case-insensitive match for {word!r} is ambiguous in {old_path}")
            if not candidates:
                continue
            old_row = candidates[0]
        for column in TARGET_COLUMNS:
            row[column] = old_row[column]
        matched_old_words.add(old_row["word"])
        matched_rows += 1

    unmatched_rows = len(new_rows) - matched_rows
    if not check_only:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                newline="",
                encoding="utf-8",
                dir=new_path.parent,
                prefix=f".{new_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                writer = csv.DictWriter(
                    temp_file,
                    fieldnames=new_fields,
                    lineterminator="\n",
                    extrasaction="raise",
                )
                writer.writeheader()
                writer.writerows(new_rows)
            os.replace(temp_path, new_path)
        except BaseException:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    source_only_words = len(set(old_by_word).difference(matched_old_words))
    print(
        f"{'Checked' if check_only else 'Updated'} {matched_rows} rows; "
        f"{unmatched_rows} newer rows had no word match; "
        f"{source_only_words} older words were absent from the newer file."
    )
    return matched_rows, unmatched_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, default=DEFAULT_OLD, help="CSV containing the target values")
    parser.add_argument("--new", type=Path, default=DEFAULT_NEW, help="CSV to update in place")
    parser.add_argument("--check-only", action="store_true", help="report match counts without writing")
    args = parser.parse_args()

    try:
        copy_targets(args.old, args.new, check_only=args.check_only)
    except (OSError, ValueError, csv.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
