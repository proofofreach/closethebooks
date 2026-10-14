from decimal import Decimal

from bookkeeping.categorizer import CategoryExample, LearningCategorizer


def test_similar_merchant_uses_reviewed_corrections_with_evidence() -> None:
    categorizer = LearningCategorizer(
        [
            CategoryExample(
                "ADOBE SYSTEMS", "Expenses:Software", Decimal("-42.50")
            ),
            CategoryExample(
                "ADOBE CREATIVE CLOUD", "Expenses:Software", Decimal("-54.99")
            ),
        ]
    )

    suggestion = categorizer.suggest(
        "POS PURCHASE ADOBE CREATIVE CLOUD 8872", Decimal("-54.99")
    )

    assert suggestion is not None
    assert suggestion.category == "Expenses:Software"
    assert suggestion.support == 2
    assert suggestion.confidence >= Decimal("0.80")
    assert "probabilistic-ledger-model" in suggestion.explanation
    assert "explicit-test-example" in suggestion.explanation


def test_ambiguous_reviewed_history_stays_uncategorized() -> None:
    categorizer = LearningCategorizer(
        [
            CategoryExample("AMAZON", "Expenses:Office", Decimal("-20.00")),
            CategoryExample("AMAZON", "Expenses:Software", Decimal("-20.00")),
        ]
    )

    assert categorizer.suggest("AMAZON", Decimal("-20.00")) is None


def test_opposite_cash_flow_direction_is_not_reused() -> None:
    categorizer = LearningCategorizer(
        [CategoryExample("STRIPE", "Income:Sales", Decimal("100.00"))]
    )

    assert categorizer.suggest("STRIPE", Decimal("-100.00")) is None


def test_unrelated_merchant_is_not_forced_into_only_known_category() -> None:
    categorizer = LearningCategorizer(
        [
            CategoryExample(
                "ADOBE CREATIVE CLOUD", "Expenses:Software", Decimal("-54.99")
            )
        ]
    )

    assert categorizer.suggest("BOBS PLUMBING EMERGENCY", Decimal("-54.99")) is None
