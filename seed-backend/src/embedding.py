"""MiniLM-L6-v2 sentence embedding via ONNX Runtime (design doc §4.3).

The model is downloaded from S3 to /tmp on cold start and the ONNX session +
tokenizer are cached in module globals for warm invocations. Importing this
module is side-effect-free; nothing loads until ``embed`` is first called.
"""

from __future__ import annotations

_session = None
_tokenizer = None

_MODEL_PATH = "/tmp/model.onnx"
_TOKENIZER_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_MAX_LENGTH = 128


def load_model() -> None:
    """Idempotently load the ONNX session and tokenizer."""
    global _session, _tokenizer
    if _session is not None:
        return

    import boto3
    import onnxruntime as ort
    from transformers import AutoTokenizer

    import config

    boto3.client("s3").download_file(
        config.model_bucket(), config.model_key(), _MODEL_PATH
    )
    _session = ort.InferenceSession(_MODEL_PATH)
    _tokenizer = AutoTokenizer.from_pretrained(_TOKENIZER_NAME)


def embed(text: str) -> list[float]:
    """Return an L2-normalized 384-dim embedding for ``text``."""
    import numpy as np

    load_model()
    assert _session is not None and _tokenizer is not None

    encoded = _tokenizer(
        text,
        padding=True,
        truncation=True,
        max_length=_MAX_LENGTH,
        return_tensors="np",
    )
    outputs = _session.run(
        None,
        {
            "input_ids": encoded["input_ids"],
            "attention_mask": encoded["attention_mask"],
            "token_type_ids": encoded["token_type_ids"],
        },
    )

    # Mean pooling over token embeddings, weighted by the attention mask.
    token_embeddings = outputs[0]
    mask = encoded["attention_mask"]
    expanded_mask = np.expand_dims(mask, -1)
    summed = np.sum(token_embeddings * expanded_mask, axis=1)
    counted = np.clip(mask.sum(axis=1, keepdims=True), 1, None)
    embedding = (summed / counted)[0]

    norm = np.linalg.norm(embedding)
    return (embedding / norm).tolist()
