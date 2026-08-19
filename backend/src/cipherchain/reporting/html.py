"""The document itself: one self-contained, printable HTML file.

Self-contained is a requirement, not tidiness. This file is emailed, attached to
a case record, and printed; anything fetched at open time is a footnote that
vanishes on the recipient's machine, and a stylesheet that fails to load turns
an evidence table into an undifferentiated wall of text where a heuristic
inference looks exactly like a sourced claim. So: no external CSS, no fonts, no
scripts, no images. The PDF path renders this same string, which is why the two
formats cannot drift apart.

The layout carries four arguments that prose alone kept losing:

- **The verdict comes first.** ``_summary_block`` states who the money came from
  and who it went to, with the full address and what each name rests on, before
  any of the material a reader would have to assemble it from. It is built from
  ``summarise_answers``, so it cannot say something the answer sections and the
  status banner do not.
- **A named operator and an unnamed one never share a shape.** They are separate
  cards with separate labels, because the entire practical difference between
  them — can this be served with a legal request — disappears the moment they
  are rendered as two rows of one table.
- **A claim's date sits next to the claim, always.** ``describe_claim_age`` is
  called on every third-party claim in the document, including the undated ones,
  so freshness is never inferred from silence.
- **Coverage is a section, not a footnote.** It renders unconditionally, before
  the appendix of other findings, because it is the last thing a reader must
  have read to weigh the answer — a report that hides its own gaps is worse than
  no report.

Print styling targets A4 with real margins (``@page``), and the paged rules keep
a card off a page boundary rather than letting an endpoint's evidence separate
from the endpoint it supports.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from cipherchain.chains.base import feed_name_for_code
from cipherchain.core.models import Evidence, EvidenceKind, Finding
from cipherchain.reporting.model import (
    LEAD_UNTRACED,
    AnswerEndpoint,
    AnswerSection,
    BestEffortEndpoint,
    Caveat,
    DirectionVerdict,
    EvidenceGroup,
    InvestigationReport,
    SummaryEndpoint,
    TraversalCoverage,
    VaspProfile,
    describe_claim_age,
    format_moment,
    group_evidence,
    summarise_answers,
)

_STYLESHEET = """
@page { size: A4; margin: 16mm 15mm 18mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0;
  background: #ffffff;
  color: #14181d;
  font-family: "DejaVu Sans", "Liberation Sans", Arial, Helvetica, sans-serif;
  font-size: 10.5pt;
  line-height: 1.5;
}
.sheet { max-width: 190mm; margin: 0 auto; padding: 10mm 6mm 16mm; }
h1, h2, h3, h4 { margin: 0; font-weight: 700; line-height: 1.25; }
h1 { font-size: 15pt; }
h2 { font-size: 12.5pt; letter-spacing: .02em; text-transform: uppercase; }
h3 { font-size: 11.5pt; }
h4 { font-size: 9.5pt; letter-spacing: .06em; text-transform: uppercase; color: #4a5560; }
p { margin: 0 0 .5em; }
code, .mono {
  font-family: "DejaVu Sans Mono", "Liberation Mono", Consolas, monospace;
  font-size: .92em;
  overflow-wrap: anywhere;
  word-break: break-all;
}
.kicker { font-size: 8.5pt; letter-spacing: .18em; text-transform: uppercase; color: #5b6570; }
.masthead { border-bottom: 2px solid #14181d; padding-bottom: 6mm; margin-bottom: 6mm; }
.subject { margin: 2mm 0 1mm; font-family: "DejaVu Sans Mono", Consolas, monospace;
  font-size: 13pt; overflow-wrap: anywhere; word-break: break-all; }
.facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1mm 8mm; margin: 4mm 0 0; }
.facts div { border-top: 1px solid #dfe4e9; padding-top: 1mm; }
.facts dt { font-size: 8pt; letter-spacing: .08em; text-transform: uppercase; color: #5b6570; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.banner { border: 1.5pt solid #8a4b00; background: #fdf3e6; padding: 3mm 4mm;
  margin: 5mm 0 0; break-inside: avoid; }
.banner.critical { border-color: #8a1c1c; background: #fbeceb; }
.banner .label { font-size: 9pt; letter-spacing: .1em; text-transform: uppercase;
  font-weight: 700; color: #8a4b00; }
.banner.critical .label { color: #8a1c1c; }
section.block { margin-top: 8mm; }
section.block > h2 { border-bottom: 1px solid #14181d; padding-bottom: 1.5mm; margin-bottom: 4mm; }
.lede { color: #3c454f; margin-bottom: 4mm; }
/* The verdict block: a heavy frame, because this is the part that gets
   photocopied on its own into a filing and has to survive arriving without the
   rest of the document. */
section.summary { border: 2pt solid #14181d; padding: 4mm 5mm 3mm; margin-top: 6mm; }
section.summary > h2 { border-bottom-width: 0; padding-bottom: 0; margin-bottom: 1mm; }
.verdict { border-top: 1px solid #cfd6dd; padding: 3mm 0 1mm; break-inside: avoid; }
.verdict:first-of-type { border-top: 0; }
.verdict .lead-in { font-size: 9.5pt; color: #4a5560; margin: 0; }
.verdict .operator { font-size: 14pt; margin: .5mm 0; }
.verdict .operator.none { font-size: 11.5pt; color: #7a5c10; }
.verdict-lead { border-left: 3pt solid #a2560c; background: #fdf6ee; padding: 2.5mm 3mm;
  margin: 2.5mm 0 1mm; break-inside: avoid; }
.verdict-lead .label { font-size: 8pt; letter-spacing: .1em; text-transform: uppercase;
  font-weight: 700; color: #a2560c; }
/* Smaller and amber even when the lead carries a real, sourced name: the size
   difference is what stops the eye reading it as the answer. */
.verdict-lead .operator { font-size: 11.5pt; color: #7a5c10; }
.verdict-lead p { margin: 0 0 1mm; }
.answer { margin-bottom: 7mm; }
.answer > h3 { border-left: 3pt solid #14181d; padding-left: 3mm; }
.question { color: #4a5560; font-style: italic; padding-left: 3mm; margin-bottom: 3mm; }
/* Cards may break across pages. Forbidding it left half-empty pages whenever a
   card was taller than the space remaining, and a card carrying a metadata
   table plus four evidence groups is taller than a page on its own — at which
   point the rule stops being honoured and only the waste remains. The units
   that must not split are the small ones below. */
.card { border: 1px solid #cfd6dd; padding: 4mm; margin-bottom: 3mm; }
.card > .card-head { break-after: avoid; }
.card.named { border-left: 3pt solid #14532d; }
.card.unnamed { border-left: 3pt solid #8a6d1c; }
.card.empty { border-style: dashed; }
/* A lead is neither the green of a traced answer nor the red of a sanctions
   hit: amber, dashed, so it reads as provisional even in a monochrome
   photocopy — which is how a filing usually arrives. */
.card.lead { border-left: 3pt solid #a2560c; border-style: dashed; }
.card-head { display: flex; flex-wrap: wrap; gap: 2mm 4mm; align-items: baseline;
  justify-content: space-between; margin-bottom: 2mm; }
.badge { display: inline-block; border: 1px solid currentColor; border-radius: 2pt;
  padding: 0 1.5mm; font-size: 8pt; letter-spacing: .06em; text-transform: uppercase;
  font-weight: 700; }
.badge.named { color: #14532d; }
.badge.unnamed { color: #7a5c10; }
.badge.stale { color: #8a1c1c; }
.badge.lead { color: #a2560c; }
/* Never truncated, and one click selects the whole string: an address a reader
   has to retype from a PDF is an address that reaches the exchange with a typo
   in it. `break-all` wraps the tail onto the next line rather than off the
   page, which is why nothing here needs shortening. */
.address { font-family: "DejaVu Sans Mono", Consolas, monospace; font-size: 10pt;
  overflow-wrap: anywhere; word-break: break-all;
  -webkit-user-select: all; user-select: all; }
.operator { font-size: 12pt; font-weight: 700; }
.attrs { margin: 2mm 0 0; font-size: 9.5pt; color: #3c454f; }
.attrs span + span::before { content: "  ·  "; color: #99a3ad; }
.divergence { border: 1px solid #cfd6dd; border-left: 3pt solid #4a5560; background: #f6f8fa;
  padding: 3mm 4mm; margin: 0 0 3mm; break-inside: avoid; }
.divergence .label { font-size: 8pt; letter-spacing: .1em; text-transform: uppercase;
  font-weight: 700; color: #4a5560; }
.weakness { border-left: 3pt solid #a2560c; background: #fdf6ee; padding: 2.5mm 3mm;
  margin: 3mm 0 0; break-inside: avoid; }
.weakness .label { font-size: 8pt; letter-spacing: .1em; text-transform: uppercase;
  font-weight: 700; color: #a2560c; }
table.meta { border-collapse: collapse; width: 100%; margin: 3mm 0 0; font-size: 9.5pt;
  break-inside: avoid; }
table.meta th, table.meta td { border: 1px solid #cfd6dd; padding: 1.5mm 2.5mm;
  text-align: left; vertical-align: top; }
table.meta th { width: 38%; background: #f6f8fa; font-weight: 600; }
/* Evidence folds away on screen and prints in full: the `open` attribute is
   always emitted, so Chromium lays every item out when printing and a reader
   who collapses one in a browser cannot produce a PDF missing it. */
.evidence { margin-top: 3mm; }
.evidence > summary { font-size: 9.5pt; letter-spacing: .06em; text-transform: uppercase;
  color: #4a5560; font-weight: 700; cursor: pointer; }
.evidence-group { margin-top: 3mm; break-inside: avoid; }
.evidence-group .caption { font-size: 8.5pt; color: #5b6570; margin: 0 0 1.5mm; }
ul.items { margin: 0; padding: 0; list-style: none; }
ul.items > li { border-top: 1px solid #e4e9ee; padding: 1.5mm 0; }
ul.items > li:first-child { border-top: 0; }
.meta-line { font-size: 8.5pt; color: #4a5560; margin: .5mm 0 0; }
.meta-line.flag { color: #8a1c1c; font-weight: 600; }
.refs { font-size: 8.5pt; color: #3c454f; margin-top: .5mm; }
.finding { border-top: 1px solid #dfe4e9; padding: 3mm 0; }
.finding:first-of-type { border-top: 0; }
.finding .kind { font-size: 8pt; letter-spacing: .08em; text-transform: uppercase;
  color: #5b6570; font-weight: 700; }
.caveat { border: 1px solid #cfd6dd; border-left: 3pt solid #8a4b00; padding: 3mm 4mm;
  margin-bottom: 2.5mm; break-inside: avoid; }
.caveat.clean { border-left-color: #14532d; }
.caveat .headline { font-weight: 700; }
.caveat .code { font-size: 8pt; letter-spacing: .06em; color: #5b6570;
  font-family: "DejaVu Sans Mono", Consolas, monospace; }
.coverage-figures { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1mm 8mm; margin: 0 0 4mm; }
.coverage-figures div { border-top: 1px solid #dfe4e9; padding-top: 1mm; }
.coverage-figures dt { font-size: 8pt; letter-spacing: .08em; text-transform: uppercase;
  color: #5b6570; }
.coverage-figures dd { margin: 0; font-size: 11pt; font-weight: 700; }
.colophon { margin-top: 8mm; border-top: 1px solid #dfe4e9; padding-top: 3mm;
  font-size: 8.5pt; color: #5b6570; }
@media print {
  .sheet { max-width: none; padding: 0; }
  section.block { break-before: auto; }
  /* A heading stranded at the foot of a page reads as an empty section. */
  h2, h3, h4 { break-after: avoid; }
  p { orphans: 2; widows: 2; }
  /* The disclosure triangle means nothing on paper. */
  .evidence > summary { list-style: none; }
}
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _fact(term: str, value: str, *, mono: bool = False) -> str:
    css = ' class="mono"' if mono else ""
    return f"<div><dt>{_e(term)}</dt><dd{css}>{_e(value)}</dd></div>"


def _status_banner(report: InvestigationReport) -> str:
    """The run's own health, in the header where it cannot be missed.

    A partial run buried below the answers is how a reader ends up treating "we
    stopped early" as "there was nothing more". Status appears twice on purpose —
    once here, once in the caveats — because the two audiences differ: the
    skimmer never reaches the last page.

    That last sentence is why the pursuit clause is here and not only in the
    caveats. Every unanswered banner tells the reader to resume with a larger
    budget, and for a run that already granted itself several allowances and
    still named nobody, that advice alone reads as a tooling limit — when the
    run has in fact spent multiples of the authorised budget and come back
    empty, which is a statement about the money. The skimmer who acts on this
    banner is precisely the reader who never sees ``budget_extended``.
    """
    header = report.header
    pursued = len(report.coverage.budget_extensions)

    def chased(outcome: str) -> str:
        """The pursuit clause, worded for what the run did or did not name.

        Taking ``outcome`` from the caller rather than composing one sentence
        for both branches: "and still named nobody" is false of a run that
        named an operator in one direction of two, and a banner that overstated
        the failure would be the same defect as one that understated it.
        """
        if not pursued:
            return ""
        return (
            f" This investigation granted itself {pursued} further allowance(s) of its own "
            f"budget chasing an answer before it stopped, so it has already looked well past "
            f"the budget printed above and {outcome} — raise it substantially, not marginally."
        )

    if header.is_partial:
        # "Partial" describes COVERAGE, never the answers, and printing one
        # sentence for both cases is what makes it unreadable. A run can stop on
        # a budget having already named an exchange in every direction asked —
        # that reader needs "you have your answer, there is simply more chain we
        # did not walk". A run that stopped with nothing named needs the
        # opposite warning. Same status, opposite meaning, so the banner has to
        # look at the answers before it speaks.
        # ``actionable``, not "an endpoint was reached": this banner's whole
        # claim is that an objective reached a NAMED operator, and an endpoint
        # whose only claim identifies the address (a mixer, a sanctions entry)
        # reaches no such thing. Counting it here would print "Every objective
        # was answered with a named operator" above a list with nobody in it.
        named = [s for s in report.answers if s.actionable]
        asked = len(report.answers)
        if named and len(named) == asked:
            answered = ", ".join(
                f"{s.heading.lower()} — <strong>{_e(s.nearest_named.entity)}</strong>"
                for s in named
                if s.nearest_named is not None and s.nearest_named.entity
            )
            return (
                '<div class="banner"><p class="label">Answered · coverage partial</p>'
                f"<p><strong>Every objective was answered with a named operator</strong>"
                f"{': ' + answered if answered else ''}. "
                "The run then stopped on an exhausted budget rather than an exhausted trail, "
                "so parts of the graph were never walked. That affects completeness, not the "
                "answers above — a further run could find a <em>nearer</em> operator, but it "
                "cannot unfind these. See <strong>Coverage and caveats</strong>.</p></div>"
            )
        if named:
            return (
                '<div class="banner"><p class="label">Partly answered · coverage partial</p>'
                f"<p>{len(named)} of {asked} objective(s) reached a named operator. The run "
                "stopped on an exhausted budget, not an exhausted trail, so the unanswered "
                "direction(s) may simply not have been walked far enough — resume with larger "
                "budgets before concluding there is nothing there."
                + chased("could not name an operator in every direction asked")
                + " See <strong>Coverage and caveats</strong>.</p></div>"
            )
        return (
            '<div class="banner"><p class="label">Not answered · coverage partial</p>'
            "<p><strong>No objective reached a named operator</strong>, and this investigation "
            "stopped on an exhausted budget rather than an exhausted trail. That is not a "
            "finding that no operator exists — it is a statement that the trace ran out of "
            "allowance first. Resume with larger budgets."
            + chased("still named nobody")
            + " See <strong>Coverage and caveats</strong> for what was left unexamined.</p></div>"
        )
    if header.status == "failed":
        return (
            '<div class="banner critical"><p class="label">Run failed</p>'
            "<p>The engine stopped with an error: "
            f"<code>{_e(header.error or 'unrecorded')}</code>. "
            "Every conclusion below is provisional — the trace was interrupted.</p></div>"
        )
    if not header.is_settled:
        return (
            '<div class="banner"><p class="label">Run in progress</p>'
            f"<p>This document was generated while the investigation was still "
            f"<strong>{_e(header.status)}</strong>. It is a snapshot of an unfinished trace and "
            "may not show the nearest endpoint.</p></div>"
        )
    return ""


def _masthead(report: InvestigationReport) -> str:
    """Who this document is about, and nothing a reader does not need yet.

    Four facts, not nine. The versions, ids and printing times that used to sit
    here are reproducibility material, they were already repeated in the
    colophon, and five extra rows of them is ~25mm of page 1 — which is the
    distance between the verdict landing above the fold and landing below it.
    They now appear once, in the colophon, in full and under the same labels.
    """
    header = report.header
    facts = [
        _fact("Chain", header.subject.chain),
        _fact("Status", header.status),
        # When the run happened, not only when this document was printed: a
        # report read months later is a report about the chain as it was then,
        # and that is context for the verdict rather than provenance for a
        # re-run, so this one stays at the top.
        _fact("Run started", format_moment(header.started_at)),
        _fact("Objectives", ", ".join(header.objectives) or "none recorded"),
    ]
    return (
        '<header class="masthead">'
        '<p class="kicker">CipherChain — blockchain investigation report</p>'
        f'<h1 class="subject">{_e(header.subject.value)}</h1>'
        f'<dl class="facts">{"".join(facts)}</dl>'
        f"{_status_banner(report)}"
        "</header>"
    )


def _summary_facts(endpoint: SummaryEndpoint) -> str:
    """The address in full, then the three numbers that qualify it."""
    return (
        f'<p class="address">{_e(endpoint.address)}</p>'
        f'<p class="attrs"><span>{_e(endpoint.hop)} hop(s) from the subject</span>'
        f"<span>confidence {_e(_pct(endpoint.confidence))}</span>"
        f"<span>{_e(endpoint.chain)}</span></p>"
    )


def _verdict_lead(verdict: DirectionVerdict) -> str:
    """The address that is NOT the answer, written so it cannot be quoted as one.

    This is the half of the summary that is easy to get wrong. The forward
    direction of a real run reached an address behaving exactly like a service
    endpoint at 61% and nobody named it; printed with a name, a bare address and
    a confidence figure it reads as a VASP, and the reader files against a
    party this report never identified. So the block says which of the two
    weaknesses applies — nobody is named here, or the route here was guessed —
    before it says anything else.
    """
    lead = verdict.lead
    if lead is None:
        return ""
    if verdict.lead_kind == LEAD_UNTRACED:
        label = "Lead — the route to it was not traced"
        explanation = (
            "This run did not follow the subject's funds to this address: the route crosses a "
            "mixer, where the onward branch was selected by a published heuristic rather than "
            "observed. The name above may be perfectly true of an address that has nothing to "
            "do with this subject."
            if lead.operator
            else "No operator is named at this address, and the route to it crosses a mixer "
            "where the onward branch was selected by a published heuristic rather than "
            "observed. It is a lead to develop, not a respondent."
        )
    else:
        # Wording turns on whether this sits under a name or replaces one. Under
        # a name it is a footnote; in place of one it is the only thing the
        # direction produced, and calling that a "nearer address" would let a
        # reader take it for the answer.
        label = (
            "A nearer address on this route — operator unnamed"
            if verdict.answered
            else "Lead — an inference, not an attribution"
        )
        explanation = (
            "CipherChain inferred from this address's own on-chain behaviour that it is "
            "custodial infrastructure. An inference describes what an address does and never "
            "who runs it, so nobody is named here: it is a lead to develop, not a VASP and "
            "not a party any request can be served on."
        )
    operator = f'<p class="operator">{_e(lead.operator)}</p>' if lead.operator else ""
    # Sourced only where there is a name to source. On a lead nobody named, the
    # line would be citing the address itself, which no claim ever asserts.
    basis = (
        f'<p class="meta-line">That name rests on: {_e(lead.basis)}.</p>'
        if lead.operator and lead.basis
        else ""
    )
    return (
        f'<div class="verdict-lead"><p class="label">{_e(label)}</p>'
        f"{operator}{_summary_facts(lead)}{basis}"
        f"<p>{_e(explanation)}</p></div>"
    )


def _verdict_card(verdict: DirectionVerdict) -> str:
    named = verdict.named
    if named is not None and named.names_an_operator:
        basis = (
            f'<p class="{"meta-line flag" if named.stale else "meta-line"}">'
            f"The name rests on: {_e(named.basis)}.</p>"
            if named.basis
            else ""
        )
        body = (
            f'<p class="operator">{_e(named.operator)}</p>{_summary_facts(named)}{basis}'
            '<p class="meta-line">A legal request can be addressed to this operator.</p>'
        )
    elif named is not None:
        # A claim of the right KIND that names nobody — a sanctions listing, a
        # mixer identification. This branch exists because the alternative was
        # printing "A legal request can be addressed to this operator" under a
        # blank name, on the one page that gets photocopied into a filing on its
        # own: an instruction to serve a request on nobody.
        #
        # The address and the claim still print. That an endpoint is a known
        # mixer is one of the most important things this document can say, and
        # suppressing the whole card to avoid over-claiming would lose it.
        claim = (
            f'<p class="{"meta-line flag" if named.stale else "meta-line"}">'
            f"The claim recorded here: {_e(named.basis)}.</p>"
            if named.basis
            else ""
        )
        body = (
            '<p class="operator none">No operator can be named</p>'
            f"{_summary_facts(named)}{claim}"
            "<p>A third-party source does make a claim about this address, but the claim "
            "describes the address itself rather than naming an operator — no company or "
            "service is identified. There is no respondent here for a legal request; the "
            "claim is set out in full in the evidence below, where it may matter on its "
            "own terms.</p>"
        )
    else:
        # Three ways to have no answer, and they are not interchangeable. The
        # untraced branch matters most: that lead may carry a perfectly good
        # name, so "no address here carries a sourced label" would be false of
        # it — and a reader who caught the contradiction would be entitled to
        # discount the sentence rather than the lead.
        if verdict.lead is None:
            explanation = (
                "This run reached no endpoint in this direction at all, so there is nothing "
                "here to name. That is a statement about what was examined, not a finding "
                "that no operator exists — see Coverage and caveats."
            )
        elif verdict.lead_kind == LEAD_UNTRACED:
            explanation = (
                "Nothing was reached in this direction by following value, so no operator is "
                "established as the counterparty. The best lead this run can offer is below, "
                "with the reason it is only a lead."
            )
        else:
            explanation = (
                "No address on this route carries a sourced label, so this report names "
                "nobody in this direction and there is no operator to serve a request on."
            )
        body = f'<p class="operator none">No operator can be named</p><p>{_e(explanation)}</p>'
    return (
        f'<div class="verdict"><p class="kicker">{_e(verdict.label)}</p>'
        f'<p class="lead-in">{_e(verdict.lead_in)}</p>'
        f"{body}{_verdict_lead(verdict)}</div>"
    )


def _summary_block(report: InvestigationReport, now: datetime) -> str:
    """The whole document in two lines, at the top, where it cannot be missed.

    It exists because the answer was reachable but not findable: a reader had to
    hold two headings, two cards and a divergence note in their head before they
    could say who the money came from. This block says it — operator, full
    address, distance, confidence, and what the name rests on — and defers every
    qualification to the sections below rather than repeating them.

    It states nothing the answers section does not also state, and it derives
    what it states from the same ``nearest_named`` the status banner counts, so
    there is no arrangement of findings that makes the two disagree.
    """
    verdicts = summarise_answers(report.answers, now)
    if not verdicts:
        body = (
            '<div class="verdict"><p class="operator none">No objective was recorded</p>'
            "<p>This investigation records no direction to answer, so there is no counterparty "
            "to state.</p></div>"
        )
    else:
        body = "".join(_verdict_card(verdict) for verdict in verdicts)
    return (
        '<section class="block summary" id="summary"><h2>Summary — money in, money out</h2>'
        '<p class="lede">Who the funds came from and who they went to, with the address to '
        "quote and the source each name rests on. Only a named operator can be served with a "
        "legal request; everything below this block is the evidence for what it says.</p>"
        f"{body}</section>"
    )


def _vasp_table(profile: VaspProfile) -> str:
    """Reference data for a named operator — only the rows that exist.

    Absent fields are omitted rather than printed empty: a blank cell in a table
    headed "jurisdiction" invites the reading that there isn't one, when the
    truth is that nothing is on file.
    """
    rows = [
        ("Legal entity", profile.legal_entity),
        ("Jurisdiction", profile.jurisdiction),
        ("KYC regime", profile.kyc_regime),
        ("KYC in force since", format_moment(profile.kyc_since) if profile.kyc_since else None),
        ("Law-enforcement request channel", profile.le_request_channel),
        ("Reference source", profile.source),
        ("Reference dated", format_moment(profile.source_date) if profile.source_date else None),
    ]
    body = "".join(
        f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>" for label, value in rows if value
    )
    if not body:
        return ""
    return (
        f'<h4 style="margin-top:3mm">Operator reference data — {_e(profile.entity)}</h4>'
        f'<table class="meta">{body}</table>'
    )


def _evidence_item(evidence: Evidence, now: datetime) -> str:
    lines = [f"<li><div>{_e(evidence.summary)}</div>"]
    if evidence.kind is EvidenceKind.THIRD_PARTY_CLAIM:
        # Unconditional: the date is what separates a live attribution from a
        # stale one, and both look identical without it.
        age, flag = describe_claim_age(evidence, now)
        source = evidence.source or "source not recorded"
        css = "meta-line flag" if flag else "meta-line"
        lines.append(
            f'<p class="{css}">Source: {_e(source)} · {_e(age)}'
            f" · claim confidence {_e(_pct(evidence.confidence))}</p>"
        )
    elif evidence.kind is EvidenceKind.HEURISTIC_INFERENCE:
        lines.append(
            f'<p class="meta-line">Rule: <span class="mono">{_e(evidence.heuristic)}</span>'
            f" · confidence {_e(_pct(evidence.confidence))} · names no operator</p>"
        )
    if evidence.refs:
        refs = ", ".join(_e(ref) for ref in evidence.refs)
        lines.append(f'<p class="refs mono">{refs}</p>')
    lines.append("</li>")
    return "".join(lines)


def _evidence_groups(groups: Sequence[EvidenceGroup], now: datetime) -> str:
    """Every item, foldable on screen and unfoldable on paper.

    ``open`` is not optional and is never conditional: a closed ``details`` is
    omitted from print output, so emitting one would produce a PDF whose
    evidence tables are simply absent — the one failure this document may not
    have. The fold exists so a reader scanning six endpoints can collapse what
    they have already read, not so the page can ship without it.
    """
    if not groups:
        return ""
    blocks = [
        f'<div class="evidence-group"><h4>{_e(group.title)}</h4>'
        f'<p class="caption">{_e(group.caption)}</p>'
        f'<ul class="items">{"".join(_evidence_item(e, now) for e in group.items)}</ul></div>'
        for group in groups
    ]
    items = sum(len(group.items) for group in groups)
    return (
        f'<details class="evidence" open><summary>Evidence — {items} item(s) across '
        f"{len(groups)} kind(s)</summary>{''.join(blocks)}</details>"
    )


def _endpoint_card(endpoint: AnswerEndpoint, role: str, now: datetime) -> str:
    # Every line on this card turns on whether an operator was NAMED, never on
    # whether a claim exists: the two came apart on an endpoint whose only claim
    # identified it as a mixer, and the card read "Named operator" over the
    # words "Operator unnamed" and offered a respondent it could not name.
    named = endpoint.names_an_operator
    badge = (
        '<span class="badge named">Named operator</span>'
        if named
        else '<span class="badge unnamed">Operator not named</span>'
    )
    operator = (
        f'<p class="operator">{_e(endpoint.entity)}</p>'
        if named
        else '<p class="operator">Operator unnamed</p>'
    )
    if named:
        actionability = "A legal request can be addressed to this operator."
    elif endpoint.named:
        actionability = (
            "A third-party source makes a claim about this address, but it names no "
            "operator — see the claim itself below. There is nobody here to address a "
            "legal request to."
        )
    else:
        actionability = (
            "There is no operator to address a legal request to; this address is a lead, "
            "not a respondent."
        )
    metadata = _vasp_table(endpoint.vasp) if endpoint.vasp is not None else ""
    return (
        f'<div class="card {"named" if named else "unnamed"}">'
        f'<div class="card-head"><h4>{_e(role)}</h4>{badge}</div>'
        f"{operator}"
        f'<p class="address">{_e(endpoint.address)}</p>'
        f'<p class="attrs"><span>{_e(endpoint.hop)} hop(s) from the subject</span>'
        f"<span>finding confidence {_e(_pct(endpoint.confidence))}</span>"
        f"<span>{_e(endpoint.chain)}</span></p>"
        f"<p>{_e(endpoint.summary)}</p>"
        f'<p class="meta-line">{_e(actionability)}</p>'
        f"{metadata}"
        f"{_evidence_groups(endpoint.evidence_groups, now)}"
        "</div>"
    )


def _lead_card(endpoint: BestEffortEndpoint, now: datetime) -> str:
    """A lead, rendered so it cannot be mistaken for a traced endpoint.

    Three separations from ``_endpoint_card``, all deliberate: its own heading
    word ("Lead", never "Nearest"), a badge that states the path is unverified
    even when the operator IS named, and the weakness printed as a block on the
    card itself rather than a footnote — a caveat a reader has to go and find is
    a caveat that gets dropped when the page is photocopied into a filing.
    """
    named = endpoint.names_an_operator
    operator = (
        f'<p class="operator">{_e(endpoint.entity)}</p>'
        if named
        else '<p class="operator">Operator unnamed</p>'
    )
    # The name may be sourced; the PATH to it never is. Both halves are said,
    # because "named operator" alone on this card is the misread that matters.
    # A claim that named nobody takes the second sentence: it is a claim, and it
    # still leaves this lead with no party to serve.
    actionability = (
        "The operator is named on a citable source, but this run did not trace the "
        "subject's funds to it. Treat it as an investigative lead, not as a party to "
        "serve until the link is established by other means."
        if named
        else "No operator is named here, and the route to this address is a guess. It is a "
        "lead to develop, not a respondent."
    )
    metadata = _vasp_table(endpoint.vasp) if endpoint.vasp is not None else ""
    return (
        '<div class="card lead">'
        '<div class="card-head"><h4>Best available lead — not a traced result</h4>'
        '<span class="badge lead">Path unverified</span></div>'
        f"{operator}"
        f'<p class="address">{_e(endpoint.address)}</p>'
        f'<p class="attrs"><span>{_e(endpoint.hop)} hop(s) from the subject</span>'
        f"<span>finding confidence {_e(_pct(endpoint.confidence))}</span>"
        f"<span>{_e(endpoint.chain)}</span></p>"
        f"<p>{_e(endpoint.summary)}</p>"
        f'<div class="weakness"><p class="label">Why this is a lead</p>'
        f"<p>{_e(endpoint.weakness)}</p></div>"
        f'<p class="meta-line">{_e(actionability)}</p>'
        f"{metadata}"
        f"{_evidence_groups(endpoint.evidence_groups, now)}"
        "</div>"
    )


def _answer_section(section: AnswerSection, now: datetime) -> str:
    parts = [
        f'<div class="answer"><h3>{_e(section.heading)}</h3>'
        f'<p class="question">{_e(section.question)}</p>'
    ]
    divergence = section.divergence
    if divergence:
        parts.append(
            '<div class="divergence"><p class="label">Why two answers</p>'
            f"<p>{_e(divergence)}</p></div>"
        )
    if section.best_effort is not None:
        # Headline position (REACHING_THE_VASP.md §4), and the honest "nothing
        # was traced" is printed UNDER it rather than replaced by it: the lead
        # answers "what have you got", it does not answer the objective.
        parts.append(_lead_card(section.best_effort, now))
        parts.append(
            '<div class="card empty"><h4>Traced endpoint</h4>'
            "<p>None. No endpoint was reached in this direction by following value alone. "
            "The lead above was selected across a mixer crossing — see "
            "<strong>Coverage and caveats</strong>.</p></div>"
        )
    elif section.nearest is None and section.nearest_named is None:
        parts.append(
            '<div class="card empty"><h4>No endpoint reached</h4>'
            "<p>This run reached no attributed endpoint in this direction. That is a statement "
            "about what was examined, not a finding that none exists — see "
            "<strong>Coverage and caveats</strong>.</p></div>"
        )
    elif section.same and section.nearest is not None:
        # The heading is a claim like any other line on the card. "and it is
        # named" over a card badged "Operator not named" — the shape a
        # claim-backed endpoint that named nobody produces — is the same
        # over-statement, printed as a heading where it is read first.
        parts.append(
            _endpoint_card(
                section.nearest,
                "Nearest endpoint — and it is named"
                if section.nearest.names_an_operator
                else "Nearest endpoint — no operator is named",
                now,
            )
        )
    else:
        # THE NAME GOES FIRST. This is the same rule the best_effort branch
        # above already follows (REACHING_THE_VASP.md §4, "the name is the
        # headline, the caveat rides with it") and the ordinary path was simply
        # missed.
        #
        # What it cost: on the shipped case the trace answered both directions
        # with a real exchange, and a reader opening the PDF saw the words
        # "Operator unnamed" — because the unnamed nearest endpoint printed
        # first and pushed "Binance" onto the next page. The report was read as
        # having found no VASP when it had found two. For a document whose
        # entire purpose is handing a regulator a name to act on, the ordering
        # of these two cards IS the deliverable, not a layout preference.
        #
        # The unnamed endpoint still prints, directly underneath, because it is
        # a true and different answer: it is where the funds actually went
        # first. Second is not the same as suppressed.
        if section.nearest_named is not None:
            parts.append(
                _endpoint_card(
                    section.nearest_named,
                    "Nearest NAMED endpoint"
                    if section.nearest_named.names_an_operator
                    else "Nearest endpoint carrying a third-party claim — no operator named",
                    now,
                )
            )
        if section.nearest is not None:
            parts.append(_endpoint_card(section.nearest, "Nearest endpoint", now))
        if section.nearest_named is None and section.nearest is not None:
            # Two different absences, and saying the wrong one is a false
            # statement about the evidence on the same page. "No claim at all"
            # and "a sourced claim that names nobody" are not the same finding,
            # and the second is the one a reader has to weigh.
            reason = (
                "A source does record a claim about this address, but it names no operator, "
                "so there is nobody to serve a request on."
                if section.nearest.carries_a_claim
                else "No endpoint on this route carries a third-party claim, so no operator "
                "is identified and there is nobody to serve a request on."
            )
            parts.append(
                '<div class="card empty"><h4>Nearest NAMED endpoint</h4>'
                f"<p>None. {reason}</p></div>"
            )
    parts.append("</div>")
    return "".join(parts)


def _answers_block(sections: Sequence[AnswerSection], now: datetime) -> str:
    if not sections:
        body = (
            '<div class="card empty"><h4>No objectives recorded</h4>'
            "<p>This investigation records no objective, so there is no directional answer to "
            "state.</p></div>"
        )
    else:
        body = "".join(_answer_section(section, now) for section in sections)
    # The lede no longer explains what "named" buys the reader — the summary
    # above has already said it, and saying it twice on one page is how the
    # second saying stops being read.
    return (
        '<section class="block" id="answers"><h2>The answers</h2>'
        '<p class="lede">The endpoints behind the summary, in full. Where the closest endpoint '
        "and the closest NAMED one are different addresses, both are shown, and each conclusion "
        "carries the evidence supporting it grouped by the kind of evidence it is.</p>"
        f"{body}</section>"
    )


def _findings_block(findings: Sequence[Finding], now: datetime) -> str:
    """Everything the run recorded that is not an answer — kept, and kept last.

    This is the bulk of a long report and none of it is the deliverable, so it
    reads as an appendix and sits after the caveats. Moved, not thinned: a
    sanctions hit or a mixer contact belongs in the document whether or not it
    answers the objective, and every finding still prints with its evidence.
    """
    if not findings:
        return (
            '<section class="block" id="findings"><h2>Appendix — other findings</h2>'
            '<p class="lede">This run recorded no findings beyond the answers above.</p>'
            "</section>"
        )
    cards = "".join(
        f'<div class="finding"><p class="kind">{_e(finding.kind)}'
        f"{' · ' + _e(finding.direction) if finding.direction else ''}</p>"
        f"<p>{_e(finding.summary)}</p>"
        f'<p class="address">{_e(finding.subject.value)}</p>'
        f'<p class="meta-line">Finding confidence {_e(_pct(finding.confidence))}</p>'
        f"{_evidence_groups(group_evidence(finding), now)}</div>"
        for finding in findings
    )
    return (
        '<section class="block" id="findings"><h2>Appendix — other findings</h2>'
        '<p class="lede">Everything else this run recorded: sanctions hits, mixer contacts, '
        "bridge crossings, structural patterns, and the points at which branches ended.</p>"
        f"{cards}</section>"
    )


def _coverage_figures(coverage: TraversalCoverage) -> str:
    figures = [
        ("Addresses reached", str(coverage.addresses_reached)),
        (
            "Transactions examined",
            "not recorded"
            if coverage.transactions_examined is None
            else str(coverage.transactions_examined),
        ),
        ("Histories read only in part", str(coverage.truncated_histories)),
        # Named separately from the row above, which these addresses also count
        # toward. "Read only in part" is true of them but says nothing about
        # WHICH part, and the two answers lead a reader somewhere different: a
        # cut page means there is more of the same, a dead token feed means a
        # whole kind of value is missing from what was read.
        ("Addresses missing an acquisition feed", str(coverage.addresses_missing_feeds)),
        (
            "Feeds unavailable",
            ", ".join(feed_name_for_code(code) for code in coverage.feeds_unavailable) or "none",
        ),
        ("Stopped at the depth horizon", str(coverage.depth_horizon_stops)),
        # "Queued", not "Reached": the supernode row below counts addresses that
        # were also reached and never explored, and while this row said "reached"
        # the two figures contradicted each other on the same page — this one
        # printing 0 beside a caveat card stating that forty had been.
        ("Queued but never explored", str(coverage.unexplored_frontier)),
        ("Addresses expanded only in part", str(coverage.capped_expansions)),
        ("Branches dropped by that cap", str(coverage.counterparties_dropped)),
        ("Branches stopped at a mixer", str(coverage.mixer_stops)),
        ("Branches continued past a mixer", str(coverage.mixer_crossings)),
        ("Search depth limit", str(coverage.max_depth) if coverage.max_depth else "not recorded"),
        # Beside the depth limit deliberately: this is the figure that says the
        # other cost limits on this page were not the ones the run actually
        # obeyed, and the depth limit is the one it never raised.
        ("Budget extensions granted", str(len(coverage.budget_extensions))),
    ]
    cells = "".join(
        f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in figures
    )
    return f'<dl class="coverage-figures">{cells}</dl>'


def _caveat_card(caveat: Caveat) -> str:
    clean = " clean" if caveat.code == "no_gaps_recorded" else ""
    subject = f'<p class="address">{_e(caveat.subject)}</p>' if caveat.subject else ""
    return (
        f'<div class="caveat{clean}">'
        f'<p class="headline">{_e(caveat.headline)} '
        f'<span class="code">[{_e(caveat.code)}]</span></p>'
        f"<p>{_e(caveat.detail)}</p>{subject}</div>"
    )


def _coverage_block(report: InvestigationReport) -> str:
    """Mandatory and unconditional — the section a reader must not skip.

    It sits after the answers and before the appendix: a reader who stops at the
    end of it has seen the claim and everything that qualifies it, which is not
    true of anyone who stops before it.

    Rendered from ``report.caveats``, which cannot be empty (see
    ``derive_caveats``), so there is no branch here that can omit it.
    """
    return (
        '<section class="block" id="coverage"><h2>Coverage and caveats</h2>'
        '<p class="lede">What this investigation examined, and what it did not. A conclusion '
        "above is only as good as this section allows: an address that was never read cannot "
        "have been ruled out.</p>"
        f"{_coverage_figures(report.coverage)}"
        f"{''.join(_caveat_card(c) for c in report.caveats)}"
        "</section>"
    )


def _colophon(report: InvestigationReport) -> str:
    """Provenance, in one place instead of two.

    These fields used to print twice — as labelled facts in the masthead and
    again as prose here — which cost page 1 the space the verdict now occupies
    and told a reader nothing the second time. They are all still here, under
    the same labels, because a conclusion is only reproducible against the
    engine and ruleset that produced it and a report handed on without them
    cannot be re-run by the recipient.
    """
    header = report.header
    budgets = ", ".join(f"{k}={v}" for k, v in sorted(header.budgets.items())) or "not recorded"
    spent = ", ".join(f"{k}={v}" for k, v in sorted(header.spent.items())) or "not recorded"
    facts = [
        _fact("Investigation id", header.investigation_id, mono=True),
        _fact("Engine version", header.engine_version, mono=True),
        _fact("Ruleset version", header.ruleset_version, mono=True),
        _fact("Report generated", format_moment(header.generated_at)),
        _fact("Run last updated", format_moment(header.updated_at)),
        _fact("Budgets", budgets),
        _fact("Spent", spent),
    ]
    return (
        '<footer class="colophon">'
        "<h4>How to reproduce this report</h4>"
        f'<dl class="facts">{"".join(facts)}</dl>'
        "<p>Evidence in this document belongs to exactly four kinds — on-chain fact, "
        "third-party claim, heuristic inference, engine observation — and they are never "
        "merged. A heuristic inference never names an operator; only a third-party claim "
        "does.</p>"
        "</footer>"
    )


def render_html(report: InvestigationReport, *, now: datetime | None = None) -> str:
    """Render one investigation as a standalone HTML document.

    ``now`` fixes the clock claim ages are measured against. It defaults to the
    moment the report was generated rather than to the wall clock, so re-rendering
    a stored report does not silently age its claims by the delay between
    building and printing.
    """
    moment = now or report.header.generated_at
    # Reading order, and it is an argument: who this is about, what the answer
    # is, the endpoints behind it, what the run could not see, then everything
    # else it recorded. The appendix moved below the caveats because it is the
    # only part a reader can stop before and still have weighed the claim.
    body = "".join(
        [
            _masthead(report),
            _summary_block(report, moment),
            _answers_block(report.answers, moment),
            _coverage_block(report),
            _findings_block(report.other_findings, moment),
            _colophon(report),
        ]
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(report.title)}</title>"
        f"<style>{_STYLESHEET}</style></head>"
        f'<body><main class="sheet">{body}</main></body></html>'
    )
