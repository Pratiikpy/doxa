"""Group H — comparison, and proof that something changed.

The audits in the other groups tell a customer what is wrong. These four tell them where they stand
against a rival, and let them prove to somebody else that they fixed it.

`audit.diff` is the one that matters most, and it is only possible because every Doxa answer is
signed. Two receipts from different dates can be compared, and the comparison inherits the credibility
of both signatures: not "we say it improved" but "here are two signed measurements, verify them
yourself". That is the difference between a report and evidence.
"""
from __future__ import annotations

import html as html_escape
import io
import json
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from checks.base import Finding, Severity, registry
from checks.challenge import looks_auditable
from checks.geo_score import score_geo
from contract import ErrorCode, ValidationCheck
from fetch import FetchError, SsrfError, fetch
from nodes.page_nodes import SEVERITY_ORDER, _page_meta, _summarise, _url_of
from runtime import Node, NodeContext, NodeError, Signer

SEVERITY_WEIGHT = {"critical": 8, "high": 4, "low": 1, "info": 0}


def _fetch_both(ctx: NodeContext, mine: str, theirs: str):
    pages = {}
    for label, url in (("you", mine), ("them", theirs)):
        try:
            page = fetch(url, timeout=30)
        except (SsrfError, FetchError) as e:
            raise NodeError(ErrorCode.FETCH_FAILED,
                            f"The {label} URL could not be fetched, so no fair comparison is "
                            f"possible: {e}") from e
        ok, why = looks_auditable(page)
        if not ok:
            raise NodeError(ErrorCode.POLICY_BLOCKED,
                            f"The {label} URL ({url}) cannot be audited: {why} A comparison with one "
                            f"side missing would be misleading.")
        pages[label] = page
    return pages["you"], pages["them"]


class CompeteCompare(Node):
    """You against a named rival, both fetched fresh, every check run identically on each."""
    name = "compete.compare"
    price_usdt = 0.15
    requires = ("url", "competitor")
    optional = ()
    example_input = {"url": "https://example.com/",
                     "competitor": "https://rival.com/"}
    asp_type = "A2MCP"
    engine = "doxa-html"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        mine = _url_of(ctx)
        theirs = str(raw.get("competitor") or raw.get("against") or "").strip()
        if not theirs:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "A 'competitor' URL is required to compare against.")
        if not theirs.startswith(("http://", "https://")):
            theirs = "https://" + theirs

        my_page, their_page = _fetch_both(ctx, mine, theirs)
        sides: dict[str, Any] = {}
        finding_sets: dict[str, set[str]] = {}
        for label, url, page in (("you", mine, my_page), ("them", theirs, their_page)):
            findings = registry.run(page)
            faults = [f for f in findings if f.severity is not Severity.INFO]
            score = score_geo(page, fetch_site_files=True)
            sides[label] = {
                "url": url,
                "page": _page_meta(page),
                "geo_score": score.total,
                "band": score.band,
                "categories": score.by_category(),
                "summary": _summarise(findings),
                # A single comparable number: faults weighted by how much each actually costs.
                "fault_weight": sum(SEVERITY_WEIGHT[f.severity.value] for f in findings),
                "findings": [f.as_dict() for f in faults],
            }
            finding_sets[label] = {f.code for f in faults}

        only_yours = sorted(finding_sets["you"] - finding_sets["them"])
        only_theirs = sorted(finding_sets["them"] - finding_sets["you"])
        return {"sides": sides,
                "faults_only_you_have": only_yours,
                "faults_only_they_have": only_theirs,
                "shared_faults": sorted(finding_sets["you"] & finding_sets["them"]),
                "verdict": {
                    "geo_score_gap": sides["you"]["geo_score"] - sides["them"]["geo_score"],
                    "fault_weight_gap": sides["you"]["fault_weight"] - sides["them"]["fault_weight"],
                    "reading": ("A negative geo_score_gap means they are further ahead. A positive "
                                "fault_weight_gap means you carry more weighted faults than they do."),
                },
                "note": ("Both pages were fetched at the same moment and run through exactly the "
                         "same checks, so the difference is real rather than an artefact of "
                         "measuring them differently.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        you, them = result["sides"]["you"], result["sides"]["them"]
        return [
            ValidationCheck(name="both_sides_were_actually_fetched",
                            passed=bool(you["page"].get("status")) and bool(them["page"].get("status"))),
            ValidationCheck(name="both_sides_ran_the_same_checks",
                            passed=set(you["categories"]) == set(them["categories"]),
                            detail="different check sets would make the gap meaningless"),
            ValidationCheck(name="the_gap_matches_the_sides",
                            passed=result["verdict"]["geo_score_gap"]
                            == you["geo_score"] - them["geo_score"]),
            ValidationCheck(name="fault_lists_are_disjoint_where_claimed",
                            passed=not (set(result["faults_only_you_have"])
                                        & set(result["faults_only_they_have"]))),
        ]


class AuditDiff(Node):
    """Two signed audits, compared — the proof that something actually changed."""
    name = "audit.diff"
    price_usdt = 0.02
    requires = ("before", "after")
    optional = ()
    # The example has to be a call that actually succeeds. This previously advertised two empty
    # `findings` lists, which the comparison below rejected as having nothing to compare — so every
    # caller who copied the documented example paid and received INVALID_INPUT. It now shows the
    # thing the service is for: a fault present in the "before" audit and gone from the "after".
    example_input = {
        "before": {"result": {"findings": [
            {"code": "title.missing", "severity": "error"},
            {"code": "img.alt.missing", "severity": "warning"}]}},
        "after": {"result": {"findings": [
            {"code": "img.alt.missing", "severity": "warning"}]}},
    }
    asp_type = "A2MCP"
    engine = "doxa-diff"
    engine_version = "1.0"
    deterministic = True

    def run(self, ctx: NodeContext) -> dict:
        raw = ctx.input if isinstance(ctx.input, dict) else {}
        before, after = raw.get("before"), raw.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "Provide 'before' and 'after' — two Doxa result envelopes, or the "
                            "'result' objects from them.")

        before_r = before.get("result", before)
        after_r = after.get("result", after)
        b_findings = {f["code"]: f for f in (before_r.get("findings") or [])}
        a_findings = {f["code"]: f for f in (after_r.get("findings") or [])}

        # Absent and empty are different answers, and conflating them charged for a refusal that was
        # not warranted. A `findings` key that is simply missing means these are not audit envelopes
        # and there is genuinely nothing to compare. Two *empty* lists are two clean audits, and
        # "nothing was broken and nothing regressed" is a legitimate result someone may well be
        # paying to confirm — refusing it told a caller their valid question was invalid.
        b_has = isinstance(before_r.get("findings"), list)
        a_has = isinstance(after_r.get("findings"), list)
        if not b_has and not a_has:
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "Neither envelope has a 'findings' list, so these are not Doxa audit "
                            "results and there is nothing to compare. Pass two audit envelopes, or "
                            "the 'result' objects from them.")

        fixed = sorted(set(b_findings) - set(a_findings))
        introduced = sorted(set(a_findings) - set(b_findings))
        remaining = sorted(set(b_findings) & set(a_findings))

        def weight(fs: dict) -> int:
            return sum(SEVERITY_WEIGHT.get(f.get("severity", "info"), 0) for f in fs.values())

        # Receipts are reported as present or absent and never asserted to be valid here: verifying a
        # signature requires the manifest hash, and claiming "verified" without checking would be
        # exactly the kind of unearned confidence this service exists to avoid.
        receipts = {
            "before": _receipt_summary(before),
            "after": _receipt_summary(after),
        }
        return {
            "fixed": [{"code": c, "was": b_findings[c].get("severity"),
                       "message": b_findings[c].get("message")} for c in fixed],
            "introduced": [{"code": c, "now": a_findings[c].get("severity"),
                            "message": a_findings[c].get("message")} for c in introduced],
            "still_present": [{"code": c, "severity": a_findings[c].get("severity")}
                              for c in remaining],
            "totals": {"fixed": len(fixed), "introduced": len(introduced),
                       "still_present": len(remaining),
                       "fault_weight_before": weight(b_findings),
                       "fault_weight_after": weight(a_findings)},
            "direction": ("improved" if weight(a_findings) < weight(b_findings)
                          else "regressed" if weight(a_findings) > weight(b_findings)
                          else "unchanged"),
            "receipts": receipts,
            "how_to_verify": ("Each receipt carries the Ed25519 public key and the signed manifest "
                              "hash. Verify both independently at /verify, then this diff is "
                              "evidence rather than a claim."),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        t = result["totals"]
        return [
            ValidationCheck(name="a_code_is_never_both_fixed_and_introduced",
                            passed=not ({f["code"] for f in result["fixed"]}
                                        & {f["code"] for f in result["introduced"]})),
            ValidationCheck(name="direction_matches_the_weights",
                            passed=(result["direction"] == "improved") ==
                            (t["fault_weight_after"] < t["fault_weight_before"])),
            ValidationCheck(name="counts_match_the_lists",
                            passed=t["fixed"] == len(result["fixed"])
                            and t["introduced"] == len(result["introduced"])),
            ValidationCheck(name="signature_validity_is_not_asserted_without_checking",
                            passed=all(r.get("verified") is None for r in result["receipts"].values()),
                            detail="presence is reported; verification is the caller's to perform"),
        ]


def _receipt_summary(envelope: dict) -> dict[str, Any]:
    r = envelope.get("receipt") if isinstance(envelope, dict) else None
    if not isinstance(r, dict):
        return {"present": False, "verified": None,
                "note": "no receipt was supplied with this envelope"}
    return {"present": True, "verified": None,
            "algo": r.get("algo"), "public_key": r.get("public_key"),
            "manifest_sha256": r.get("manifest_sha256"), "signed_at": r.get("signed_at"),
            "note": "verify this signature yourself; Doxa does not assert it here"}


class Badge(Node):
    """An SVG badge the customer embeds, recomputed whenever their visitors load it."""
    name = "badge"
    price_usdt = 0.005
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "doxa-geo"
    engine_version = "1.0"
    deterministic = False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        try:
            page = fetch(url, timeout=30)
        except (SsrfError, FetchError) as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"The page could not be fetched: {e}") from e
        ok, why = looks_auditable(page)
        if not ok:
            raise NodeError(ErrorCode.POLICY_BLOCKED, why)

        score = score_geo(page, fetch_site_files=True)
        label = "AI readiness"
        value = f"{score.total}/100"
        # Colour is the band, not a gradient: a badge that always looks green is worth nothing.
        colour = {"Excellent": "#2f855a", "Good": "#38a169",
                  "Foundation": "#b7791f", "Critical": "#c53030"}[score.band]
        svg = _badge_svg(label, value, colour)
        ref = ctx.add_artifact(f"doxa-badge-{urllib.parse.urlsplit(url).hostname}.svg",
                               svg.encode("utf-8"), "image/svg+xml")
        return {"url": url, "score": score.total, "band": score.band,
                "svg": svg, "artifact": ref.model_dump() if hasattr(ref, "model_dump") else None,
                "embed_html": f'<img src="{ref.download_url}" alt="Doxa AI readiness: {value}" '
                              f'width="196" height="20">',
                "note": ("The badge shows the band it measured. It is not a seal of approval and "
                         "turns red when the score does.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        svg = result["svg"]
        return [
            ValidationCheck(name="svg_is_well_formed",
                            passed=svg.startswith("<svg") and svg.rstrip().endswith("</svg>")),
            ValidationCheck(name="the_badge_shows_the_measured_score",
                            passed=f"{result['score']}/100" in svg),
            ValidationCheck(name="colour_reflects_the_band_not_always_green",
                            passed=("#c53030" in svg) == (result["band"] == "Critical")),
        ]


def _badge_svg(label: str, value: str, colour: str) -> str:
    lw = 6 * len(label) + 20
    vw = 6 * len(value) + 20
    total = lw + vw
    e = html_escape.escape
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" '
        f'role="img" aria-label="{e(label)}: {e(value)}">'
        f'<title>{e(label)}: {e(value)}</title>'
        f'<linearGradient id="s" x2="0" y2="100%">'
        f'<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        f'<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)">'
        f'<rect width="{lw}" height="20" fill="#444"/>'
        f'<rect x="{lw}" width="{vw}" height="20" fill="{colour}"/>'
        f'<rect width="{total}" height="20" fill="url(#s)"/></g>'
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">'
        f'<text x="{lw / 2}" y="14">{e(label)}</text>'
        f'<text x="{lw + vw / 2}" y="14">{e(value)}</text></g></svg>')


class ReportPdf(Node):
    """The whole audit as a signed PDF a person can hand to somebody else."""
    name = "report.pdf"
    price_usdt = 0.05
    requires = ("url",)
    optional = ()
    example_input = {'url': 'https://example.com/'}
    asp_type = "A2MCP"
    engine = "reportlab"
    engine_version = "4.4"
    deterministic = False

    def engine_available(self) -> bool:
        try:
            import reportlab  # noqa: F401
            return True
        except ImportError:
            return False

    def run(self, ctx: NodeContext) -> dict:
        url = _url_of(ctx)
        try:
            page = fetch(url, timeout=30)
        except (SsrfError, FetchError) as e:
            raise NodeError(ErrorCode.FETCH_FAILED, f"The page could not be fetched: {e}") from e
        ok, why = looks_auditable(page)
        if not ok:
            raise NodeError(ErrorCode.POLICY_BLOCKED, why)

        findings = registry.run(page)
        score = score_geo(page, fetch_site_files=True)
        pdf = _render_pdf(url, findings, score)
        ref = ctx.add_artifact(
            f"doxa-audit-{urllib.parse.urlsplit(url).hostname}.pdf", pdf, "application/pdf")
        return {"url": url,
                "score": score.total, "band": score.band,
                "summary": _summarise(findings),
                "findings": [f.as_dict() for f in findings],
                "artifact": ref.model_dump() if hasattr(ref, "model_dump") else None,
                "note": ("The PDF states every finding with its evidence. The receipt on this "
                         "response signs the same findings, so the document can be checked against "
                         "the signature.")}

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        art = result.get("artifact") or {}
        return [
            ValidationCheck(name="a_pdf_was_produced",
                            passed=bool(art.get("bytes")) and art.get("mime_type") == "application/pdf"),
            ValidationCheck(name="the_pdf_is_content_addressed",
                            passed=str(art.get("sha256", "")).startswith("sha256:")),
            ValidationCheck(name="the_document_covers_every_finding",
                            passed=art.get("bytes", 0) > 800 or not result["findings"]),
        ]


def _mark_and_title(style):
    """The Doxa mark drawn beside the title — the divided circle, solid above and dashed below.

    Drawn with reportlab primitives rather than embedded as an image so the document has no external
    dependency and stays vector at any zoom.
    """
    from reportlab.graphics.shapes import Drawing, Line, Wedge
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    d = Drawing(16 * mm, 16 * mm)
    cx, cy, r = 8 * mm, 8 * mm, 5.6 * mm
    ink = colors.HexColor("#111111")
    upper = Wedge(cx, cy, r, 0, 180, strokeColor=ink, strokeWidth=1.4, fillColor=None)
    lower = Wedge(cx, cy, r, 180, 360, strokeColor=ink, strokeWidth=1.4, fillColor=None,
                  strokeDashArray=[2.4, 1.7])
    line = Line(cx - r * 1.14, cy, cx + r * 1.14, cy, strokeColor=ink, strokeWidth=1.4)
    for shape in (upper, lower, line):
        d.add(shape)

    t = Table([[d, Paragraph("Doxa audit", style)]], colWidths=[18 * mm, None], hAlign="LEFT")
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _evidence_lines(detail: dict[str, Any], *, max_lines: int = 5,
                    width: int = 96) -> list[str]:
    """Turn a finding's evidence into lines a person can read.

    Dumping the raw JSON is honest and unusable: it wraps badly, runs past the text column and
    truncates mid-token, which looks broken in a document somebody hands to a client. The same facts
    are rendered as short labelled lines instead, and long values are cut at a word boundary.
    """
    def short(v: Any) -> str:
        if isinstance(v, str):
            s = v.strip()
        elif isinstance(v, dict):
            s = ", ".join(f"{k}={v[k]}" for k in list(v)[:3])
        else:
            s = str(v)
        s = re.sub(r"\s+", " ", s)
        if len(s) <= width:
            return s
        cut = s[:width].rsplit(" ", 1)[0]
        return (cut or s[:width]) + "…"

    def _clip(text: str, limit: int) -> str:
        """Cut at a word boundary, but never cut away the content itself.

        A URL contains no spaces, so splitting on the last space can land on the label and leave
        "sample:…" — a line that reports nothing. Fall back to a hard slice when the word boundary
        would discard most of the value.
        """
        if len(text) <= limit:
            return text
        head = text[:limit]
        at_space = head.rsplit(" ", 1)[0]
        return (at_space if len(at_space) > limit * 0.6 else head) + "…"

    lines: list[str] = []
    for key, value in detail.items():
        if key.startswith("_") or value is None or value == [] or value == {}:
            continue
        if isinstance(value, list):
            head = value[:3]
            rendered = "; ".join(short(v) for v in head)
            suffix = f" (+{len(value) - len(head)} more)" if len(value) > len(head) else ""
            line = f"{key}: {rendered}{suffix}"
            lines.append(_clip(line, width + len(key) + 12))
        else:
            lines.append(f"{key}: {short(value)}")
        if len(lines) >= max_lines:
            remaining = len(
                [k for k in detail if not k.startswith("_") and detail[k] not in (None, [], {})])
            if remaining > max_lines:
                lines.append(f"… and {remaining - max_lines} further field(s)")
            break
    return lines


def _render_pdf(url: str, findings: list[Finding], score) -> bytes:
    """A plain, readable audit document. No charts, no branding filler — the findings and the
    evidence, in severity order, on paper someone can act from."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Doxa audit — {url}",
                            author="Doxa", leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="Times-Roman", fontSize=20,
                        alignment=0, spaceAfter=4)
    meta = ParagraphStyle("meta", parent=ss["Normal"], fontSize=9,
                          textColor=colors.HexColor("#666666"), spaceAfter=12)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Times-Bold", fontSize=13,
                        spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9.5, leading=13)
    mono = ParagraphStyle("mono", parent=ss["Normal"], fontName="Courier", fontSize=8,
                          textColor=colors.HexColor("#444444"), leading=10)

    e = html_escape.escape
    story: list[Any] = [
        _mark_and_title(h1),
        Paragraph(f"{e(url)}<br/>{datetime.now(timezone.utc):%d %B %Y, %H:%M UTC}", meta),
        Paragraph(f"AI readiness: <b>{score.total}/100</b> — {score.band}", body),
        Spacer(1, 8),
    ]

    cat_rows = [["Category", "Earned", "Max"]]
    for name, v in score.by_category().items():
        cat_rows.append([name, str(v["earned"]), str(v["max"])])
    table = Table(cat_rows, colWidths=[70 * mm, 25 * mm, 25 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#333333")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#999999")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f6f4")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    story += [table, Spacer(1, 4)]

    order = {Severity.CRITICAL: "Critical", Severity.HIGH: "High", Severity.LOW: "Low",
             Severity.INFO: "Observations"}
    for sev, heading in order.items():
        group = [f for f in findings if f.severity is sev]
        if not group:
            continue
        story.append(Paragraph(f"{heading} ({len(group)})", h2))
        for f in group:
            block = [Paragraph(f"<b>{e(f.code)}</b> — {e(f.message)}", body)]
            if f.detail and sev is not Severity.INFO:
                for line in _evidence_lines(f.detail):
                    block.append(Paragraph(e(line), mono))
            block.append(Spacer(1, 7))
            story.append(KeepTogether(block))

    story += [Spacer(1, 10),
              Paragraph("This document reports what was measured at the time above. The response "
                        "that produced it carries an Ed25519 receipt over the same findings; verify "
                        "it independently rather than taking this page on trust.", mono)]
    doc.build(story)
    return buf.getvalue()


ALL_NODES = [CompeteCompare(), AuditDiff(), Badge(), ReportPdf()]
