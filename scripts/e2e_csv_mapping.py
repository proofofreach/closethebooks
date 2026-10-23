"""Synthetic CSV mapping → saved mapping → CLI proposals → immutable evidence.

Run: .venv/bin/python scripts/e2e_csv_mapping.py --artifact /tmp/csv-mapping-e2e.json

Failure inventory (written before implementation): empty/header-only CSV; duplicate
or blank headers; ragged and malformed rows; unknown/reused columns; incompatible
amount/debit/credit selections; missing/unknown date, number, delimiter, and sign
formats; missing/invalid dates and amounts; nonfinite numbers; malformed grouping;
fractional cents; signed debit/credit ambiguity; duplicate references; unexpected
currency; invalid rows beyond the preview; partial mapping writes; content changes;
tampered saved hash; repeated equal transactions; filename-dependent IDs; changed
mapping semantics after evidence retention; proposal changes on a rejected import.

Only synthetic files in TemporaryDirectory are read. The JSON artifact contains
the actual CLI/check output and deterministic assertion names for repeatability.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    from bookkeeping.csv_mapping import inspect_csv, load_mapping, parse_mapped_csv, save_mapping
    from bookkeeping.statements import parse_statement

    checks = []
    commands = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    def rejected(name, operation, *messages):
        try:
            operation()
        except ValueError as exc:
            check(name, all(message.lower() in str(exc).lower() for message in messages))
        else:
            raise AssertionError(f"{name}: accepted invalid input")

    with TemporaryDirectory(prefix="csv-mapping-e2e-") as directory:
        root = Path(directory)
        path = root / "synthetic.csv"
        headers = ["When", "Details", "Value", "Ref", "Running", "Unit"]
        mapping = dict(date="When", description="Details", amount="Value", reference="Ref",
                       balance="Running", currency="Unit", expected_currency="USD",
                       date_format="mdy", delimiter=",", number_format="dot", amount_sign="inflow", balance_order="oldest_first")

        def write(rows, selected_headers=headers, delimiter=","):
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter=delimiter)
                writer.writerow(selected_headers)
                writer.writerows(rows)

        valid = [["06/03/2026", "Repeated", "-10.00", "", "90", "USD"],
                 ["06/03/2026", "Repeated", "-10.00", "", "90", "USD"]]
        write(valid)
        preview = inspect_csv(path)
        check("inspect exposes headers, bounded sample, exact count",
              preview["headers"] == headers and preview["row_count"] == 2 and len(preview["sample"]) == 2)
        check("no mapping initially", load_mapping(path) is None)
        save_mapping(path, mapping)
        parsed = parse_statement(path)
        check("saved mapping overrides aliases and keeps equal transactions",
              len(parsed.transactions) == 2 and len({t.txn_id for t in parsed.transactions}) == 2)
        renamed = root / "renamed.csv"
        renamed.write_bytes(path.read_bytes())
        check("mapping and IDs survive rename", load_mapping(renamed) == load_mapping(path)
              and [t.txn_id for t in parse_statement(renamed).transactions] == [t.txn_id for t in parsed.transactions])
        check("explicit mdy date", parsed.transactions[0].date == "2026-06-03")
        check("explicit dmy date", parse_mapped_csv(path, {**mapping, "date_format": "dmy"}).transactions[0].date == "2026-03-06")
        check("outflow sign inversion", parse_mapped_csv(path, {**mapping, "amount_sign": "outflow"}).transactions[0].amount == Decimal("10"))

        write([["06/03/2026", "First", "1", "one", "101", "USD"],
               ["06/03/2026", "Last", "2", "two", "103", "USD"]])
        check("oldest-first same-day closing balance", parse_mapped_csv(path, mapping).ending_balance == Decimal("103"))
        check("newest-first same-day closing balance", parse_mapped_csv(path, {**mapping, "balance_order": "newest_first"}).ending_balance == Decimal("101"))
        rejected("balance order must be explicit", lambda: parse_mapped_csv(path, {**mapping, "balance_order": ""}), "balance row order")

        invalid = [
            ("missing date", ["", "X", "1", "", "", "USD"], "date"),
            ("invalid date", ["02/30/2026", "X", "1", "", "", "USD"], "date"),
            ("missing amount", ["06/03/2026", "X", "", "", "", "USD"], "amount"),
            ("nonfinite amount", ["06/03/2026", "X", "NaN", "", "", "USD"], "amount"),
            ("infinite amount", ["06/03/2026", "X", "Infinity", "", "", "USD"], "amount"),
            ("fractional cents", ["06/03/2026", "X", "1.001", "", "", "USD"], "precision"),
            ("malformed grouping", ["06/03/2026", "X", "1,23.00", "", "", "USD"], "amount"),
            ("invalid balance", ["06/03/2026", "X", "1", "", "NaN", "USD"], "balance"),
            ("unexpected currency", ["06/03/2026", "X", "1", "", "", "EUR"], "currency"),
            ("missing currency", ["06/03/2026", "X", "1", "", "", ""], "currency"),
        ]
        for name, row, message in invalid:
            write([row])
            rejected(name, lambda: save_mapping(path, mapping), "line 2", message)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            check(f"{name} writes no mapping", not (root / ".csv-mappings" / f"{digest}.json").exists())
        write([case[1] for case in invalid])
        rejected("all row errors reported", lambda: parse_mapped_csv(path, mapping), "line 2", "line 11")
        write(valid * 15 + [invalid[0][1]])
        rejected("validation extends beyond preview", lambda: save_mapping(path, mapping), "line 32")
        write([["06/03/2026", "X", "1", "same", "", "USD"]] * 2)
        rejected("duplicate references", lambda: parse_mapped_csv(path, mapping), "line 3", "reference")
        for rows in ([valid[0][:-1]], [valid[0] + ["extra"]], [[]]):
            write(rows)
            rejected("ragged row", lambda: inspect_csv(path), "line 2")
        write(valid, selected_headers=["When", "Details", "Value", "Ref", "Running", "When"])
        rejected("duplicate headers", lambda: inspect_csv(path), "line 1", "duplicate")
        write(valid, selected_headers=["When", "Details", "Value", "Ref", "Running", ""])
        rejected("blank headers", lambda: inspect_csv(path), "line 1")
        for content in ("", ",".join(headers) + "\n"):
            path.write_text(content)
            rejected("empty CSV", lambda: inspect_csv(path), "empty")
        path.write_text('When,Details,Value\n"unclosed,X,1\n')
        rejected("malformed quotes", lambda: inspect_csv(path), "line")
        path.write_text('"unclosed header\n')
        rejected("malformed header quotes", lambda: inspect_csv(path), "line")
        write(valid)
        for change, message in [({"date_format": "auto"}, "date_format"),
                                ({"date_format": None}, "date_format"),
                                ({"delimiter": "|"}, "delimiter"),
                                ({"number_format": "auto"}, "number_format"),
                                ({"amount_sign": "auto"}, "amount_sign"),
                                ({"amount": "Missing"}, "column"),
                                ({"description": "When"}, "column"),
                                ({"debit": "Value", "credit": "Running"}, "amount")]:
            rejected("invalid mapping schema", lambda: save_mapping(path, {**mapping, **change}), message)

        split = {key: value for key, value in mapping.items() if key not in {"amount", "balance"}}
        split.update(debit="Value", credit="Running")
        write([["06/03/2026", "Debit", "2", "", "", "USD"],
               ["06/04/2026", "Credit", "", "", "3", "USD"]])
        automatic_split_sign = {key: value for key, value in split.items() if key != 'amount_sign'}
        check("split amounts need no sign choice", [t.amount for t in parse_mapped_csv(path, automatic_split_sign).transactions] == [Decimal("-2"), Decimal("3")])
        check("debit/credit direction", [t.amount for t in parse_mapped_csv(path, split).transactions] == [Decimal("-2"), Decimal("3")])
        for debit, credit in (("", ""), ("-2", ""), ("2", "3")):
            write([["06/03/2026", "Bad split", debit, "", credit, "USD"]])
            rejected("ambiguous split amount", lambda: parse_mapped_csv(path, split), "line 2", "amount")
        for delimiter in (";", "\t"):
            write([["2026-06-03", "Euro format", "1.234,50", "", "2.000,00", "USD"]], delimiter=delimiter)
            local = {**mapping, "delimiter": delimiter, "date_format": "iso", "number_format": "comma"}
            save_mapping(path, local)
            check("delimiter and decimal convention", parse_statement(path).transactions[0].amount == Decimal("1234.50"))

        write(valid)
        save_mapping(path, mapping)
        mapping_file = root / ".csv-mappings" / f"{hashlib.sha256(path.read_bytes()).hexdigest()}.json"
        saved_bytes = mapping_file.read_bytes()
        rejected("invalid overwrite is atomic", lambda: save_mapping(path, {**mapping, "amount": "Missing"}), "column")
        check("failed save preserves previous mapping", saved_bytes == mapping_file.read_bytes())
        path.write_text(path.read_text() + "06/05/2026,Added,1,,,USD\n")
        check("modified raw bytes do not reuse mapping", load_mapping(path) is None)
        write(valid)
        payload = json.loads(mapping_file.read_text())
        payload["sha256"] = "0" * 64
        mapping_file.write_text(json.dumps(payload))
        rejected("tampered saved content binding", lambda: load_mapping(path), "hash")
        mapping_file.write_bytes(saved_bytes)

        from bookkeeping.accounts import AccountSource
        from bookkeeping.evidence import create_statement_artifact
        parsed_before_change = parse_statement(path)
        path.write_text(path.read_text() + "06/05/2026,Added,1,,,USD\n")
        source = AccountSource(key="checking", name="Synthetic", account="Assets:Bank:Checking", cutover_date="2026-01-01")
        rejected("source cannot change after parsing", lambda: create_statement_artifact(parsed_before_change, source, root / "race-evidence"), "changed")
        check("source race writes no evidence", not (root / "race-evidence").exists())
        write(valid)

        (root / "books.toml").write_text('[company]\nname = "CSV E2E"\ncurrency = "USD"\n')
        (root / "sources.toml").write_text('[accounts.checking]\nname = "Synthetic"\naccount = "Assets:Bank:Checking"\ncutover_date = "2026-01-01"\ningestion = "statement"\n')
        ledger = root / "ledger"
        ledger.mkdir()
        (ledger / "main.beancount").write_text(
            '2026-01-01 open Assets:Bank:Checking USD\n'
            '2026-01-01 open Expenses:Uncategorized USD\n'
            '2026-01-01 open Income:Uncategorized USD\n'
            '2026-01-01 open Equity:Opening USD\n'
            '2026-01-01 * "Opening"\n  Assets:Bank:Checking 110 USD\n  Equity:Opening -110 USD\n')
        cli = [sys.executable, "-m", "bookkeeping", "--workspace", str(root), "propose", "--statement", f"checking={path}"]

        def run(command):
            result = subprocess.run(command, capture_output=True, text=True)
            commands.append(dict(command=[part.replace(str(root), "<workspace>") for part in command],
                                 returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
            return result

        result = run(cli)
        check("CLI auto-mapping propose succeeds", result.returncode == 0)
        proposal = root / "proposed.beancount"
        check("equal transactions survive proposals", proposal.read_text().count("stmt-id:") == 2)
        manifest_path = next((ledger / "evidence" / "statements").glob("*.json"))
        manifest = json.loads(manifest_path.read_text())
        check("evidence retains mapping fingerprint and semantics", bool(manifest["csv_mapping_sha256"]) and bool(manifest["csv_mapping"]))
        (ledger / "combined.beancount").write_text((ledger / "main.beancount").read_text() + proposal.read_text())
        result = run([str(Path(sys.executable).with_name("bean-check")), str(ledger / "combined.beancount")])
        check("proposals and balance assertion pass bean-check", result.returncode == 0)
        result = run(cli)
        check("same interpretation rerun succeeds", result.returncode == 0)
        original_proposal = proposal.read_bytes()
        original_manifest = manifest_path.read_bytes()
        save_mapping(path, {**mapping, "amount_sign": "outflow"})
        result = run(cli)
        check("reinterpretation conflicts with evidence", result.returncode != 0 and "conflict" in (result.stderr + result.stdout).lower())
        check("rejected import preserves proposal and evidence", proposal.read_bytes() == original_proposal and manifest_path.read_bytes() == original_manifest)
        save_mapping(path, {**mapping, "expected_currency": "EUR", "currency": None})
        result = run(cli)
        check("source currency mismatch rejected", result.returncode != 0 and "currency" in (result.stderr + result.stdout).lower())

    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(json.dumps(dict(status="passed", checks=checks, commands=commands), indent=2) + "\n")
    print(f"PASS: {len(checks)} checks; artifact: {args.artifact}")


if __name__ == "__main__":
    main()
