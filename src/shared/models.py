"""Pydantic domain models for Sorted.

These classes are the single source of truth for the *shape* of our data — what
an expense, a monthly aggregate, an override and a profile look like, and what
counts as valid. They deliberately know nothing about DynamoDB keys (PK/SK):
that translation lives in ``shared.repo``, so the single-table key design stays
in exactly one place.

Two representations, one model:

* In Python we use snake_case: ``s3_key``, ``created_at``.
* On the wire (DynamoDB items and the JSON API) we use camelCase: ``s3Key``,
  ``createdAt`` — what the React frontend expects.

Pydantic bridges the two with an alias generator, so we write idiomatic Python
and still store/emit camelCase.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

# --- Controlled vocabularies -------------------------------------------------


class Category(StrEnum):
    """The fixed set of spending categories.

    ``str``-based (``StrEnum``) so each member *is* its text value, which
    serialises cleanly to JSON and DynamoDB with no extra conversion.
    """

    GROCERIES = "Groceries"
    DINING = "Dining"
    TRANSPORT = "Transport"
    UTILITIES = "Utilities"
    SHOPPING = "Shopping"
    HEALTH = "Health"
    OTHER = "Other"


class ExpenseStatus(StrEnum):
    """Lifecycle state of an expense as it moves through the pipeline."""

    OK = "OK"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"


# --- Reusable field types ----------------------------------------------------

# A monetary amount. Never a float: 0.10 has no exact float representation, so
# floats silently drift when summed. Decimal is exact — and is also precisely
# what boto3 requires for DynamoDB number attributes.
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

# 3-letter ISO 4217 currency code, e.g. "AUD".
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


def _utc_now() -> datetime:
    """Timezone-aware 'now' in UTC.

    We store UTC everywhere and localise only for display (e.g. the Sydney-time
    weekly digest). A naive datetime — one without tzinfo — is a classic bug
    source, so we never create one.
    """
    return datetime.now(UTC)


# --- Base model --------------------------------------------------------------


class SortedModel(BaseModel):
    """Shared configuration for every entity.

    * ``alias_generator=to_camel`` + ``validate_by_name``: fields are declared
      in snake_case but accepted *and* emitted as camelCase — and are still
      constructable by their Python name (handy in tests and internal code).
    * ``extra="ignore"``: when we load a DynamoDB item that also carries key
      attributes (PK/SK/GSI*), the model simply ignores them instead of erroring.
    * ``validate_assignment=True``: mutating a field later (e.g. re-categorising)
      is re-validated, so an instance can never drift into an invalid state.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# --- Entities ----------------------------------------------------------------


class LineItem(SortedModel):
    """A single line parsed from a receipt (Textract AnalyzeExpense)."""

    description: str = Field(min_length=1, max_length=300)
    price: Money
    quantity: Decimal | None = Field(default=None, ge=0)


class Expense(SortedModel):
    """One expense — the core entity of the app.

    ``merchant`` and ``total`` are optional because an expense can be persisted
    in a partial state: if Textract can't read a confident total or merchant the
    pipeline stores it as ``NEEDS_REVIEW`` for the user to fix. The
    model validator below enforces that an ``OK`` expense is always complete.
    """

    user_id: str = Field(min_length=1)  # Cognito subject ("sub")
    id: str = Field(default_factory=lambda: str(uuid4()))
    date: date  # receipt date (yyyy-mm-dd), part of the sort key
    merchant: str | None = Field(default=None, min_length=1, max_length=200)
    total: Money | None = None
    currency: CurrencyCode = "AUD"
    category: Category = Category.OTHER
    status: ExpenseStatus = ExpenseStatus.OK
    s3_key: str = Field(min_length=1)  # "{userId}/{uuid}"
    content_hash: str = Field(min_length=1)  # sha256, drives dedupe via GSI2
    line_items: list[LineItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def _ok_expense_is_complete(self) -> Expense:
        """Cross-field invariant: a confidently-processed expense must have the
        fields the dashboard and aggregates depend on."""
        if self.status is ExpenseStatus.OK and (self.merchant is None or self.total is None):
            raise ValueError("An OK expense must have both a merchant and a total.")
        return self


class MonthlyAgg(SortedModel):
    """Pre-computed monthly totals so dashboard reads are O(1) key lookups.
    Updated transactionally with each expense
    mutation, never derived at read time."""

    user_id: str = Field(min_length=1)
    month: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]  # "2026-07"
    totals_by_category: dict[Category, Money] = Field(default_factory=dict)
    expense_count: int = Field(default=0, ge=0)
    grand_total: Money = Decimal("0")


class Override(SortedModel):
    """A user's correction: 'expenses from this merchant are category X'.

    ``merchant_norm`` is the normalised (lower-cased, trimmed) merchant name.
    """

    user_id: str = Field(min_length=1)
    merchant_norm: str = Field(min_length=1)
    category: Category
    updated_at: datetime = Field(default_factory=_utc_now)


class Profile(SortedModel):
    """Per-user profile and settings."""

    user_id: str = Field(min_length=1)
    email: str = Field(min_length=3)  # supplied and verified by Cognito
    digest_opt_in: bool = True  # weekly digest on by default (product choice; opt-out)
    created_at: datetime = Field(default_factory=_utc_now)
