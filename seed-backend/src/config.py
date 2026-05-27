"""Runtime configuration: environment variables + lazy secret resolution.

Importing this module has no side effects and requires no AWS credentials.
Secrets are fetched from Secrets Manager on first use and cached for the life
of the warm Lambda container.
"""

from __future__ import annotations

import os
from functools import lru_cache


# ── Environment (read lazily so imports never fail) ──

def table_name() -> str:
    return os.environ["DDB_TABLE_NAME"]


def cognito_user_pool_id() -> str:
    return os.environ["COGNITO_USER_POOL_ID"]


def cognito_client_id() -> str:
    return os.environ["COGNITO_CLIENT_ID"]


def model_bucket() -> str:
    return os.environ["MODEL_S3_BUCKET"]


def model_key() -> str:
    return os.environ.get("MODEL_S3_KEY", "minilm-l6-v2.onnx")


def pinecone_index_name() -> str:
    return os.environ.get("PINECONE_INDEX_NAME", "seed-canonicals")


def similarity_threshold_auto() -> float:
    return float(os.environ.get("SIMILARITY_THRESHOLD_AUTO", "0.85"))


def similarity_threshold_min() -> float:
    return float(os.environ.get("SIMILARITY_THRESHOLD_MIN", "0.60"))


# ── Secrets (cached) ──

@lru_cache(maxsize=None)
def _secret(arn: str) -> str:
    import boto3

    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=arn)["SecretString"]


def pinecone_api_key() -> str:
    return _secret(os.environ["PINECONE_API_KEY_SECRET"])


def anthropic_api_key() -> str:
    return _secret(os.environ["ANTHROPIC_API_KEY_SECRET"])
