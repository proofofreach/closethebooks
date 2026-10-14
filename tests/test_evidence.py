import hashlib
import json
import subprocess
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from bookkeeping.accounts import AccountSource
from bookkeeping.evidence import create_statement_artifact, select_balance_assertion
from bookkeeping.statements import FeedTransaction, ParsedStatement
from bookkeeping.status import account_status

from tests.support import make_workspace


class StatementEvidenceTests(unittest.TestCase):
    def test_statement_creates_hashed_evidence_and_linked_balance_assertion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            statement = root / "checking.csv"
            statement.write_bytes(b"source statement bytes")
            parsed = ParsedStatement(
                path=statement,
                format="csv",
                transactions=[
                    FeedTransaction(
                        txn_id="txn-1",
                        date="2026-06-15",
                        amount=Decimal("25.50"),
                        description="Deposit",
                        source="statement",
                    )
                ],
                ending_balance=Decimal("125.50"),
                ending_balance_date="2026-06-30",
            )
            source = AccountSource(
                key="checking",
                name="Checking",
                account="Assets:Bank:Checking",
                cutover_date="2026-04-06",
                statement_balance_sign="ledger",
            )

            artifact = create_statement_artifact(
                parsed,
                source,
                root / "ledger" / "evidence" / "statements",
            )

            digest = hashlib.sha256(statement.read_bytes()).hexdigest()
            self.assertEqual(artifact.evidence_path.name, f"{digest}.json")
            self.assertEqual(artifact.source_evidence_path.name, f"{digest}.csv")
            self.assertEqual(
                artifact.source_evidence_path.read_bytes(), b"source statement bytes"
            )
            evidence = json.loads(artifact.evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["sha256"], digest)
            self.assertEqual(evidence["account_key"], "checking")
            self.assertEqual(evidence["transaction_coverage_start"], "2026-06-15")
            self.assertEqual(evidence["ending_balance"], "125.50 USD")
            self.assertEqual(artifact.balance_date, "2026-07-01")
            self.assertEqual(artifact.balance_amount, Decimal("125.50"))
            self.assertEqual(
                artifact.balance_assertion,
                "2026-07-01 balance Assets:Bank:Checking  125.50 USD\n"
                f'  evidence: "../evidence/statements/{digest}.json"\n',
            )

    def test_liability_statement_balance_is_explicitly_inverted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            statement = root / "card.qfx"
            statement.write_bytes(b"credit card statement")
            parsed = ParsedStatement(
                path=statement,
                format="ofx",
                transactions=[],
                ending_balance=Decimal("450.25"),
                ending_balance_date="2026-06-30",
            )
            source = AccountSource(
                key="card",
                name="Card",
                account="Liabilities:Cards:Card",
                cutover_date="2026-04-06",
                statement_balance_sign="invert",
            )

            artifact = create_statement_artifact(
                parsed,
                source,
                root / "evidence",
            )

            evidence = json.loads(artifact.evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["ending_balance"], "-450.25 USD")
            self.assertIn(
                "balance Liabilities:Cards:Card  -450.25 USD",
                artifact.balance_assertion,
            )

            self.assertIsNone(
                select_balance_assertion(artifact, Decimal("-450.25"))
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                select_balance_assertion(artifact, Decimal("-449.00"))

    def test_books_propose_emits_statement_evidence_and_balance_assertion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger"
            ledger.mkdir()
            (ledger / "main.beancount").write_text(
                "2026-01-01 open Assets:Bank:Checking USD\n"
                "2026-01-01 open Expenses:Uncategorized USD\n"
                "2026-01-01 open Equity:Opening USD\n"
                "2026-01-01 * \"Opening balance\"\n"
                "  Assets:Bank:Checking  100.00 USD\n"
                "  Equity:Opening  -100.00 USD\n",
                encoding="utf-8",
            )
            account_map = root / "account_map.toml"
            account_map.write_text(
                '[accounts."checking"]\n'
                'name = "Checking"\n'
                'account = "Assets:Bank:Checking"\n'
                'cutover_date = "2026-01-01"\n'
                'lifecycle = "active"\n'
                'statement_balance_sign = "ledger"\n',
                encoding="utf-8",
            )
            statement = root / "checking.csv"
            statement.write_text(
                "Date,Description,Amount,Balance\n"
                "06/30/2026,OFFICE,-25.00,75.00\n",
                encoding="utf-8",
            )
            output = root / "proposed.beancount"

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bookkeeping",
                    "--workspace",
                    str(make_workspace(root, sources="account_map.toml").root),
                    "propose",
                    "--statement",
                    f"checking={statement}",
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            proposal = output.read_text(encoding="utf-8")
            self.assertIn("2026-07-01 balance Assets:Bank:Checking  75.00 USD", proposal)
            evidence = list((ledger / "evidence" / "statements").glob("*.json"))
            self.assertEqual(len(evidence), 1)
            with (ledger / "main.beancount").open("a", encoding="utf-8") as handle:
                handle.write('\ninclude "years/2026.beancount"\n')
            (ledger / "years").mkdir()
            (ledger / "years" / "2026.beancount").write_text(
                proposal,
                encoding="utf-8",
            )
            validation = subprocess.run(
                [
                    str(Path(sys.executable).with_name("bean-check")),
                    str(ledger / "main.beancount"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(validation.returncode, 0, validation.stderr)

            account_map.write_text(
                account_map.read_text(encoding="utf-8")
                + 'statement_evidence_required_after = "2026-04-07"\n',
                encoding="utf-8",
            )
            status = account_status(make_workspace(root, sources="account_map.toml"), today=date(2026, 7, 2))
            checking = next(row for row in status["accounts"] if row["key"] == "checking")
            self.assertEqual(checking["last_assertion"], "2026-07-01")

            year_file = ledger / "years" / "2026.beancount"
            year_file.write_text(
                year_file.read_text(encoding="utf-8").replace(
                    next(
                        line
                        for line in proposal.splitlines(keepends=True)
                        if "evidence:" in line
                    ),
                    "",
                ),
                encoding="utf-8",
            )
            status = account_status(make_workspace(root, sources="account_map.toml"), today=date(2026, 7, 2))
            checking = next(row for row in status["accounts"] if row["key"] == "checking")
            self.assertIsNone(checking["last_assertion"])
            self.assertEqual(status["statement_evidence_errors"], 1)

            year_file.write_text(proposal, encoding="utf-8")
            manifest = json.loads(evidence[0].read_text(encoding="utf-8"))
            retained_source = evidence[0].parent / manifest["source_evidence"]
            retained_source.write_bytes(b"tampered")
            status = account_status(make_workspace(root, sources="account_map.toml"), today=date(2026, 7, 2))
            self.assertEqual(status["statement_evidence_errors"], 1)

            evidence[0].unlink()
            status = account_status(make_workspace(root, sources="account_map.toml"), today=date(2026, 7, 2))
            checking = next(row for row in status["accounts"] if row["key"] == "checking")
            self.assertIsNone(checking["last_assertion"])
            self.assertEqual(status["statement_evidence_errors"], 1)

    def test_rejected_proposal_leaves_no_statement_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger"
            ledger.mkdir()
            (ledger / "main.beancount").write_text(
                "2026-01-01 open Assets:Bank:Checking USD\n"
                "2026-01-01 open Equity:Opening USD\n"
                '2026-01-01 * "Opening"\n'
                "  Assets:Bank:Checking  99.00 USD\n"
                "  Equity:Opening  -99.00 USD\n"
                "2026-07-01 balance Assets:Bank:Checking  99.00 USD\n",
                encoding="utf-8",
            )
            account_map = root / "account_map.toml"
            account_map.write_text(
                '[accounts."checking"]\n'
                'account = "Assets:Bank:Checking"\n'
                'cutover_date = "2026-01-01"\n',
                encoding="utf-8",
            )
            statement = root / "checking.csv"
            statement.write_text(
                "Date,Description,Amount,Balance\n"
                "06/30/2026,OFFICE,-25.00,75.00\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bookkeeping",
                    "--workspace",
                    str(make_workspace(root, sources="account_map.toml").root),
                    "propose",
                    "--statement",
                    f"checking={statement}",
                    "--output",
                    str(root / "proposed.beancount"),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("conflicts with existing assertion", result.stderr)
            self.assertFalse((ledger / "evidence" / "statements").exists())


if __name__ == "__main__":
    unittest.main()
