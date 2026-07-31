"""Patient flow diagram (CONSORT/STROBE style) for study pipeline stages."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.database import DATA_ROOT, get_connection
from core.reporting import (
    format_label as _format_label,
    latest_locked_plan as _latest_locked_plan,
    svg_escape as _svg_escape,
)


@dataclass
class FlowStage:
    name: str
    total: int
    excluded: int
    remaining: int
    details: dict = field(default_factory=dict)
    note: str | None = None


@dataclass
class FlowchartData:
    study_id: str
    stages: list[FlowStage]
    study_name: str = ""
    study_type: str = ""
    arm_column: str | None = None
    arm_counts: dict[str, int] = field(default_factory=dict)
    arm_analyzed_counts: dict[str, int] = field(default_factory=dict)
    primary_analysis_label: str = ""


def _get_plan_arm_col(study_id: str) -> str | None:
    plan = _latest_locked_plan(study_id)
    cox_models = plan.get("cox_ph_models", [])
    if cox_models:
        return cox_models[0].get("primary_treatment_col")
    return None


def load_flowchart_data(study_id: str) -> FlowchartData:
    conn = get_connection(study_id)
    raw = f"raw_{study_id}"

    study_name = ""
    study_type = ""
    row = conn.execute("SELECT name, study_type FROM studies WHERE id=?", (study_id,)).fetchone()
    if row:
        study_name = row[0] or ""
        study_type = row[1] or ""

    stages: list[FlowStage] = []

    # Stage 1: Assessed for eligibility
    total_ingested = conn.execute(f'SELECT COUNT(*) FROM {raw}').fetchone()[0]
    stages.append(FlowStage(
        name="Assessed for eligibility",
        total=total_ingested,
        excluded=0,
        remaining=total_ingested,
    ))

    # Stage 2: Excluded at ingest (duplicate patient IDs)
    dupes = []
    try:
        from core.ingestion.csv_loader import find_duplicate_patient_ids
        dupes = find_duplicate_patient_ids(study_id)
    except Exception:
        dupes = []
    n_dupes = sum(cnt - 1 for _, cnt in dupes) if dupes else 0
    remaining_after_ingest = total_ingested - n_dupes
    stages.append(FlowStage(
        name="Excluded at ingest (duplicate patient IDs)",
        total=total_ingested,
        excluded=n_dupes,
        remaining=remaining_after_ingest,
        details={"duplicates": dupes} if dupes else {},
        note="Duplicates are retained in analysis; verify they are genuine repeat records" if dupes else None,
    ))

    # Stage 3: Eligible cohort
    stages.append(FlowStage(
        name="Eligible cohort",
        total=remaining_after_ingest,
        excluded=0,
        remaining=remaining_after_ingest,
        note="No row exclusions at classification step; purely a labeling step",
    ))

    # Stage 4: Allocated to each arm/group
    arm_col = _get_plan_arm_col(study_id)
    arm_counts = {}
    if arm_col:
        rows = conn.execute(f'SELECT "{arm_col}", COUNT(*) as cnt FROM {raw} GROUP BY "{arm_col}"').fetchall()
        for r in rows:
            val = r[0] if r[0] is not None else "missing"
            arm_counts[str(val)] = r[1]
    stages.append(FlowStage(
        name="Allocated to each arm/group",
        total=remaining_after_ingest,
        excluded=0,
        remaining=remaining_after_ingest,
        details={"arm_counts": arm_counts} if arm_counts else {},
    ))

    # Stage 5: Analyzed
    # Determine primary analysis label from plan
    primary_analysis_label = "Primary Endpoints"
    plan = _latest_locked_plan(study_id)
    if plan:
        try:
            cox_models = plan.get("cox_ph_models", [])
            if cox_models:
                mn = cox_models[0].get("model_name") or ""
                if mn and mn.lower() not in ("pfs_multivariable", "cox_ph_model", "pfs_model"):
                    primary_analysis_label = f"Primary Endpoints ({_format_label(mn)})"
                else:
                    primary_analysis_label = "Primary Endpoints"
            else:
                all_tests = plan.get("planned_tests", []) + plan.get("post_hoc_tests", [])
                if all_tests:
                    tn = all_tests[0].get("test_name", "")
                    if tn:
                        primary_analysis_label = f"Primary Endpoints ({_format_label(tn)})"
        except Exception:
            pass

    analyzed_n = remaining_after_ingest
    excluded_from_analysis = 0
    latest_result = conn.execute("""
        SELECT sample_counts_json
        FROM analysis_results
        WHERE study_id=? AND test_name='cox_ph_model'
          AND id NOT IN (
            SELECT COALESCE(superseded_previous_result_id, -1)
            FROM analysis_results
            WHERE study_id=? AND test_name='cox_ph_model'
              AND superseded_previous_result_id IS NOT NULL
          )
        ORDER BY id DESC LIMIT 1
    """, (study_id, study_id)).fetchone()

    if latest_result and latest_result[0]:
        sc = json.loads(latest_result[0])
        analyzed_n = sc.get("n_analyzed", remaining_after_ingest)
        excluded_from_analysis = sc.get("n_excluded", 0)

    stages.append(FlowStage(
        name="Analyzed (primary analysis: Cox PH model)",
        total=remaining_after_ingest,
        excluded=excluded_from_analysis,
        remaining=analyzed_n,
        details={"n_total": remaining_after_ingest, "n_analyzed": analyzed_n, "n_excluded": excluded_from_analysis},
    ))

    # Per-arm analyzed counts: rows where all Cox model columns are non-null
    arm_analyzed_counts: dict[str, int] = {}
    if arm_col and analyzed_n > 0:
        plan = _latest_locked_plan(study_id)
        cox_cols = [arm_col]
        try:
            m = plan.get("cox_ph_models", [{}])[0]
            if m.get("survival_time_col"):
                cox_cols.append(m["survival_time_col"])
            if m.get("event_col"):
                cox_cols.append(m["event_col"])
            for c in m.get("covariate_cols", []):
                cox_cols.append(c)
        except Exception:
            pass
        nonnull_conditions = " AND ".join(f'"{c}" IS NOT NULL' for c in set(cox_cols))
        rows = conn.execute(
            f'SELECT "{arm_col}", COUNT(*) as cnt FROM {raw} WHERE {nonnull_conditions} GROUP BY "{arm_col}"'
        ).fetchall()
        for r in rows:
            val = r[0] if r[0] is not None else "missing"
            arm_analyzed_counts[str(val)] = r[1]

    # Stage 6: Final analyzed set per arm
    final_arm_counts = {}
    if arm_col and analyzed_n > 0:
        rows = conn.execute(f'SELECT "{arm_col}", COUNT(*) as cnt FROM {raw} GROUP BY "{arm_col}"').fetchall()
        for r in rows:
            val = r[0] if r[0] is not None else "missing"
            final_arm_counts[str(val)] = r[1]

    stages.append(FlowStage(
        name="Final analyzed set per arm",
        total=analyzed_n,
        excluded=0,
        remaining=analyzed_n,
        details={"arm_counts": final_arm_counts} if final_arm_counts else {},
    ))

    conn.close()

    return FlowchartData(
        study_id=study_id,
        stages=stages,
        study_name=study_name,
        study_type=study_type,
        arm_column=arm_col,
        arm_counts=arm_counts,
        arm_analyzed_counts=arm_analyzed_counts,
        primary_analysis_label=primary_analysis_label,
    )


# ── SVG renderer ─────────────────────────────────────────────────────────────



def _draw_box(parts: list, x: int, y: int, w: int, h: int,
              stroke: str = "#1F4E78", fill: str = "#F8F9FA") -> None:
    parts.append(f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" '
                 f'rx="6" ry="6" fill="{fill}" stroke="{stroke}" stroke-width="2" />')


def _draw_text(parts: list, x: int, y: int, text: str, font_size: int = 13,
               fill: str = "#333", bold: bool = False,
               italic: bool = False, anchor: str | None = None) -> None:
    attrs = f'font-size="{font_size}" fill="{fill}"'
    if bold:
        attrs += ' font-weight="bold"'
    if italic:
        attrs += ' font-style="italic"'
    if anchor:
        attrs += f' text-anchor="{anchor}"'
    parts.append(f'  <text x="{x}" y="{y}" {attrs}>{_svg_escape(text)}</text>')


def _draw_arrow(parts: list, x1: int, y1: int, x2: int, y2: int) -> None:
    parts.append(f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="#1F4E78" stroke-width="2" marker-end="url(#arrowhead)" />')


def _draw_line(parts: list, x1: int, y1: int, x2: int, y2: int) -> None:
    parts.append(f'  <line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="#1F4E78" stroke-width="2" />')


def _draw_pill(parts: list, x: int, y: int, w: int, h: int, label: str) -> None:
    """Rounded pill-box for CONSORT phase labels."""
    ry = h // 2
    parts.append(f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{ry}" ry="{ry}" '
                 f'fill="#D6EAF8" stroke="#85C1E9" stroke-width="1" />')
    cx = x + w // 2
    cy = y + h // 2 + 4  # vertical center + font baseline offset
    parts.append(f'  <text x="{cx}" y="{cy}" font-size="9" fill="#1F4E78" font-weight="bold" '
                 f'text-anchor="middle">{label}</text>')


def render_svg(data: FlowchartData, output_path: str,
               show_title: bool = True, show_watermark: bool = False,
               verbose: bool = False,
               show_study_name: bool = False) -> None:
    box_h = 65
    main_w = 340
    arm_w = 240
    excl_w = 185
    excl_h = 42
    vgap = 30
    hgap = 25
    col_gap = 35
    pill_w = 125
    pill_h = 22
    pill_x = 8
    header_h = 80
    margin = 40

    stages = data.stages
    arms = list(data.arm_counts.keys())
    n_arms = len(arms)
    has_arms = n_arms > 0

    needs_final_row = False
    if has_arms:
        for a in arms:
            if data.arm_analyzed_counts.get(a, 0) != data.arm_counts.get(a, 0):
                needs_final_row = True
                break

    # Compute arm column positions first — these are the centering reference
    if has_arms:
        total_arm_w = n_arms * arm_w + (n_arms - 1) * col_gap
    else:
        total_arm_w = main_w

    # SVG width: arms centered, with room for pill labels + exclusion overflow
    svg_w = max(total_arm_w + 2 * (pill_x + pill_w + 15), margin + main_w + hgap + excl_w + margin, 700)

    # Center arms in svg_w
    arm_start_x = (svg_w - total_arm_w) // 2
    arm_xs = [arm_start_x + i * (arm_w + col_gap) for i in range(n_arms)]
    arm_cx = [x + arm_w // 2 for x in arm_xs]

    # Enrollment centered on arm midpoint, exclusion overflows right
    if has_arms:
        main_cx = (arm_cx[0] + arm_cx[-1]) // 2
    else:
        main_cx = svg_w // 2
    main_x = main_cx - main_w // 2
    excl_x = main_x + main_w + hgap

    # Expand SVG if exclusion box overflows
    need_w = excl_x + excl_w + margin
    if need_w > svg_w:
        svg_w = need_w

    # Y positions
    y0 = margin + header_h                    # Enrollment
    y3 = y0 + box_h + vgap                    # Arm row
    y4 = y3 + box_h + vgap                    # Analyzed
    y5 = y4 + box_h + vgap                    # Final (if needed)
    excl_center_y = y0 + box_h // 2
    svg_h = (y5 + box_h + 40) if (has_arms and needs_final_row) else (y4 + box_h + 40)

    # Phase label Y — center of each phase row
    pill_enroll_y = y0 + box_h // 2 - pill_h // 2
    pill_alloc_y = y3 + box_h // 2 - pill_h // 2
    pill_analy_y = y4 + box_h // 2 - pill_h // 2

    parts: list[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" '
                 f'font-family="Arial, Helvetica, sans-serif" font-size="13">')

    # ── Title ──────────────────────────────────────────────────────────
    # CONSORT for RCTs, STROBE for observational studies
    if data.study_type == "rct":
        flow_label = "CONSORT Participant Flow Diagram"
    else:
        flow_label = "STROBE Participant Flow Diagram"
    if show_title:
        _draw_text(parts, main_cx, margin + 22, flow_label,
                   18, "#1F4E78", bold=True, anchor="middle")
        if show_study_name and data.study_name:
            _draw_text(parts, main_cx, margin + 40, data.study_name,
                       12, "#777", italic=True, anchor="middle")

    # Phase pills (left margin) — CONSORT for RCTs, STROBE for observational
    if data.study_type == "rct":
        _draw_pill(parts, pill_x, pill_enroll_y, pill_w, pill_h, "ENROLLMENT")
        _draw_pill(parts, pill_x, pill_alloc_y, pill_w, pill_h, "ALLOCATION")
        _draw_pill(parts, pill_x, pill_analy_y, pill_w, pill_h, "ANALYSIS")
    else:
        _draw_pill(parts, pill_x, pill_enroll_y, pill_w, pill_h, "ELIGIBILITY")
        _draw_pill(parts, pill_x, pill_alloc_y, pill_w, pill_h, "COHORT ASSIGNMENT")
        _draw_pill(parts, pill_x, pill_analy_y, pill_w, pill_h, "ANALYSIS")

    # ═══════════════════════════════════════════════════════════════════
    # Stage 0: Enrollment (assessed + eligible merged — no exclusion here)
    # ═══════════════════════════════════════════════════════════════════
    s0 = stages[0]
    _draw_box(parts, main_x, y0, main_w, box_h)
    _draw_text(parts, main_cx, y0 + 22, "Assessed for eligibility", 14, "#1F4E78", bold=True, anchor="middle")
    _draw_text(parts, main_cx, y0 + 42, f"n = {s0.remaining}", 13, "#333", anchor="middle")

    # Exclusion side box (stage 1) — branches off enrollment
    s1 = stages[1]
    _draw_box(parts, excl_x, y0, excl_w, excl_h, "#bbb", "#fafafa")
    _draw_text(parts, excl_x + 8, y0 + 16, f"Excluded (n = {s1.excluded})", 11, "#1F4E78", bold=True)
    _draw_text(parts, excl_x + 8, y0 + 33, f"• Duplicate patient ID (n = {s1.excluded})", 10, "#333")

    _draw_arrow(parts, main_x + main_w, excl_center_y, excl_x, excl_center_y)

    # Enrollment → arm split (straight vertical arrows)
    if has_arms and n_arms > 1:
        arm_split_y = y0 + box_h + vgap // 2
        _draw_line(parts, main_cx, y0 + box_h, main_cx, arm_split_y)
        _draw_line(parts, arm_cx[0], arm_split_y, arm_cx[-1], arm_split_y)
        for cx in arm_cx:
            _draw_arrow(parts, cx, arm_split_y, cx, y3)
    else:
        _draw_arrow(parts, main_cx, y0 + box_h, main_cx, y3)

    # ═══════════════════════════════════════════════════════════════════
    # Arm columns
    # ═══════════════════════════════════════════════════════════════════
    if has_arms:
        for i, arm_name in enumerate(arms):
            ax = arm_xs[i]
            cx = arm_cx[i]
            arm_cnt = data.arm_counts.get(arm_name, 0)
            arm_analyzed = data.arm_analyzed_counts.get(arm_name, 0)
            analy_label = data.primary_analysis_label

            # Arm label: raw value from data, no synthetic "Arm " prefix
            _draw_box(parts, ax, y3, arm_w, box_h)
            cx_box = ax + arm_w // 2
            _draw_text(parts, cx_box, y3 + 28, f"{arm_name} (n = {arm_cnt})", 14, "#1F4E78",
                       bold=True, anchor="middle")

            # Arrow: arm label → analyzed
            _draw_arrow(parts, cx, y3 + box_h, cx, y4)

            when_analyzed_eq_allocated = (arm_analyzed == arm_cnt)

            if when_analyzed_eq_allocated:
                # Two-line: bold "Analyzed", subtext "PFS Multivariable (n = 10)"
                _draw_box(parts, ax, y4, arm_w, box_h)
                _draw_text(parts, cx_box, y4 + 22, "Analyzed", 14, "#1F4E78", bold=True, anchor="middle")
                _draw_text(parts, cx_box, y4 + 42, f"{analy_label} (n = {arm_analyzed})",
                           12, "#555", anchor="middle")
            else:
                # Two distinct boxes: analyzed + final
                _draw_box(parts, ax, y4, arm_w, box_h)
                _draw_text(parts, cx_box, y4 + 22, "Analyzed", 14, "#1F4E78", bold=True, anchor="middle")
                _draw_text(parts, cx_box, y4 + 42, f"{analy_label} (n = {arm_analyzed})",
                           12, "#555", anchor="middle")

                _draw_arrow(parts, cx, y4 + box_h, cx, y5)

                _draw_box(parts, ax, y5, arm_w, box_h)
                _draw_text(parts, cx_box, y5 + 22, "Final analyzed set", 14, "#1F4E78",
                           bold=True, anchor="middle")
                _draw_text(parts, cx_box, y5 + 42, f"n = {arm_cnt}",
                           12, "#555", anchor="middle")

    else:
        # No arm data — single column
        _draw_arrow(parts, main_cx, y0 + box_h, main_cx, y3)
        _draw_box(parts, main_x, y3, main_w, box_h)
        _draw_text(parts, main_x + 15, y3 + 22, stages[4].name, 14, "#1F4E78", bold=True)
        _draw_text(parts, main_x + 15, y3 + 42, f"N = {stages[4].remaining}")

        _draw_arrow(parts, main_cx, y3 + box_h, main_cx, y4)
        _draw_box(parts, main_x, y4, main_w, box_h)
        _draw_text(parts, main_x + 15, y4 + 22, stages[5].name, 14, "#1F4E78", bold=True)
        _draw_text(parts, main_x + 15, y4 + 42, f"N = {stages[5].remaining}")

    # ── Watermark (opt-in) ───────────────────────────────────────────────
    if show_watermark:
        wm_y = (y5 + box_h + 25) if has_arms and needs_final_row else (y4 + box_h + 25)
        _draw_text(parts, margin, wm_y, "Generated by research-tool flowchart command", 11, "#aaa")

    # ── Arrowhead marker ─────────────────────────────────────────────────
    parts.append('  <defs>')
    parts.append('    <marker id="arrowhead" markerWidth="10" markerHeight="7" '
                 'refX="9" refY="3.5" orient="auto" markerUnits="strokeWidth">')
    parts.append('      <path d="M0,0 L10,3.5 L0,7 Z" fill="#1F4E78" />')
    parts.append('    </marker>')
    parts.append('  </defs>')
    parts.append("</svg>")

    Path(output_path).write_text("\n".join(parts))


# ── ASCII renderer (unchanged - for terminal use) ───────────────────────────

def render_ascii(data: FlowchartData) -> str:
    lines = []
    box_w = 72
    connector = "│"

    title = f"Patient Flow Diagram — {data.study_name or data.study_id}"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    for i, stage in enumerate(data.stages):
        lines.append(f"┌{'─' * box_w}┐")
        name = stage.name[:box_w - 2]
        lines.append(f"│ {name.ljust(box_w - 2)} │")

        if stage.excluded > 0:
            nums = f"N = {stage.total}  |  Excluded = {stage.excluded}  |  Remaining = {stage.remaining}"
        else:
            nums = f"N = {stage.remaining}"
        lines.append(f"│ {nums.ljust(box_w - 2)} │")

        if stage.details.get("arm_counts"):
            arms = stage.details["arm_counts"]
            arm_str = " | ".join(f"{k}: {v}" for k, v in arms.items())
            if len(arm_str) > box_w - 4:
                arm_str = arm_str[:box_w - 7] + "..."
            lines.append(f"│ {arm_str.ljust(box_w - 2)} │")
        elif stage.note:
            note = stage.note[:box_w - 4]
            lines.append(f"│ {note.ljust(box_w - 2)} │")
        else:
            lines.append(f"│{' ' * box_w}│")

        lines.append(f"└{'─' * box_w}┘")

        if i < len(data.stages) - 1:
            lines.append(f"       {connector}")
            lines.append(f"       {connector}")
            lines.append("")

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────

def render_flowchart(study_id: str, output_path: str | None = None,
                     ascii: bool = False,
                     show_title: bool = True, show_watermark: bool = False,
                     verbose: bool = False,
                     show_study_name: bool = False) -> Path | str:
    data = load_flowchart_data(study_id)
    if ascii:
        return render_ascii(data)
    path = output_path or str(DATA_ROOT / study_id / "flowchart.svg")
    render_svg(data, path, show_title=show_title, show_watermark=show_watermark,
               verbose=verbose, show_study_name=show_study_name)
    return Path(path)