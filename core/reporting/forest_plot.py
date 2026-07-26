from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.database import DATA_ROOT, get_connection, init_db


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

    @property
    def ci_crosses_one(self) -> bool:
        return self.ci_lower <= 1 <= self.ci_upper

    @property
    def display_label(self) -> str:
        if self.reference_level is not None and self.tested_level is not None:
            return f"{self.covariate} ({self.tested_level} vs {self.reference_level})"
        return f"{self.covariate} (per 1-unit increase)"


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

    study_dir = DATA_ROOT / study_id
    plan_path = study_dir / "study_plan.v1.locked.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}

    warnings_text = json.dumps(plan.get("warnings", {}))
    epv, epv_warn = _extract_epv(warnings_text)

    result = conn.execute(
        """SELECT id, study_plan_version, concordance_index, lr_test_p,
                  sample_counts_json, status_json
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


def _svg_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    margin_l = 180
    margin_r = 30
    margin_t = 40
    margin_b = 80
    plot_l = margin_l + 20
    plot_w = 400
    row_h = 36
    label_w = plot_l - margin_l - 10
    text_col_w = 200

    svg_w = plot_l + plot_w + text_col_w + margin_r
    n = len(data.covariates)
    plot_h = max(n * row_h, 60)
    svg_h = margin_t + plot_h + margin_b

    # Determine log range: include all CIs and HR=1
    all_vals = [1.0]
    for c in data.covariates:
        if not c.unstable:
            all_vals.extend([c.hr, c.ci_lower, c.ci_upper])
    lo = min(all_vals) / 1.3
    hi = max(all_vals) * 1.3
    if lo <= 0:
        lo = 0.1

    parts: list[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" '
                 f'width="{svg_w}" height="{svg_h}" '
                 f'font-family="Arial, Helvetica, sans-serif" font-size="13">')
    parts.append(f'  <text x="{margin_l}" y="24" font-size="16" font-weight="bold" '
                 f'fill="#1F4E78">Forest Plot — Cox PH Model</text>')

    # Header row
    header_y = margin_t - 10
    parts.append(f'  <text x="{margin_l}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555">Covariate</text>')
    parts.append(f'  <text x="{plot_l + plot_w // 2}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555" text-anchor="middle">aHR [95% CI]</text>')
    parts.append(f'  <text x="{plot_l + plot_w + 10}" y="{header_y}" font-size="11" '
                 f'font-weight="bold" fill="#555">aHR [95% CI]</text>')
    parts.append(f'  <text x="{plot_l + plot_w + 10}" y="{header_y + 14}" font-size="11" '
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
    # Tick labels for x-axis (HR values)
    tick_vals = [0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
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

        # Covariate label (left column)
        parts.append(f'  <text x="{margin_l}" y="{y + 4}" font-size="12" fill="#333">'
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
        marker_size = 5
        parts.append(f'  <rect x="{hx - marker_size}" y="{y - marker_size}" '
                     f'width="{marker_size * 2}" height="{marker_size * 2}" '
                     f'fill="{color}" />')

        # Text: HR [CI], p-value
        hr_str = f"{cov.hr:.2f}"
        ci_str = f"[{cov.ci_lower:.2f}, {cov.ci_upper:.2f}]"
        p_str = f"p={cov.wald_p:.3f}"
        txt_x = plot_l + plot_w + 10
        parts.append(f'  <text x="{txt_x}" y="{y + 4}" font-size="11" fill="#333">'
                     f'{hr_str} {ci_str}</text>')
        parts.append(f'  <text x="{txt_x + 80}" y="{y + 4}" font-size="11" '
                     f'fill="#555">{p_str}</text>')

    # Footer: model summary
    footer_y = margin_t + plot_h + 40
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

    # aHR footnote
    parts.append(f'  <text x="{margin_l}" y="{footer_y + 18}" font-size="10" fill="#555" '
                 f'font-style="italic">aHR = adjusted Hazard Ratio (multivariable Cox model)</text>')

    # EPV caveat
    if data.epv_warning and data.epv is not None:
        parts.append(f'  <text x="{margin_l}" y="{footer_y + 32}" font-size="11" '
                     f'fill="#C0392B" font-weight="bold">'
                     f'⚠ Caution: EPV={data.epv:.1f}, below recommended threshold '
                     f'— estimates may be unstable.</text>')

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
    render_svg(data, path)
    return Path(path)
