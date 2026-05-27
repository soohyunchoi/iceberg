"""Pinecone Serverless wrappers.

The Pinecone client and index handle are created lazily and cached on the warm
container; importing this module is side-effect-free.
"""

from __future__ import annotations

from typing import Any

import config

_index = None


def _get_index():
    global _index
    if _index is None:
        from pinecone import Pinecone

        pc = Pinecone(api_key=config.pinecone_api_key())
        _index = pc.Index(config.pinecone_index_name())
    return _index


def query(vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
    """Return matches normalized to ``matching.Match`` dicts."""
    results = _get_index().query(
        vector=vector, top_k=top_k, include_metadata=True
    )
    return [
        {
            "id": m["id"],
            "score": float(m["score"]),
            "text": (m.get("metadata") or {}).get("text", ""),
        }
        for m in results.get("matches", [])
    ]


def upsert(
    *,
    canonical_id: str,
    vector: list[float],
    text: str,
    linked_count: int,
    category: str,
    created_at: str,
) -> None:
    _get_index().upsert(
        vectors=[
            {
                "id": canonical_id,
                "values": vector,
                "metadata": {
                    "text": text,
                    "linked_count": linked_count,
                    "category": category,
                    "created_at": created_at,
                },
            }
        ]
    )


def update_metadata(canonical_id: str, **metadata: Any) -> None:
    _get_index().update(id=canonical_id, set_metadata=metadata)