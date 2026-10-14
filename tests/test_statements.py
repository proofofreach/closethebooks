from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from bookkeeping.pdf_extraction import extract_pdf_text_candidates
from bookkeeping.statements import detect_format, parse_statement


OFX_SAMPLE = """
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260615120000
<TRNAMT>-42.50
<FITID>2026061501
<NAME>ADOBE SYSTEMS
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260616120000
<TRNAMT>1500.00
<FITID>2026061602
<MEMO>CLIENT PAYMENT
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1457.50<DTASOF>20260616</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

PAYPAL_CSV = """Date,Name,Type,Status,Gross,Fee,Balance,Transaction ID
06/01/2026,Client A,Payment,Completed,500.00,-14.80,485.20,TX1001
06/02/2026,Refund guy,Payment,Pending,100.00,0,485.20,TX1002
06/03/2026,Client B,Payment,Completed,250.00,-7.55,727.65,TX1003
"""

GENERIC_CSV = """Posted Date,Description,Amount,Balance
07/01/2026,COFFEE SHOP,-4.50,995.50
07/02/2026,DEPOSIT,1000.00,1995.50
"""

QUICKBOOKS_CHECKING_LINES = [
    "QUICKBOOKS CHECKING",
    "Statement period 06/01/2026 - 06/30/2026",
    "Beginning balance                                      $1,000.00",
    "Ending balance                                         $2,447.25",
    "",
    "DATE        DESCRIPTION                    MONEY IN     MONEY OUT    BALANCE",
    "06/15/2026  CLIENT PAYMENT                 1,500.00                   2,500.00",
    "06/16/2026  ADOBE SYSTEMS                               42.50        2,457.50",
    "06/17/2026  MONTHLY FEE                                 10.25        2,447.25",
]

QUICKBOOKS_PRODUCTION_LAYOUT_LINES = [
    "QUICKBOOKS CHECKING",
    "Beginning Balance on Jul. 10, 2025                               $100.00",
    "Credits                                                           + $10.00",
    "Debits                                                            - $20.00",
    "Ending Balance on Aug. 09, 2025                                  $90.00",
    "",
    "TRANSACTIONS",
    "DATE             DESCRIPTION                                      AMOUNT",
    "08/01/2025       SOFTWARE VENDOR                                  - $20.00",
    "                 Debit",
    "",
    "INTEREST",
    "DATE             DESCRIPTION                                      AMOUNT",
    "08/09/2025       Interest Earned Credit                           + $10.00",
]


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_simple_pdf(path: Path, lines: list[str]) -> Path:
    """Write a small searchable PDF, following Xandrio's extraction fixture."""
    content = "\n".join(
        ["BT", "/F1 10 Tf", "54 750 Td", "14 TL"]
        + [f"({_pdf_escape(line)}) Tj T*" for line in lines]
        + ["ET"]
    )
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        4: (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ),
        5: f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream",
    }
    payload = b"%PDF-1.4\n"
    offsets = [0]
    for object_id in range(1, 6):
        offsets.append(len(payload))
        payload += f"{object_id} 0 obj\n{objects[object_id]}\nendobj\n".encode()
    xref_offset = len(payload)
    payload += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        payload += f"{offset:010d} 00000 n \n".encode()
    payload += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    path.write_bytes(payload)
    return path


class StatementParsingTests(unittest.TestCase):
    def _write(self, name: str, content: str) -> Path:
        path = Path(self._dir.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def test_ofx_uses_fitid_and_ledger_balance(self) -> None:
        parsed = parse_statement(self._write("sample.qfx", OFX_SAMPLE))
        self.assertEqual(parsed.format, "ofx")
        self.assertEqual(len(parsed.transactions), 2)
        self.assertEqual(parsed.transactions[0].txn_id, "2026061501")
        self.assertEqual(parsed.transactions[0].amount, Decimal("-42.50"))
        self.assertEqual(parsed.transactions[1].description, "CLIENT PAYMENT")
        self.assertEqual(parsed.ending_balance, Decimal("1457.50"))
        self.assertEqual(parsed.ending_balance_date, "2026-06-16")

    def test_qbo_is_detected_as_ofx(self) -> None:
        self.assertEqual(detect_format(Path("chase.qbo")), "ofx")

    def test_paypal_preset_filters_pending_and_splits_fees(self) -> None:
        parsed = parse_statement(self._write("paypal.csv", PAYPAL_CSV), preset="paypal")
        ids = [t.txn_id for t in parsed.transactions]
        self.assertEqual(ids, ["TX1001", "TX1001-FEE", "TX1003", "TX1003-FEE"])
        fee = parsed.transactions[1]
        self.assertEqual(fee.amount, Decimal("-14.80"))
        self.assertIn("fee", fee.description.lower())
        self.assertEqual(parsed.ending_balance, Decimal("727.65"))

    def test_generic_csv_synthesizes_stable_ids(self) -> None:
        path = self._write("chase.csv", GENERIC_CSV)
        first = parse_statement(path)
        second = parse_statement(path)
        self.assertEqual(
            [t.txn_id for t in first.transactions],
            [t.txn_id for t in second.transactions],
        )
        self.assertEqual(first.transactions[0].amount, Decimal("-4.50"))
        self.assertEqual(first.transactions[0].date, "2026-07-01")
        self.assertEqual(first.ending_balance, Decimal("1995.50"))

    def test_quickbooks_checking_pdf_reconciles_before_returning_transactions(self) -> None:
        path = write_simple_pdf(
            Path(self._dir.name) / "envelope-savings.pdf",
            QUICKBOOKS_CHECKING_LINES,
        )

        parsed = parse_statement(path, preset="quickbooks-checking")

        self.assertEqual(parsed.format, "pdf")
        self.assertEqual([txn.amount for txn in parsed.transactions], [
            Decimal("1500.00"),
            Decimal("-42.50"),
            Decimal("-10.25"),
        ])
        self.assertEqual(parsed.transactions[1].description, "ADOBE SYSTEMS")
        self.assertEqual(parsed.ending_balance, Decimal("2447.25"))
        self.assertEqual(parsed.ending_balance_date, "2026-06-30")
        self.assertTrue(parsed.transactions[0].raw["pdf_extractor"])

    def test_quickbooks_checking_pdf_fails_closed_when_balances_do_not_reconcile(self) -> None:
        lines = [
            line.replace("$2,447.25", "$2,400.00")
            for line in QUICKBOOKS_CHECKING_LINES
        ]
        path = write_simple_pdf(Path(self._dir.name) / "broken.pdf", lines)

        with self.assertRaisesRegex(ValueError, "did not reconcile"):
            parse_statement(path, preset="quickbooks-checking")

    def test_quickbooks_production_pdf_layout_uses_signed_amount_column(self) -> None:
        path = write_simple_pdf(
            Path(self._dir.name) / "green-dot.pdf",
            QUICKBOOKS_PRODUCTION_LAYOUT_LINES,
        )

        parsed = parse_statement(path, preset="quickbooks-checking")

        self.assertEqual(
            [transaction.amount for transaction in parsed.transactions],
            [Decimal("-20.00"), Decimal("10.00")],
        )
        self.assertEqual(parsed.ending_balance, Decimal("90.00"))
        self.assertEqual(parsed.ending_balance_date, "2025-08-09")

    def test_pdf_extraction_has_a_layout_fallback_without_poppler(self) -> None:
        path = write_simple_pdf(
            Path(self._dir.name) / "fallback.pdf",
            QUICKBOOKS_CHECKING_LINES,
        )
        with patch(
            "bookkeeping.pdf_extraction.subprocess.run",
            side_effect=FileNotFoundError("Poppler unavailable"),
        ):
            extraction = extract_pdf_text_candidates(path)

        modes = {candidate.mode for candidate in extraction.candidates}
        self.assertIn("pypdf-layout", modes)
        self.assertEqual(extraction.page_count, 1)

        with patch(
            "bookkeeping.pdf_extraction.subprocess.run",
            side_effect=FileNotFoundError("Poppler unavailable"),
        ):
            parsed = parse_statement(path, preset="quickbooks-checking")
        self.assertEqual(len(parsed.transactions), 3)
        self.assertEqual(parsed.ending_balance, Decimal("2447.25"))


if __name__ == "__main__":
    unittest.main()
