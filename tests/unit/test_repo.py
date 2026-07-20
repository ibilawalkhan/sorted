"""Unit tests for the single-table repository (README section 4 access patterns).

We seed items with ``item_for`` (the same marshalling the write path uses) and
then exercise each read method. Everything runs against moto's in-memory table
via the ``repo`` / ``sorted_table`` fixtures in conftest.py.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from shared.models import Category, Expense, ExpenseStatus, MonthlyAgg, Override, Profile
from shared.repo import Repo, item_for

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


def _expense(**overrides: object) -> Expense:
    defaults: dict[str, object] = {
        "user_id": "u1",
        "date": date(2026, 7, 16),
        "merchant": "Woolworths",
        "total": Decimal("42.50"),
        "category": Category.GROCERIES,
        "s3_key": "u1/a.jpg",
        "content_hash": "hash-a",
    }
    return Expense(**{**defaults, **overrides})


def _seed(table: Table, *models: Expense | MonthlyAgg | Override | Profile) -> None:
    for model in models:
        table.put_item(Item=item_for(model))


# --- marshalling round-trip --------------------------------------------------


def test_expense_round_trips_through_dynamo(repo: Repo, sorted_table: Table) -> None:
    exp = _expense()
    _seed(sorted_table, exp)

    loaded = repo.get_expense("u1", date(2026, 7, 16), exp.id)

    assert loaded is not None
    assert loaded.merchant == "Woolworths"
    assert loaded.total == Decimal("42.50")  # still an exact Decimal, not a float
    assert loaded.category is Category.GROCERIES
    assert loaded.date == date(2026, 7, 16)


def test_get_missing_expense_returns_none(repo: Repo) -> None:
    assert repo.get_expense("u1", date(2026, 7, 16), "nope") is None


# --- P1 / P2: list expenses, newest first, filter by month -------------------


def test_list_expenses_newest_first(repo: Repo, sorted_table: Table) -> None:
    older = _expense(date=date(2026, 7, 1), content_hash="h1", s3_key="u1/1.jpg")
    newer = _expense(date=date(2026, 7, 20), content_hash="h2", s3_key="u1/2.jpg")
    _seed(sorted_table, older, newer)

    expenses, cursor = repo.list_expenses("u1")

    assert [e.date for e in expenses] == [date(2026, 7, 20), date(2026, 7, 1)]
    assert cursor is None


def test_list_expenses_filters_by_month(repo: Repo, sorted_table: Table) -> None:
    july = _expense(date=date(2026, 7, 10), content_hash="hj", s3_key="u1/j.jpg")
    august = _expense(date=date(2026, 8, 3), content_hash="ha", s3_key="u1/a.jpg")
    _seed(sorted_table, july, august)

    expenses, _ = repo.list_expenses("u1", month="2026-07")

    assert len(expenses) == 1
    assert expenses[0].date == date(2026, 7, 10)


def test_list_expenses_paginates(repo: Repo, sorted_table: Table) -> None:
    for day in (1, 2, 3):
        _seed(
            sorted_table,
            _expense(date=date(2026, 7, day), content_hash=f"h{day}", s3_key=f"u1/{day}.jpg"),
        )

    first_page, cursor = repo.list_expenses("u1", limit=2)
    assert len(first_page) == 2
    assert cursor is not None

    second_page, cursor2 = repo.list_expenses("u1", limit=2, cursor=cursor)
    assert len(second_page) == 1
    assert cursor2 is None


# --- P3: list by category via GSI1 -------------------------------------------


def test_list_by_category(repo: Repo, sorted_table: Table) -> None:
    groceries = _expense(category=Category.GROCERIES, content_hash="hg", s3_key="u1/g.jpg")
    dining = _expense(category=Category.DINING, content_hash="hd", s3_key="u1/d.jpg")
    _seed(sorted_table, groceries, dining)

    expenses, _ = repo.list_expenses_by_category("u1", Category.DINING, month="2026-07")

    assert len(expenses) == 1
    assert expenses[0].category is Category.DINING


# --- P4: monthly aggregate ---------------------------------------------------


def test_get_monthly_agg(repo: Repo, sorted_table: Table) -> None:
    agg = MonthlyAgg(
        user_id="u1",
        month="2026-07",
        totals_by_category={Category.GROCERIES: Decimal("42.50")},
        expense_count=1,
        grand_total=Decimal("42.50"),
    )
    _seed(sorted_table, agg)

    loaded = repo.get_monthly_agg("u1", "2026-07")

    assert loaded is not None
    assert loaded.grand_total == Decimal("42.50")
    assert loaded.totals_by_category[Category.GROCERIES] == Decimal("42.50")


# --- P5: overrides -----------------------------------------------------------


def test_list_overrides(repo: Repo, sorted_table: Table) -> None:
    _seed(
        sorted_table,
        Override(user_id="u1", merchant_norm="woolworths", category=Category.GROCERIES),
        Override(user_id="u1", merchant_norm="uber", category=Category.TRANSPORT),
    )

    overrides = repo.list_overrides("u1")

    by_merchant = {o.merchant_norm: o.category for o in overrides}
    assert by_merchant == {
        "woolworths": Category.GROCERIES,
        "uber": Category.TRANSPORT,
    }


# --- P6: dedupe by content hash via GSI2 -------------------------------------


def test_find_expense_by_hash(repo: Repo, sorted_table: Table) -> None:
    exp = _expense(content_hash="unique-hash")
    _seed(sorted_table, exp)

    assert repo.find_expense_by_hash("u1", "unique-hash") is not None
    assert repo.find_expense_by_hash("u1", "never-seen") is None


# --- profile round-trip (a non-transactional write) --------------------------


def test_profile_put_and_get(repo: Repo) -> None:
    repo.put_profile(Profile(user_id="u1", email="a@b.com", digest_opt_in=False))

    loaded = repo.get_profile("u1")

    assert loaded is not None
    assert loaded.email == "a@b.com"
    assert loaded.digest_opt_in is False


# --- isolation between users -------------------------------------------------


def test_users_are_isolated(repo: Repo, sorted_table: Table) -> None:
    _seed(sorted_table, _expense(user_id="u1", content_hash="hu1", s3_key="u1/x.jpg"))
    _seed(sorted_table, _expense(user_id="u2", content_hash="hu2", s3_key="u2/x.jpg"))

    u1_expenses, _ = repo.list_expenses("u1")

    assert len(u1_expenses) == 1
    assert all(e.user_id == "u1" for e in u1_expenses)


# keep ExpenseStatus imported meaningfully: a NEEDS_REVIEW expense still stores
def test_needs_review_expense_persists(repo: Repo, sorted_table: Table) -> None:
    exp = _expense(status=ExpenseStatus.NEEDS_REVIEW, merchant=None, total=None)
    _seed(sorted_table, exp)

    loaded = repo.get_expense("u1", exp.date, exp.id)

    assert loaded is not None
    assert loaded.status is ExpenseStatus.NEEDS_REVIEW
    assert loaded.total is None
