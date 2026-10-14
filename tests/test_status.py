import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from bookkeeping.status import account_status, render

from tests.support import make_workspace

LEDGER = """\
2020-01-01 open Assets:Bank:Checking USD
2020-01-01 open Expenses:Stuff USD
2020-01-01 open Equity:Opening USD

2026-01-10 * "Coffee"
  Assets:Bank:Checking  -5.00 USD
  Expenses:Stuff  5.00 USD

2026-02-01 ! "Mystery"
  Assets:Bank:Checking  -9.00 USD
  Expenses:Stuff  9.00 USD

2026-02-02 balance Assets:Bank:Checking  -14.00 USD
"""

ACCOUNT_MAP = """\
[accounts."checking"]
name = "Checking"
account = "Assets:Bank:Checking"
cutover_date = "2026-01-01"
invert = false
lifecycle = "active"
expected_cadence_days = 35

[accounts."unused"]
name = "Never used"
account = "Assets:Bank:Empty"
cutover_date = "2026-01-01"
invert = false
lifecycle = "dormant"

[accounts."missing"]
name = "Missing feed"
account = "Assets:Bank:Missing"
cutover_date = "2026-01-01"
invert = false
lifecycle = "active"
expected_cadence_days = 35
"""


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ledger = root / "main.beancount"
        self.ledger.write_text(
            LEDGER
            + "2020-01-01 open Assets:Bank:Empty USD\n"
            + "2020-01-01 open Assets:Bank:Missing USD\n",
            encoding="utf-8",
        )
        self.account_map = root / "account_map.toml"
        self.account_map.write_text(ACCOUNT_MAP, encoding="utf-8")
        self.workspace = make_workspace(
            root,
            ledger="main.beancount",
            sources="account_map.toml",
            chart={"transfer_clearing": "Liabilities:Current:Tracking-Transfers"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_fields(self):
        status = account_status(self.workspace, today=dt.date(2026, 3, 1))
        self.assertEqual(status["flagged_total"], 1)
        self.assertEqual(status["ledger_errors"], 0)
        by_account = {row["account"]: row for row in status["accounts"]}
        checking = by_account["Assets:Bank:Checking"]
        self.assertEqual(checking["last_transaction"], "2026-02-01")
        self.assertEqual(checking["last_assertion"], "2026-02-02")
        self.assertEqual(checking["days_stale"], 28)
        self.assertEqual(checking["flagged"], 1)
        self.assertEqual(checking["lifecycle"], "active")
        self.assertEqual(checking["expected_cadence_days"], 35)
        self.assertEqual(checking["coverage_state"], "current")
        self.assertEqual(checking["next_statement_due"], "2026-03-09")
        empty = by_account["Assets:Bank:Empty"]
        self.assertIsNone(empty["last_transaction"])
        self.assertIsNone(empty["days_stale"])
        self.assertEqual(empty["lifecycle"], "dormant")
        self.assertEqual(empty["coverage_state"], "dormant")
        missing = by_account["Assets:Bank:Missing"]
        self.assertEqual(missing["coverage_state"], "missing_activity")
        self.assertEqual(
            status["obligations"],
            [
                {
                    "account": "Assets:Bank:Missing",
                    "account_key": "missing",
                    "kind": "missing_activity",
                    "coverage_start": "2026-01-01",
                    "coverage_end": "2026-03-01",
                },
                {
                    "account": "Assets:Bank:Missing",
                    "account_key": "missing",
                    "kind": "balance_verification",
                    "coverage_start": "2026-01-01",
                    "coverage_end": "2026-03-01",
                },
            ],
        )
        self.assertEqual(status["close_state"], "needs_attention")

    def test_render_is_tabular(self):
        status = account_status(self.workspace, today=dt.date(2026, 3, 1))
        text = render(status)
        self.assertIn("Assets:Bank:Checking", text)
        self.assertIn("28d", text)
        self.assertIn("close obligations: 2", text)
        self.assertIn("Missing feed: missing_activity · 2026-01-01 through 2026-03-01", text)
        self.assertNotIn("Assets:Bank:Empty", text)
        self.assertIn("1 dormant or closed account hidden", text)

    def test_failed_balance_is_not_reported_as_verified(self):
        self.ledger.write_text(
            LEDGER.replace("-14.00 USD", "-15.00 USD")
            + "2020-01-01 open Assets:Bank:Empty USD\n"
            + "2020-01-01 open Assets:Bank:Missing USD\n",
            encoding="utf-8",
        )

        status = account_status(self.workspace, today=dt.date(2026, 3, 1))
        checking = next(
            row for row in status["accounts"] if row["account"] == "Assets:Bank:Checking"
        )

        self.assertGreater(status["ledger_errors"], 0)
        self.assertIsNone(checking["last_assertion"])
        self.assertIn(
            "ledger_validation",
            [obligation["kind"] for obligation in status["obligations"]],
        )

    def test_unpaired_transfers_block_a_current_close(self):
        self.ledger.write_text(
            LEDGER
            + "2020-01-01 open Assets:Bank:Empty USD\n"
            + "2020-01-01 open Assets:Bank:Missing USD\n"
            + "2020-01-01 open Liabilities:Current:Tracking-Transfers USD\n"
            + "2026-02-10 * \"Unpaired transfer\"\n"
            + "  Liabilities:Current:Tracking-Transfers  25.00 USD\n"
            + "  Expenses:Stuff  -25.00 USD\n",
            encoding="utf-8",
        )

        status = account_status(self.workspace, today=dt.date(2026, 3, 1))

        self.assertEqual(status["tracking_transfer_balance"], "25.00 USD")
        self.assertIn(
            "unpaired_transfers",
            [obligation["kind"] for obligation in status["obligations"]],
        )

    def test_posted_plaid_revision_is_a_close_obligation(self):
        self.ledger.write_text(
            LEDGER
            + "2020-01-01 open Assets:Bank:Empty USD\n"
            + "2020-01-01 open Assets:Bank:Missing USD\n"
            + "2026-02-10 * \"Revised later\"\n"
            + "  Assets:Bank:Checking  -1.00 USD\n"
            + '    stmt-id: "plaid:txn-revised"\n'
            + "  Expenses:Stuff  1.00 USD\n",
            encoding="utf-8",
        )
        revisions = self.workspace.inbox / "plaid" / "current" / "revisions.json"
        revisions.parent.mkdir(parents=True)
        revisions.write_text(
            json.dumps(
                [
                    {
                        "kind": "removed",
                        "transaction_id": "txn-revised",
                        "cursor": "cursor-2",
                    }
                ]
            ),
            encoding="utf-8",
        )

        status = account_status(self.workspace, today=dt.date(2026, 3, 1))

        self.assertIn(
            "feed_revision",
            [obligation["kind"] for obligation in status["obligations"]],
        )


if __name__ == "__main__":
    unittest.main()
