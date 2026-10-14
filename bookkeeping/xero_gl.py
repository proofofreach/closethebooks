"""Merge Xero 'General Ledger Detail' XLSX exports into general_ledger_merged.csv.

The merged CSV is the source for `books xero-convert`.
"""
from __future__ import annotations

import csv
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass
class GLRow:
    date: str
    source: str
    description: str
    reference: str
    debit: str
    credit: str
    running_balance: str
    related_account: str
    account: str

    def key(self) -> tuple[str, str, str, str, str, str, str, str, str]:
        return (
            self.date,
            self.source,
            self.description,
            self.reference,
            self.debit,
            self.credit,
            self.running_balance,
            self.related_account,
            self.account,
        )


def excel_serial_to_date(serial: str) -> str:
    try:
        value = float(serial)
    except ValueError:
        return serial
    date = datetime(1899, 12, 30) + timedelta(days=value)
    return date.date().isoformat()


def read_gl_xlsx(path: Path) -> Iterable[GLRow]:
    with zipfile.ZipFile(path) as z:
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        relmap = {r.get("Id"): r.get("Target") for r in rels}
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet = wb.findall("a:sheets/a:sheet", NS)[0]
        rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = relmap.get(rid)
        if target and not target.startswith("xl/"):
            target = "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst.findall("a:si", NS):
                texts = []
                for t in si.findall(".//a:t", NS):
                    texts.append(t.text or "")
                shared.append("".join(texts))

        def cell_value(c):
            v = c.find("a:v", NS)
            if v is None:
                return ""
            if c.get("t") == "s":
                try:
                    return shared[int(v.text)]
                except Exception:
                    return ""
            return v.text or ""

        sheet_xml = z.read(target)
        sheet_tree = ET.fromstring(sheet_xml)
        rows = sheet_tree.findall("a:sheetData/a:row", NS)

        header_row_idx = None
        header = []
        header_cols = []
        for row in rows:
            cells = row.findall("a:c", NS)
            vals = {}
            for c in cells:
                ref = c.get("r") or ""
                col = "".join(ch for ch in ref if ch.isalpha())
                vals[col] = cell_value(c)
            if any(str(v).strip().lower() == "date" for v in vals.values()):
                header_row_idx = int(row.get("r"))
                # preserve column order by sorting column letters
                for col in sorted(vals.keys()):
                    header_cols.append(col)
                    header.append((vals.get(col) or "").strip())
                break
        if header_row_idx is None:
            raise RuntimeError("Header row not found in GL export.")

        col_map = {name.lower(): idx for idx, name in enumerate(header)}
        required = ["date", "source", "description", "reference", "debit", "credit", "running balance", "related account"]
        for name in required:
            if name not in col_map:
                raise RuntimeError(f"Missing column: {name}")

        current_account = ""
        for row in rows:
            r = int(row.get("r"))
            if r <= header_row_idx:
                continue
            # build row values aligned to header columns
            cells = row.findall("a:c", NS)
            row_map = {}
            for c in cells:
                ref = c.get("r") or ""
                col = "".join(ch for ch in ref if ch.isalpha())
                row_map[col] = cell_value(c)
            vals = [row_map.get(col, "") for col in header_cols]
            if not any(v for v in vals):
                continue
            if vals and isinstance(vals[0], str) and not any(v for v in vals[1:]):
                label = vals[0].strip()
                if label:
                    current_account = label.lstrip("-").strip()
                continue
            date_val = vals[col_map["date"]] if col_map["date"] < len(vals) else ""
            if not date_val:
                continue
            try:
                float(date_val)
            except ValueError:
                # skip non-date rows like Opening Balance
                continue
            yield GLRow(
                date=excel_serial_to_date(date_val),
                source=vals[col_map["source"]] if col_map["source"] < len(vals) else "",
                description=vals[col_map["description"]] if col_map["description"] < len(vals) else "",
                reference=vals[col_map["reference"]] if col_map["reference"] < len(vals) else "",
                debit=vals[col_map["debit"]] if col_map["debit"] < len(vals) else "",
                credit=vals[col_map["credit"]] if col_map["credit"] < len(vals) else "",
                running_balance=vals[col_map["running balance"]] if col_map["running balance"] < len(vals) else "",
                related_account=vals[col_map["related account"]] if col_map["related account"] < len(vals) else "",
                account=current_account,
            )


def merge_gl_files(paths: list[Path], output_csv: Path) -> dict[str, int]:
    rows_out: list[GLRow] = []
    for path in paths:
        for row in read_gl_xlsx(path):
            rows_out.append(row)

    return write_merged_gl_rows(rows_out, output_csv)


def write_merged_gl_rows(rows_out: list[GLRow], output_csv: Path) -> dict[str, int]:
    rows_out.sort(key=lambda r: (r.date, r.source, r.description, r.reference))
    key_counts = Counter(row.key() for row in rows_out)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Date",
                "Source",
                "Description",
                "Reference",
                "Debit",
                "Credit",
                "Running Balance",
                "Related account",
                "Account",
            ]
        )
        for row in rows_out:
            writer.writerow(
                [
                    row.date,
                    row.source,
                    row.description,
                    row.reference,
                    row.debit,
                    row.credit,
                    row.running_balance,
                    row.related_account,
                    row.account,
                ]
            )

    return {
        "total_rows": len(rows_out),
        "duplicate_key_rows_preserved": sum(count - 1 for count in key_counts.values() if count > 1),
        "unique_row_keys": len(key_counts),
        "output_rows": len(rows_out),
    }
