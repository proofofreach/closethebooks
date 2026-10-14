import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bookkeeping.plaid import (
    PlaidClient,
    PlaidApiError,
    PlaidItemStore,
    PlaidSnapshot,
    PlaidSync,
    affected_revisions,
    complete_connection,
    fetch_all,
    feed_transactions,
)
from bookkeeping.plaid_connect import render_link_page


class PlaidClientTests(unittest.TestCase):
    def test_transaction_sync_collects_every_revision_page(self):
        responses = iter(
            [
                {
                    "added": [
                        {
                            "transaction_id": "txn-1",
                            "account_id": "account-1",
                            "date": "2026-07-01",
                            "amount": 10,
                            "name": "First",
                            "pending": False,
                        }
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-1",
                    "has_more": True,
                },
                {
                    "added": [],
                    "modified": [
                        {
                            "transaction_id": "txn-1",
                            "account_id": "account-1",
                            "date": "2026-07-02",
                            "amount": 12,
                            "name": "Corrected",
                            "pending": False,
                        }
                    ],
                    "removed": [{"transaction_id": "txn-old"}],
                    "next_cursor": "cursor-2",
                    "has_more": False,
                },
            ]
        )
        calls = []

        def post(path, payload):
            calls.append((path, payload))
            return next(responses)

        client = PlaidClient("client", "secret", post=post)

        result = client.sync_transactions("access-token")

        self.assertEqual(result.cursor, "cursor-2")
        self.assertEqual([row["transaction_id"] for row in result.added], ["txn-1"])
        self.assertEqual(
            [row["transaction_id"] for row in result.modified], ["txn-1"]
        )
        self.assertEqual(result.removed, [{"transaction_id": "txn-old"}])
        self.assertEqual([call[1].get("cursor") for call in calls], [None, "cursor-1"])

    def test_transaction_sync_restarts_if_data_mutates_between_pages(self):
        responses = iter(
            [
                {
                    "added": [{"transaction_id": "stale"}],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-1",
                    "has_more": True,
                },
                PlaidApiError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "retry"),
                {
                    "added": [{"transaction_id": "fresh"}],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-2",
                    "has_more": False,
                },
            ]
        )
        cursors = []

        def post(_path, payload):
            cursors.append(payload.get("cursor"))
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        result = PlaidClient("client", "secret", post=post).sync_transactions(
            "access-token"
        )

        self.assertEqual([row["transaction_id"] for row in result.added], ["fresh"])
        self.assertEqual(cursors, [None, "cursor-1", None])

    def test_snapshot_applies_modifications_and_removals_without_losing_raw_events(self):
        with TemporaryDirectory() as directory:
            snapshot = PlaidSnapshot(Path(directory))
            snapshot.apply(
                "item-1",
                PlaidSync(
                    added=[
                        {"transaction_id": "txn-1", "account_id": "account-1", "name": "First"},
                        {"transaction_id": "txn-2", "account_id": "account-1", "name": "Remove"},
                    ],
                    modified=[],
                    removed=[],
                    cursor="cursor-1",
                ),
                "2026-07-10",
            )
            snapshot.apply(
                "item-1",
                PlaidSync(
                    added=[],
                    modified=[
                        {"transaction_id": "txn-1", "account_id": "account-1", "name": "Corrected"}
                    ],
                    removed=[{"transaction_id": "txn-2"}],
                    cursor="cursor-2",
                ),
                "2026-07-10",
            )

            self.assertEqual(
                snapshot.transactions(),
                [
                    {
                        "transaction_id": "txn-1",
                        "account_id": "account-1",
                        "name": "Corrected",
                    }
                ],
            )
            raw_files = sorted((Path(directory) / "raw" / "2026-07-10").glob("*.json"))
            self.assertEqual(len(raw_files), 2)
            self.assertEqual(
                [revision["transaction_id"] for revision in snapshot.pending_revisions()],
                ["txn-1", "txn-2"],
            )
            snapshot.resolve_revision("txn-1")
            self.assertEqual(
                [revision["transaction_id"] for revision in snapshot.pending_revisions()],
                ["txn-2"],
            )

    def test_item_store_keeps_tokens_private_and_updates_cursor(self):
        with TemporaryDirectory() as directory:
            store = PlaidItemStore(Path(directory))
            store.upsert(
                {
                    "item_id": "item-1",
                    "access_token": "access-secret",
                    "institution_name": "Chase",
                    "accounts": [{"account_id": "account-1", "name": "Checking"}],
                    "cursor": None,
                }
            )
            store.update_cursor("item-1", "cursor-1")
            store.upsert(
                {
                    "item_id": "item-1",
                    "access_token": "replacement-secret",
                    "institution_name": "Chase",
                    "accounts": [{"account_id": "account-1", "name": "Checking"}],
                    "cursor": None,
                }
            )

            self.assertEqual(store.items()[0]["cursor"], "cursor-1")
            self.assertEqual(store.items()[0]["access_token"], "replacement-secret")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_connection_exchange_saves_item_and_account_metadata(self):
        responses = {
            "/item/public_token/exchange": {
                "access_token": "access-secret",
                "item_id": "item-1",
            },
            "/item/get": {"item": {"institution_id": "ins-1"}},
            "/institutions/get_by_id": {"institution": {"name": "Chase"}},
            "/accounts/get": {
                "accounts": [{"account_id": "account-1", "name": "Checking"}]
            },
        }

        def post(path, _payload):
            return responses[path]

        with TemporaryDirectory() as directory:
            store = PlaidItemStore(Path(directory))
            connected = complete_connection(
                PlaidClient("client", "secret", post=post),
                store,
                "public-token",
            )

            self.assertEqual(connected["institution_name"], "Chase")
            self.assertEqual(connected["accounts"][0]["account_id"], "account-1")
            self.assertEqual(store.items(), [connected])

    def test_fetch_advances_cursor_only_after_materializing_transactions(self):
        def post(path, payload):
            self.assertEqual(payload["access_token"], "access-secret")
            if path == "/accounts/get":
                return {
                    "accounts": [
                        {
                            "account_id": "account-1",
                            "name": "Checking",
                            "balances": {"current": 100},
                        }
                    ]
                }
            self.assertEqual(path, "/transactions/sync")
            return {
                "added": [
                    {
                        "transaction_id": "txn-1",
                        "account_id": "account-1",
                        "date": "2026-07-10",
                        "amount": 15,
                        "name": "Software",
                        "pending": False,
                    }
                ],
                "modified": [],
                "removed": [],
                "next_cursor": "cursor-1",
                "has_more": False,
            }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = PlaidItemStore(root / "secrets")
            store.upsert(
                {
                    "item_id": "item-1",
                    "access_token": "access-secret",
                    "institution_name": "Chase",
                    "accounts": [],
                }
            )
            snapshot = PlaidSnapshot(root / "inbox" / "plaid")

            summary = fetch_all(
                PlaidClient("client", "secret", post=post),
                store,
                snapshot,
                "2026-07-10",
            )

            self.assertEqual(summary["item-1"]["added"], 1)
            self.assertEqual(store.items()[0]["cursor"], "cursor-1")
            self.assertEqual(snapshot.transactions()[0]["transaction_id"], "txn-1")
            self.assertEqual(
                snapshot.accounts()[0]["balances"]["current"],
                100,
            )

    def test_plaid_amounts_are_normalized_to_money_in_positive(self):
        transactions = feed_transactions(
            [
                {
                    "transaction_id": "purchase",
                    "account_id": "account-1",
                    "date": "2026-07-10",
                    "amount": 15.25,
                    "merchant_name": "Adobe",
                    "name": "ADOBE *CREATIVE CLOUD",
                    "pending": False,
                },
                {
                    "transaction_id": "refund",
                    "account_id": "account-1",
                    "date": "2026-07-11",
                    "amount": -5,
                    "merchant_name": None,
                    "name": "REFUND",
                    "pending": False,
                },
            ]
        )

        self.assertEqual(str(transactions[0].amount), "-15.25")
        self.assertEqual(transactions[0].description, "Adobe")
        self.assertEqual(str(transactions[1].amount), "5.00")
        self.assertEqual(transactions[1].source, "plaid")

    def test_local_link_page_exchanges_token_without_exposing_api_secrets(self):
        page = render_link_page('link-token-</script>', "request-nonce")

        self.assertIn("Plaid.create", page)
        self.assertIn('fetch("/exchange"', page)
        self.assertIn("request-nonce", page)
        self.assertNotIn("link-token-</script>", page)
        self.assertNotIn("PLAID_SECRET", page)

    def test_only_revisions_already_posted_to_the_ledger_block_proposals(self):
        revisions = [
            {"kind": "modified", "transaction_id": "posted"},
            {"kind": "removed", "transaction_id": "never-imported"},
        ]

        self.assertEqual(
            affected_revisions(revisions, {"plaid:posted", "statement:other"}),
            [{"kind": "modified", "transaction_id": "posted"}],
        )


if __name__ == "__main__":
    unittest.main()
