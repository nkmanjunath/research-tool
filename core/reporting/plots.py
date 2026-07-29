"""Publication-quality survival curve plots.

Visualizes already-computed Kaplan-Meier analysis results as vector SVG/PDF.
Never computes or alters any statistic — only reuses the same data and
grouping logic the stats engine already used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Optional

import pandas as pd

from core.database import get_connection, DATA_ROOT

DAYS_PER_MONTH = 30.44


@dataclass
class _KMConfig:
    """Style configuration for a KM plot. Fields control visual rendering only."""
    linewidth: float = 2.0
    ci_alpha: float = 0.25
    show_median_lines: bool = True
    median_linewidth: float = 1.2
    median_alpha: float = 0.4
    median_contrast_box: bool = True
    stats_use_box: bool = True
    stats_fontsize: int = 11
    stats_y: float = 0.30
    legend_fontsize: int = 10
    grid_alpha: float = 0.3
    risk_table_bold_header: bool = True
    risk_table_fontsize: int = 9
    risk_ytick_fontsize: int = 10
    risk_ytick_pad: int = 20
    title_fontsize: int = 14


# ── Built-in presets ──────────────────────────────────────────────────

_STYLES: dict[str, _KMConfig] = {
    "clean": _KMConfig(
        linewidth=1.5,
        ci_alpha=0.12,
        show_median_lines=False,
        median_linewidth=0.8,
        median_alpha=0.25,
        median_contrast_box=False,
        stats_use_box=False,
        stats_fontsize=9,
        stats_y=0.90,
        legend_fontsize=9,
        grid_alpha=0.2,
        risk_table_bold_header=True,
        risk_table_fontsize=8,
        risk_ytick_fontsize=9,
        risk_ytick_pad=18,
        title_fontsize=13,
    ),
    "scientific": _KMConfig(
        linewidth=1.2,
        ci_alpha=0.12,
        show_median_lines=True,
        median_linewidth=0.8,
        median_alpha=0.25,
        median_contrast_box=True,
        stats_use_box=True,
        stats_fontsize=9,
        stats_y=0.30,
        legend_fontsize=8,
        grid_alpha=0.2,
        risk_table_bold_header=True,
        risk_table_fontsize=8,
        risk_ytick_fontsize=9,
        risk_ytick_pad=18,
        title_fontsize=12,
    ),
    "presentation": _KMConfig(
        linewidth=2.2,
        ci_alpha=0.30,
        show_median_lines=True,
        median_linewidth=1.5,
        median_alpha=0.5,
        median_contrast_box=True,
        stats_use_box=True,
        stats_fontsize=11,
        stats_y=0.30,
        legend_fontsize=10,
        grid_alpha=0.3,
        risk_table_bold_header=True,
        risk_table_fontsize=9,
        risk_ytick_fontsize=10,
        risk_ytick_pad=20,
        title_fontsize=14,
    ),
}


def _lookup_cox_hr(study_id: str, group_col: str) -> Optional[dict]:
    """Check whether a completed cox_proportional_hazards result exists.

    Returns a dict with 'hr', 'ci_lower', 'ci_upper', 'p_value' if found,
    else None.  No recomputation — only reads already-stored records.
    """
    conn = get_connection(study_id)
    # Use json_extract to reliably check the status field
    cur = conn.execute(
        """SELECT statistic, ci_lower, ci_upper, p_value
           FROM analysis_results
           WHERE study_id=? AND test_name='cox_proportional_hazards'
             AND json_extract(status_json, '$.status') = 'completed'
           ORDER BY id DESC LIMIT 1""",
        (study_id,),
    )
    row = cur.fetchone()
    conn.close()
    if row and row["statistic"] is not None:
        return {
            "hr": row["statistic"],
            "ci_lower": row["ci_lower"],
            "ci_upper": row["ci_upper"],
            "p_value": row["p_value"],
        }
    return None


def _resolve_km_vars(study_id: str, test_id: int) -> dict:
    """Resolve the data columns and grouping for a KM test.

    Returns a dict with keys:
      ``time_col``, ``event_col``, ``group_col``, ``p_value``, ``statistic``,
      ``groups``, ``n0``, ``n1``, ``time_unit`` (str)
    or raises ValueError/FileNotFoundError.
    """
    conn = get_connection(study_id)

    cur = conn.execute(
        "SELECT * FROM analysis_results WHERE id=? AND study_id=?",
        (test_id, study_id),
    )
    result = cur.fetchone()
    if not result:
        conn.close()
        raise ValueError(f"Test ID {test_id} not found in study '{study_id}'.")

    if result["test_name"] != "kaplan_meier_logrank":
        conn.close()
        raise ValueError(
            f"Cannot plot test ID {test_id}: "
            f"test is '{result['test_name']}', not 'kaplan_meier_logrank'."
        )

    status_data = json.loads(result["status_json"]) if result["status_json"] else {}
    if status_data.get("status") != "completed":
        reason = status_data.get("reason", "unknown reason")
        conn.close()
        raise ValueError(
            f"Cannot plot test ID {test_id}: "
            f"test did not complete successfully ({reason})."
        )

    p_value = result["p_value"]
    statistic = result["statistic"]

    locked_plans = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    if not locked_plans:
        conn.close()
        raise FileNotFoundError(f"No locked plan found for study '{study_id}'.")

    plan_data = json.loads(locked_plans[-1].read_text())
    all_tests = plan_data.get("planned_tests", []) + plan_data.get("post_hoc_tests", [])
    planned_test = next(
        (t for t in all_tests
         if t.get("test_name") == "kaplan_meier_logrank"),
        None,
    )
    if not planned_test:
        conn.close()
        raise ValueError("No planned Kaplan-Meier test found in the locked plan.")

    var_name = planned_test.get("variable_name", "")

    cur = conn.execute(
        "SELECT column_name, data_type FROM variables WHERE study_id=? AND column_name=?",
        (study_id, var_name),
    )
    var_row = cur.fetchone()
    time_unit = "days"
    if var_row:
        dtype = var_row["data_type"] or ""
        if "_months" in var_name.lower() or "months" in dtype.lower():
            time_unit = "months"

    prefix = var_name.replace("_days", "").replace("_months", "").replace("_time", "")
    event_col = f"{prefix}_event"
    group_col = "treatment_arm"

    raw_table = f"raw_{study_id}"
    df = pd.read_sql_query(f"SELECT * FROM {raw_table}", conn)
    conn.close()

    for col in [var_name, event_col, group_col]:
        if col not in df.columns:
            raise ValueError(
                f"Required column '{col}' not found in raw data. "
                f"The data may need to be re-ingested or the study re-unmasked."
            )

    df[var_name] = pd.to_numeric(df[var_name], errors="coerce")
    df[event_col] = pd.to_numeric(df[event_col], errors="coerce")

    groups = df[group_col].unique()
    if len(groups) != 2:
        raise ValueError(f"Log-rank requires exactly 2 groups, found {len(groups)}.")

    g0 = df[df[group_col] == groups[0]].dropna(subset=[var_name, event_col])
    g1 = df[df[group_col] == groups[1]].dropna(subset=[var_name, event_col])

    return {
        "time_col": var_name,
        "event_col": event_col,
        "group_col": group_col,
        "p_value": p_value,
        "statistic": statistic,
        "groups": [str(groups[0]), str(groups[1])],
        "n0": len(g0),
        "n1": len(g1),
        "time_unit": time_unit,
        "df": df,
    }


def _compute_at_risk(fitters, visible_ticks):
    """Compute number-at-risk counts at each tick from each fitter's event table."""
    data = []
    for f in fitters:
        et = f.event_table
        row = []
        for tick in visible_ticks:
            subset = et.loc[:tick]
            if not subset.empty:
                last = subset.iloc[-1]
                at_risk = int(last["at_risk"] - last["removed"])
                row.append(max(at_risk, 0))
            else:
                row.append(0)
        data.append(row)
    return data


def _build_risk_axes(ax_risk, groups, at_risk_data, visible_ticks,
                     bold_header=True, fontsize=9, ytick_fontsize=10,
                     ytick_pad=20):
    """Render the dual-axes risk table on a dedicated subplot."""
    import matplotlib.ticker as ticker

    # Hide all spines
    for spine in ax_risk.spines.values():
        spine.set_visible(False)

    # Set y-ticks to group labels — invert y-axis so group[0] appears at TOP
    # matching legend order (legend lists first entry at top)
    ax_risk.set_yticks(range(len(groups)))
    ax_risk.set_yticklabels(groups, fontweight="bold", fontsize=ytick_fontsize)
    ax_risk.invert_yaxis()

    # Color-code row labels to match curve colors (publication standard)
    colors = ["#1f77b4", "#ff7f0e"]
    for i, label in enumerate(ax_risk.get_yticklabels()):
        if i < len(colors):
            label.set_color(colors[i])

    # No x-axis ticks or labels (they live on ax_km between the two subplots)
    ax_risk.set_xticks([])
    ax_risk.tick_params(axis="x", length=0)

    # Remove y-tick marks (keep labels) and shift labels left of counts
    ax_risk.tick_params(axis="y", length=0, pad=ytick_pad)

    # Visual separator line at the top of the risk table (boundary with KM plot)
    # After invert_yaxis, "top" in data coords is y=0, so line = -0.5; draw just above it
    ax_risk.axhline(
        y=-0.5, color="gray", linewidth=0.5, alpha=0.3,
    )

    # Set y-lim to center groups (inverted: from -0.5 at top to n-0.5 at bottom)
    ax_risk.set_ylim(len(groups) - 0.5, -0.5)

    # Place the at-risk count text at each (tick, group) intersection
    # gi indexes groups in original order; after invert_yaxis, gi=0 plots at top
    for gi, row in enumerate(at_risk_data):
        for ti, tick in enumerate(visible_ticks):
            ax_risk.text(
                tick, gi, str(row[ti]),
                ha="center", va="center",
                fontsize=fontsize, fontfamily="monospace",
            )

    # Section header anchored above the rows
    fw = "bold" if bold_header else "normal"
    ax_risk.text(
        -0.08, 1.2, "Number at risk",
        transform=ax_risk.transAxes,
        fontweight=fw, fontsize=fontsize + 1,
        va="bottom",
    )


def generate_km_plot(
    study_id: str,
    test_id: int,
    output_path: Optional[str | Path] = None,
    fmt: str = "svg",
    show_risk_table: bool = True,
    show_medians: Optional[bool] = None,
    time_unit_display: str = "months",
    style: str = "clean",
) -> Path:
    """Generate a Kaplan-Meier survival curve plot for a completed KM test.

    Parameters
    ----------
    study_id : str
    test_id : int
        ID of the ``analysis_results`` row for the KM test.
    output_path : str or Path, optional
        Full path for the output file.  Defaults to
        ``data/studies/{study_id}/km_plot_{test_id}.{fmt}``.
    fmt : str
        ``"svg"`` (default) or ``"pdf"``.
    show_risk_table : bool
        Show the number-at-risk table subplot below the KM curves (default True).
    show_medians : bool, optional
        Override the preset's median-line setting.  ``None`` (default) uses the
        preset's default; set to True/False to force on/off regardless of preset.
    time_unit_display : str
        Axis label unit — ``"months"`` (default) or ``"days"``.
    style : str
        One of ``"clean"`` (default), ``"scientific"``, ``"presentation"``.

    Returns
    -------
    Path to the generated plot file.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from lifelines import KaplanMeierFitter

    cfg = _STYLES.get(style, _STYLES["clean"])

    # `show_medians` CLI flag overrides the preset's median setting
    median_enabled = cfg.show_median_lines if show_medians is None else show_medians

    vars_ = _resolve_km_vars(study_id, test_id)
    df = vars_["df"]
    time_col = vars_["time_col"]
    event_col = vars_["event_col"]
    group_col = vars_["group_col"]
    groups = vars_["groups"]
    p_value = vars_["p_value"]
    statistic = vars_["statistic"]

    display_unit = time_unit_display if time_unit_display in ("days", "months") else "months"
    time_scale = DAYS_PER_MONTH if display_unit == "months" else 1.0

    if output_path is None:
        output_path = DATA_ROOT / study_id / f"km_plot_{test_id}.{fmt}"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ext = output_path.suffix.lstrip(".")
        if ext in ("svg", "pdf", "png", "jpg", "jpeg", "tiff"):
            fmt = ext

    colors = ["#1f77b4", "#ff7f0e"]
    fitters = []
    max_observed_time = 0.0

    # Two subplots: KM curves on top, risk table below.  Manually sync
    # x-limits rather than using sharex=True so the tick labels and
    # "Time (months)" can live on the KM axes (between the two subplots)
    # while the risk table has only its rows, no redundant tick numbers.
    if show_risk_table:
        fig, (ax_km, ax_risk) = plt.subplots(
            2, 1, figsize=(8, 6),
            gridspec_kw={"height_ratios": [4, 1]},
        )
    else:
        fig, ax_km = plt.subplots(figsize=(8, 5))
        ax_risk = None

    # CI alpha: use preset value directly. For very small N (N<30 per arm),
    # CI bands will be wide; the preset's alpha (0.12-0.30) keeps them
    # visually subordinate to the step function. (§9: investigated N threshold;
    # scaling was tested but collapsed preset differentiation. The issue is
    # CI width, not alpha — preset alphas are already appropriate.)
    effective_ci_alpha = cfg.ci_alpha

    for i, grp in enumerate(groups):
        grp_df = df[df[group_col] == grp].dropna(subset=[time_col, event_col]).copy()
        grp_df["_display_time"] = grp_df[time_col] / time_scale
        kmf = KaplanMeierFitter()
        kmf.fit(
            grp_df["_display_time"],
            event_observed=grp_df[event_col],
            label=f"{grp} (n={len(grp_df)})",
        )
        kmf.plot_survival_function(
            ax=ax_km,
            color=colors[i],
            ci_show=True,
            ci_alpha=effective_ci_alpha,
            show_censors=True,
            linewidth=cfg.linewidth,
        )
        fitters.append(kmf)

        grp_max = grp_df["_display_time"].max()
        if grp_max is not None and not (isinstance(grp_max, float) and np.isnan(grp_max)):
            max_observed_time = max(max_observed_time, float(grp_max))

    # ── Labels ────────────────────────────────────────────────────────────
    ax_km.set_xlabel(f"Time ({display_unit})", fontsize=12)
    ax_km.set_ylabel("Survival Probability", fontsize=12)
    ax_km.set_title("Kaplan-Meier Survival Curve", fontsize=cfg.title_fontsize)

    # X-axis ticks and label on the KM axes (between plot and risk table)
    ax_km.tick_params(axis="x", labelsize=9)
    if ax_risk is not None:
        ax_risk.set_xlabel("")
        ax_risk.tick_params(axis="x", labelbottom=False)

    # ── Stats annotation ─────────────────────────────────────────────────
    cox_data = _lookup_cox_hr(study_id, group_col)
    if cox_data is not None:
        hr_line = (
            f"HR {cox_data['hr']:.2f} "
            f"(95% CI {cox_data['ci_lower']:.2f}-{cox_data['ci_upper']:.2f})"
        )
        p_line = f"P = {cox_data['p_value']:.3f}"
        annotation = f"{hr_line}\n{p_line}"
    else:
        p_line = f"P = {p_value:.4f}" if p_value is not None else "P = N/A"
        annotation = f"Log-rank {p_line}"

    bbox_kw = dict(
        boxstyle="round,pad=0.4",
        facecolor="white",
        edgecolor="gray",
        alpha=0.8,
    ) if cfg.stats_use_box else {}

    ax_km.text(
        0.95, cfg.stats_y, annotation,
        transform=ax_km.transAxes,
        fontsize=cfg.stats_fontsize,
        verticalalignment="bottom",
        horizontalalignment="right",
        bbox=bbox_kw if bbox_kw else None,
    )

    # ── Censoring legend ─────────────────────────────────────────────────
    ax_km.plot([], [], "|", color="gray", alpha=0.6, label="+ Censored observation")
    ax_km.legend(fontsize=cfg.legend_fontsize)

    # ── Median survival reference lines ───────────────────────────────────
    if median_enabled:
        ax_km.axhline(y=0.5, color="gray", linestyle="--", alpha=0.3, linewidth=1.0)

        median_notes = []
        for i, (grp, kmf) in enumerate(zip(groups, fitters)):
            median = getattr(kmf, "median_survival_time_", None)
            if median is not None and not (isinstance(median, float) and np.isinf(median)):
                median_val = float(median)
                ax_km.axvline(
                    x=median_val, color=colors[i], linestyle="--",
                    alpha=cfg.median_alpha, linewidth=cfg.median_linewidth,
                    ymax=0.472,
                )
                text_kw = dict(
                    color=colors[i], fontsize=9,
                    ha="center", va="bottom",
                )
                if cfg.median_contrast_box:
                    text_kw["bbox"] = dict(
                        boxstyle="round,pad=0.2",
                        facecolor="white",
                        edgecolor="none",
                        alpha=0.85,
                    )
                ax_km.text(
                    median_val, 0.52, f"{median_val:.0f} {display_unit}",
                    **text_kw,
                )
            else:
                median_notes.append(str(grp))

        if median_notes:
            note_text = "Median not reached: " + ", ".join(median_notes)
            ax_km.text(
                0.95, 0.18, note_text,
                transform=ax_km.transAxes, fontsize=8,
                ha="right", va="top",
                color="gray", fontstyle="italic",
            )

    # ── X-axis framing ───────────────────────────────────────────────────
    if max_observed_time > 0:
        # Add 5% right padding so curves don't collide with right border
        right_pad = max_observed_time * 0.05
        ax_km.set_xlim(0, max_observed_time + right_pad)
    ax_km.set_ylim(0, 1.05)
    ax_km.grid(True, alpha=cfg.grid_alpha)

    # Sync risk table x-axis to match KM axes (no sharex=True anymore)
    if ax_risk is not None:
        ax_risk.set_xlim(ax_km.get_xlim())

    # Visual separator between KM curve and risk table
    ax_km.axhline(y=0, color="gray", linewidth=0.5, alpha=0.4)

    # ── Number-at-risk table ─────────────────────────────────────────────
    if ax_risk is not None:
        xticks = ax_km.get_xticks()
        xmin, xmax = ax_km.get_xlim()
        visible_ticks = sorted(t for t in xticks if xmin <= t <= xmax and t >= 0)
        if len(visible_ticks) < 2:
            visible_ticks = [0, max_observed_time]

        at_risk_data = _compute_at_risk(fitters, visible_ticks)
        _build_risk_axes(
            ax_risk, groups, at_risk_data, visible_ticks,
            bold_header=cfg.risk_table_bold_header,
            fontsize=cfg.risk_table_fontsize,
            ytick_fontsize=cfg.risk_ytick_fontsize,
            ytick_pad=cfg.risk_ytick_pad,
        )

    plt.tight_layout()
    # Sync risk table position to match KM axes (§9 fix for alignment drift)
    if ax_risk is not None:
        km_pos = ax_km.get_position()
        risk_pos = ax_risk.get_position()
        ax_risk.set_position([km_pos.x0, risk_pos.y0, km_pos.width, risk_pos.height])
    fig.savefig(str(output_path), format=fmt, bbox_inches="tight")
    plt.close(fig)

    return output_path
