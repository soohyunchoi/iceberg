"""CloudFormation custom-resource handler for a Pinecone Serverless index.

Invoked by CDK during `cdk deploy` / `cdk destroy`. Creates the index on
Create/Update and deletes it on Delete by calling the Pinecone REST API.
Uses only the stdlib + boto3 (both present in the Lambda Python runtime) so
no extra packaging is required.
"""

import json
import os
import urllib.error
import urllib.request

import boto3

PINECONE_API = "https://api.pinecone.io/indexes"
API_VERSION = "2024-10"


def _api_key() -> str:
    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=os.environ["API_KEY_SECRET_ARN"])[
        "SecretString"
    ]


def _create(props: dict, api_key: str) -> None:
    body = json.dumps(
        {
            "name": props["IndexName"],
            "dimension": int(props["Dimension"]),
            "metric": props["Metric"],
            "spec": {
                "serverless": {
                    "cloud": props["Cloud"],
                    "region": props["Region"],
                }
            },
        }
    ).encode()
    req = urllib.request.Request(
        PINECONE_API,
        data=body,
        headers={
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "X-Pinecone-API-Version": API_VERSION,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = already exists, treat as success
            raise


def _delete(props: dict, api_key: str) -> None:
    req = urllib.request.Request(
        f"{PINECONE_API}/{props['IndexName']}",
        headers={"Api-Key": api_key, "X-Pinecone-API-Version": API_VERSION},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError:
        pass  # already gone


def handler(event, context):
    props = event["ResourceProperties"]
    req_type = event["RequestType"]
    api_key = _api_key()

    if req_type in ("Create", "Update"):
        _create(props, api_key)
    elif req_type == "Delete":
        _delete(props, api_key)

    return {"PhysicalResourceId": props["IndexName"]}
