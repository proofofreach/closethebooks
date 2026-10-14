from __future__ import annotations

from pathlib import Path
import csv
import json

from .utils import ensure_dir, flatten_record, json_dumps, ordered_fieldnames


def write_page_json(base_dir: Path, endpoint_key: str, cursor_label: str, records: list[dict]) -> Path:
    target_dir = ensure_dir(base_dir / "raw" / endpoint_key)
    path = target_dir / f"{cursor_label}.json"
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_csv(base_dir: Path, endpoint_key: str, records: list[dict]) -> Path:
    csv_dir = ensure_dir(base_dir / "csv")
    path = csv_dir / f"{endpoint_key}.csv"
    flattened = [flatten_record(record) for record in records]
    fieldnames = ordered_fieldnames(flattened)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in flattened:
            writer.writerow(row)
    return path


def write_csv_rows(base_dir: Path, file_name: str, rows: list[dict[str, str]]) -> Path:
    csv_dir = ensure_dir(base_dir / "csv")
    path = csv_dir / file_name
    fieldnames = ordered_fieldnames(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_manifest(base_dir: Path, data: dict) -> Path:
    manifest_dir = ensure_dir(base_dir / "manifests")
    path = manifest_dir / "run_summary.json"
    path.write_text(json_dumps(data), encoding="utf-8")
    return path
