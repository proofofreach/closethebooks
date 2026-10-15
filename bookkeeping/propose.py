"""Turn feed transactions into proposed beancount entries, deduped against the ledger.

The ledger itself is the dedup state: every ingested posting carries
`stmt-id: "<source>:<txn-id>"` metadata. Re-running propose over overlapping
data yields nothing new.
"""

from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from beancount import loader
from beancount.core import data as bc_data

from .accounts import AccountSource as MappedAccount
from .accounts import load_account_sources
from .categorizer import CategoryExample, LearningCategorizer
from .statements import FeedTransaction
from .workspace import Chart


def load_account_map(path: Path, default_currency: str = "USD") -> dict[str, MappedAccount]:
    sources = load_account_sources(path, default_currency)
    mapping = dict(sources)
    for source in sources.values():
        if not source.feed_account_id:
            continue
        if source.feed_account_id in mapping:
            raise ValueError(
                f"{path}: duplicate account or feed id {source.feed_account_id!r}."
            )
        mapping[source.feed_account_id] = source
    return mapping


class Ledger:
    def __init__(self, main_file: Path) -> None:
        self.main_file = main_file
        self.entries, self.errors, self.options = loader.load_file(str(main_file))
        if self.errors:
            raise ValueError(
                f"Cannot propose entries while the ledger has {len(self.errors)} "
                "ledger error(s). Run bean-check and fix them first."
            )

    def seen_stmt_ids(self) -> set[str]:
        seen: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, bc_data.Transaction):
                continue
            for posting in entry.postings:
                meta = posting.meta or {}
                if "stmt-id" in meta:
                    seen.add(str(meta["stmt-id"]))
            if entry.meta and "stmt-id" in entry.meta:
                seen.add(str(entry.meta["stmt-id"]))
        return seen

    def balance_assertion(
        self, account: str, assertion_date: str, currency: str = "USD"
    ) -> Decimal | None:
        matches = [
            entry.amount.number
            for entry in self.entries
            if isinstance(entry, bc_data.Balance)
            and entry.account == account
            and entry.date.isoformat() == assertion_date
            and entry.amount.currency == currency
        ]
        if len(set(matches)) > 1:
            raise ValueError(
                f"Conflicting balance assertions for {account} on {assertion_date}."
            )
        return matches[0] if matches else None

    def category_examples(
        self, for_account: str, currency: str = "USD", chart: Chart | None = None
    ) -> list[CategoryExample]:
        """Return explicitly reviewed or trusted-import learning examples."""
        examples = []
        for entry in self.entries:
            if not isinstance(entry, bc_data.Transaction) or entry.flag != "*":
                continue
            metadata = entry.meta or {}
            if metadata.get("category-review") == "accepted":
                provenance = "reviewed"
            elif metadata.get("xero-source") in {"Spend Money", "Receive Money"}:
                provenance = "trusted-xero-import"
            else:
                continue
            source_postings = [p for p in entry.postings if p.account == for_account]
            others = [p.account for p in entry.postings if p.account != for_account]
            if len(source_postings) != 1 or len(others) != 1:
                continue
            if not others[0].startswith(("Expenses:", "Income:")) or (
                chart.is_uncategorized(others[0])
                if chart
                else "Uncategorized" in others[0]
            ):
                continue
            units = source_postings[0].units
            if units is None or units.currency != currency:
                continue
            description = " ".join(
                part for part in (entry.payee, entry.narration) if part
            )
            statement_ids = [
                str(posting.meta["stmt-id"])
                for posting in entry.postings
                if posting.meta and "stmt-id" in posting.meta
            ]
            filename = Path(str(metadata.get("filename", self.main_file.name))).resolve()
            try:
                ledger_reference = filename.relative_to(
                    self.main_file.parent.resolve()
                )
            except ValueError:
                ledger_reference = Path(filename.name)
            identity = (
                str(metadata.get("stmt-id") or metadata.get("xero-je"))
                if metadata.get("stmt-id") or metadata.get("xero-je")
                else statement_ids[0]
                if statement_ids
                else f"{ledger_reference}:{metadata.get('lineno')}"
            )
            examples.append(
                CategoryExample(
                    description,
                    others[0],
                    units.number,
                    identity,
                    provenance,
                )
            )
        return examples


def quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def propose_entries(
    transactions: list[FeedTransaction],
    mapping: dict[str, MappedAccount],
    ledger: Ledger,
    chart: Chart | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Render proposed beancount entries. Returns (entries, stats)."""
    seen = ledger.seen_stmt_ids()
    stats = Counter()
    proposals: list[tuple[str, str]] = []
    categorizer_cache: dict[str, LearningCategorizer] = {}
    chart = chart or Chart()

    for txn in sorted(transactions, key=lambda t: (t.date, t.txn_id)):
        mapped = mapping.get(txn.account_key)
        if mapped is None:
            stats["unmapped_account"] += 1
            continue
        if mapped.lifecycle != "active":
            stats["inactive_account"] += 1
            continue
        if txn.source != "statement" and txn.source != mapped.ingestion:
            stats["source_mismatch"] += 1
            continue
        if txn.pending:
            stats["pending_skipped"] += 1
            continue
        if not txn.date:
            stats["missing_date"] += 1
            continue
        if txn.date < mapped.cutover_date:
            stats["pre_cutover"] += 1
            continue
        stmt_id = f"{txn.source}:{txn.txn_id}"
        if stmt_id in seen:
            stats["duplicate"] += 1
            continue
        seen.add(stmt_id)
        amount = -txn.amount if mapped.invert else txn.amount
        counter_account = (
            chart.uncategorized_income if amount > 0 else chart.uncategorized_expense
        )

        if mapped.account not in categorizer_cache:
            categorizer_cache[mapped.account] = LearningCategorizer(
                ledger.category_examples(mapped.account, mapped.currency, chart)
            )
        suggestion = categorizer_cache[mapped.account].suggest(
            txn.description, amount
        )
        flag = "!"
        if suggestion:
            counter_account = suggestion.category
            stats["categorized_suggestion"] += 1

        lines = [
            f"{txn.date} {flag} {quote(txn.description)}",
        ]
        if suggestion:
            lines.extend(
                [
                    '  category-source: "ledger-learning"',
                    f"  category-suggested: {quote(suggestion.category)}",
                    f'  category-confidence: "{suggestion.confidence:.2f}"',
                    f"  category-evidence: {quote(suggestion.explanation)}",
                ]
            )
        lines.extend([
            f"  {mapped.account}  {amount:.2f} {mapped.currency}",
            f"    stmt-id: {quote(stmt_id)}",
            f"  {counter_account}  {-amount:.2f} {mapped.currency}",
        ])
        proposals.append((txn.date, "\n".join(lines) + "\n"))
        stats["proposed"] += 1

    return [text for _date, text in sorted(proposals)], dict(stats)


def parse_statement_spec(spec: str) -> tuple[str, Path, str | None]:
    """Split `KEY=PATH[:preset]` into its parts."""
    key, separator, rest = spec.partition("=")
    if not separator or not key or not rest:
        raise ValueError(f"Statement must look like KEY=PATH[:preset], got {spec!r}.")
    # Only a trailing ":name" is a preset, so Windows paths (C:\...) survive.
    path_text, colon, preset = rest.rpartition(":")
    if colon and path_text and len(path_text) > 1 and re.fullmatch(r"[A-Za-z0-9_-]+", preset):
        return key, Path(path_text), preset
    return key, Path(rest), None


def run_propose(
    workspace,
    statements: list[tuple[str, Path, str | None]],
    output: Path | None = None,
) -> dict:
    """Normalize feeds and statements into reviewed-later proposals.

    Writes the proposal file and SHA-addressed statement evidence. Never
    touches ledger entries; posting proposals stays a reviewed step.
    """
    import json
    import tempfile

    from .evidence import create_statement_artifact, select_balance_assertion
    from .plaid import PlaidSnapshot, affected_revisions, feed_transactions
    from .statements import money, parse_statement

    mapping = load_account_map(workspace.sources_file, workspace.currency)
    ledger = Ledger(workspace.ledger_main)
    transactions: list[FeedTransaction] = []
    balance_assertions: dict[tuple[str, str], tuple[Decimal, str]] = {}
    evidence_paths: list[str] = []
    evidence_target = workspace.evidence
    evidence_target.parent.mkdir(parents=True, exist_ok=True)

    plaid_snapshot = PlaidSnapshot(workspace.inbox / "plaid")
    impacted = affected_revisions(
        plaid_snapshot.pending_revisions(), ledger.seen_stmt_ids()
    )
    if impacted:
        ids = ", ".join(revision["transaction_id"] for revision in impacted)
        raise RuntimeError(
            "Plaid changed or removed transaction(s) already posted to the ledger: "
            f"{ids}. Review the raw sync evidence, adjust the ledger if needed, "
            "then run `books plaid-revisions --resolve <transaction-id>`."
        )
    transactions.extend(feed_transactions(plaid_snapshot.transactions()))

    teller_raw = workspace.inbox / "teller"
    if teller_raw.exists():
        latest = sorted(d for d in teller_raw.iterdir() if d.is_dir())
        if latest:
            for txn_file in latest[-1].glob("transactions-*.json"):
                account_id = txn_file.stem.replace("transactions-", "")
                for raw in json.loads(txn_file.read_text(encoding="utf-8")):
                    transactions.append(
                        FeedTransaction(
                            txn_id=raw["id"],
                            date=raw.get("date", ""),
                            amount=money(raw.get("amount")),
                            description=(raw.get("description") or "").strip(),
                            source="teller",
                            account_key=account_id,
                            pending=raw.get("status") == "pending",
                            raw=raw,
                        )
                    )

    with tempfile.TemporaryDirectory(
        prefix=".statement-evidence-", dir=evidence_target.parent
    ) as stage_name:
        stage = Path(stage_name)
        for key, path, preset in statements:
            mapped_account = mapping.get(key)
            if mapped_account is None:
                raise ValueError(f"Unknown statement account key: {key!r}.")
            if mapped_account.lifecycle != "active":
                raise ValueError(f"Statement account {key!r} is not active.")
            parsed = parse_statement(path, preset=preset or mapped_account.statement_preset)
            for txn in parsed.transactions:
                transactions.append(
                    FeedTransaction(
                        txn_id=txn.txn_id,
                        date=txn.date,
                        amount=txn.amount,
                        description=txn.description,
                        source=txn.source,
                        account_key=key,
                        pending=txn.pending,
                        raw=txn.raw,
                    )
                )
            artifact = create_statement_artifact(parsed, mapped_account, stage)
            evidence_paths.append(
                workspace.relative(evidence_target / artifact.evidence_path.name)
            )
            if artifact.balance_date and artifact.balance_amount is not None:
                existing = ledger.balance_assertion(
                    mapped_account.account,
                    artifact.balance_date,
                    mapped_account.currency,
                )
                assertion = select_balance_assertion(artifact, existing)
                assertion_key = (mapped_account.account, artifact.balance_date)
                if assertion_key in balance_assertions:
                    prior_amount, _ = balance_assertions[assertion_key]
                    if prior_amount != artifact.balance_amount:
                        raise ValueError(
                            f"Statements propose conflicting balances for "
                            f"{mapped_account.account} on {artifact.balance_date}."
                        )
                elif assertion:
                    balance_assertions[assertion_key] = (
                        artifact.balance_amount,
                        assertion,
                    )

        entries, stats = propose_entries(transactions, mapping, ledger, workspace.chart)
        assertions = [value[1] for value in balance_assertions.values()]
        stats["statement_evidence"] = len(evidence_paths)
        stats["balance_assertions"] = len(assertions)
        output = output or workspace.proposals
        output.parent.mkdir(parents=True, exist_ok=True)
        staged_files = list(stage.iterdir())
        for staged in staged_files:
            destination = evidence_target / staged.name
            if destination.exists() and destination.read_bytes() != staged.read_bytes():
                raise ValueError(f"Statement evidence conflict: {destination}.")
        temporary_output = output.with_suffix(output.suffix + ".tmp")
        temporary_output.write_text(
            "; Proposed entries — review, categorize, then append to "
            f"{workspace.relative(workspace.ledger_root)}/years/<year>.beancount\n\n"
            + "\n".join(entries + assertions),
            encoding="utf-8",
        )
        evidence_target.mkdir(parents=True, exist_ok=True)
        for staged in staged_files:
            destination = evidence_target / staged.name
            if not destination.exists():
                staged.replace(destination)
        temporary_output.replace(output)
    return {
        "stats": stats,
        "output": workspace.relative(output),
        "statement_evidence": evidence_paths,
    }
