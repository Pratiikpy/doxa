"""The proof deck — every service, bought for real, with the answer it returned.

Written from recorded evidence, never by hand. The page is generated from `proof-data.json`, which is
the verbatim output of `scripts/paid_sweep.py`: one real x402 purchase per service, the settlement
transaction hash, and the deliverable that came back. Nothing here is illustrative and nothing is
retyped, so the page cannot drift away from what the service actually did.

What a reviewer needs to see, in order:

  1. that a user really paid and really received something, for every service;
  2. that the answers are *correct*, checked against facts established independently;
  3. that the agent-to-agent path works too, not only direct HTTP;
  4. what the outputs actually look like, so the usefulness is visible rather than asserted.
"""
from __future__ import annotations

import html
import json
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "proof-data.json"
EXPLORER = "https://www.oklink.com/xlayer/tx/"

# The services whose output makes the case on sight, and why each is worth showing. Ordered so the
# differentiator leads.
SHOWCASE: list[tuple[str, str, str]] = [
    ("page.asai", "What an AI crawler actually sees",
     "The measurement nobody else sells: the same page fetched raw and rendered, then diffed."),
    ("geo.score", "AI readiness, 0–100, with the fix list",
     "Every point traced to a signal, and the remaining gaps ordered by return on effort."),
    ("ai.visibility", "Do models recommend you?",
     "Buyer questions that never name the brand, asked repeatedly, with the sentence as evidence."),
    ("robots.check", "Which AI crawlers your robots.txt allows",
     "Longest-match, per named agent, with the exact rule that decided each verdict."),
    ("page.audit", "The full technical pass",
     "Every fault with a stable code and the evidence behind it."),
    ("kw.discover", "The long tail people actually type",
     "One seed expanded through live autocomplete, labelled by intent."),
    ("site.audit", "The nine checks a single page cannot reveal",
     "Duplicate bodies, orphans, click depth — and exactly where the crawl stopped."),
    ("links.inbound", "Citations you can open",
     "From the sources models demonstrably read. No estimated totals."),
    ("content.charts", "Your figures, machine-readable",
     "Extracted, then verified against the page. Anything unverifiable is discarded."),
    ("seo.engagement", "A costed programme, worst problem first",
     "Built from a real assessment, every phase citing the findings that justify it."),
]

CSS = """
:root{--bg:#fbfbfa;--ink:#14141a;--dim:#5b5b66;--line:#e3e3de;--card:#fff;
--ok:#1f7a4d;--warn:#9a6a00;--bad:#b3261e;--accent:#14141a}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0f;--ink:#ececea;--dim:#9a9aa4;
--line:#26262b;--card:#141418;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#ececea}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:56px 24px 96px}
header{text-align:center;margin-bottom:14px}
.mark{width:64px;height:64px;margin-bottom:14px}
h1{font-size:40px;margin:0 0 6px;letter-spacing:-.02em;
font-family:"Iowan Old Style",Palatino,Georgia,serif;font-weight:600}
.tag{color:var(--dim);font-size:17px;margin:0 0 6px}
.sub{color:var(--dim);font-size:13.5px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:34px 0 8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 16px;text-align:center}
.stat b{display:block;font-size:27px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{color:var(--dim);font-size:12.5px;display:block;margin-top:3px;line-height:1.4}
h2{font-size:23px;margin:52px 0 6px;letter-spacing:-.01em;
font-family:"Iowan Old Style",Palatino,Georgia,serif;font-weight:600}
h2 .n{color:var(--dim);font-weight:400;margin-right:8px}
.lede{color:var(--dim);margin:0 0 18px;font-size:15px;max-width:74ch}
table{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--card);
border:1px solid var(--line);border-radius:12px;overflow:hidden}
th{text-align:left;font-weight:600;color:var(--dim);font-size:11.5px;text-transform:uppercase;
letter-spacing:.06em;padding:11px 13px;border-bottom:1px solid var(--line)}
td{padding:10px 13px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code,.mono{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;font-size:12.5px}
a{color:inherit;text-decoration-color:var(--line);text-underline-offset:3px}
a:hover{text-decoration-color:currentColor}
.pass{color:var(--ok);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);
font-size:11.5px;color:var(--dim)}
.case{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:20px 22px;margin:16px 0}
.case h3{margin:0 0 3px;font-size:17px;letter-spacing:-.01em}
.case .why{color:var(--dim);font-size:14px;margin:0 0 14px}
.case .meta{color:var(--dim);font-size:12px;margin-bottom:12px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:14px;
overflow-x:auto;margin:0;font-size:12.5px;line-height:1.55}
.note{border-left:3px solid var(--line);padding:2px 0 2px 16px;color:var(--dim);
font-size:14.5px;margin:18px 0}
.flow{display:grid;gap:9px;margin:18px 0}
.step{display:grid;grid-template-columns:30px 1fr;gap:13px;align-items:start;
background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
.step b{display:flex;align-items:center;justify-content:center;width:24px;height:24px;
border-radius:99px;background:var(--bg);border:1px solid var(--line);font-size:12px}
/* A grid track defaults to min-width:auto, so a 66-character transaction hash — which has no break
   opportunity — widens the track past a phone's viewport and the whole page scrolls sideways. The
   track has to be allowed to shrink, and the hash has to be allowed to wrap. */
.step>div{min-width:0}
.step .t{font-size:14.5px}
.step .d{color:var(--dim);font-size:12.5px;margin-top:2px;overflow-wrap:anywhere}
footer{margin-top:64px;padding-top:22px;border-top:1px solid var(--line);
color:var(--dim);font-size:13px;text-align:center}
@media(max-width:640px){.wrap{padding:34px 15px 64px}h1{font-size:31px}
table{display:block;overflow-x:auto}}
"""

MARK = ('<svg class="mark" viewBox="0 0 512 512" fill="none" stroke="currentColor" '
        'stroke-width="18" stroke-linecap="round" aria-hidden="true">'
        '<path d="M 106 256 A 150 150 0 0 1 406 256"/>'
        '<path d="M 106 256 A 150 150 0 0 0 406 256" stroke-dasharray="27 19" '
        'stroke-linecap="butt" opacity=".9"/>'
        '<path d="M 86 256 H 426"/></svg>')


def _e(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _linkify(text: str) -> str:
    """Escape, then make settlement hashes clickable.

    A transaction hash a reader cannot open is decoration. Escaping happens first so the link is
    built over already-safe text and no input can introduce markup.
    """
    out = _e(text)
    for h in set(re.findall(r"0x[0-9a-fA-F]{64}", out)):
        out = out.replace(h, f'<a class="mono" href="{EXPLORER}{h}" rel="noopener">{h}</a>')
    return out


WIDTH = 96          # columns a code block shows before it needs scrolling


def _wrap(text: str, indent: str = "") -> str:
    """Fold a long sentence onto continuation lines.

    A finding message or a quoted model answer runs to a few hundred characters. Left on one line it
    makes the block 1,890px wide and the reader drags a scrollbar to read a sentence — the evidence is
    present and effectively unreadable, which is the same as absent.
    """
    return textwrap.fill(str(text or ""), width=WIDTH, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False) or ""


_TABLE_SCAFFOLD = re.compile(r"\|\s*-{2,}|\|.*\|.*\|")


def _best_quote(runs: list) -> str:
    """Pick the run whose evidence reads as a sentence.

    The service keeps every raw answer, and some models reply with a markdown comparison table. Its
    context window then reads `|------|---------| | **Confluence** | Atlassian/Jira users` — honest
    evidence and unreadable as a quote. This selects a different *existing* run rather than editing
    one, because a quote the customer can check is the entire point of publishing it.
    """
    quotes = [r["evidence"]["context"] for r in runs
              if r.get("evidence") and r["evidence"].get("context")]
    if not quotes:
        return ""
    return next((q for q in quotes if not _TABLE_SCAFFOLD.search(q)), quotes[0])


def _clip(text: str, width: int) -> str:
    """Shorten at a word boundary.

    A hard slice leaves "splitting th" and "descr" on the page — it reads as a rendering fault rather
    than a deliberate summary, which undermines a page whose whole job is looking trustworthy.
    """
    return textwrap.shorten(str(text or ""), width=width, placeholder="…")


def _counts(mapping: dict | None) -> str:
    """Render a count-by-category map as prose.

    `{'informational': 282, 'commercial': 31}` on a page a customer reads is a Python repr that
    escaped, not a presentation choice.
    """
    return ", ".join(f"{n} {k}" for k, n in
                     sorted((mapping or {}).items(), key=lambda kv: -kv[1])) or "none"


def _excerpt(endpoint: str, result: dict) -> str:
    """The part of a deliverable that shows what it is worth, not the first N bytes of JSON."""
    if not isinstance(result, dict):
        return json.dumps(result)[:700]

    def findings(limit=5):
        return [_wrap(f"[{f['severity']:<8}] {f['code']:<26} {f['message']}", " " * 39)
                for f in (result.get("findings") or [])[:limit]]

    if endpoint == "page.asai":
        f = next((x for x in result.get("findings", []) if x["code"].startswith("asai.")), None)
        d = (f or {}).get("detail", {})
        return "\n".join([
            f"url            {result.get('page', {}).get('url')}",
            f"raw HTML       {d.get('raw_words')} words",
            f"after JS       {d.get('rendered_words')} words",
            f"JS-only share  {d.get('js_only_share')}",
            "", f"{f['code']}  [{f['severity']}]" if f else "", _wrap((f or {}).get("message", ""))])
    if endpoint == "geo.score":
        cats = "\n".join(f"  {k:<14} {v['earned']:>3}/{v['max']:<3}"
                         for k, v in (result.get("categories") or {}).items())
        # `why`, not `effort` — a column of "[file]" repeated five times tells a reader nothing,
        # whereas "There is no /llms.txt." is the actual finding they are being asked to act on.
        fixes = "\n".join(f"  +{x['points_available']:<3} {x['key']:<16} {x['why']}"
                          for x in (result.get("fix_order") or [])[:5])
        return (f"score  {result.get('score')}/100  ({result.get('band')})\n\n"
                f"{cats}\n\nbest next moves, by points per unit of effort:\n{fixes}")
    if endpoint == "ai.visibility":
        o = result.get("overall", {})
        lines = [f"brand          {result.get('brand')}",
                 f"answers        {o.get('answers_measured')} measured, "
                 f"{o.get('answers_mentioning_brand')} mentioned the brand",
                 f"mention rate   {o.get('mention_rate')}", ""]
        for s in (result.get("by_prompt") or [])[:2]:
            lines.append(_wrap(f'Q: {s["prompt"]}', "   "))
            lines.append(f'   rate {s["mention_rate"]}  best rank {s.get("best_rank")}')
            quote = _best_quote(s.get("runs") or [])
            if quote:
                lines.append(_wrap(f'   evidence: "{_clip(quote, 220)}"', " " * 13))
            lines.append("")
        return "\n".join(lines)
    if endpoint == "robots.check":
        rows = "\n".join(f"  {v['agent']:<20} {'allowed' if v['allowed'] else 'BLOCKED':<8} "
                         f"{v['rule']}" for v in (result.get("verdicts") or [])[:10])
        return f"url  {result.get('url')}\n\n{rows}"
    if endpoint == "kw.discover":
        rows = "\n".join(f"  {k['times_suggested']:>3}x  [{k['intent']:<13}] {k['phrase']}"
                         for k in (result.get("keywords") or [])[:10])
        return (f"seed           {result.get('seed')}\n"
                f"phrases found  {result.get('total')}\n"
                f"by intent      {_counts(result.get('by_intent'))}\n\n{rows}")
    if endpoint == "site.audit":
        cov, crawl = result.get("coverage", {}), result.get("crawl", {})
        return ("\n".join([f"pages crawled  {cov.get('pages_crawled')}",
                           f"complete       {'yes' if cov.get('complete') else 'no'}",
                           f"stopped        {cov.get('stopped_because') or 'finished'}",
                           f"max depth      {crawl.get('max_depth')}", ""] + findings()))
    if endpoint == "links.inbound":
        rows = "\n".join(f"  [{c['source']:<10}] {_clip(c['title'], 58)}"
                         for c in (result.get("citations") or [])[:8])
        ent = result.get("wikidata_entity") or {}
        return (f"total {result.get('total')}   from {_counts(result.get('by_source'))}\n"
                f"wikidata  {ent.get('id')}  {_clip(ent.get('description'), 52)}\n\n{rows}")
    if endpoint == "content.charts":
        kinds = result.get("numeric_values_by_kind") or {}
        breakdown = ", ".join(f"{n} {k}" + ("s" if n != 1 and not k.endswith("s") else "")
                              for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
        rows = "\n".join(f"  {_clip(f.get('label'), 44):<46} {str(f.get('value'))[:14]:<16} "
                         f"{f.get('unit')}" for f in (result.get("figures") or [])[:7])
        head = (f"numbers found on the page  {result.get('statistics_detected_in_text')}"
                + (f"  ({breakdown})" if breakdown else "") + "\n"
                f"of those, measurable       {result.get('chartable_values_found')}\n"
                f"published (verified)       {len(result.get('figures') or [])}\n"
                f"discarded (unverifiable)   {result.get('figures_rejected')}")
        # When nothing is chartable the note is the deliverable — it says why, which is the useful
        # answer for a page whose only numbers are dates.
        return f"{head}\n\n{rows}" if rows else f"{head}\n\n{_wrap(result.get('note', ''))}"
    if endpoint == "seo.engagement":
        a = result.get("assessment", {})
        ph = "\n".join(f"  {p['phase']}. [{p['severity']:<8}] ${p['cost_per_pass_usdt']:<7} "
                       f"{_clip(p['problem'], 62)}" for p in (result.get("phases") or [])[:6])
        est = result.get("estimate", {})
        return (f"assessed   {a.get('pages_sampled')} pages, geo {a.get('geo_score')}/100 "
                f"({a.get('band')})\n\n{ph}\n\n"
                f"one pass ${est.get('one_measurement_pass_usdt')} + proof "
                f"${result.get('proof_pass', {}).get('cost_usdt')}")
    if result.get("findings"):
        return "\n".join(findings(6))
    return json.dumps(result, indent=1)[:700]


def render(data: dict) -> str:
    rows = data["services"]
    passed = [r for r in rows if not r.get("problems")]
    spent = sum(r.get("price", 0) for r in rows)
    deliverables = data.get("deliverables", {})
    a2a = data.get("a2a", {})
    audit = data.get("audit", {})
    generated = data.get("generated_at", "")

    stats = [
        (f"{len(passed)}/{len(rows)}", "services bought and delivered"),
        (f"{sum(1 for r in rows if r.get('tx'))}", "settlements on X Layer"),
        (f"{spent:.3f}", "USD₮0 actually spent"),
        (f"{audit.get('passed', 0)}/{audit.get('total', 0)}", "outcome checks vs<br>independent facts"),
        (f"{data.get('tests', 0)}", "tests in the suite"),
    ]
    stat_html = "".join(f'<div class="stat"><b>{v}</b><span>{l}</span></div>' for v, l in stats)

    svc_rows = "".join(
        f"<tr><td><code>{_e(r['endpoint'])}</code></td>"
        f"<td class='num'>${r.get('price', 0)}</td>"
        f"<td class='num'>{r.get('seconds', '')}s</td>"
        f"<td class='num'>{_e(r.get('checks', '—'))}</td>"
        f"<td class='num'>{r.get('bytes', 0):,}</td>"
        f"<td><a class='mono' href='{EXPLORER}{_e(r.get('tx',''))}' "
        f"rel='noopener'>{_e(str(r.get('tx',''))[:18])}…</a></td>"
        f"<td class='pass'>{'delivered' if not r.get('problems') else 'FAILED'}</td></tr>"
        for r in rows)

    cases = ""
    overrides = data.get("showcase") or {}
    for endpoint, title, why in SHOWCASE:
        result = deliverables.get(endpoint)
        # A card backed by its own purchase must cite that purchase's hash, price and time — quoting
        # the sweep's row beside a different deliverable would be a mismatched receipt.
        row = overrides.get(endpoint) or next((r for r in rows if r["endpoint"] == endpoint), {})
        if not result:
            continue
        cases += (
            f'<div class="case"><h3>{_e(title)}</h3>'
            f'<p class="why">{_e(why)}</p>'
            f'<div class="meta"><code>{_e(endpoint)}</code> &middot; '
            f'${row.get("price", "?")} &middot; {row.get("seconds", "?")}s &middot; '
            f'<span class="pill">{_e(row.get("checks", "—"))} checks passed</span> &middot; '
            f'<a class="mono" href="{EXPLORER}{_e(row.get("tx",""))}" rel="noopener">'
            f'settlement {_e(str(row.get("tx",""))[:14])}…</a></div>'
            f'<pre>{_e(_excerpt(endpoint, result))}</pre></div>')

    a2a_steps = "".join(
        f'<div class="step"><b>{i}</b><div><div class="t">{_e(s[0])}</div>'
        f'<div class="d">{_linkify(s[1])}</div></div></div>'
        for i, s in enumerate(a2a.get("steps", []), 1))

    audit_items = "".join(f"<li>{_e(x)}</li>" for x in audit.get("examples", []))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Doxa — proof it works</title>
<meta name="description" content="Every one of Doxa's 36 services bought with a real x402 payment on
X Layer, with the settlement hash and the answer it returned.">
<style>{CSS}</style></head><body><div class="wrap">

<header>{MARK}
<h1>Doxa — proof it works</h1>
<p class="tag">Every service bought with real money, and the answer it gave.</p>
<p class="sub">Agent #9626 &middot; generated from a recorded run on {_e(generated)} &middot;
not written by hand</p>
</header>

<div class="stats">{stat_html}</div>
<p class="sub" style="text-align:center">Payer and payee are the same wallet, so this proves the
payment path and the deliverable — not that a stranger bought it.</p>

<h2><span class="n">1</span>A user paid for every service, and every service delivered</h2>
<p class="lede">Each row is one real purchase from registered user agent #8515: a 402 challenge from
the live endpoint, an authorization signed by the OKX agentic wallet, settlement through the OKX
facilitator on X Layer, and a signed deliverable in return. Open any hash to check it on-chain.</p>
<table><thead><tr><th>Service</th><th class="num">Price</th><th class="num">Time</th>
<th class="num">Checks</th><th class="num">Bytes</th><th>Settlement</th><th>Result</th></tr></thead>
<tbody>{svc_rows}</tbody></table>

<h2><span class="n">2</span>The agent-to-agent path, end to end</h2>
<p class="lede">{_e(a2a.get("lede", ""))}</p>
<div class="flow">{a2a_steps}</div>
<div class="note">{_e(a2a.get("note", ""))}</div>

<h2><span class="n">3</span>The answers are correct, not merely present</h2>
<p class="lede">A 200 with a signature proves the service ran. It does not prove the answer is right.
So the deliverables are compared against facts established independently — python.org fetched
directly and its markup counted by hand — and {audit.get('passed', 0)} of
{audit.get('total', 0)} checks passed. A sample of what is actually asserted:</p>
<ul class="lede">{audit_items}</ul>

<h2><span class="n">4</span>What you actually get</h2>
<p class="lede">Real excerpts from the deliverables above — the same bytes the settlement paid for,
not illustrations.</p>
{cases}

<footer>
Reproduce all of this: <code>python scripts/paid_sweep.py</code> buys every service over the wire,
<code>python scripts/audit_outcomes.py</code> checks the answers against independent facts.<br>
<a href="/services">All 36 services</a> &middot;
<a href="/verify">Verify a receipt yourself</a> &middot;
<a href="https://github.com/Pratiikpy/doxa" rel="noopener">Source</a> &middot;
<a href="https://comfortable-goal-205.notion.site/Doxa-3aa9c0ce7876810185e4e77ebb5bb5de"
rel="noopener">Write-up</a>
</footer>
</div></body></html>"""


def load() -> dict | None:
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def page() -> str:
    data = load()
    if not data:
        return ("<!doctype html><meta charset=utf-8><title>Doxa — proof</title>"
                "<p style='font:16px system-ui;padding:40px'>No recorded run is published yet. "
                "Run <code>python scripts/paid_sweep.py</code> and publish "
                "<code>proof-data.json</code>.</p>")
    return render(data)
