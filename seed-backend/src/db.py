"""DynamoDB single-table repository (design doc §2).

Covers every access pattern in §2.3 over the `seed-primary` table + GSI1.
The boto3 resource is created lazily so importing this module needs no AWS
credentials.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import config

_table = None


class ThoughtExistsError(Exception):
    """Raised when a user already submitted a thought for the given day."""


def _get_table():
    global _table
    if _table is None:
        import boto3

        _table = boto3.resource("dynamodb").Table(config.table_name())
    return _table


def _user_pk(user_id: str) -> str:
    return f"USER#{user_id}"


def _canonical_pk(canonical_id: str) -> str:
    return f"CANONICAL#{canonical_id}"


# ── Users ──

def get_profile(user_id: str) -> dict[str, Any] | None:
    resp = _get_table().get_item(
        Key={"PK": _user_pk(user_id), "SK": "PROFILE"}
    )
    return resp.get("Item")


# ── Thoughts ──

def put_thought(
    *,
    user_id: str,
    date_iso: str,
    raw_text: str,
    canonical_id: str,
    similarity_score: float,
    match_type: str,
    now_iso: str,
) -> dict[str, Any]:
    """Conditionally write today's thought (one-per-day enforced at the DB)."""
    from botocore.exceptions import ClientError

    item = {
        "PK": _user_pk(user_id),
        "SK": f"THOUGHT#{date_iso}",
        "raw_text": raw_text,
        "canonical_id": canonical_id,
        "similarity_score": Decimal(str(similarity_score)),
        "match_type": match_type,
        "created_at": now_iso,
        "GSI1PK": _canonical_pk(canonical_id),
        "GSI1SK": f"THOUGHT#{date_iso}",
    }
    try:
        _get_table().put_item(
            Item=item, ConditionExpression="attribute_not_exists(SK)"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ThoughtExistsError(date_iso) from e
        raise
    return item


def get_thought_for_date(user_id: str, date_iso: str) -> dict[str, Any] | None:
    resp = _get_table().get_item(
        Key={"PK": _user_pk(user_id), "SK": f"THOUGHT#{date_iso}"}
    )
    return resp.get("Item")


def query_history(
    user_id: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    from boto3.dynamodb.conditions import Key

    resp = _get_table().query(
        KeyConditionExpression=Key("PK").eq(_user_pk(user_id))
        & Key("SK").between(f"THOUGHT#{start_date}", f"THOUGHT#{end_date}")
    )
    return resp.get("Items", [])


# ── Canonicals ──

def get_canonical(canonical_id: str) -> dict[str, Any] | None:
    resp = _get_table().get_item(
        Key={"PK": _canonical_pk(canonical_id), "SK": "META"}
    )
    return resp.get("Item")


def put_canonical(
    *, canonical_id: str, text: str, source_thought_id: str, now_iso: str
) -> dict[str, Any]:
    item = {
        "PK": _canonical_pk(canonical_id),
        "SK": "META",
        "text": text,
        "linked_count": 1,
        "category_tags": [],
        "source_thought_id": source_thought_id,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    _get_table().put_item(Item=item)
    return item


def increment_linked_count(canonical_id: str, now_iso: str) -> int:
    resp = _get_table().update_item(
        Key={"PK": _canonical_pk(canonical_id), "SK": "META"},
        UpdateExpression="SET linked_count = linked_count + :inc, "
        "updated_at = :now",
        ExpressionAttributeValues={":inc": 1, ":now": now_iso},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["linked_count"])


def query_room(
    canonical_id: str, limit: int = 25, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginated thoughts linked to a canonical, via GSI1."""
    from boto3.dynamodb.conditions import Key

    kwargs: dict[str, Any] = {
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("GSI1PK").eq(_canonical_pk(canonical_id)),
        "Limit": limit,
        "ScanIndexForward": False,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = _decode_cursor(cursor)

    resp = _get_table().query(**kwargs)
    next_cursor = _encode_cursor(resp.get("LastEvaluatedKey"))
    return resp.get("Items", []), next_cursor


def _encode_cursor(key: dict[str, Any] | None) -> str | None:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
