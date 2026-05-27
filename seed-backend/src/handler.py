"""API Gateway Lambda entry point (design doc §6).

Routes every endpoint from §6.1 with the Powertools REST resolver and
implements the POST /thoughts core flow from §6.2.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import (
    APIGatewayRestResolver,
    Response,
    content_types,
)
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    NotFoundError,
    ServiceError,
    UnauthorizedError,
)
from pydantic import ValidationError

import canonicalization
import config
import db
import embedding
import matching
import pinecone_client
from models import (
    ConfirmInput,
    LoginInput,
    MatchType,
    RefreshInput,
    SignupInput,
    ThoughtInput,
)

logger = Logger()
app = APIGatewayRestResolver()

_cognito = None


# ── helpers ──

def _cognito_client():
    global _cognito
    if _cognito is None:
        import boto3

        _cognito = boto3.client("cognito-idp")
    return _cognito


def _user_id() -> str:
    """Cognito subject from the API Gateway authorizer claims."""
    authorizer = app.current_event.request_context.authorizer
    claims = getattr(authorizer, "claims", None) or {}
    sub = claims.get("sub")
    if not sub:
        raise UnauthorizedError("missing subject claim")
    return sub


def _json_body() -> dict[str, Any]:
    body = app.current_event.json_body
    if not isinstance(body, dict):
        raise BadRequestError("request body must be a JSON object")
    return body


def _clean(value: Any) -> Any:
    """Recursively convert DynamoDB Decimals for JSON serialization."""
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def _created(payload: dict[str, Any]) -> Response:
    return Response(
        status_code=201,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(_clean(payload)),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── auth (Cognito wrapper) ──

@app.post("/auth/signup")
def signup() -> dict[str, Any]:
    try:
        body = SignupInput(**_json_body())
    except ValidationError as e:
        raise BadRequestError(str(e))
    try:
        resp = _cognito_client().sign_up(
            ClientId=config.cognito_client_id(),
            Username=body.email,
            Password=body.password,
            UserAttributes=[{"Name": "email", "Value": body.email}],
        )
    except Exception as e:  # noqa: BLE001 - surface Cognito errors as 400
        raise BadRequestError(_cognito_message(e))
    return {
        "user_sub": resp["UserSub"],
        "confirmation_required": not resp.get("UserConfirmed", False),
    }


@app.post("/auth/login")
def login() -> dict[str, Any]:
    try:
        body = LoginInput(**_json_body())
    except ValidationError as e:
        raise BadRequestError(str(e))
    try:
        resp = _cognito_client().initiate_auth(
            ClientId=config.cognito_client_id(),
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": body.email, "PASSWORD": body.password},
        )
    except Exception as e:  # noqa: BLE001
        raise UnauthorizedError(_cognito_message(e))
    return _auth_result(resp)


@app.post("/auth/refresh")
def refresh() -> dict[str, Any]:
    try:
        body = RefreshInput(**_json_body())
    except ValidationError as e:
        raise BadRequestError(str(e))
    try:
        resp = _cognito_client().initiate_auth(
            ClientId=config.cognito_client_id(),
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": body.refresh_token},
        )
    except Exception as e:  # noqa: BLE001
        raise UnauthorizedError(_cognito_message(e))
    return _auth_result(resp)


def _auth_result(resp: dict[str, Any]) -> dict[str, Any]:
    result = resp.get("AuthenticationResult", {})
    return {
        "access_token": result.get("AccessToken"),
        "refresh_token": result.get("RefreshToken"),
        "id_token": result.get("IdToken"),
        "expires_in": result.get("ExpiresIn"),
    }


def _cognito_message(e: Exception) -> str:
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Message", str(e))
    return str(e)


# ── thoughts ──

@app.post("/thoughts")
def submit_thought() -> Response:
    user_id = _user_id()
    try:
        body = ThoughtInput(**_json_body())
    except ValidationError as e:
        raise BadRequestError(str(e))

    today = date.today().isoformat()

    # Early existence check avoids a needless embedding when the day is taken;
    # the conditional write below is still the authoritative guard.
    if db.get_thought_for_date(user_id, today) is not None:
        raise ServiceError(409, "thought already submitted today")

    vector = embedding.embed(body.text)
    matches = pinecone_client.query(vector, top_k=5)
    decision = matching.decide_match(
        matches,
        config.similarity_threshold_auto(),
        config.similarity_threshold_min(),
    )

    if decision.action is matching.MatchAction.CANDIDATES:
        return Response(
            status_code=200,
            content_type=content_types.APPLICATION_JSON,
            body=json.dumps(
                {
                    "status": "candidates",
                    "candidates": [
                        {
                            "canonical_id": c["id"],
                            "text": c["text"],
                            "score": c["score"],
                        }
                        for c in decision.candidates
                    ],
                    "raw_text_hash": hashlib.sha256(
                        body.text.encode()
                    ).hexdigest(),
                }
            ),
        )

    if decision.action is matching.MatchAction.AUTO_LINK:
        top = decision.top
        assert top is not None
        canonical_id = top["id"]
        score = top["score"]
        canonical_text = top["text"]
        match_type = MatchType.AUTO_LINKED
    else:  # NEW_CANONICAL
        canonical_id, canonical_text, score = _create_canonical(
            body.text, user_id, today, matches
        )
        match_type = MatchType.NEW_CANONICAL

    return _link_thought(
        user_id=user_id,
        today=today,
        raw_text=body.text,
        canonical_id=canonical_id,
        canonical_text=canonical_text,
        score=score,
        match_type=match_type,
        increment=match_type is not MatchType.NEW_CANONICAL,
    )


@app.post("/thoughts/confirm")
def confirm_thought() -> Response:
    user_id = _user_id()
    try:
        body = ConfirmInput(**_json_body())
    except ValidationError as e:
        raise BadRequestError(str(e))

    today = date.today().isoformat()
    if db.get_thought_for_date(user_id, today) is not None:
        raise ServiceError(409, "thought already submitted today")

    canonical = db.get_canonical(body.canonical_id)
    if canonical is None:
        raise NotFoundError("canonical not found")

    # Recover the similarity score for the chosen canonical.
    vector = embedding.embed(body.text)
    matches = pinecone_client.query(vector, top_k=10)
    score = next(
        (m["score"] for m in matches if m["id"] == body.canonical_id), 0.0
    )

    return _link_thought(
        user_id=user_id,
        today=today,
        raw_text=body.text,
        canonical_id=body.canonical_id,
        canonical_text=canonical.get("text", ""),
        score=score,
        match_type=MatchType.USER_CONFIRMED,
        increment=True,
    )


def _create_canonical(
    raw_text: str, user_id: str, today: str, matches: list[dict[str, Any]]
) -> tuple[str, str, float]:
    """Generalize, embed, and persist a brand-new canonical."""
    generalized = canonicalization.canonicalize(raw_text)
    gen_vector = embedding.embed(generalized)
    canonical_id = str(uuid.uuid4())
    now = _now_iso()
    source_thought_id = f"USER#{user_id}|THOUGHT#{today}"

    # Pinecone upsert first; an orphaned vector is inert if a later write
    # fails (design doc §5.3).
    pinecone_client.upsert(
        canonical_id=canonical_id,
        vector=gen_vector,
        text=generalized,
        linked_count=1,
        category="uncategorized",
        created_at=now,
    )
    db.put_canonical(
        canonical_id=canonical_id,
        text=generalized,
        source_thought_id=source_thought_id,
        now_iso=now,
    )
    # Best prior match score we saw (always < min threshold here), else 0.
    prior = max((m["score"] for m in matches), default=0.0)
    return canonical_id, generalized, prior


def _link_thought(
    *,
    user_id: str,
    today: str,
    raw_text: str,
    canonical_id: str,
    canonical_text: str,
    score: float,
    match_type: MatchType,
    increment: bool,
) -> Response:
    now = _now_iso()
    try:
        db.put_thought(
            user_id=user_id,
            date_iso=today,
            raw_text=raw_text,
            canonical_id=canonical_id,
            similarity_score=score,
            match_type=match_type.value,
            now_iso=now,
        )
    except db.ThoughtExistsError:
        raise ServiceError(409, "thought already submitted today")

    if increment:
        new_count = db.increment_linked_count(canonical_id, now)
        pinecone_client.update_metadata(canonical_id, linked_count=new_count)

    return _created(
        {
            "thought_id": f"USER#{user_id}|THOUGHT#{today}",
            "canonical_id": canonical_id,
            "canonical_text": canonical_text,
            "similarity_score": score,
            "match_type": match_type.value,
        }
    )


# ── history ──

@app.get("/thoughts/today")
def get_today() -> dict[str, Any]:
    user_id = _user_id()
    item = db.get_thought_for_date(user_id, date.today().isoformat())
    if item is None:
        raise NotFoundError("no thought submitted today")
    return _clean(item)


@app.get("/thoughts/mine")
def get_history() -> dict[str, Any]:
    user_id = _user_id()
    event = app.current_event
    end = event.get_query_string_value("end") or date.today().isoformat()
    start = event.get_query_string_value("start") or (
        date.fromisoformat(end) - timedelta(days=30)
    ).isoformat()
    items = db.query_history(user_id, start, end)
    return {"start": start, "end": end, "thoughts": _clean(items)}


# ── rooms ──

@app.get("/rooms/<canonical_id>")
def get_room(canonical_id: str) -> dict[str, Any]:
    _user_id()  # auth required
    canonical = db.get_canonical(canonical_id)
    if canonical is None:
        raise NotFoundError("room not found")
    return {
        "canonical_id": canonical_id,
        "text": canonical.get("text", ""),
        "linked_count": _clean(canonical.get("linked_count", 0)),
        "created_at": canonical.get("created_at"),
    }


@app.get("/rooms/<canonical_id>/thoughts")
def get_room_thoughts(canonical_id: str) -> dict[str, Any]:
    _user_id()
    event = app.current_event
    limit = int(event.get_query_string_value("limit") or "25")
    cursor = event.get_query_string_value("cursor")
    items, next_cursor = db.query_room(canonical_id, limit=limit, cursor=cursor)
    # P0 privacy (doc §10): expose only non-identifying fields, never the
    # raw_text of other users' thoughts.
    thoughts = [
        {
            "created_at": i.get("created_at"),
            "match_type": i.get("match_type"),
            "similarity_score": _clean(i.get("similarity_score")),
        }
        for i in items
    ]
    return {"thoughts": thoughts, "next_cursor": next_cursor}


@logger.inject_lambda_context
def handler(event, context):
    return app.resolve(event, context)
