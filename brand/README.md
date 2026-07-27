# Doxa — brand

## The name

**δόξα** — *doxa* — is Plato's word for appearance, opinion, how a thing seems. He sets it against
**ἐπιστήμη** — *episteme*, knowledge — and separates the two with a line (*Republic* VI, 509d).

That distinction is the entire product. A page has facts you can prove: its HTML, its headers, its
schema. It also has an appearance: what a crawler actually manages to read, and what a model says
about you when a buyer asks. The second is just as consequential as the first, and almost nobody
measures it.

The name is also a deliberate pair with **Episteme**, the sibling service. Episteme handles what is
known. Doxa handles how it appears.

## The mark

A circle divided by a horizontal rule.

- **Solid above** — ἐπιστήμη. What is demonstrably in the document.
- **Dashed below** — δόξα. What is observed rather than proven: sampled, real, never certain.
- **The rule overhangs** the circle on both sides, because it orders more than this one thing.

It resolves as an eye and as a horizon. Both are the same idea.

The dashes are not decoration. Half of what Doxa reports comes from asking models questions and
counting answers, and that half is a sample, not a proof. The mark says so before the customer reads
a word.

## Files

| File | Use |
|---|---|
| `doxa-mark.svg` | The mark alone. Favicons, avatars, anywhere under ~200px. |
| `doxa-logo.svg` | Mark, wordmark and tagline. Headers, documents, decks. |
| `doxa-pfp.png` | 1024×1024 avatar, square corners, dark ground. |
| `doxa-mark-1024.png` | Raster mark for contexts that cannot take SVG. |

## Rules

**Colour** comes from `currentColor` — the mark inherits the surrounding text colour, so it is correct
in light and dark without a second file. Ink `#111111` on light, `#f5f5f3` on dark.

**Stroke weight** is fixed relative to the circle: `18` at `r=150`. Scale the whole viewBox rather than
restyling.

**The dash must stay longer than the stroke is thick** (`27 19` against weight `18`). A dash shorter
than the weight renders as a lozenge and the lower arc turns into a string of beads. The dashed arc
also uses butt caps — round caps extend every dash by half the weight at both ends and close the gaps.

**Do not** round the avatar's corners; the OKX listing requires a square 1:1 image. Do not fill the
circle, recolour the halves separately, or set the wordmark in a sans — the serif is the point.

The wordmark is Constantia, converted to outlines, so the lockup renders identically without the font
installed.
