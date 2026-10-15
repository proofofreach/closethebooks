from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bookkeeping.accounts import configured_feed_providers
from bookkeeping.propose import Ledger, MappedAccount, load_account_map, propose_entries
from bookkeeping.statements import FeedTransaction


LEDGER = """
option "operating_currency" "USD"

2020-01-01 open Assets:Bank:Chase-Checking USD
2020-01-01 open Expenses:Software USD
2020-01-01 open Expenses:Uncategorized USD
2020-01-01 open Income:Uncategorized USD

2026-05-01 * "ADOBE SYSTEMS"
  category-review: "accepted"
  Assets:Bank:Chase-Checking  -42.50 USD
  Expenses:Software  42.50 USD

2026-05-02 * "ADOBE SYSTEMS"
  category-review: "accepted"
  Assets:Bank:Chase-Checking  -42.50 USD
  Expenses:Software  42.50 USD

2026-06-01 * "OLD IMPORT"
  Assets:Bank:Chase-Checking  -10.00 USD
    stmt-id: "teller:txn_dup"
  Expenses:Uncategorized  10.00 USD
"""

MAPPING = {
    "acc_1": MappedAccount(
        key="acc_1",
        name="Chase Checking",
        account="Assets:Bank:Chase-Checking",
        cutover_date="2026-04-06",
        ingestion="teller",
    )
}


def txn(txn_id: str, date: str, amount: str, description: str, **kwargs) -> FeedTransaction:
    return FeedTransaction(
        txn_id=txn_id,
        date=date,
        amount=Decimal(amount),
        description=description,
        source="teller",
        account_key="acc_1",
        **kwargs,
    )


class ProposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        main = Path(self._dir.name) / "main.beancount"
        main.write_text(LEDGER, encoding="utf-8")
        self.ledger = Ledger(main)

    def test_dedup_pending_cutover_and_history_suggestion(self) -> None:
        transactions = [
            txn("txn_dup", "2026-06-01", "-10.00", "OLD IMPORT"),  # already in ledger
            txn("txn_pending", "2026-07-01", "-5.00", "PENDING THING", pending=True),
            txn("txn_old", "2026-03-01", "-5.00", "PRE CUTOVER"),
            txn("txn_adobe", "2026-07-02", "-42.50", "ADOBE SYSTEMS"),
            txn("txn_new", "2026-07-03", "250.00", "MYSTERY DEPOSIT"),
        ]
        entries, stats = propose_entries(transactions, MAPPING, self.ledger)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(stats["pending_skipped"], 1)
        self.assertEqual(stats["pre_cutover"], 1)
        self.assertEqual(stats["proposed"], 2)

        adobe = next(e for e in entries if "ADOBE" in e)
        self.assertIn("Expenses:Software", adobe)
        self.assertIn('2026-07-02 ! "ADOBE SYSTEMS"', adobe)
        self.assertIn('category-source: "ledger-learning"', adobe)
        self.assertIn('category-suggested: "Expenses:Software"', adobe)
        self.assertIn('category-confidence: "0.99"', adobe)
        self.assertIn("repeated-reviewed-pattern", adobe)
        self.assertIn("main.beancount:9", adobe)
        self.assertEqual(stats["categorized_suggestion"], 1)
        self.assertIn('stmt-id: "teller:txn_adobe"', adobe)

        deposit = next(e for e in entries if "MYSTERY" in e)
        self.assertIn("Income:Uncategorized", deposit)
        self.assertIn("!", deposit.split("\n")[0])

    def test_rerun_is_idempotent_within_batch(self) -> None:
        transactions = [
            txn("txn_same", "2026-07-02", "-1.00", "X"),
            txn("txn_same", "2026-07-02", "-1.00", "X"),
        ]
        entries, stats = propose_entries(transactions, MAPPING, self.ledger)
        self.assertEqual(stats["proposed"], 1)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(len(entries), 1)

    def test_learning_examples_require_provenance_and_exclude_transfers(self) -> None:
        path = Path(self._dir.name) / "training.beancount"
        path.write_text(
            '2020-01-01 open Assets:Bank:Chase-Checking USD\n'
            '2020-01-01 open Assets:Bank:Savings USD\n'
            '2020-01-01 open Expenses:Software USD\n'
            '2026-01-01 * "Unreviewed"\n'
            '  Assets:Bank:Chase-Checking  -10.00 USD\n'
            '  Expenses:Software  10.00 USD\n'
            '2026-01-02 * "Reviewed transfer"\n'
            '  category-review: "accepted"\n'
            '  Assets:Bank:Chase-Checking  -20.00 USD\n'
            '  Assets:Bank:Savings  20.00 USD\n'
            '2026-01-03 * "Trusted import"\n'
            '  xero-je: "XERO-1"\n'
            '  xero-source: "Spend Money"\n'
            '  Assets:Bank:Chase-Checking  -30.00 USD\n'
            '  Expenses:Software  30.00 USD\n',
            encoding="utf-8",
        )

        examples = Ledger(path).category_examples("Assets:Bank:Chase-Checking")

        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0].description, "Trusted import")
        self.assertEqual(examples[0].reference, "XERO-1")
        self.assertEqual(examples[0].provenance, "trusted-xero-import")

    def test_unmapped_account_reported(self) -> None:
        transactions = [
            FeedTransaction(
                txn_id="t1",
                date="2026-07-01",
                amount=Decimal("-1.00"),
                description="X",
                source="teller",
                account_key="acc_unknown",
            )
        ]
        _entries, stats = propose_entries(transactions, MAPPING, self.ledger)
        self.assertEqual(stats["unmapped_account"], 1)

    def test_account_map_carries_statement_parser_preset(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."envelope-savings"]\n'
            'name = "QuickBooks Checking savings"\n'
            'account = "Assets:Bank:Envelope-Savings"\n'
            'cutover_date = "2026-04-06"\n'
            'statement_preset = "quickbooks-checking"\n',
            encoding="utf-8",
        )

        mapping = load_account_map(path)

        self.assertEqual(
            mapping["envelope-savings"].statement_preset,
            "quickbooks-checking",
        )

    def test_account_map_rejects_unknown_lifecycle(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."checking"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n'
            'lifecycle = "forgotten"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "lifecycle"):
            load_account_map(path)

    def test_account_map_accepts_feed_account_id_as_an_import_alias(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."checking"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n'
            'feed_account_id = "plaid-account-1"\n',
            encoding="utf-8",
        )

        mapping = load_account_map(path)

        self.assertIs(mapping["checking"], mapping["plaid-account-1"])

    def test_ledger_with_parse_errors_cannot_generate_proposals(self) -> None:
        path = Path(self._dir.name) / "broken.beancount"
        path.write_text("2026-99-99 open Assets:Broken USD\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "ledger error"):
            Ledger(path)

    def test_account_map_rejects_unknown_ingestion_provider(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."checking"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n'
            'ingestion = "magic"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "ingestion"):
            load_account_map(path)

    def test_feed_source_must_match_configured_ingestion_provider(self) -> None:
        transaction = txn("plaid-txn", "2026-07-02", "-10", "WRONG SOURCE")
        transaction = FeedTransaction(
            **{**transaction.__dict__, "source": "plaid"}
        )

        entries, stats = propose_entries([transaction], MAPPING, self.ledger)

        self.assertEqual(entries, [])
        self.assertEqual(stats["source_mismatch"], 1)

    def test_account_map_rejects_duplicate_ledger_accounts(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."first"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n'
            '[accounts."second"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "duplicate ledger account"):
            load_account_map(path)

    def test_auto_fetch_uses_only_explicitly_mapped_active_feed_providers(self) -> None:
        sources = {
            "statement": MappedAccount(
                key="statement",
                name="Statement only",
                account="Assets:Bank:Statement",
                cutover_date="2026-04-06",
            ),
            "plaid": MappedAccount(
                key="plaid",
                name="Plaid",
                account="Assets:Bank:Plaid",
                cutover_date="2026-04-06",
                ingestion="plaid",
                feed_account_id="plaid-account",
            ),
            "old-teller": MappedAccount(
                key="old-teller",
                name="Old Teller",
                account="Assets:Bank:Old",
                cutover_date="2026-04-06",
                lifecycle="dormant",
                ingestion="teller",
                feed_account_id="teller-account",
            ),
        }

        self.assertEqual(configured_feed_providers(sources), {"plaid"})

    def test_account_map_rejects_unknown_statement_balance_sign(self) -> None:
        path = Path(self._dir.name) / "account_map.toml"
        path.write_text(
            '[accounts."checking"]\n'
            'account = "Assets:Bank:Checking"\n'
            'cutover_date = "2026-04-06"\n'
            'statement_balance_sign = "guess"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "statement_balance_sign"):
            load_account_map(path)

    def test_ledger_finds_existing_balance_assertion(self) -> None:
        path = Path(self._dir.name) / "with-balance.beancount"
        path.write_text(
            LEDGER
            + "2026-07-01 balance Assets:Bank:Chase-Checking  -95.00 USD\n",
            encoding="utf-8",
        )

        ledger = Ledger(path)

        self.assertEqual(
            ledger.balance_assertion("Assets:Bank:Chase-Checking", "2026-07-01"),
            Decimal("-95.00"),
        )
        self.assertIsNone(
            ledger.balance_assertion("Assets:Bank:Chase-Checking", "2026-08-01")
        )


if __name__ == "__main__":
    unittest.main()


def test_statement_spec_keeps_windows_paths_and_trailing_presets():
    from bookkeeping.propose import parse_statement_spec

    assert parse_statement_spec(r"checking=C:\Users\me\june.csv") == ("checking", Path(r"C:\Users\me\june.csv"), None)
    assert parse_statement_spec(r"paypal=C:\Users\me\pp.csv:paypal") == ("paypal", Path(r"C:\Users\me\pp.csv"), "paypal")
    assert parse_statement_spec("savings=inbox/stmt.pdf:quickbooks-checking") == ("savings", Path("inbox/stmt.pdf"), "quickbooks-checking")
    assert parse_statement_spec("card=inbox/july.csv") == ("card", Path("inbox/july.csv"), None)
