from matching import MatchAction, decide_match

AUTO = 0.85
MIN = 0.60


def _m(id_, score):
    return {"id": id_, "score": score, "text": f"text-{id_}"}


def test_auto_link_above_auto_threshold():
    d = decide_match([_m("a", 0.91), _m("b", 0.70)], AUTO, MIN)
    assert d.action is MatchAction.AUTO_LINK
    assert d.top["id"] == "a"


def test_auto_link_exactly_at_threshold():
    d = decide_match([_m("a", 0.85)], AUTO, MIN)
    assert d.action is MatchAction.AUTO_LINK


def test_candidates_in_grey_zone():
    d = decide_match(
        [_m("a", 0.78), _m("b", 0.72), _m("c", 0.65), _m("d", 0.40)],
        AUTO,
        MIN,
    )
    assert d.action is MatchAction.CANDIDATES
    assert [c["id"] for c in d.candidates] == ["a", "b", "c"]


def test_candidates_capped_at_limit():
    matches = [_m(str(i), 0.80 - i * 0.01) for i in range(6)]
    d = decide_match(matches, AUTO, MIN)
    assert d.action is MatchAction.CANDIDATES
    assert len(d.candidates) == 3


def test_new_canonical_below_min():
    d = decide_match([_m("a", 0.55)], AUTO, MIN)
    assert d.action is MatchAction.NEW_CANONICAL


def test_new_canonical_on_empty():
    d = decide_match([], AUTO, MIN)
    assert d.action is MatchAction.NEW_CANONICAL


def test_unsorted_input_is_ranked():
    d = decide_match([_m("low", 0.62), _m("high", 0.95)], AUTO, MIN)
    assert d.action is MatchAction.AUTO_LINK
    assert d.top["id"] == "high"
