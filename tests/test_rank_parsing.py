"""A rank must agree with the evidence printed beside it.

`ai.visibility` returns a rank and, in the same payload, the sentence the rank was derived from. Two
recorded paid runs contradicted themselves: rank 2 next to evidence reading "🥇 **Notion**", and rank
11 next to an answer whose first line was "**1. Notion**". The cause was that a bold-numbered list —
the most common way a model writes a ranking — matched none of the patterns, so the real list was
invisible and a section heading was counted as the first recommendation.

The module's own docstring already said it: a wrong rank is worse than no rank, because the customer
believes it.
"""
from nodes.visibility_nodes import ranked_items


def test_a_bold_numbered_list_is_the_ranking():
    answer = (
        "Here are the top options:\n"
        "\n"
        "## Top Picks by Use Case\n"
        "\n"
        "**1. Notion** - Best overall for docs + lightweight project management\n"
        "**2. Coda** - Best for spreadsheet-style docs\n"
        "**3. Confluence** - Best for large teams\n"
    )

    assert ranked_items(answer) == ["Notion", "Coda", "Confluence"]


def test_the_section_heading_above_a_numbered_list_is_not_a_recommendation():
    """"Top Picks by Use Case" is a label. Counting it pushed every product down one place."""
    answer = "## Top Picks by Use Case\n\n**1. Notion** - best\n**2. Coda** - also good\n"

    assert "Top Picks by Use Case" not in ranked_items(answer)
    assert ranked_items(answer)[0] == "Notion"


def test_a_plain_numbered_list_still_works():
    answer = "1. Notion - best overall\n2. Coda - runner up\n3. Confluence - enterprise\n"

    assert ranked_items(answer) == ["Notion - best overall", "Coda - runner up",
                                    "Confluence - enterprise"]


def test_headings_are_still_used_when_there_is_no_numbered_list():
    answer = "## Notion\nGreat for docs.\n## Coda\nGood too.\n## Confluence\nEnterprise.\n"

    assert ranked_items(answer) == ["Notion", "Coda", "Confluence"]


def test_bold_bullets_are_still_used_when_there_is_no_numbered_list():
    answer = "- **Notion** - best overall\n- **Coda** - runner up\n- **Confluence** - enterprise\n"

    assert ranked_items(answer) == ["Notion", "Coda", "Confluence"]


def test_a_single_bold_numbered_line_does_not_hijack_a_heading_list():
    """One numbered line is not a ranking; the headings still are."""
    answer = "## Notion\nSee **1. the pricing page** for detail.\n## Coda\ntext\n## Confluence\ntext\n"

    assert ranked_items(answer)[:3] == ["Notion", "Coda", "Confluence"]


def test_the_brand_position_matches_the_evidence():
    """The end-to-end symptom: first place in the text must read as rank 1."""
    answer = "## Top Picks\n**1. Notion** - best\n**2. Coda** - next\n**3. Obsidian** - third\n"

    items = ranked_items(answer)

    assert next(i for i, x in enumerate(items, 1) if "notion" in x.lower()) == 1


def test_a_bold_name_with_no_bullet_is_a_recommendation():
    """The format that produced the second wrong rank: "**🥇 Notion** – Best for docs".

    No bullet, no number. Headings were counted instead, so the product that led the answer was not
    in the list at all.
    """
    answer = (
        'The "best" depends on priorities, but here are the top contenders:\n'
        "\n"
        "## Top Recommendations\n"
        "\n"
        "**\U0001F947 Notion** - Best for docs & knowledge management\n"
        "- Flexible docs, wikis, databases\n"
        "\n"
        "**\U0001F947 ClickUp** - Best for feature breadth\n"
        "- Tasks, docs, whiteboards\n"
        "\n"
        "**\U0001F947 Basecamp** - Best for simplicity\n"
    )

    items = ranked_items(answer)

    assert items == ["Notion", "ClickUp", "Basecamp"]
    assert "Top Recommendations" not in items


def test_medals_and_decoration_are_stripped_from_the_name():
    answer = "**\U0001F947 Notion** - first\n**\U0001F948 Coda** - second\n"

    assert ranked_items(answer) == ["Notion", "Coda"]


def test_section_headings_lose_to_bold_names():
    """Both structures present: the bold names are the recommendations, the headings are labels."""
    answer = (
        "## Top Recommendations\n**Notion** - a\n**Coda** - b\n"
        "## Other Strong Options\n**Asana** - c\n"
    )

    items = ranked_items(answer)

    assert items == ["Notion", "Coda", "Asana"]
    assert "Other Strong Options" not in items


def test_headings_still_win_when_nothing_else_marks_the_items():
    answer = "## Notion\nGreat.\n## Coda\nGood.\n## Confluence\nFine.\n"

    assert ranked_items(answer) == ["Notion", "Coda", "Confluence"]


def test_unstructured_prose_yields_no_ranking_rather_than_a_guess():
    answer = "Honestly it depends on your team. Notion is popular, and Coda has fans too."

    assert ranked_items(answer) == []
