"""HTML renderer for the review surface design.

Implements the handoff design (MediLens Review): design tokens, layout, and the
citation click-to-highlight interaction, while preserving every compliance
string required by CLAUDE.md section 3 and the design handoff. Pure functions
with no streamlit import, so the rendering (including escaping and the
compliance strings) is unit-testable without the UI.

Safety notes:
- Every piece of note text and model output is HTML-escaped before it is
  rendered. The note and the model are untrusted inputs to this surface.
- Documentation gaps arrive from the pipeline already phrased conditionally
  ("If clinically accurate, ..."). The renderer bolds that prefix; it never
  invents or removes it.
- No CPT descriptors are rendered anywhere; services are plain-language names.
"""

import html as html_lib
import math

from medilens.ui.recommendation_view import RecommendationView

# The conditional phrase every documentation gap must carry (guardrail 1).
CONDITIONAL_PREFIX = "if clinically accurate,"

# Design tokens from the handoff. Kept as named constants so the palette is
# changed in one place.
COLOR_PAGE_BG = "#fff9f9"
COLOR_BANNER_BG = "#ffd6d7"
COLOR_ALERT = "#ff5a6b"
COLOR_PRIMARY = "#008f9b"
COLOR_DEEP = "#004b55"
COLOR_RISK_LOW = "#7be0d1"
COLOR_RISK_MODERATE = "#ffb9bf"
COLOR_RISK_HIGH = "#ff5a6b"
COLOR_TEXT = "#123c43"
COLOR_TEXT_SECONDARY = "#35565c"
COLOR_MUTED = "#6a8a90"
COLOR_ALERT_TEXT_DARK = "#7c1f2b"
COLOR_ALERT_TEXT = "#c22f41"

_FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:'
    "wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
    '" rel="stylesheet">'
)

HONESTY_SENTENCE = (
    "This suggestion is based only on documentation currently present in the "
    "note. Do not add documentation unless it is clinically accurate. Every "
    "code below is a recommendation for a certified coder or provider to "
    "review; nothing is ever submitted to a payer."
)

FOOTER_SENTENCE = (
    "MediLens recommends the most accurate supported code, never the highest "
    "paying one. A certified coder or provider makes every final decision."
)


def _esc(text: str) -> str:
    """Escape untrusted text for safe HTML embedding."""
    return html_lib.escape(str(text), quote=True)


def split_conditional_gap(gap: str) -> tuple[str, str]:
    """Split a gap into (conditional prefix, remainder) for styled rendering.

    The pipeline guarantees the conditional phrasing (guardrail 1); this only
    detects the standard leading form so the UI can bold it. When the phrase
    is not at the start, the whole gap is returned as the remainder unchanged:
    the renderer must never rewrite a verified string.
    """
    if gap.lower().startswith(CONDITIONAL_PREFIX):
        prefix = gap[: len(CONDITIONAL_PREFIX)]
        remainder = gap[len(CONDITIONAL_PREFIX):].lstrip()
        return prefix, remainder
    return "", gap


def build_note_segments(
    note_text: str, spans: list[tuple[str, int, int]]
) -> list[tuple[str, list[str]]]:
    """Split note text into segments tagged with the citation ids covering them.

    spans is a list of (span_id, start_offset, end_offset) with offsets into
    note_text. Overlapping spans are handled by splitting at every boundary,
    so each returned segment carries the full set of ids that cover it. The
    concatenated segment texts always equal the original note exactly: this
    function must never alter note content, only partition it.
    """
    boundaries = {0, len(note_text)}
    for _, start_offset, end_offset in spans:
        clamped_start = max(0, min(start_offset, len(note_text)))
        clamped_end = max(0, min(end_offset, len(note_text)))
        boundaries.add(clamped_start)
        boundaries.add(clamped_end)
    ordered = sorted(boundaries)

    segments: list[tuple[str, list[str]]] = []
    for index in range(len(ordered) - 1):
        segment_start = ordered[index]
        segment_end = ordered[index + 1]
        covering_ids: list[str] = []
        for span_id, start_offset, end_offset in spans:
            if start_offset <= segment_start and segment_end <= end_offset:
                covering_ids.append(span_id)
        segments.append((note_text[segment_start:segment_end], covering_ids))
    return segments


def risk_band(score: float) -> tuple[str, str]:
    """Map a denial-risk score to its display band label and color."""
    if score < 0.34:
        return "Low", COLOR_RISK_LOW
    if score < 0.67:
        return "Moderate", COLOR_RISK_MODERATE
    return "High", COLOR_RISK_HIGH


# --- host-page fragments (rendered by Streamlit st.markdown) -----------------


def page_css() -> str:
    """Global CSS injected into the Streamlit host page."""
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
html, body, [data-testid="stAppViewContainer"] {{
  background: {COLOR_PAGE_BG};
  font-family: 'IBM Plex Sans', system-ui, sans-serif;
  color: {COLOR_TEXT};
}}
header[data-testid="stHeader"] {{ display: none; }}
.block-container {{ padding-top: 16px; padding-bottom: 48px; max-width: 1400px; }}
div[data-testid="stWidgetLabel"] p {{
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.07em; color: {COLOR_MUTED};
}}
div[data-testid="stSelectbox"] > div > div,
div[data-testid="stDateInput"] input {{
  background: #fdfbfb; border-color: #e3cdcd; color: {COLOR_DEEP};
  font-weight: 600;
}}
button[kind="primary"] {{
  background: {COLOR_PRIMARY}; border: none; font-weight: 700;
}}
button[kind="primary"]:hover {{ background: {COLOR_DEEP}; }}
</style>
"""


def top_bar_html(live: bool, model_name: str) -> str:
    """The dark top bar: brand, live/sample pill, synthetic-only badge."""
    if live:
        pill_text = _esc(f"Live · {model_name}")
        dot_color = COLOR_RISK_LOW
    else:
        pill_text = "Sample mode · not analyzing"
        dot_color = COLOR_RISK_MODERATE
    return f"""
<header style="background: {COLOR_DEEP}; color: {COLOR_PAGE_BG}; padding: 0 32px; height: 60px; display: flex; align-items: center; gap: 16px; border-radius: 10px;">
  <div style="display: flex; align-items: center; gap: 10px;">
    <div style="width: 26px; height: 26px; border-radius: 50%; border: 3px solid {COLOR_ALERT}; display: grid; place-items: center;">
      <div style="width: 8px; height: 8px; border-radius: 50%; background: {COLOR_BANNER_BG};"></div>
    </div>
    <div style="font-size: 18px; font-weight: 700; letter-spacing: -0.01em;">MediLens</div>
    <div style="font-size: 13px; color: rgba(255,249,249,0.7); border-left: 1px solid rgba(255,249,249,0.25); padding-left: 12px;">Documentation and coding review</div>
  </div>
  <div style="margin-left: auto; display: flex; align-items: center; gap: 8px;">
    <div style="display: flex; align-items: center; gap: 6px; font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; background: rgba(255,249,249,0.1); border: 1px solid rgba(255,249,249,0.2); border-radius: 999px; padding: 5px 12px;">
      <span style="width: 7px; height: 7px; border-radius: 50%; background: {dot_color}; display: inline-block;"></span>
      {pill_text}
    </div>
    <div style="font-size: 11.5px; font-weight: 600; background: {COLOR_ALERT}; color: {COLOR_PAGE_BG}; border-radius: 999px; padding: 5px 12px; letter-spacing: 0.02em;">SYNTHETIC NOTES ONLY</div>
  </div>
</header>
"""


def honesty_banner_html() -> str:
    """The persistent honesty banner (guardrail 8). Never dismissible."""
    return f"""
<div style="background: {COLOR_BANNER_BG}; color: {COLOR_ALERT_TEXT_DARK}; padding: 10px 32px; font-size: 13px; line-height: 1.5; display: flex; gap: 10px; align-items: baseline; border-radius: 8px; margin-top: 8px;">
  <strong style="font-weight: 700; white-space: nowrap;">Review required.</strong>
  <span>{_esc(HONESTY_SENTENCE)}</span>
</div>
"""


def dos_note_html() -> str:
    """The date-of-service resolution note shown in the request bar."""
    return f"""
<div style="font-size: 12px; color: {COLOR_MUTED}; line-height: 1.45; padding-top: 24px;">Code sets and payer policy are resolved against the date of service, not today.</div>
"""


def stale_chip_html() -> str:
    """Chip shown when any request input changed since the last completed run."""
    return f"""
<div style="display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 600; color: {COLOR_ALERT_TEXT}; background: #fff0f0; border: 1px solid {COLOR_BANNER_BG}; border-radius: 999px; padding: 7px 14px; margin-top: 20px;">
  <span style="width: 7px; height: 7px; border-radius: 50%; background: {COLOR_ALERT}; display: inline-block;"></span>
  Inputs changed · run review to refresh results
</div>
"""


def analyzing_pill_html(payer_name: str, date_of_service_iso: str) -> str:
    """Pill shown while the pipeline call is in flight."""
    return f"""
<div style="display: flex; align-items: center; gap: 10px; background: #e3f2f3; border: 1px solid #bfe0e2; border-radius: 8px; padding: 11px 16px; font-size: 13px; font-weight: 600; color: #00646f; margin-top: 12px;">
  <span style="width: 9px; height: 9px; border-radius: 50%; background: {COLOR_PRIMARY}; display: inline-block;"></span>
  Validating note against payer policy for {_esc(payer_name)} · resolving code sets against {_esc(date_of_service_iso)}
</div>
"""


def error_card_html(title: str, message: str) -> str:
    """Design-consistent alert card for refusals and pipeline errors."""
    return f"""
<div style="background: #fff0f0; border: 1px solid {COLOR_BANNER_BG}; border-radius: 10px; padding: 16px 20px; margin-top: 12px;">
  <div style="font-size: 13.5px; font-weight: 700; color: {COLOR_ALERT_TEXT_DARK};">{_esc(title)}</div>
  <div style="font-size: 13.5px; line-height: 1.6; color: {COLOR_ALERT_TEXT_DARK}; margin-top: 6px;">{_esc(message)}</div>
</div>
"""


def sample_banner_html() -> str:
    """Banner shown when configuration is missing and output is a fixed sample."""
    return f"""
<div style="background: #fff0f0; border: 1px solid {COLOR_ALERT}; border-radius: 10px; padding: 14px 20px; margin-top: 12px; font-size: 13.5px; line-height: 1.6; color: {COLOR_ALERT_TEXT_DARK};">
  <strong>SAMPLE OUTPUT.</strong> ANTHROPIC_API_KEY or DATABASE_URL is not configured, so this screen shows a fixed illustrative example. It does not analyze the note. Do not use it for any real coding decision.
</div>
"""


# --- the results document (rendered in a component iframe) -------------------


def _results_css() -> str:
    return f"""
html, body {{ margin: 0; padding: 0; background: {COLOR_PAGE_BG}; }}
body {{ font-family: 'IBM Plex Sans', system-ui, sans-serif; color: {COLOR_TEXT}; -webkit-font-smoothing: antialiased; }}
.mono {{ font-family: 'IBM Plex Mono', monospace; }}
.grid {{ display: grid; grid-template-columns: minmax(380px, 44%) 1fr; gap: 24px; align-items: start; }}
.card {{ background: #ffffff; border: 1px solid #f0dcdc; border-radius: 10px; }}
.card-header {{ padding: 14px 22px; border-bottom: 1px solid #f0dcdc; display: flex; align-items: baseline; gap: 10px; }}
.card-title {{ font-size: 14px; font-weight: 700; color: {COLOR_DEEP}; }}
.card-subtitle {{ font-size: 12px; color: {COLOR_MUTED}; }}
.overline {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: {COLOR_MUTED}; }}
.ml-cit {{ background: #fdeeee; border-bottom: 2px solid #f2cdd0; border-radius: 3px; padding: 1px 0; transition: background 0.25s, border-color 0.25s; }}
.ml-cit.active {{ background: {COLOR_BANNER_BG}; border-bottom: 2px solid {COLOR_ALERT}; }}
.ml-chip {{ all: unset; cursor: pointer; display: flex; align-items: baseline; gap: 10px; padding: 9px 12px; border-radius: 7px; border: 1px solid #f0dcdc; background: #fdfbfb; transition: background 0.2s, border-color 0.2s; box-sizing: border-box; width: 100%; }}
.ml-chip:hover {{ background: #fff0f0; border-color: #ffb9bf; }}
.ml-chip.active {{ background: #fff0f0; border-color: {COLOR_ALERT}; }}
.ml-chip.active .chip-mark {{ color: {COLOR_ALERT_TEXT}; }}
.chip-mark {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: #8a9ea3; font-weight: 600; white-space: nowrap; }}
.chip-quote {{ font-size: 13px; color: {COLOR_TEXT_SECONDARY}; font-style: italic; }}
.clause-row {{ display: flex; gap: 12px; padding: 11px 14px; border-bottom: 1px solid #f7e9e9; background: #fdfbfb; align-items: baseline; }}
.clause-row:last-child {{ border-bottom: none; }}
"""


def _render_note_panel(note_text: str, spans: list[tuple[str, int, int]]) -> str:
    segments = build_note_segments(note_text, spans)
    segment_parts: list[str] = []
    for segment_text, covering_ids in segments:
        escaped = _esc(segment_text)
        if len(covering_ids) > 0:
            id_list = _esc(" ".join(covering_ids))
            segment_parts.append(
                f'<span class="ml-cit" data-spans="{id_list}">{escaped}</span>'
            )
        else:
            segment_parts.append(escaped)
    note_body = "".join(segment_parts)

    return f"""
<section class="card" style="position: sticky; top: 8px; overflow: hidden;">
  <div class="card-header" style="background: #fdf3f3;">
    <div class="card-title">Clinical note</div>
    <div style="font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em; color: {COLOR_ALERT_TEXT_DARK}; background: {COLOR_BANNER_BG}; border-radius: 4px; padding: 3px 7px;">SYNTHETIC</div>
    <div style="margin-left: auto; font-size: 12px; color: {COLOR_MUTED};">Normalized before analysis · screened for PHI</div>
  </div>
  <div style="padding: 18px 22px 22px; font-size: 13.5px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: break-word;">{note_body}</div>
  <div style="padding: 0 22px 18px; font-size: 11.5px; color: #a4757c;" class="mono">SYNTHETIC NOTE. Not a real patient. For testing only.</div>
</section>
"""


def _render_hero(view: RecommendationView) -> str:
    band_label, band_color = risk_band(view.denial_risk_score)
    marker_left = f"{view.denial_risk_score * 100:.0f}%"
    return f"""
<div style="background: {COLOR_DEEP}; color: {COLOR_PAGE_BG}; border-radius: 10px; padding: 24px 26px;">
  <div style="display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
    <div style="display: flex; flex-direction: column; gap: 2px;">
      <div style="font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.09em; color: rgba(255,249,249,0.6);">Predicted denial risk · {_esc(view.payer_name)}</div>
      <div style="display: flex; align-items: baseline; gap: 12px;">
        <div style="font-size: 42px; font-weight: 700; letter-spacing: -0.02em; color: {band_color};">{band_label}</div>
        <div style="font-size: 20px; color: rgba(255,249,249,0.75);" class="mono">{view.denial_risk_score:.2f}</div>
      </div>
    </div>
    <div style="flex: 1; min-width: 220px; padding-top: 18px;">
      <div style="position: relative; height: 10px; border-radius: 999px; background: linear-gradient(90deg, {COLOR_PRIMARY} 0%, #7ab6a9 45%, #ff9aa5 70%, {COLOR_ALERT} 100%); opacity: 0.9;">
        <div style="position: absolute; top: -5px; left: {marker_left}; width: 20px; height: 20px; margin-left: -10px; border-radius: 50%; background: {COLOR_PAGE_BG}; border: 4px solid {band_color}; box-sizing: border-box;"></div>
      </div>
      <div style="display: flex; justify-content: space-between; font-size: 10.5px; color: rgba(255,249,249,0.5); margin-top: 8px;" class="mono">
        <span>0.0</span><span>0.5</span><span>1.0</span>
      </div>
    </div>
  </div>
  <p style="margin: 16px 0 0; font-size: 13.5px; line-height: 1.6; color: rgba(255,249,249,0.85);">{_esc(view.denial_risk_rationale)}</p>
</div>
"""


def _render_code_block(code_index: int, suggestion) -> str:
    if suggestion.has_coverage_basis:
        status_text = "Supported by note + policy"
    else:
        status_text = "Documentation-supported only"

    chip_parts: list[str] = []
    for span_index, span in enumerate(suggestion.supporting_note_spans):
        quote = _esc(span.text)
        if span.is_located:
            span_id = f"s{code_index}-{span_index}"
            mark = f"chars {span.start_offset}-{span.end_offset}"
            chip_parts.append(
                f'<button class="ml-chip" data-span="{span_id}">'
                f'<span class="chip-mark">{_esc(mark)}</span>'
                f'<span class="chip-quote">"{quote}"</span></button>'
            )
        else:
            chip_parts.append(
                '<div class="ml-chip" style="cursor: default; opacity: 0.7;">'
                '<span class="chip-mark">illustrative</span>'
                f'<span class="chip-quote">"{quote}"</span></div>'
            )
    chips_html = "".join(chip_parts)

    if suggestion.has_coverage_basis:
        clause_parts: list[str] = []
        policy_ids: list[str] = []
        for clause in suggestion.cited_policy_clauses:
            if clause.policy_identifier not in policy_ids:
                policy_ids.append(clause.policy_identifier)
            ref = f"{clause.policy_identifier} · cl. {clause.clause_number}"
            clause_parts.append(
                '<div class="clause-row">'
                f'<span class="chip-mark" style="color: #00646f;">{_esc(ref)}</span>'
                f'<span style="font-size: 13px; line-height: 1.55; color: {COLOR_TEXT_SECONDARY};">{_esc(clause.clause_text)}</span>'
                "</div>"
            )
        policy_id_label = _esc(", ".join(policy_ids))
        coverage_html = f"""
    <div style="margin-top: 20px; display: flex; align-items: baseline; gap: 10px;">
      <span class="overline">Cited policy clauses</span>
      <span class="chip-mark" style="color: #00646f;">{policy_id_label}</span>
    </div>
    <div style="margin-top: 8px; border: 1px solid #f0dcdc; border-radius: 7px; overflow: hidden;">{"".join(clause_parts)}</div>
"""
    else:
        coverage_html = f"""
    <div style="margin-top: 20px; background: #fff0f0; border: 1px solid {COLOR_BANNER_BG}; border-radius: 7px; padding: 12px 14px; font-size: 13px; line-height: 1.55; color: {COLOR_ALERT_TEXT_DARK};">
      <strong>No coverage basis:</strong> this code is supported by the note text, but no clause from the applicable payer policy was cited for it. Coverage is unconfirmed; review the payer policy manually.
    </div>
"""

    return f"""
  <div style="padding: 18px 22px 22px; border-bottom: 1px solid #f7e9e9;">
    <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
      <div class="mono" style="font-size: 19px; font-weight: 600; color: {COLOR_DEEP}; background: #e3f2f3; border: 1px solid #bfe0e2; border-radius: 6px; padding: 4px 10px;">{_esc(suggestion.code)}</div>
      <div style="font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em; color: #00646f; background: #e3f2f3; border-radius: 4px; padding: 3px 7px;">{_esc(suggestion.code_system)}</div>
      <div style="font-size: 15px; font-weight: 600; color: {COLOR_TEXT};">{_esc(suggestion.description)}</div>
      <div style="margin-left: auto; font-size: 11.5px; font-weight: 600; color: #00646f; display: flex; align-items: center; gap: 6px;">
        <span style="width: 8px; height: 8px; border-radius: 50%; background: {COLOR_PRIMARY}; display: inline-block;"></span>
        {_esc(status_text)}
      </div>
    </div>
    <p style="margin: 14px 0 0; font-size: 13.5px; line-height: 1.6; color: {COLOR_TEXT_SECONDARY};">{_esc(suggestion.rationale)}</p>
    <div class="overline" style="margin-top: 18px;">Supporting note spans · click to locate in note</div>
    <div style="display: flex; flex-direction: column; gap: 8px; margin-top: 10px;">{chips_html}</div>
    {coverage_html}
  </div>
"""


def _render_codes_card(view: RecommendationView) -> str:
    if len(view.code_suggestions) == 0:
        body = f"""
  <div style="padding: 18px 22px 22px; font-size: 13.5px; line-height: 1.6; color: {COLOR_TEXT_SECONDARY};">
    No supported codes found in the documentation. See the denial risk rationale and documentation gaps.
  </div>
"""
    else:
        block_parts: list[str] = []
        for code_index, suggestion in enumerate(view.code_suggestions):
            block_parts.append(_render_code_block(code_index, suggestion))
        body = "".join(block_parts)

    return f"""
<div class="card">
  <div class="card-header">
    <div class="card-title">Recommended codes</div>
    <div class="card-subtitle">Most accurate supported code, never the highest paying one</div>
  </div>
  {body}
</div>
"""


def _render_gaps_card(view: RecommendationView) -> str:
    if len(view.documentation_gaps) == 0:
        rows = f"""
    <div style="display: flex; gap: 10px; align-items: center;">
      <div style="width: 22px; height: 22px; border-radius: 50%; background: #e3f2f3; color: {COLOR_PRIMARY}; display: grid; place-items: center; font-weight: 700; font-size: 13px; flex: none;">&#10003;</div>
      <p style="margin: 0; font-size: 13.5px; color: {COLOR_TEXT_SECONDARY};">No documentation gaps identified for this request.</p>
    </div>
"""
    else:
        row_parts: list[str] = []
        gap_number = 1
        for gap in view.documentation_gaps:
            prefix, remainder = split_conditional_gap(gap)
            if prefix != "":
                gap_body = (
                    f'<strong style="color: {COLOR_ALERT_TEXT_DARK};">{_esc(prefix)}</strong> '
                    f"{_esc(remainder)}"
                )
            else:
                gap_body = _esc(remainder)
            row_parts.append(
                '<div style="display: flex; gap: 12px; align-items: flex-start;">'
                f'<div style="width: 22px; height: 22px; border-radius: 50%; background: {COLOR_BANNER_BG}; color: {COLOR_ALERT_TEXT}; display: grid; place-items: center; font-weight: 700; font-size: 13px; flex: none; margin-top: 1px;">{gap_number}</div>'
                f'<p style="margin: 0; font-size: 13.5px; line-height: 1.6; color: {COLOR_TEXT_SECONDARY};">{gap_body}</p>'
                "</div>"
            )
            gap_number = gap_number + 1
        rows = "".join(row_parts)

    return f"""
<div class="card">
  <div class="card-header">
    <div class="card-title">Documentation gaps</div>
    <div class="card-subtitle">Each item is conditional on clinical accuracy. Do not document anything that is not clinically true.</div>
  </div>
  <div style="padding: 16px 22px 20px; display: flex; flex-direction: column; gap: 12px;">{rows}</div>
</div>
"""


def _render_facts_card(view: RecommendationView) -> str:
    fact_parts: list[str] = []
    for fact in view.extracted_facts:
        fact_parts.append(
            '<div style="display: flex; gap: 9px; align-items: baseline; '
            f'font-size: 13px; line-height: 1.5; color: {COLOR_TEXT_SECONDARY};">'
            f'<span style="width: 6px; height: 6px; border-radius: 50%; background: {COLOR_PRIMARY}; flex: none; position: relative; top: -2px;"></span>'
            f"<span>{_esc(fact)}</span></div>"
        )
    return f"""
<div class="card">
  <div class="card-header">
    <div class="card-title">Extracted facts</div>
    <div class="card-subtitle">Facts read from the note. The tool never invents clinical facts.</div>
  </div>
  <div style="padding: 14px 22px 18px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px;">{"".join(fact_parts)}</div>
</div>
"""


def _render_rejections_card(view: RecommendationView) -> str:
    if len(view.verification_rejections) == 0:
        return ""
    item_parts: list[str] = []
    for rejection in view.verification_rejections:
        item_parts.append(
            f'<li style="margin-bottom: 6px;">{_esc(rejection)}</li>'
        )
    return f"""
<div style="background: #fff0f0; border: 1px solid {COLOR_BANNER_BG}; border-radius: 10px;">
  <div class="card-header" style="border-bottom-color: {COLOR_BANNER_BG};">
    <div class="card-title" style="color: {COLOR_ALERT_TEXT_DARK};">Dropped by verification</div>
    <div class="card-subtitle" style="color: {COLOR_ALERT_TEXT};">The model produced these, but they failed a grounding check and were not shown as recommendations or stored as codes. Recorded in the audit trail.</div>
  </div>
  <ul style="margin: 0; padding: 14px 22px 16px 40px; font-size: 13px; line-height: 1.55; color: {COLOR_ALERT_TEXT_DARK};">{"".join(item_parts)}</ul>
</div>
"""


def _render_provenance(view: RecommendationView, audit_id: int | None) -> str:
    if audit_id is not None:
        audit_label = str(audit_id)
    else:
        audit_label = "not stored (sample)"
    generated_label = view.generated_at.isoformat(timespec="seconds")
    return f"""
<div style="background: #fdf3f3; border: 1px dashed #e8c9c9; border-radius: 10px; padding: 14px 22px 16px;">
  <div style="display: flex; align-items: baseline; gap: 10px;">
    <div style="font-size: 12.5px; font-weight: 700; color: {COLOR_DEEP};">Provenance</div>
    <div class="card-subtitle">Every recommendation is reconstructable for audit. Audit records are append only.</div>
  </div>
  <div class="mono" style="display: flex; gap: 28px; flex-wrap: wrap; margin-top: 10px; font-size: 11.5px; color: #6b555a;">
    <span>model: {_esc(view.model_name)}</span>
    <span>prompt: {_esc(view.prompt_template_version)}</span>
    <span>input: {_esc(view.input_reference)}</span>
    <span>generated: {_esc(generated_label)}</span>
    <span>audit id: {_esc(audit_label)}</span>
  </div>
</div>
"""


_INTERACTION_JS = """
<script>
(function () {
  var chips = document.querySelectorAll('button.ml-chip[data-span]');
  function clearActive() {
    document.querySelectorAll('.ml-chip.active').forEach(function (el) {
      el.classList.remove('active');
    });
    document.querySelectorAll('.ml-cit.active').forEach(function (el) {
      el.classList.remove('active');
    });
  }
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var spanId = chip.dataset.span;
      var wasActive = chip.classList.contains('active');
      clearActive();
      if (wasActive) { return; }
      chip.classList.add('active');
      var firstMatch = null;
      document.querySelectorAll('.ml-cit').forEach(function (segment) {
        var ids = segment.dataset.spans.split(' ');
        if (ids.indexOf(spanId) !== -1) {
          segment.classList.add('active');
          if (firstMatch === null) { firstMatch = segment; }
        }
      });
      if (firstMatch !== null) {
        firstMatch.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });
  });
})();
</script>
"""


def build_results_html(
    view: RecommendationView,
    note_text: str,
    audit_id: int | None,
    stale: bool = False,
) -> str:
    """Build the full results document rendered inside the component iframe.

    Contains the note panel (left, with citation highlight targets) and the
    results stack (right). All content is escaped; the only scripting is the
    static citation click-to-highlight handler.
    """
    span_index: list[tuple[str, int, int]] = []
    for code_index, suggestion in enumerate(view.code_suggestions):
        for span_j, span in enumerate(suggestion.supporting_note_spans):
            if span.is_located:
                span_id = f"s{code_index}-{span_j}"
                span_index.append((span_id, span.start_offset, span.end_offset))

    note_panel = _render_note_panel(note_text, span_index)

    if stale:
        results_opacity = "0.55"
    else:
        results_opacity = "1"

    if view.is_sample:
        sample_strip = sample_banner_html()
    else:
        sample_strip = ""

    results_stack = f"""
<section style="display: flex; flex-direction: column; gap: 18px; min-width: 0; opacity: {results_opacity};">
  {sample_strip}
  {_render_hero(view)}
  {_render_codes_card(view)}
  {_render_gaps_card(view)}
  {_render_facts_card(view)}
  {_render_rejections_card(view)}
  {_render_provenance(view, audit_id)}
  <div style="font-size: 12px; color: #a4757c; text-align: center; padding: 4px 0 0;">{_esc(FOOTER_SENTENCE)}</div>
</section>
"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{_FONT_LINK}
<style>{_results_css()}</style></head>
<body>
<main class="grid">
{note_panel}
{results_stack}
</main>
{_INTERACTION_JS}
</body></html>
"""


def results_height(view: RecommendationView, note_text: str) -> int:
    """Estimate the component iframe height from the content volume.

    The Streamlit component iframe needs a fixed height. This is a heuristic,
    not layout math; scrolling=True on the component covers the misses.
    """
    note_lines = note_text.count("\n") + 1
    left_height = 180 + note_lines * 23

    right_height = 360
    if view.is_sample:
        right_height = right_height + 100
    if len(view.code_suggestions) == 0:
        right_height = right_height + 140
    for suggestion in view.code_suggestions:
        right_height = right_height + 300
        right_height = right_height + len(suggestion.supporting_note_spans) * 48
        if suggestion.has_coverage_basis:
            right_height = right_height + len(suggestion.cited_policy_clauses) * 70
        else:
            right_height = right_height + 100
    right_height = right_height + 120 + max(len(view.documentation_gaps), 1) * 64
    right_height = right_height + 110 + math.ceil(len(view.extracted_facts) / 2) * 42
    if len(view.verification_rejections) > 0:
        right_height = right_height + 110 + len(view.verification_rejections) * 44
    right_height = right_height + 200

    estimated = max(left_height, right_height) + 80
    return max(900, min(estimated, 6000))
