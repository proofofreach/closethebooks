"""Auditable categorization learned from reviewed ledger transactions."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

NOISE_TOKENS = {
    "ach",
    "card",
    "debit",
    "online",
    "payment",
    "pos",
    "purchase",
    "recurring",
    "transaction",
}


@dataclass(frozen=True)
class CategoryExample:
    description: str
    category: str
    amount: Decimal
    reference: str = "explicit-test-example"
    provenance: str = "reviewed"


@dataclass(frozen=True)
class CategorySuggestion:
    category: str
    confidence: Decimal
    support: int
    examples: tuple[CategoryExample, ...]
    method: str

    @property
    def explanation(self) -> str:
        sample = "; ".join(
            f"{example.description} [{example.provenance}:{example.reference}]"
            for example in self.examples[:2]
        )
        return f"{self.method}; {self.support} evidence example(s): {sample}"


def merchant_fingerprint(description: str) -> str:
    tokens = [
        token
        for token in "".join(
            character.lower()
            if character.isalnum() or character.isspace()
            else " "
            for character in description
        ).split()
        if not token.isdigit() and token not in NOISE_TOKENS
    ]
    return " ".join(tokens)


def _features(description: str, amount: Decimal) -> tuple[str, ...]:
    tokens = merchant_fingerprint(description).split()
    features = [f"token:{token}" for token in tokens]
    features.extend(
        f"bigram:{left}:{right}" for left, right in zip(tokens, tokens[1:])
    )
    if tokens:
        features.append(f"merchant:{tokens[0]}")
    absolute = abs(amount)
    bucket = next(
        label
        for ceiling, label in (
            (Decimal("10"), "under-10"),
            (Decimal("50"), "under-50"),
            (Decimal("200"), "under-200"),
            (Decimal("1000"), "under-1000"),
            (Decimal("Infinity"), "1000-plus"),
        )
        if absolute < ceiling
    )
    features.extend((f"amount:{bucket}", f"direction:{'in' if amount > 0 else 'out'}"))
    return tuple(features)


class LearningCategorizer:
    """Train a small probabilistic model and expose confidence plus provenance."""

    def __init__(self, examples: list[CategoryExample]) -> None:
        self.examples = examples

    def suggest(
        self, description: str, amount: Decimal
    ) -> CategorySuggestion | None:
        fingerprint = merchant_fingerprint(description)
        if not fingerprint:
            return None
        eligible = [
            example
            for example in self.examples
            if (example.amount > 0) == (amount > 0)
        ]
        if not eligible:
            return None

        exact = [
            example
            for example in eligible
            if merchant_fingerprint(example.description) == fingerprint
        ]
        exact_categories = {example.category for example in exact}
        if len(exact) >= 2 and len(exact_categories) == 1:
            return CategorySuggestion(
                exact[0].category,
                Decimal("0.99"),
                len(exact),
                tuple(exact[:2]),
                "repeated-reviewed-pattern",
            )

        feature_counts: dict[str, Counter[str]] = defaultdict(Counter)
        category_counts: Counter[str] = Counter()
        category_totals: Counter[str] = Counter()
        vocabulary = set()
        for example in eligible:
            category_counts[example.category] += 1
            features = _features(example.description, example.amount)
            feature_counts[example.category].update(features)
            category_totals[example.category] += len(features)
            vocabulary.update(features)
        query_features = _features(description, amount)
        vocabulary.update(query_features)
        total_examples = len(eligible)
        category_total = len(category_counts)
        scores = {}
        for category, count in category_counts.items():
            score = math.log((count + 1) / (total_examples + category_total))
            denominator = category_totals[category] + len(vocabulary)
            for feature in query_features:
                score += math.log((feature_counts[category][feature] + 1) / denominator)
            scores[category] = score
        highest = max(scores.values())
        probabilities = {
            category: math.exp(score - highest) for category, score in scores.items()
        }
        probability_total = sum(probabilities.values())
        ranked = sorted(
            (
                probability / probability_total,
                category,
            )
            for category, probability in probabilities.items()
        )
        top_probability, category = ranked[-1]
        runner_up = ranked[-2][0] if len(ranked) > 1 else 0.0
        if len(ranked) > 1 and top_probability - runner_up < 0.15:
            return None

        text_features = {
            feature
            for feature in query_features
            if feature.startswith(("token:", "bigram:", "merchant:"))
        }
        evidence = []
        for example in eligible:
            if example.category != category:
                continue
            overlap = len(text_features & set(_features(example.description, amount)))
            if overlap:
                evidence.append((overlap, abs(example.amount) == abs(amount), example))
        if not evidence:
            return None
        evidence.sort(key=lambda item: (item[0], item[1]), reverse=True)
        support = len(evidence)
        coverage = evidence[0][0] / max(len(text_features), 1)
        confidence = Decimal(
            str(
                min(
                    0.98,
                    top_probability * 0.70
                    + coverage * 0.20
                    + min(support, 3) * 0.03,
                )
            )
        )
        if confidence < Decimal("0.65"):
            return None
        return CategorySuggestion(
            category,
            confidence.quantize(Decimal("0.01")),
            support,
            tuple(item[2] for item in evidence[:2]),
            "probabilistic-ledger-model",
        )
