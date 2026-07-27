"""The thin-content threshold has to be measured the way it was calibrated.

Doxa uses SEONaut's 200-word threshold. SEONaut's `countWords` skips the whole subtree of every `<a>`
and strips Unicode punctuation before splitting, so its 200 counts prose and not navigation. Counting
naively inflates python.org from 592 words to 1,024 — and a page that is mostly menu passes a check
it should fail.
"""
from checks.page_html import body_text, check_content, content_words
from fetch import Page


def _page(body: str) -> Page:
    url = "https://site.test/p"
    return Page(url=url, requested_url=url, status=200, ok=True,
                html=f"<html><head><title>t</title></head><body>{body}</body></html>")


def test_link_text_is_not_counted_as_body_copy():
    page = _page("<nav>" + "".join(f'<a href="/{i}">menu item {i}</a>' for i in range(60)) + "</nav>"
                 "<p>Only these five words.</p>")

    assert content_words(page) == 4        # "Only these five words"


def test_a_menu_heavy_page_is_still_reported_thin():
    """The false negative this fixes: enough navigation to clear 200 words on its own."""
    page = _page("<nav>" + "".join(f'<a href="/{i}">navigation link number {i}</a>'
                                   for i in range(80)) + "</nav>"
                 "<p>" + "word " * 40 + "</p>")

    assert len(body_text(page).split()) > 200, "naive counting would clear the threshold"

    codes = [f.code for f in check_content(page)]
    assert "content.thin" in codes


def test_punctuation_is_stripped_before_splitting():
    """SEONaut replaces Unicode punctuation and symbols with spaces, so hyphenates split."""
    page = _page("<p>state-of-the-art</p>")

    assert content_words(page) == 4


def test_ordinary_prose_is_counted_normally():
    page = _page("<p>" + "word " * 250 + "</p>")

    assert content_words(page) == 250
    assert [f.code for f in check_content(page) if f.code == "content.thin"] == []


def test_body_text_still_includes_link_text():
    """Only the threshold changes. Quoting, chunking and figure extraction need the anchor text."""
    page = _page('<p>Read <a href="/docs">the documentation</a> first.</p>')

    assert body_text(page) == "Read the documentation first."
    assert content_words(page) == 2        # "Read" and "first"


def test_the_count_is_cached_per_page_not_recomputed():
    page = _page("<p>" + "word " * 10 + "</p>")

    assert content_words(page) == content_words(page) == 10
