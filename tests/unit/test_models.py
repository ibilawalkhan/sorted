"""Unit tests for the Pydantic domain models.

Each test also documents one validation rule, so reading this file tells you
exactly what the models guarantee.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from shared.models import (
    Category,
    Expense,
    ExpenseStatus,
    MonthlyAgg,
    Override,
    Profile,
)


def _make_expense(**overrides: object) -> Expense:
    """Build a valid Expense, letting individual tests override single fields."""
    defaults: dict[str, object] = {
        "user_id": "user-123",
        "date": date(2026, 7, 16),
        "merchant": "Woolworths",
        "total": Decimal("42.50"),
        "s3_key": "user-123/abc.jpg",
        "content_hash": "deadbeef",
    }
    return Expense(**{**defaults, **overrides})


# --- defaults & auto-generated fields ----------------------------------------


def test_expense_defaults_are_populated() -> None:
    exp = _make_expense()
    assert exp.category is Category.OTHER
    assert exp.status is ExpenseStatus.OK
    assert exp.currency == "AUD"
    assert exp.line_items == []
    assert exp.id  # a uuid was generated
    assert isinstance(exp.created_at, datetime)
    assert exp.created_at.tzinfo is not None  # timezone-aware, not naive


# --- snake_case <-> camelCase bridging ---------------------------------------


def test_expense_serialises_to_camelcase() -> None:
    dumped = _make_expense().model_dump(by_alias=True)
    assert "s3Key" in dumped
    assert "contentHash" in dumped
    assert "createdAt" in dumped
    # money stays a Decimal in python-mode dump — exactly what boto3/DynamoDB needs
    assert isinstance(dumped["total"], Decimal)


def test_expense_accepts_camelcase_input() -> None:
    exp = Expense.model_validate(
        {
            "userId": "user-123",
            "date": "2026-07-16",
            "merchant": "Cafe",
            "total": "9.90",
            "s3Key": "user-123/x.jpg",
            "contentHash": "abc",
        }
    )
    assert exp.merchant == "Cafe"
    assert exp.total == Decimal("9.90")
    assert exp.date == date(2026, 7, 16)  # string was parsed into a date


# --- field-level validation --------------------------------------------------


def test_negative_total_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_expense(total=Decimal("-1.00"))


def test_too_many_decimal_places_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_expense(total=Decimal("9.999"))


def test_invalid_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_expense(category="Groceriez")


def test_invalid_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make_expense(currency="dollars")


def test_reassignment_is_validated() -> None:
    exp = _make_expense()
    with pytest.raises(ValidationError):
        exp.total = Decimal("-5.00")  # blocked by validate_assignment=True


# --- cross-field invariant (the model_validator) -----------------------------


def test_ok_expense_requires_merchant_and_total() -> None:
    with pytest.raises(ValidationError):
        Expense(
            user_id="u",
            date=date(2026, 7, 16),
            s3_key="u/x.jpg",
            content_hash="h",
            status=ExpenseStatus.OK,  # OK but missing merchant/total
        )


def test_needs_review_expense_allows_missing_fields() -> None:
    exp = Expense(
        user_id="u",
        date=date(2026, 7, 16),
        s3_key="u/x.jpg",
        content_hash="h",
        status=ExpenseStatus.NEEDS_REVIEW,
    )
    assert exp.total is None
    assert exp.merchant is None


# --- other entities ----------------------------------------------------------


def test_monthly_agg_defaults_and_month_pattern() -> None:
    agg = MonthlyAgg(user_id="u", month="2026-07")
    assert agg.grand_total == Decimal("0")
    assert agg.expense_count == 0
    assert agg.totals_by_category == {}
    with pytest.raises(ValidationError):
        MonthlyAgg(user_id="u", month="2026-7")  # month not zero-padded


def test_override_is_valid() -> None:
    ovr = Override(user_id="u", merchant_norm="woolworths", category=Category.GROCERIES)
    assert ovr.category is Category.GROCERIES
    assert ovr.updated_at.tzinfo is not None


def test_profile_defaults_to_opt_in() -> None:
    prof = Profile(user_id="u", email="a@b.com")
    assert prof.digest_opt_in is True
