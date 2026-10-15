"""Portable human-readable dates.

strftime's unpadded-day flags (%-d on Unix, %#d on Windows) are platform
specific, so day numbers are formatted from the date itself instead.
"""

from __future__ import annotations

import datetime as dt


def short_date(value: dt.date) -> str:
    """'Oct 4'"""
    return f"{value:%b} {value.day}"


def long_date(value: dt.date) -> str:
    """'Oct 4, 2026'"""
    return f"{value:%b} {value.day}, {value.year}"
