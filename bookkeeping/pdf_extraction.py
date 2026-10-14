"""PDF text extraction candidates for deterministic downstream parsers.

This module follows Xandrio's mature extraction seam: try independent engines,
preserve their diagnostics, and let the domain parser choose only a result that
passes its own quality invariants. Financial text is deliberately not repaired
or reflowed here because changing spacing or characters can change amounts.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PdfTextCandidate:
    mode: str
    text: str


@dataclass(frozen=True)
class PdfTextExtraction:
    page_count: int
    candidates: tuple[PdfTextCandidate, ...]
    errors: tuple[str, ...]


def extract_pdf_text_candidates(path: Path) -> PdfTextExtraction:
    """Extract text several ways without deciding which representation is valid."""
    page_count, info_error = _pdf_page_count(path)
    candidates: list[PdfTextCandidate] = []
    errors = [info_error] if info_error else []

    variants = (
        ("poppler-layout", ["-layout"]),
        ("poppler-default", []),
        ("poppler-raw", ["-raw"]),
    )
    for mode, options in variants:
        try:
            result = subprocess.run(
                ["pdftotext", *options, "-enc", "UTF-8", str(path), "-"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            text = _normalize_extracted_text(result.stdout)
            if text:
                candidates.append(PdfTextCandidate(mode, text))
            else:
                errors.append(f"{mode}: no text extracted")
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            errors.append(f"{mode}: {exc}")

    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        for mode, options in (
            ("pypdf-layout", {"extraction_mode": "layout"}),
            ("pypdf-default", {}),
        ):
            try:
                pages = [page.extract_text(**options) or "" for page in reader.pages]
                text = _normalize_extracted_text("\n\f\n".join(pages))
                if text:
                    candidates.append(PdfTextCandidate(mode, text))
                else:
                    errors.append(f"{mode}: no text extracted")
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
        page_count = page_count or len(reader.pages)
    except Exception as exc:  # pypdf exposes several backend-specific errors
        errors.append(f"pypdf: {exc}")

    unique: list[PdfTextCandidate] = []
    seen = set()
    for candidate in candidates:
        fingerprint = candidate.text.strip()
        if fingerprint not in seen:
            unique.append(candidate)
            seen.add(fingerprint)

    return PdfTextExtraction(page_count, tuple(unique), tuple(errors))


def _pdf_page_count(path: Path) -> tuple[int, str | None]:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return 0, f"pdfinfo: {exc}"
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.MULTILINE)
    return (int(match.group(1)) if match else 0), None


def _normalize_extracted_text(text: str) -> str:
    return (
        str(text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .strip()
    )
