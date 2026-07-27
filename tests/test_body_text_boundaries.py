"""Only block boundaries are word boundaries.

`body_text` feeds the word count, the thin-content threshold, readability, keyword extraction,
chunking and figure extraction. Getting its separator wrong is therefore not a formatting nit: with
`get_text(" ")` a page that writes `Py<b>thon</b>` reports two words where a reader sees one, and
python.org's `<time>2026-<span>07-23</span></time>` reaches a figure extractor as "2026- 07-23",
which then publishes the space in a schema.org Dataset.
"""
from checks.page_html import body_text
from fetch import Page


def _page(html: str) -> Page:
    url = "https://example.com/"
    return Page(url=url, requested_url=url, status=200, ok=True, html=html)


def _text(html: str) -> str:
    return body_text(_page(f"<html><body>{html}</body></html>"))


def test_inline_markup_does_not_split_a_word():
    assert _text("<p>Py<b>thon</b> is <em>great</em>.</p>") == "Python is great."


def test_a_date_broken_across_inline_tags_survives_intact():
    """The python.org case, verbatim — this is what put a stray space into a published figure."""
    assert _text("<p><time>2026-<span>07-23</span></time></p>") == "2026-07-23"


def test_block_boundaries_still_separate_words():
    assert _text("<p>first</p><p>second</p>") == "first second"


def test_a_list_does_not_run_its_items_together():
    assert _text("<ul><li>alpha</li><li>beta</li></ul>") == "alpha beta"


def test_table_cells_are_separated():
    assert _text("<table><tr><td>10</td><td>20</td></tr></table>") == "10 20"


def test_br_breaks_a_line():
    assert _text("<p>one<br>two</p>") == "one two"


def test_a_link_inside_a_sentence_stays_part_of_it():
    assert _text('<p>see <a href="/x">the docs</a> for more</p>') == "see the docs for more"


def test_word_count_matches_what_a_reader_would_count():
    html = "<p>The <strong>quick</strong> brown <em>fox</em> jumps</p>"

    assert len(_text(html).split()) == 5


def test_the_title_is_still_excluded():
    page = _page("<html><head><title>Ignored words here</title></head>"
                 "<body><p>Only this</p></body></html>")

    assert body_text(page) == "Only this"
