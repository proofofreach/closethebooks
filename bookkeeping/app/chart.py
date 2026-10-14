"""Geometry for the single-series cash balance chart, rendered as inline SVG."""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

WIDTH = 960
HEIGHT = 220
LEFT = 56
RIGHT = 120
TOP = 14
BOTTOM = 28


def _nice_step(span: float, target: int = 4) -> float:
    raw = span / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


def compact(value: float) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            text = f"{value / limit:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"
    return f"{sign}{value:.0f}"


def cash_chart(points: list[tuple[dt.date, Decimal]]) -> dict | None:
    if len(points) < 2:
        return None
    values = [float(value) for _month, value in points]
    low = min(0.0, min(values))
    high = max(0.0, max(values))
    if high == low:
        high = low + 1
    step = _nice_step(high - low)
    low = math.floor(low / step) * step
    high = math.ceil(high / step) * step
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM

    def x(index: int) -> float:
        return LEFT + plot_width * index / (len(points) - 1)

    def y(value: float) -> float:
        return TOP + plot_height * (1 - (value - low) / (high - low))

    coordinates = [(round(x(i), 1), round(y(v), 1)) for i, v in enumerate(values)]
    line = "M" + " L".join(f"{px},{py}" for px, py in coordinates)
    baseline = round(y(max(low, 0.0)), 1)
    area = f"{line} L{coordinates[-1][0]},{baseline} L{coordinates[0][0]},{baseline} Z"
    ticks = []
    tick = low
    while tick <= high + step / 2:
        ticks.append({"y": round(y(tick), 1), "label": compact(tick), "zero": abs(tick) < step / 1000})
        tick += step
    label_indexes = sorted({0, len(points) // 2, len(points) - 1})
    months = [
        {"x": coordinates[i][0], "label": points[i][0].strftime("%b %Y"), "anchor": "start" if i == 0 else "end" if i == len(points) - 1 else "middle"}
        for i in label_indexes
    ]
    return {
        "width": WIDTH,
        "height": HEIGHT,
        "left": LEFT,
        "right": WIDTH - RIGHT,
        "line": line,
        "area": area,
        "ticks": ticks,
        "months": months,
        "end": {"x": coordinates[-1][0], "y": coordinates[-1][1]},
        "points": [
            {"x": px, "y": py, "month": points[i][0].strftime("%B %Y"), "value": f"{points[i][1]:,.2f}"}
            for i, (px, py) in enumerate(coordinates)
        ],
    }
