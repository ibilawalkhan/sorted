"""Single-table DynamoDB repository for Sorted.

This module is the ONLY place DynamoDB keys (PK/SK/GSI*) are constructed.
Everything above it speaks in domain models; everything
below it is raw DynamoDB. The read access patterns it serves are numbered
P1-P6.

Table layout (one table, generic keys); <u>=userId::

    Entity      PK        SK                  GSI1PK            GSI1SK  GSI2PK
    Expense     USER#<u>  EXP#<date>#<id>     USER#<u>#CAT#<c>  <date>  USER#<u>#HASH#<h>
    MonthlyAgg  USER#<u>  AGG#<yyyy-mm>       -                 -       -
    Override    USER#<u>  OVR#<merchantNorm>  -                 -       -
    Profile     USER#<u>  PROFILE             -                 -       -

Writes here are the non-transactional ones (a profile put, and item marshalling
reused by the pipeline). The transactional expense create/edit/delete that keep
the MonthlyAgg in sync live alongside this in a follow-up.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.conditions import ConditionBase, Key

from shared.models import Category, Expense, MonthlyAgg, Override, Profile

if TYPE_CHECKING:
    # Imported for types only. mypy-boto3-dynamodb is a dev dependency and is NOT
    # present in the Lambda runtime, so it must never be imported at runtime.
    from mypy_boto3_dynamodb.service_resource import Table

DEFAULT_TABLE_NAME = "sorted"
DEFAULT_PAGE_SIZE = 25

# Key prefixes — the vocabulary of the single table, defined once, only here.
_USER = "USER#"
_EXPENSE_PREFIX = "EXP#"
_AGG_PREFIX = "AGG#"
_OVERRIDE_PREFIX = "OVR#"
_PROFILE_SK = "PROFILE"


# --- Key construction (the single source of PK/SK truth) ---------------------


def _pk(user_id: str) -> str:
    return f"{_USER}{user_id}"


def _expense_sk(expense_date: date, expense_id: str) -> str:
    return f"{_EXPENSE_PREFIX}{expense_date.isoformat()}#{expense_id}"


def _agg_sk(month: str) -> str:
    return f"{_AGG_PREFIX}{month}"


def _override_sk(merchant_norm: str) -> str:
    return f"{_OVERRIDE_PREFIX}{merchant_norm}"


def _gsi1pk(user_id: str, category: Category) -> str:
    return f"{_USER}{user_id}#CAT#{category.value}"


def _gsi2pk(user_id: str, content_hash: str) -> str:
    return f"{_USER}{user_id}#HASH#{content_hash}"


# --- Marshalling: domain model <-> DynamoDB item -----------------------------


def _to_dynamo(value: Any) -> Any:
    # `Any` is honest here: a DynamoDB item is a tree of heterogeneous values.
    """Convert a Pydantic-dumped value into DynamoDB-storable primitives.

    Pydantic can *parse* rich types, but the boto3 resource can only *store*
    str / number / bool / bytes / list / dict / None. So we turn dates and
    datetimes into ISO strings and enums into their string value, while leaving
    Decimal untouched (DynamoDB numbers must be Decimal, never float).
    """
    if value is None or isinstance(value, bool | int | Decimal | bytes):
        return value
    if isinstance(value, Enum):  # StrEnum members -> their plain str value
        return value.value
    if isinstance(value, str):
        return value
    if isinstance(value, date):  # covers datetime too (datetime subclasses date)
        return value.isoformat()
    if isinstance(value, dict):
        return {_to_dynamo(k): _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _keys_for(model: Expense | MonthlyAgg | Override | Profile) -> dict[str, str]:
    """Build the key attributes (PK/SK and any GSI keys) for a model."""
    if isinstance(model, Expense):
        return {
            "PK": _pk(model.user_id),
            "SK": _expense_sk(model.date, model.id),
            "GSI1PK": _gsi1pk(model.user_id, model.category),
            "GSI1SK": model.date.isoformat(),
            "GSI2PK": _gsi2pk(model.user_id, model.content_hash),
        }
    if isinstance(model, MonthlyAgg):
        return {"PK": _pk(model.user_id), "SK": _agg_sk(model.month)}
    if isinstance(model, Override):
        return {"PK": _pk(model.user_id), "SK": _override_sk(model.merchant_norm)}
    if isinstance(model, Profile):
        return {"PK": _pk(model.user_id), "SK": _PROFILE_SK}
    raise TypeError(f"Unsupported model type: {type(model).__name__}")  # defensive


def item_for(model: Expense | MonthlyAgg | Override | Profile) -> dict[str, Any]:
    """Marshal a domain model into a complete DynamoDB item (keys + attributes).

    The item stores every model field (as camelCase attributes) *plus* the key
    attributes. Storing the fields — not just the keys — is what lets us rebuild
    the model on read by validating the attributes and ignoring the keys.
    """
    attrs: dict[str, Any] = _to_dynamo(model.model_dump(by_alias=True))
    return {**_keys_for(model), **attrs}


# --- The repository ----------------------------------------------------------


class Repo:
    """Thin wrapper over the DynamoDB table exposing Sorted's access patterns.

    The boto3 ``Table`` can be injected (tests pass a moto-backed table); in
    Lambda it is created once at construction and reused across warm invocations.
    """

    def __init__(self, table: Table | None = None, *, table_name: str | None = None) -> None:
        if table is not None:
            self._table = table
        else:
            name = table_name or os.environ.get("TABLE_NAME", DEFAULT_TABLE_NAME)
            self._table = boto3.resource("dynamodb").Table(name)

    # -- reads ----------------------------------------------------------------

    def get_expense(self, user_id: str, expense_date: date, expense_id: str) -> Expense | None:
        resp = self._table.get_item(
            Key={"PK": _pk(user_id), "SK": _expense_sk(expense_date, expense_id)}
        )
        item = resp.get("Item")
        return Expense.model_validate(item) if item else None

    def list_expenses(
        self,
        user_id: str,
        *,
        month: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: dict[str, Any] | None = None,
    ) -> tuple[list[Expense], dict[str, Any] | None]:
        """P1 (all) / P2 (one month): a user's expenses, newest first, paginated."""
        prefix = _EXPENSE_PREFIX if month is None else f"{_EXPENSE_PREFIX}{month}"
        condition = Key("PK").eq(_pk(user_id)) & Key("SK").begins_with(prefix)
        items, next_cursor = self._query(condition, limit=limit, cursor=cursor)
        return [Expense.model_validate(i) for i in items], next_cursor

    def list_expenses_by_category(
        self,
        user_id: str,
        category: Category,
        *,
        month: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: dict[str, Any] | None = None,
    ) -> tuple[list[Expense], dict[str, Any] | None]:
        """P3: a user's expenses in one category (optionally one month) via GSI1."""
        # Annotate as the base type: it starts as an `Equals` and may become an
        # `And` when a month filter is added — both are `ConditionBase`.
        condition: ConditionBase = Key("GSI1PK").eq(_gsi1pk(user_id, category))
        if month is not None:
            condition = condition & Key("GSI1SK").begins_with(month)
        items, next_cursor = self._query(condition, index="GSI1", limit=limit, cursor=cursor)
        return [Expense.model_validate(i) for i in items], next_cursor

    def get_monthly_agg(self, user_id: str, month: str) -> MonthlyAgg | None:
        """P4: the pre-computed monthly totals item for the dashboard."""
        resp = self._table.get_item(Key={"PK": _pk(user_id), "SK": _agg_sk(month)})
        item = resp.get("Item")
        return MonthlyAgg.model_validate(item) if item else None

    def list_overrides(self, user_id: str) -> list[Override]:
        """P5: a user's merchant -> category overrides (few per user)."""
        condition = Key("PK").eq(_pk(user_id)) & Key("SK").begins_with(_OVERRIDE_PREFIX)
        items, _ = self._query(condition, limit=1000)
        return [Override.model_validate(i) for i in items]

    def find_expense_by_hash(self, user_id: str, content_hash: str) -> Expense | None:
        """P6: dedupe lookup by content hash via GSI2.

        GSIs are eventually consistent, so a just-written duplicate may not be
        visible immediately — the pipeline's Persist stage backstops this with a
        conditional write (handled in the transactional follow-up).
        """
        condition = Key("GSI2PK").eq(_gsi2pk(user_id, content_hash))
        items, _ = self._query(condition, index="GSI2", limit=1)
        return Expense.model_validate(items[0]) if items else None

    # -- writes (non-transactional) -------------------------------------------

    def put_profile(self, profile: Profile) -> None:
        self._table.put_item(Item=item_for(profile))

    def get_profile(self, user_id: str) -> Profile | None:
        resp = self._table.get_item(Key={"PK": _pk(user_id), "SK": _PROFILE_SK})
        item = resp.get("Item")
        return Profile.model_validate(item) if item else None

    # -- internal -------------------------------------------------------------

    def _query(
        self,
        condition: ConditionBase,
        *,
        index: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: dict[str, Any] | None = None,
        forward: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Run a single Query page. ``forward=False`` returns newest-first."""
        params: dict[str, Any] = {
            "KeyConditionExpression": condition,
            "ScanIndexForward": forward,
            "Limit": limit,
        }
        if index is not None:
            params["IndexName"] = index
        if cursor is not None:
            params["ExclusiveStartKey"] = cursor
        response = self._table.query(**params)
        items: list[dict[str, Any]] = list(response.get("Items", []))
        next_cursor: dict[str, Any] | None = response.get("LastEvaluatedKey")
        return items, next_cursor
