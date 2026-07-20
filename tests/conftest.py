"""Shared pytest fixtures.

`moto` intercepts boto3 calls and emulates DynamoDB entirely in memory, so these
tests need no AWS account, no network, and leave nothing behind. Each test gets a
fresh table, created with the exact key schema and GSIs from README section 4.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

    from shared.repo import Repo

# moto refuses to run without (fake) credentials and a region; set them before
# any boto3 client is created so a real AWS call can never leak out.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("AWS_REGION", "ap-southeast-2")


@pytest.fixture
def sorted_table() -> Iterator[Table]:
    """A fresh, empty `sorted` table under moto, torn down after each test."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.create_table(
            TableName="sorted",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            # Only KEY attributes are declared; everything else is schemaless.
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "GSI2",
                    "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        yield table


@pytest.fixture
def repo(sorted_table: Table) -> Repo:
    """A Repo wired to the moto-backed table (imported lazily, inside the mock)."""
    from shared.repo import Repo

    return Repo(table=sorted_table)
