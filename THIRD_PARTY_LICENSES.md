# Third-party licences and attribution

Doxa ports check thresholds and scoring weights from open-source projects. Where a threshold came
from one of these, the source is named in the code beside it. Nothing here is vendored — the
implementations are Doxa's own; what is borrowed is the taxonomy and the numbers.

## Ported from

**[SEONaut](https://github.com/StJudeWasHere/seonaut)** — MIT, © Jaume Vidal Sirvent and contributors.
The technical SEO taxonomy: 21 single-page and 9 cross-page reporters. Thresholds used verbatim —
title short `<20` / long `>60`, description short `<80` / long `>160`, thin content `<200` words,
TTFB `>800ms`, alt text `>100` runes, oversized image `>500,000` bytes, click depth `>4`, links on a
page `>100`. Read from `internal/issues/page/` and `internal/issues/multipage/`.

**[geo-optimizer-skill](https://github.com/Auriti-Labs/geo-optimizer-skill)** — MIT, © Auriti Labs.
The GEO scoring rubric v4.0.0: eight categories and their published point weights (robots.txt 18,
llms.txt 18, schema 16, meta 14, content 12, brand 10, signals 6, AI discovery 6), used verbatim so a
Doxa score and a `geo audit` score mean the same thing.

**[geo-optimizer](https://github.com/geo-team-red/geo-optimizer)** — MIT, © the geo-optimizer authors.
Anti-citation signal definitions and the AI-discovery endpoint conventions
(`/.well-known/ai.txt`, `/ai/summary.json`, `/ai/faq.json`, `/ai/service.json`).

## Read, and deliberately not used

**[Lightpanda](https://github.com/lightpanda-io/browser)** — AGPL-3.0. Not linked, not vendored, and
no code taken. AGPL-3.0 is copyleft across a network boundary, so linking it into a paid API would
oblige this project to become AGPL. The idea it demonstrates — that a browser driven by an agent does
not need to paint — is implemented independently here by disabling images and resource loading during
render.

**[Lighthouse](https://github.com/GoogleChrome/lighthouse)** — Apache-2.0. Not wrapped. Core Web
Vitals are already measured well and freely by the people who defined them.

## Data sources

Queried live at request time under each provider's public terms; no data is redistributed.

| Source | Terms |
|---|---|
| [Common Crawl](https://commoncrawl.org) index | open data, keyless |
| Wikipedia / Wikidata / Wikimedia pageviews | CC BY-SA, via the public MediaWiki and REST APIs |
| Hacker News (Algolia) | public API |
| GitHub | public REST API |
| Stack Exchange | public API, CC BY-SA content |

## Runtime dependencies

FastAPI, Uvicorn, Pydantic, Requests (Apache-2.0) · BeautifulSoup4, lxml (MIT / BSD) ·
cryptography (Apache-2.0 / BSD) · ReportLab (BSD) · Scrapling (BSD). Each retains its own licence.
