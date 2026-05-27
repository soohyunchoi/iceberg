"""Pure threshold-matching logic (design doc §3.4).

No AWS / network imports — directly unit-testable. Callers normalize their
vector-store results into ``Match`` dicts before invoking ``decide_match``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict


class Match(TypedDict):
    id: str
    score: float
    text: str


class MatchAction(str, Enum):
    AUTO_LINK = "AUTO_LINK"
    CANDIDATES = "CANDIDATES"
    NEW_CANONICAL = "NEW_CANONICAL"


@dataclass
class MatchDecision:
    action: MatchAction
    top: Match | None = None
    candidates: list[Match] = field(default_factory=list)


def decide_match(
    matches: list[Match],
    auto_threshold: float,
    min_threshold: float,
    candidate_limit: int = 3,
) -> MatchDecision:
    """Map ranked matches to an action.

    >= auto_threshold        -> AUTO_LINK to the top match
    [min, auto) grey zone    -> CANDIDATES (top-N at or above min)
    < min_threshold / empty  -> NEW_CANONICAL
    """
    if not matches:
        return MatchDecision(MatchAction.NEW_CANONICAL)

    ranked = sorted(matches, key=lambda m: m["score"], reverse=True)
    top = ranked[0]

    if top["score"] >= auto_threshold:
        return MatchDecision(MatchAction.AUTO_LINK, top=top)

    if top["score"] >= min_threshold:
        candidates = [m for m in ranked if m["score"] >= min_threshold][
            :candidate_limit
        ]
        return MatchDecision(MatchAction.CANDIDATES, candidates=candidates)

    return MatchDecision(MatchAction.NEW_CANONICAL)
