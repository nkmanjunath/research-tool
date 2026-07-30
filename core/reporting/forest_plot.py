from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.database import DATA_ROOT, get_connection, init_db
from core.planning.diagnostics import check_violation
from core.reporting import latest_locked_plan as _latest_locked_plan, svg_escape as _svg_escape


COVARIATE_LABEL_MAP = {
    "treatment_arm": "Treatment Group",
    "high_risk_fish": "High-Risk Cytogenetics",
    "prior_lines": "Prior Lines of Therapy",
    "age": "Age, years",
    "iss_stage": "ISS Stage",
}

COVARIATE_UNIT_MAP = {
    "prior_lines": "Per additional line",
    "age": "Per year increase",
}


@dataclass
class CovariateRow:
    covariate: str
    hr: float
    ci_lower: float
    ci_upper: float
    wald_p: float
    coef: float | None
    se: float | None
    z: float | None
    reference_level: str | None = None
    tested_level: str | None = None
    unstable: bool = False
    violated: bool = False

    @property
    def ci_crosses_one(self) -> bool:
        return self.ci_lower <= 1 <= self.ci_upper

    @property
    def display_label(self) -> str:
        base_name = COVARIATE_LABEL_MAP.get(self.covariate, self.covariate.replace("_", " ").title())
        if self.reference_level is not None and self.tested_level is not None:
            ref_str = str(self.reference_level).capitalize() if str(self.reference_level).lower() in ("yes", "no") else str(self.reference_level)
            test_str = str(self.tested_level).capitalize() if str(self.tested_level).lower() in ("yes", "no") else str(self.tested_level)
            return f"{base_name} ({test_str} vs {ref_str})"
        if " (interaction)" in self.covariate:
            return self.covariate
        unit_str = COVARIATE_UNIT_MAP.get(self.covariate, "per 1-unit increase")
        return f"{base_name} ({unit_str})"


@dataclass
class ForestPlotData:
    result_id: int
    study_plan_version: int
    concordance_index: float | None
    lr_test_p: float | None
    n_analyzed: int
    n_events: int
    epv: float | None
    epv_warning: bool
    covariates: list[CovariateRow]
    violation_warning: bool = False
    violation_summary: str = ""


def _extract_epv(warnings_text: str | None) -> tuple[float | None, bool]:
    if not warnings_text:
        return None, False
    m = re.search(r"EPV=([\d.]+)", warnings_text)
    if m:
        epv = float(m.group(1))
        return epv, epv < 10
    return None, False


def _get_event_count(study_id: str, event_col: str, conn) -> int:
    for table in (f"raw_{study_id}", f"raw_masked_{study_id}"):
        try:
            row = conn.execute(
                f'SELECT COUNT("{event_col}") AS n_observed, '
                f'SUM(CASE WHEN CAST("{event_col}" AS INTEGER) = 1 THEN 1 ELSE 0 END) '
                f'AS n_events FROM {table}'
            ).fetchone()
        except Exception:
            continue
        if row and int(row["n_observed"] or 0) > 0:
            return int(row["n_events"] or 0)
    return 0


def _get_events_from_sample_counts(sc_json: str | None) -> int:
    if not sc_json:
        return 0
    try:
        sc = json.loads(sc_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    na = sc.get("n_analyzed", 0)
    # events isn't stored — infer from Cox PH covariate warnings
    return na



def load_forest_data(study_id: str) -> ForestPlotData:
    conn = get_connection(study_id)
    init_db(conn)

    plan = _latest_locked_plan(study_id)

    warnings_text = json.dumps(plan.get("warnings", {}))
    epv, epv_warn = _extract_epv(warnings_text)

    result = conn.execute(
        """SELECT id, study_plan_version, concordance_index, lr_test_p,
                  sample_counts_json, status_json, ph_diagnostics_json
           FROM analysis_results
           WHERE study_id=? AND test_name='cox_ph_model'
             AND id NOT IN (
               SELECT COALESCE(superseded_previous_result_id, -1)
               FROM analysis_results
               WHERE study_id=? AND test_name='cox_ph_model'
                 AND superseded_previous_result_id IS NOT NULL
             )
           ORDER BY id DESC
           LIMIT 1""",
        (study_id, study_id),
    ).fetchone()
    if not result:
        conn.close()
        raise ValueError(f"No non-superseded cox_ph_model result found for {study_id}")

    rd = dict(result)
    result_id = rd["id"]

    sc = json.loads(rd["sample_counts_json"]) if rd.get("sample_counts_json") else {}
    n_analyzed = sc.get("n_analyzed", 0)

    # Get event column from study plan's cox model config
    event_col = None
    cox_models = plan.get("cox_ph_models", [])
    if cox_models:
        event_col = cox_models[0].get("event_col")
    n_events = _get_event_count(study_id, event_col, conn) if event_col else 0

    cov_rows = conn.execute(
        """SELECT covariate, hr, ci_lower, ci_upper, wald_p, coef, se, z,
                  reference_level, tested_level
           FROM analysis_covariate_results
           WHERE result_id=?
           ORDER BY id""",
        (result_id,),
    ).fetchall()

    covariates: list[CovariateRow] = []
    for r in cov_rows:
        cd = dict(r)
        unstable = (
            cd.get("hr") is None
            or cd.get("ci_lower") is None
            or cd.get("ci_upper") is None
            or (cd.get("hr") is not None and (math.isinf(cd["hr"]) or math.isnan(cd["hr"])))
            or (cd.get("ci_lower") is not None and (math.isinf(cd["ci_lower"]) or math.isnan(cd["ci_lower"])))
            or (cd.get("ci_upper") is not None and (math.isinf(cd["ci_upper"]) or math.isnan(cd["ci_upper"])))
        )
        covariates.append(CovariateRow(
            covariate=cd["covariate"],
            hr=cd["hr"] if cd.get("hr") is not None else 0,
            ci_lower=cd["ci_lower"] if cd.get("ci_lower") is not None else 0,
            ci_upper=cd["ci_upper"] if cd.get("ci_upper") is not None else 0,
            wald_p=cd["wald_p"] if cd.get("wald_p") is not None else 1.0,
            coef=cd.get("coef"),
            se=cd.get("se"),
            z=cd.get("z"),
            reference_level=cd.get("reference_level"),
            tested_level=cd.get("tested_level"),
            unstable=unstable,
        ))

    conn.close()

    return ForestPlotData(
        result_id=result_id,
        study_plan_version=rd["study_plan_version"],
        concordance_index=rd.get("concordance_index"),
        lr_test_p=rd.get("lr_test_p"),
        n_analyzed=n_analyzed,
        n_events=n_events,
        epv=epv,
        epv_warning=epv_warn,
        covariates=covariates,
    )


def _populate_violation(data: ForestPlotData, result_row: dict) -> ForestPlotData:
    """Add post-unmask violation info from result row to ForestPlotData.
    Also marks individual CovariateRow.violated for matched Schoenfeld violations."""
    has_viol, summary, details = check_violation(result_row)
    data.violation_warning = has_viol
    data.violation_summary = summary
    if has_viol:
        ph_raw = result_row.get("ph_diagnostics_json")
        if ph_raw:
            import json
            ph = json.loads(ph_raw) if isinstance(ph_raw, str) else ph_raw
            violated_bases: list[str] = []
            for cov in ph.get("covariates", []):
                p = cov.get("p_value", 1)
                if p < 0.05:
                    name = cov.get("covariate", "")
                    # Strip lifelines' [T.level] suffix for matching
                    base = name.split("[")[0].strip()
                    violated_bases.append(base)
            for cv in data.covariates:
                if cv.covariate in violated_bases:
                    cv.violated = True
    return data



# ── SVG renderer ─────────────────────────────────────────────────────────────

def _log_scale_x(hr: float, x_min: float, x_max: float, width: float) -> float:
    if hr <= 0 or x_min <= 0 or x_max <= 0:
        return width / 2
    return (math.log(hr) - math.log(x_min)) / (math.log(x_max) - math.log(x_min)) * width


SVG_TPL = """<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"
     font-family="Arial, Helvetica, sans-serif" font-size="13">
{body}
</svg>"""


def render_svg(data: ForestPlotData, output_path: str) -> None:
    margin_l = 20
    margin_r = 30
    margin_t = 40
    margin_b = 80
    label_w = 280
    plot_l = margin_l + label_w + 20  # 320
    plot_w = 400
    row_h = 36
    txt_x = plot_l + plot_w + 20       # 740
    p_col_offset = 190                 # 930 for p-value column
    text_col_w = 270

    svg_w = txt_x + text_col_w + margin_r  # 1040
    n = len(data.covariates)
    plot_h = max(n * row_h, 60)
    svg_h = margin_t + plot_h + margin_b

    # Determine log range: include all CIs and HR=1, with padding for x-axis ticks
    all_vals = [1.0]
    for c in data.covariates:
        if not c.unstable:
            all_vals.extend([c.hr, c.ci_lower, c.ci_upper])
    min_v = min(all_vals)
    max_v = max(all_vals)

    lo = min(0.04 if min_v < 0.1 else 0.08, min_v / 1.5)
    hi = max(15.0 if max_v > 8.0 else 12.0, max_v * 1.5)
    if lo <= 0:
        lo = 0.04

    svg_header = f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" ' \
                 f'font-family="Arial, Helvetica, sans-serif" font-size="13">'

    parts: list[str] = []

    parts.append(f'  <text x="{margin_l}" y="24" font-size="16" font-weight="bold" '
                 f'fill="#1F4E78">Forest Plot — Cox PH Model</text>')

    # Header row — side-by-side column headers aligned with data columns
    header_y = margin_t
    parts.append(f'  <text x="{margin_l}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555">Covariate</text>')
    parts.append(f'  <text x="{txt_x}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555">aHR [95% CI]</text>')
    parts.append(f'  <text x="{txt_x + p_col_offset}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555">p-value</text>')

    # Horizontal reference line at HR=1
    ref_x = _log_scale_x(1.0, lo, hi, plot_w) + plot_l
    parts.append(
        f'  <line x1="{ref_x}" y1="{margin_t}" x2="{ref_x}" y2="{margin_t + plot_h}" '
        f'stroke="#999" stroke-width="1.5" stroke-dasharray="6,3" />'
    )

    # X-axis
    ax_y = margin_t + plot_h + 5
    parts.append(f'  <line x1="{plot_l}" y1="{ax_y}" x2="{plot_l + plot_w}" y2="{ax_y}" '
                 f'stroke="#333" stroke-width="1" />')
    # Tick labels for x-axis (HR values) — include 0.05 and 15.0
    tick_vals = [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0]
    tick_vals = [v for v in tick_vals if lo <= v <= hi]
    for tv in tick_vals:
        tx = _log_scale_x(tv, lo, hi, plot_w) + plot_l
        parts.append(f'  <line x1="{tx}" y1="{ax_y}" x2="{tx}" y2="{ax_y + 4}" '
                     f'stroke="#333" stroke-width="1" />')
        parts.append(f'  <text x="{tx}" y="{ax_y + 16}" font-size="10" fill="#666" '
                     f'text-anchor="middle">{tv}</text>')

    # Render each covariate row
    for i, cov in enumerate(data.covariates):
        y = margin_t + i * row_h + row_h // 2 + 10

        use_grey = cov.violated
        label_color = "#888" if use_grey else "#333"

        # Covariate label (left column)
        parts.append(f'  <text x="{margin_l}" y="{y + 4}" font-size="12" fill="{label_color}">'
                     f'{_svg_escape(cov.display_label)}</text>')

        if cov.unstable:
            parts.append(f'  <text x="{plot_l + 10}" y="{y + 4}" font-size="11" '
                         f'fill="#E74C3C" font-style="italic">'
                         f'Did not converge / unstable estimate</text>')
            continue

        color = "#888" if cov.ci_crosses_one else "#1F4E78"

        # CI line
        lx = _log_scale_x(cov.ci_lower, lo, hi, plot_w) + plot_l
        ux = _log_scale_x(cov.ci_upper, lo, hi, plot_w) + plot_l
        parts.append(f'  <line x1="{lx}" y1="{y}" x2="{ux}" y2="{y}" '
                     f'stroke="{color}" stroke-width="2.5" stroke-linecap="round" />')

        # CI whiskers
        whisk_h = 5
        parts.append(f'  <line x1="{lx}" y1="{y - whisk_h}" x2="{lx}" y2="{y + whisk_h}" '
                     f'stroke="{color}" stroke-width="1.5" />')
        parts.append(f'  <line x1="{ux}" y1="{y - whisk_h}" x2="{ux}" y2="{y + whisk_h}" '
                     f'stroke="{color}" stroke-width="1.5" />')

        # Point estimate marker
        hx = _log_scale_x(cov.hr, lo, hi, plot_w) + plot_l
        if use_grey:
            # Hollow diamond for violated covariates
            d = 6  # half-size
            parts.append(f'  <polygon points="{hx},{y - d} {hx + d},{y} {hx},{y + d} {hx - d},{y}" '
                         f'fill="white" stroke="{color}" stroke-width="2" />')
        else:
            marker_size = 5
            parts.append(f'  <rect x="{hx - marker_size}" y="{y - marker_size}" '
                         f'width="{marker_size * 2}" height="{marker_size * 2}" '
                         f'fill="{color}" />')

        # Text: HR [CI], p-value — 3dp to avoid hiding CI crossing 1.0 (§9 fix)
        hr_str = f"{cov.hr:.3f}"
        ci_str = f"[{cov.ci_lower:.3f}, {cov.ci_upper:.3f}]"
        p_str = f"p = {cov.wald_p:.3f}"
        txt_color = "#888" if use_grey else "#333"
        parts.append(f'  <text x="{txt_x}" y="{y + 4}" font-size="11" fill="{txt_color}">'
                     f'{hr_str} {ci_str}</text>')
        parts.append(f'  <text x="{txt_x + p_col_offset}" y="{y + 4}" font-size="11" '
                     f'fill="{txt_color}">{p_str}</text>')

    # Footer: model summary
    footer_y = margin_t + plot_h + 40
    footer_lines = 0

    summary_parts = []
    if data.concordance_index is not None:
        summary_parts.append(f"C-index={data.concordance_index:.3f}")
    if data.lr_test_p is not None:
        summary_parts.append(f"LR test p={data.lr_test_p:.4f}")
    summary_parts.append(f"N={data.n_analyzed}")
    summary_parts.append(f"Events={data.n_events}")
    summary_str = " | ".join(summary_parts)
    parts.append(f'  <text x="{margin_l}" y="{footer_y}" font-size="11" fill="#555">'
                 f'{_svg_escape(summary_str)}</text>')
    footer_lines += 1

    # aHR footnote
    parts.append(f'  <text x="{margin_l}" y="{footer_y + 18}" font-size="10" fill="#555" '
                 f'font-style="italic">aHR = adjusted Hazard Ratio (multivariable Cox model)</text>')
    footer_lines += 1

    # EPV caveat
    epv_y = footer_y + 30
    if data.epv_warning and data.epv is not None:
        parts.append(f'  <text x="{margin_l}" y="{epv_y}" font-size="11" '
                     f'fill="#C0392B" font-weight="bold">'
                     f'⚠ Caution: EPV={data.epv:.1f}, below recommended threshold '
                     f'— estimates may be unstable.</text>')
        epv_y += 16
        footer_lines += 1

    # Post-unmask violation annotation + legend
    if data.violation_warning and data.violation_summary:
        parts.append(f'  <text x="{margin_l}" y="{epv_y}" font-size="11" '
                     f'fill="#C0392B" font-weight="bold">'
                     f'⚠ Assumption violation: {_svg_escape(data.violation_summary)}</text>')
        epv_y += 16
        footer_lines += 1
        has_marked = any(c.violated for c in data.covariates)
        if has_marked:
            parts.append(f'  <text x="{margin_l}" y="{epv_y}" font-size="10" '
                         f'fill="#888">'
                         f'◇ = assumption violation detected for this covariate</text>')
            epv_y += 16
            footer_lines += 1

    # Resize canvas if footer overflowed the original margin_b (§9 fix)
    # Use epv_y (last text y-position) + descender + padding instead of line count
    needed_footer_h = epv_y - (margin_t + plot_h) + 20
    if needed_footer_h > margin_b:
        svg_h = margin_t + plot_h + needed_footer_h
        svg_header = f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" ' \
                     f'font-family="Arial, Helvetica, sans-serif" font-size="13">'

    parts.insert(0, svg_header)
    parts.append("</svg>")
    Path(output_path).write_text("\n".join(parts))


# ── ASCII renderer ───────────────────────────────────────────────────────────

def render_ascii(data: ForestPlotData) -> str:
    lines: list[str] = []
    lines.append("Forest Plot — Cox PH Model")
    lines.append("")

    # Determine max covariate name width
    max_name_len = max(len(c.display_label) for c in data.covariates) if data.covariates else 10
    name_w = max(max_name_len + 2, 14)

    # Build scale
    all_vals = [1.0]
    for c in data.covariates:
        if not c.unstable and c.hr > 0 and c.ci_lower > 0 and c.ci_upper > 0:
            all_vals.extend([c.hr, c.ci_lower, c.ci_upper])
    lo = min(all_vals) / 1.3
    hi = max(all_vals) * 1.3
    if lo <= 0:
        lo = 0.1

    plot_w = 30
    hr_w = 7
    ci_w = 6

    def _pos(v: float) -> int:
        if v <= 0:
            return plot_w // 2
        return int((math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)) * plot_w)

    ref = _pos(1.0)

    # Header row above covariate table — exact same format as data rows
    header_name = "Covariate".ljust(name_w)
    header_plot = " " * plot_w
    header_hr = "aHR".rjust(hr_w)
    header_ci = "[95% CI]".ljust(1 + ci_w + 2 + ci_w + 1)
    header_p = "p-value".rjust(7)
    lines.append(f"{header_name}│{header_plot}  {header_hr} {header_ci} {header_p}")

    # Header
    scale_line = " " * name_w + "|"
    tick_labels = [0.1, 0.2, 0.5, 1, 2, 5, 10]
    tick_labels = [t for t in tick_labels if lo <= t <= hi]
    for tv in tick_labels:
        p = _pos(tv)
        if p < 0:
            continue
        scale_line += f"{'─' * max(0, p - len(scale_line) + name_w + 1)}┬"
    lines.append(scale_line)

    tick_text = " " * name_w + "│"
    for tv in tick_labels:
        p = _pos(tv)
        s = f"{tv}"
        pad = p - len(tick_text) + name_w + 1
        if pad > 0:
            tick_text += " " * pad + s
    lines.append(tick_text)

    # Separator covers name│ [plot]  HR  [CI, CI]  p-value
    _ci_field = 1 + ci_w + 2 + ci_w + 1   # [lower, upper]
    _data_w = 1 + plot_w + 2 + hr_w + 1 + _ci_field + 1 + 8  # │plot  HR CI  pv
    lines.append("─" * (name_w + _data_w))

    for cov in data.covariates:
        name = cov.display_label.ljust(name_w)

        if cov.unstable:
            lines.append(f"{name}│  ** Did not converge / unstable estimate **")
            continue

        p = _pos(cov.hr)
        lp = _pos(cov.ci_lower)
        up = _pos(cov.ci_upper)

        marker = "◇" if cov.ci_crosses_one else "◆"
        color_tag = " (n.s.)" if cov.ci_crosses_one else ""

        # Build plot line
        plot_buf = [" "] * plot_w
        for j in range(max(0, lp), min(plot_w, up + 1)):
            plot_buf[j] = "─"
        if 0 <= p < plot_w:
            plot_buf[p] = marker
        if 0 <= ref < plot_w:
            plot_buf[ref] = "│"

        plot_str = "".join(plot_buf)

        hr_w = 7
        # ci_w is already defined at function scope (line 361)
        hr_str = f"{cov.hr:>{hr_w}.2f}"
        ci_str = f"[{cov.ci_lower:>{ci_w}.2f}, {cov.ci_upper:>{ci_w}.2f}]"
        p_str = f"p={cov.wald_p:.3f}"
        lines.append(f"{name}│{plot_str}  {hr_str} {ci_str} {p_str}{color_tag}")

    lines.append("")

    # Summary
    summary_parts = []
    if data.concordance_index is not None:
        summary_parts.append(f"C-index={data.concordance_index:.3f}")
    if data.lr_test_p is not None:
        summary_parts.append(f"LR test p={data.lr_test_p:.4f}")
    summary_parts.append(f"N={data.n_analyzed}")
    summary_parts.append(f"Events={data.n_events}")
    lines.append(" | ".join(summary_parts))
    lines.append("aHR = adjusted Hazard Ratio (multivariable Cox model)")

    if data.epv_warning and data.epv is not None:
        lines.append(
            f"⚠ Caution: EPV={data.epv:.1f}, below recommended threshold "
            f"— estimates may be unstable."
        )

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────

def render_forest(study_id: str, output_path: str | None = None, ascii: bool = False) -> Path | str:
    data = load_forest_data(study_id)
    if ascii:
        return render_ascii(data)
    path = output_path or str(DATA_ROOT / study_id / "forest_plot.svg")
    # Re-read the result row to pass status_json + ph_diagnostics_json to violation check
    conn = get_connection(study_id)
    init_db(conn)
    row = conn.execute(
        """SELECT status_json, ph_diagnostics_json
           FROM analysis_results
           WHERE study_id=? AND test_name='cox_ph_model'
             AND id NOT IN (
               SELECT COALESCE(superseded_previous_result_id, -1)
               FROM analysis_results
               WHERE study_id=? AND test_name='cox_ph_model'
                 AND superseded_previous_result_id IS NOT NULL
             )
           ORDER BY id DESC LIMIT 1""",
        (study_id, study_id),
    ).fetchone()
    conn.close()
    if row:
        data = _populate_violation(data, dict(row))
    render_svg(data, path)
    return Path(path)


def generate_forest_plot_png(study_id: str, output_path: str | Path | None = None) -> Path:
    """Render a publication-ready PNG forest plot using matplotlib."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    data = load_forest_data(study_id)
    if output_path is None:
        output_path = DATA_ROOT / study_id / "forest_plot.png"
    else:
        output_path = Path(output_path)

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=150)
    fig.subplots_adjust(left=0.32, right=0.62, top=0.85, bottom=0.2)

    n = len(data.covariates)
    y_positions = np.arange(n, 0, -1)

    for i, cov in enumerate(data.covariates):
        y = y_positions[i]
        color = "#888888" if cov.ci_crosses_one else "#1F4E78"

        err_low = max(0, cov.hr - cov.ci_lower)
        err_high = max(0, cov.ci_upper - cov.hr)
        ax.errorbar(
            cov.hr, y, xerr=[[err_low], [err_high]],
            fmt="s", color=color, ecolor=color, elinewidth=2, capsize=4, capthick=1.5, markersize=6,
        )

        ax.text(-0.03, y, cov.display_label, transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=11)

        hr_str = f"{cov.hr:.2f} [{cov.ci_lower:.2f}, {cov.ci_upper:.2f}]"
        p_str = f"p = {cov.wald_p:.3f}" if cov.wald_p >= 0.001 else "p < 0.001"
        ax.text(1.03, y, hr_str, transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=11)
        ax.text(1.55, y, p_str, transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=11)

    ax.set_xscale("log")
    ax.axvline(1.0, color="#999999", linestyle="--", linewidth=1.5)
    ax.set_yticks([])
    ax.set_ylim(0.5, n + 0.8)
    ax.set_xlabel("Hazard Ratio (log scale)", fontsize=11)
    ax.set_title("Forest Plot — Cox Proportional Hazards Model", fontsize=13, fontweight="bold", color="#1F4E78", pad=15)

    ax.text(-0.03, n + 0.5, "Covariate", transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=11, fontweight="bold", color="#555555")
    ax.text(1.03, n + 0.5, "aHR [95% CI]", transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=11, fontweight="bold", color="#555555")
    ax.text(1.55, n + 0.5, "p-value", transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=11, fontweight="bold", color="#555555")

    plt.savefig(str(output_path), bbox_inches="tight")
    plt.close(fig)
    return output_path
