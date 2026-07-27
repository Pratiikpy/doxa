"""The extraction window has to follow the figures.

`content.charts` detects figures across the whole page but can only send part of it to a model. The
first implementation sent the first 4,000 characters, which on python.org contains none of the
eighteen figures — every one starts at character 4006. The service charged for the call and reported
"18 statistics detected, 0 extracted, 0 rejected", which is not a page with nothing to say; it is a
service that never looked where the numbers were.
"""
from nodes.content_nodes import STATISTIC, _BUDGET, _MAX_FIGURES, _figure_excerpt


def _excerpt(text: str):
    return _figure_excerpt(text, list(STATISTIC.finditer(text)))


def test_figures_past_the_head_of_the_page_still_reach_the_extractor():
    text = "filler sentence with no numbers at all. " * 120 + "Revenue grew 47% to $3.2 million."
    assert len(text) > 4000, "the figures must sit past a naive head window for this to mean anything"

    excerpt, covered = _excerpt(text)

    assert "47%" in excerpt and "$3.2 million" in excerpt
    assert covered is True


def test_the_sentence_around_a_figure_comes_with_it():
    """A bare "47%" cannot be labelled. The margin is what makes the figure interpretable."""
    text = "x " * 3000 + "Conversion on the pricing page rose to 47% after the redesign." + " y" * 3000

    excerpt, _ = _excerpt(text)

    assert "Conversion on the pricing page rose to 47%" in excerpt


def test_a_page_with_more_figures_than_fit_says_so_rather_than_truncating_silently():
    text = " ".join(f"Metric {i} reached {i}% in the period under review, a notable change."
                    for i in range(400))
    assert len(text) > _BUDGET * 2

    excerpt, covered = _excerpt(text)

    assert covered is False, "the caller must be able to disclose partial coverage"
    assert len(excerpt) <= _BUDGET + 64      # + the separators between kept spans


def test_the_excerpt_is_a_subsequence_of_the_page():
    """Nothing may be introduced into the text a model is asked to quote verbatim from."""
    text = "Before. " * 300 + "Growth was 12.5% year on year. " + "After. " * 300

    excerpt, _ = _excerpt(text)

    for span in excerpt.split("\n[…]\n"):
        assert span in text


def test_one_oversized_span_is_still_sent_rather_than_dropped():
    """Figures packed tightly enough merge into a single span larger than the whole budget.

    Spending the budget span-by-span would then keep nothing at all, and a page dense with numbers —
    exactly the page this service is for — would come back empty.
    """
    text = "".join(f"{i}% " for i in range(_BUDGET))       # every figure within a margin of the next

    excerpt, covered = _excerpt(text)

    assert excerpt, "an empty excerpt would guarantee zero figures from the densest possible page"
    assert covered is False


def test_no_figures_yields_an_empty_excerpt_rather_than_an_error():
    assert _excerpt("prose with no numbers whatsoever") == ("", True)


def test_the_figure_count_is_capped_not_only_the_character_budget():
    """Wikipedia's Python article states 484 numbers.

    A character budget alone does not stop that: the spans are short and hundreds fit. Asking one
    call to label them all exhausted the model's tokens inside its reasoning block and returned
    nothing whatsoever — a paid call that produced an outage message about a page that was fine.
    """
    text = " ".join(f"Region {i} grew {i}% last year." for i in range(_MAX_FIGURES * 6))
    assert len(STATISTIC.findall(text)) > _MAX_FIGURES

    excerpt, covered = _excerpt(text)

    assert covered is False, "a capped read must be disclosed, never presented as the whole page"
    assert len(excerpt) < len(text), "the whole page was sent, so nothing was capped"


def test_a_bare_year_is_not_a_chartable_figure():
    """python.org states eighteen numbers and every one is a year.

    Charting them produces a schema.org Dataset of news dates — valid, machine-readable, worthless.
    Worse, asking a model whether "2026" is a figure answered differently on each run, so one page
    returned 0, 12 and 18 rows across three calls of a paid service.
    """
    from nodes.content_nodes import _CHARTABLE, _classify

    assert _classify("2026") == "year"
    assert "year" not in _CHARTABLE


def test_measurements_are_chartable():
    from nodes.content_nodes import _CHARTABLE, _classify

    for token, kind in [("47%", "percentage"), ("$3.2 million", "currency"),
                        ("2.5x", "multiplier"), ("340 ms", "measurement"),
                        ("1,200,000", "large number")]:
        assert _classify(token) == kind, token
        assert kind in _CHARTABLE, token
