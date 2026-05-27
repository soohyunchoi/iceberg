import pytest
from pydantic import ValidationError

from models import ThoughtInput


def test_strips_control_characters():
    t = ThoughtInput(text="hel\x00lo\x07 world")
    assert t.text == "hello world"


def test_trims_surrounding_whitespace():
    assert ThoughtInput(text="  spaced  ").text == "spaced"


def test_rejects_empty():
    with pytest.raises(ValidationError):
        ThoughtInput(text="")


def test_rejects_whitespace_only_after_sanitization():
    with pytest.raises(ValidationError):
        ThoughtInput(text="   \x00  ")


def test_rejects_over_280_chars():
    with pytest.raises(ValidationError):
        ThoughtInput(text="x" * 281)


def test_accepts_max_length():
    assert len(ThoughtInput(text="x" * 280).text) == 280
