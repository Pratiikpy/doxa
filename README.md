<div align="center">
  <img src="brand/doxa-mark.svg" width="96" alt="">

  # Doxa

  **Nothing is optimised until it is measured.**

  Technical SEO, answer-engine readiness and AI visibility — sold one call at a time, to agents.

  Whether **ChatGPT, Perplexity, Claude, Gemini and Google AI Overviews** can reach your page,
  read it, and cite it — answered with evidence, and signed.

  <sub>36 services · 28 checks · 289 tests · $0.005–$0.25 a call · x402 on X Layer · USD₮0 ·
  no account, no key, no subscription</sub>

  [**Write-up**](https://comfortable-goal-205.notion.site/Doxa-3aa9c0ce7876810185e4e77ebb5bb5de) ·
  [**Proof: every service, bought**](https://doxa.ivaronix.xyz/proof) ·
  [**Live service**](https://doxa.ivaronix.xyz) ·
  [**All 36 services**](https://doxa.ivaronix.xyz/services) ·
  [**Verify a receipt**](https://doxa.ivaronix.xyz/verify)
</div>

---

Your page renders perfectly in your browser, so you assume the content is there.

Most AI crawlers never run JavaScript. If your article body is injected client-side, they see an
empty shell — and nothing in your analytics will ever tell you.

```console
$ curl -X POST https://doxa.ivaronix.xyz/a2mcp/page.asai \
       -H "PAYMENT-SIGNATURE: <x402>" \
       -d '{"input":{"url":"https://todomvc.com/examples/react/dist/"}}'

  asai.mostly_js   CRITICAL
  About 95% of the readable content appears only after JavaScript runs,
  so most crawlers never see it.

  { "raw_words": 15, "rendered_words": 120, "js_only_share": 0.947 }
```

The same call on `react.dev` returns `asai.server_rendered`, 1.3%.

The measurement is a **shingle diff over 8-word spans** between the raw HTTP response and the fully
rendered DOM — not a length ratio, which would call any page that reformats its text "90%
JavaScript-only".

## Who this is for

An SEO suite is priced per seat, per month, and assumes a person logs in and reads a dashboard. That
is the wrong shape for an agent that needs one number, once, in the middle of a task — and the wrong
price for a question worth half a cent.

Doxa sells the checks individually. A robots policy check costs $0.005 and takes three seconds. The
most expensive service here is $0.25. There is no account to create, no key to rotate and nothing to
cancel: an agent pays for the call it makes, over x402, and gets a signed answer back.

The trade is deliberate. A suite gives you rank tracking, historical charts and a crawl scheduler.
Doxa gives you the measurements a model-driven answer depends on — and refuses the ones that cannot
be measured honestly, which is [its own section below](#what-doxa-will-not-claim).

## What δόξα means, and why it is the name

δόξα is Plato's word for *appearance* — how a thing seems. He sets it against ἐπιστήμη, knowledge,
and divides the two with a line (*Republic* VI, 509d).

That line is the product. A page has facts you can prove: its HTML, its headers, its schema. It also
has an appearance — what a crawler actually manages to read, and what a model says about you when a
buyer asks. The second is now as consequential as the first, and almost nobody measures it.

The mark is that idea: a circle split by the line, solid above for what is demonstrable, dashed below
for what is observed but never certain. Half of what Doxa reports comes from asking models questions
and counting answers. That half is a sample, not a proof, and the logo says so.

## The 36 services

| Group | Services |
|---|---|
| **What a machine sees** | `page.asai` `page.blocked` `robots.check` `llms.check` |
| **Page technical** | `page.audit` `page.links` `page.images` `schema.validate` `page.hreflang` |
| **Answer-engine readiness** | `page.aeo` `page.chunk` `page.readability` `geo.score` |
| **AI visibility** | `ai.visibility` `ai.brand` `ai.citations` `ai.prompts` |
| **Whole site** | `site.audit` `site.graph` `site.sitemap` `site.aeo` |
| **Keywords** | `kw.discover` `kw.questions` `kw.demand` `kw.cluster` |
| **Corpus & citations** | `corpus.presence` `links.inbound` `links.compare` |
| **Comparison & proof** | `compete.compare` `audit.diff` `badge` `report.pdf` |
| **Content** | `content.audit` `content.brief` `content.charts` |
| **Negotiated** | `seo.engagement` |

`GET /services` lists every one with its price and schema.

A few that are hard to find elsewhere:

- **`page.blocked`** presents each AI crawler's real published user-agent and compares the response
  with a browser's. A CDN rule that challenges GPTBot is invisible in robots.txt and invisible to you.
- **`robots.check`** implements the **longest-match rule real crawlers use**. Python's standard
  library parser does not, and disagrees with Google on any file mixing `Allow` and `Disallow`.
- **`page.chunk`** splits a page the way a retrieval system would and returns each span *with the
  heading it sits under* and character offsets, so a quote can be traced back to its source.
- **`audit.diff`** compares two signed audits, so an improvement is evidence rather than a claim.

## What makes an answer trustworthy

Anyone can return JSON. These are the rules that decide whether it means anything — each one is
enforced by a test, and most of them exist because the opposite shipped first.

**A failure is never an absence.** If a model could not be reached, `ai.visibility` reports an outage,
never "you were not mentioned". If a crawler probe has no working baseline, `page.blocked` returns a
CRITICAL "nothing could be tested", not "all crawlers are fine". A customer would act on the second.

**A cap is always disclosed.** A crawl that stops at its budget says so, and `site.audit` will not
report "no orphan pages" from a partial crawl. Corpus coverage returns an uncapped scale figure
alongside the sampled count, because two sites that both hit the sample limit are not equal.

**The wrong document is refused.** Bot-mitigation products answer with an HTML page, sometimes with
HTTP 200. Doxa recognises ten vendors and refuses to audit the interstitial, because a report about a
challenge page is confidently wrong about the site.

**Nothing is quoted that the source does not contain.** `content.charts` extracts figures a model
found, then verifies each against the page and discards any it cannot find. A fabricated statistic
published as schema.org Dataset is machine-readable, quotable and wrong.

**Every answer is signed.** Ed25519 over the canonical manifest. [`/verify`](https://doxa.ivaronix.xyz/verify)
publishes the exact recipe and a copy-paste snippet; a test executes that snippet against a real
receipt, because instructions that do not work are worse than none.

## What Doxa will not claim

- **Exact monthly search volume.** `kw.demand` returns a comparative index with every input and
  weight itemised. Anyone selling you a precise number is reselling an estimate.
- **A complete backlink graph.** Common Crawl is a sample. `links.inbound` returns citations you can
  open and verify — Wikipedia, Hacker News, GitHub, Wikidata — and no estimated total.
- **SERP difficulty.** Search engines return 202 with no results to automated clients, so rather than
  ship a fabricated score the service was left out entirely.
- **Guaranteed rankings.** Nobody can.

Saying this in the response, every time, is the reason the signature is worth anything.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add a model API key for the seven model-backed services
uvicorn server:app --port 8000
```

```bash
pytest -m "not network"       # 289 offline tests
python scripts/e2e.py         # buys all 36 services in-process and checks the outcomes
python scripts/paid_sweep.py  # buys all 36 over the wire with real x402 settlement
python scripts/audit_outcomes.py   # checks the answers against independently established facts
```

`audit_outcomes.py` is the one that matters. It fetches a page directly, counts its markup by hand,
and compares the deliverables against that — so a wrong answer fails there rather than being
discovered by a customer. A 200 has repeatedly hidden a broken outcome.

## Where the checks come from

Thresholds are ported rather than invented, and the source is named in the code beside each one.

| Source | Licence | What it feeds |
|---|---|---|
| [SEONaut](https://github.com/StJudeWasHere/seonaut) | MIT | The technical taxonomy — 21 single-page and 9 cross-page reporters, thresholds used verbatim |
| [geo-optimizer-skill](https://github.com/Auriti-Labs/geo-optimizer-skill) | MIT | The GEO rubric — 8 categories, published v4.0.0 weights, summing to exactly 100; and the idea of classifying a page's numbers rather than counting them |
| [geo-optimizer](https://github.com/geo-team-red/geo-optimizer) | MIT | Anti-citation signals and the AI-discovery endpoints |
| [Common Crawl](https://commoncrawl.org) | open data | Corpus presence across 125 crawl indexes, keyless |
| Wikipedia · Wikidata · Hacker News · GitHub · Stack Exchange | open APIs | Verifiable citations and real questions, keyless |

[Lightpanda](https://github.com/lightpanda-io/browser) is AGPL-3.0 and is therefore **not** linked
into this service — a network-boundary copyleft would oblige Doxa to become AGPL too. Its good idea,
that a browser driven by an agent does not need to paint, is implemented independently by blocking
images and resources during render.

Porting a threshold means porting the measurement beneath it, and that is easy to get wrong. SEONaut's
200-word thin-content line is calibrated on a count that skips the whole subtree of every `<a>` and
strips punctuation first — so its 200 counts prose, not navigation. Measured naively, python.org has
1,024 words; measured SEONaut's way, 592. Applying a borrowed number to a different quantity is how a
check quietly stops firing, so `content_words()` reproduces their measurement and the docstring says
where it deliberately differs.

## Notes for anyone building an x402 service in Python

Two things cost real debugging time here, and neither is documented prominently:

- The OKX agentic wallet sends the authorization in **`PAYMENT-SIGNATURE`**, not `X-PAYMENT`. A
  server reading only the latter hands a 402 to a customer who has already signed and paid.
- `paymentRequirements` sent to the facilitator must be built from **the server's own price**, never
  echoed from the caller's request — otherwise a caller declares they owe a fraction of a cent and
  the facilitator agrees.

OKX ship their x402 client only as an npm package, so [`okx_facilitator.py`](okx_facilitator.py) is a
small Python client for the same HTTP API, with the endpoints and signing scheme documented inline.

One more, if you render pages: Playwright's synchronous API refuses to start when an asyncio loop is
already running in the calling thread — which is exactly an ASGI request handler. Called directly it
raises on every request, and if that exception is swallowed the service reports "could not render"
for every URL in production while working perfectly from a script. Render on a worker thread.

## Layout

```
server.py          HTTP surface, x402 handshake, service listing
runtime.py         node contract, validation levels, Ed25519 receipts
x402.py            challenge building, payment verification
okx_facilitator.py Python client for the OKX x402 facilitator
fetch.py           SSRF-guarded fetch, raw + rendered capture
crawler.py         bounded, robots-respecting site crawler
checks/            28 checks — the taxonomy, grouped by concern
nodes/             the 36 services, one class each
providers/         models, corpus, keyword sources
scripts/           end-to-end, live and outcome-audit harnesses
tests/             289 tests, most named after the bug they pin
```

## Licence

MIT — see [LICENSE](LICENSE).
