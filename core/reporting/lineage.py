"""Provenance lineage: assemble and render a study's lifecycle as a DAG.

Every timestamp and hash is read from stored data (locked plan files,
SQLite DB, bundle manifests).  Nothing is recomputed or guessed.
"""

from __future__ import annotations

import glob
import json
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.database import get_connection, DATA_ROOT
from core.reporting import svg_escape as _svg_escape


# ── Event model ──────────────────────────────────────────────────────────────

@dataclass
class LineageEvent:
    event_type: str
    timestamp: str
    label: str
    detail: dict = field(default_factory=dict)
    depth: int = 0
    # SVG branch coloring
    branch: str = "main"  # "main", "pre_unmask_amend", "post_hoc_amend", "rerun"


# ── Event assembly ───────────────────────────────────────────────────────────

def _parse_iso(ts: str | None) -> str:
    """Normalise ISO-8601 timestamp for sorting; empty string if None."""
    if not ts:
        return ""
    return ts.replace(" ", "T")


def _study_exists(study_id: str) -> bool:
    return (DATA_ROOT / study_id).exists()


def _get_locked_plans(study_id: str) -> list[dict]:
    """Return sorted list of locked plan dicts, each with a _version key."""
    pattern = str(DATA_ROOT / study_id / "study_plan.v*.locked.json")
    plans = []
    for path in sorted(glob.glob(pattern)):
        data = json.loads(Path(path).read_text())
        try:
            data["_version"] = int(path.split(".v")[1].split(".")[0])
        except (IndexError, ValueError):
            data["_version"] = 0
        data["_file_path"] = path
        plans.append(data)
    return plans


def _get_state(conn, study_id: str) -> int:
    row = conn.execute(
        "SELECT is_locked FROM studies WHERE id=?", (study_id,)
    ).fetchone()
    return row["is_locked"] if row else 0


def _get_unmasked_at(conn, study_id: str) -> str | None:
    row = conn.execute(
        "SELECT unmasked_at FROM studies WHERE id=?", (study_id,)
    ).fetchone()
    return row["unmasked_at"] if row and row["unmasked_at"] else None


def _get_created_at(conn, study_id: str) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM studies WHERE id=?", (study_id,)
    ).fetchone()
    return row["created_at"] if row else None


def _get_var_count(conn, study_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM variables WHERE study_id=?", (study_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def _get_analysis_results(conn, study_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, test_name, computed_at, p_value, statistic,
                  status_json, is_pre_registered, study_plan_version,
                  provenance_json, superseded_previous_result_id,
                  sample_counts_json
           FROM analysis_results
           WHERE study_id=?
           ORDER BY id""",
        (study_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_bundle_info(study_id: str) -> dict | None:
    pattern = str(DATA_ROOT / study_id / "*_bundle.tar.gz")
    paths = sorted(glob.glob(pattern))
    if not paths:
        return None
    path = Path(paths[-1])
    try:
        with tarfile.open(str(path), "r:gz") as tf:
            member = tf.getmember("manifest.json")
            manifest = json.loads(tf.extractfile(member).read())
        return {
            "path": str(path),
            "generated_at": manifest.get("generated_at", ""),
            "composite_hash": manifest.get("composite_hash", ""),
        }
    except Exception:
        return None


def assemble_events(study_id: str) -> list[LineageEvent]:
    """Walk study directory + DB and return ordered provenance events.

    Every timestamp is read directly from stored data.
    Missing stages produce no event (no fabricated placeholder nodes).
    """
    events: list[LineageEvent] = []

    if not _study_exists(study_id):
        return []

    conn = get_connection(study_id)
    study_exists_in_db = conn.execute(
        "SELECT COUNT(*) AS cnt FROM studies WHERE id=?", (study_id,)
    ).fetchone()["cnt"] > 0

    if not study_exists_in_db:
        conn.close()
        return []

    state = _get_state(conn, study_id)
    created_at = _get_created_at(conn, study_id)
    unmasked_at = _get_unmasked_at(conn, study_id)
    var_count = _get_var_count(conn, study_id)
    locked_plans = _get_locked_plans(study_id)
    analysis_results = _get_analysis_results(conn, study_id)
    bundle_info = _get_bundle_info(study_id)
    conn.close()

    # --- Ingest ---
    if created_at:
        events.append(LineageEvent(
            event_type="ingest",
            timestamp=created_at,
            label="Study created / data ingested",
            detail={},
        ))

    # --- Variable classification ---
    if var_count > 0:
        events.append(LineageEvent(
            event_type="variable_classification",
            timestamp=created_at or "",
            label=f"Variables classified ({var_count} variables)",
            detail={"n_variables": var_count},
        ))

    # --- Plan locks (including amendments) ---
    for plan in locked_plans:
        version = plan.get("_version", 0)
        locked_at = plan.get("locked_at", "")
        content_hash = plan.get("content_hash", "")
        amendment_reason = plan.get("amendment_reason", "")
        is_amendment = bool(amendment_reason)

        if is_amendment:
            if unmasked_at and locked_at > unmasked_at:
                branch = "post_hoc_amend"
                label = f"Post-hoc amendment (v{version})"
            else:
                branch = "pre_unmask_amend"
                label = f"Pre-unmask amendment (v{version})"
            events.append(LineageEvent(
                event_type="amendment",
                timestamp=locked_at,
                label=label,
                detail={
                    "version": version,
                    "reason": amendment_reason,
                    "content_hash": content_hash,
                },
                depth=1,
                branch=branch,
            ))
        else:
            events.append(LineageEvent(
                event_type="plan_lock",
                timestamp=locked_at,
                label=f"Plan locked (v{version})",
                detail={
                    "version": version,
                    "content_hash": content_hash,
                },
                branch="main",
            ))

    # --- Seal outcomes ---
    # seal_outcomes() is called immediately after variable classification
    # at ingest time (core/masking/gate.py:seal_outcomes). No independent
    # timestamp is recorded — use created_at as the closest approximation.
    if var_count > 0:
        events.append(LineageEvent(
            event_type="seal",
            timestamp=created_at or "",
            label="Outcome data sealed (ingest-time masking)",
            detail={},
            branch="main",
        ))

    # --- Unmask ---
    if unmasked_at:
        events.append(LineageEvent(
            event_type="unmask",
            timestamp=unmasked_at,
            label="Study unmasked (outcome data restored)",
            detail={},
            branch="main",
        ))

    # --- Analysis results ---
    superseded_ids = {r["id"] for r in analysis_results
                      if r.get("superseded_previous_result_id")}
    for r in analysis_results:
        computed_at = r.get("computed_at", "")
        test_name = r.get("test_name", "")
        p_value = r.get("p_value")
        statistic = r.get("statistic")
        status_data = json.loads(r["status_json"]) if r.get("status_json") else {}
        status = status_data.get("status", "completed")
        is_pre = r.get("is_pre_registered", 1)
        plan_ver = r.get("study_plan_version", "")

        if not test_name:
            continue

        # Build label
        p_str = f", p={p_value:.4f}" if p_value is not None else ""
        stat_str = f", stat={statistic:.4f}" if statistic is not None else ""
        tag = "pre-registered" if is_pre else "post-hoc"
        status_str = f" [{status}]" if status != "completed" else ""
        label = f"{test_name}{stat_str}{p_str} ({tag}, plan v{plan_ver}){status_str}"

        detail = {
            "result_id": r["id"],
            "test_name": test_name,
            "is_pre_registered": is_pre,
            "plan_version": plan_ver,
        }

        supersedes = r.get("superseded_previous_result_id")
        if supersedes:
            detail["supersedes_result_id"] = supersedes
            label = f"{label}  [supersedes id={supersedes}]"

        events.append(LineageEvent(
            event_type="analyze",
            timestamp=computed_at,
            label=label,
            detail=detail,
            branch="main",
        ))

    # --- Bundle ---
    if bundle_info:
        events.append(LineageEvent(
            event_type="bundle",
            timestamp=bundle_info["generated_at"],
            label="Bundle created",
            detail={
                "composite_hash": bundle_info["composite_hash"],
                "path": bundle_info["path"],
            },
            branch="main",
        ))

    # Sort chronologically; insertion order preserved for same-timestamp ties
    events.sort(key=lambda e: _parse_iso(e.timestamp))
    return events


# ── ASCII tree renderer ──────────────────────────────────────────────────────

def render_text(events: list[LineageEvent]) -> str:
    """Render lineage as an ASCII tree to stdout."""
    if not events:
        return "No provenance data found for this study.\n"

    icon_map = {
        "ingest": "●",
        "variable_classification": "○",
        "plan_lock": "◈",
        "amendment": "◇",
        "seal": "△",
        "unmask": "▽",
        "analyze": "◆",
        "bundle": "■",
    }

    lines: list[str] = []
    lines.append("Study provenance DAG")
    lines.append("")

    for i, ev in enumerate(events):
        ts = ev.timestamp[:19].replace("T", " ") if ev.timestamp else "—"
        icon = icon_map.get(ev.event_type, "○")
        is_branch = ev.depth > 0

        tag = ""
        if ev.branch == "pre_unmask_amend":
            tag = " [CONFIRMATORY]"
        elif ev.branch == "post_hoc_amend":
            tag = " [EXPLORATORY_POST_HOC]"

        if is_branch:
            has_next = any(e.depth > 0 for e in events[i + 1:])
            prefix = "├─ " if has_next else "└─ "
            detail_prefix = "│  " if has_next else "   "
        else:
            prefix = "│ "
            detail_prefix = "│  "

        lines.append(f"{prefix}{icon} {ts}  {ev.label}{tag}")

        if ev.detail.get("content_hash"):
            lines.append(f"{detail_prefix}hash: {ev.detail['content_hash'][:12]}...")
        if ev.detail.get("reason"):
            lines.append(f"{detail_prefix}reason: {ev.detail['reason']}")
        if ev.detail.get("composite_hash"):
            lines.append(f"{detail_prefix}hash: {ev.detail['composite_hash'][:12]}...")

    # Legend
    lines.append("")
    lines.append("Legend:")
    lines.append("  ● Ingest  ○ Classification  ◈ Plan lock  △ Seal")
    lines.append("  ▽ Unmask  ◇ Amendment  ◆ Analysis  ■ Bundle")
    has_amend = any(e.event_type == "amendment" for e in events)
    if has_amend:
        lines.append("  [CONFIRMATORY] = pre-unmask amendment")
        lines.append("  [EXPLORATORY_POST_HOC] = post-unmask amendment")

    return "\n".join(lines)


# ── SVG renderer ─────────────────────────────────────────────────────────────

def render_svg(events: list[LineageEvent], output_path: str) -> None:
    """Render lineage as an SVG DAG suitable for publication appendices."""
    if not events:
        svg = _svg_wrap(400, 100, "<text x='20' y='40' font-family='sans-serif'>"
                                  "No provenance data.</text>")
        Path(output_path).write_text(svg)
        return

    margin = 40
    top = 40
    label_left = 100
    detail_left = label_left + 400
    row_h = 36
    branch_indent = 20

    # Phase coloring
    phase_colors = {
        "ingest": "#2E86C1",
        "variable_classification": "#5DADE2",
        "plan_lock": "#1F4E78",
        "amendment": "#E67E22",
        "seal": "#F39C12",
        "unmask": "#27AE60",
        "analyze": "#7F8C8D",
        "bundle": "#8E44AD",
    }

    # Determine width from longest label and detail
    max_label_w = max(
        len(ev.label) for ev in events
    ) if events else 80
    svg_w = max(800, label_left + max_label_w * 8 + 300)
    svg_h = top + len(events) * row_h + 120 + margin

    svg_parts = [
        '<svg xmlns="http://www.w3.org/2000/svg"',
        f'     width="{svg_w}" height="{svg_h}"',
        '     font-family="Consolas, Courier, monospace" font-size="13">',
        '',
        '  <defs>',
        '    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"',
        '            markerWidth="6" markerHeight="6" orient="auto-start-reverse">',
        '      <path d="M 0 0 L 10 5 L 0 10 z" fill="#999" />',
        '    </marker>',
        '  </defs>',
        '',
        '  <!-- Title -->',
        f'  <text x="40" y="24" font-family="sans-serif" font-weight="bold"',
        f'        font-size="16" fill="#1F4E78">Study Provenance DAG</text>',
        '',
        '  <!-- Timeline spine -->',
        f'  <line x1="{margin + 8}" y1="{top}"',
        f'        x2="{margin + 8}" y2="{top + len(events) * row_h}"',
        '        stroke="#ccc" stroke-width="2" />',
    ]

    # Determine branch parents: each branch event connects from its nearest
    # preceding depth-0 ancestor on the timeline.
    branch_connectors: list[tuple[int, int, str]] = []  # (parent_y, child_y, branch_label)
    for i, ev in enumerate(events):
        y = top + i * row_h + row_h // 2
        x = margin + 8
        is_branch = ev.depth > 0

        if is_branch:
            # Find parent (preceding depth-0 event)
            parent_idx = next(
                (j for j in range(i - 1, -1, -1) if events[j].depth == 0),
                i - 1,
            )
            parent_y = top + parent_idx * row_h + row_h // 2
            bx = x + 14 + branch_indent

            # Draw vertical branch line from parent to child
            branch_color = "#E67E22"
            svg_parts.append(
                f'  <line x1="{x + 6}" y1="{parent_y}" x2="{x + 6}" y2="{y}"'
                f'        stroke="{branch_color}" stroke-width="1.5"'
                f'        stroke-dasharray="3,2" />'
            )
            # Horizontal from branch line to label
            svg_parts.append(
                f'  <line x1="{x + 6}" y1="{y}" x2="{bx}" y2="{y}"'
                f'        stroke="{branch_color}" stroke-width="1.5"'
                f'        marker-end="url(#arrow)" />'
            )

            # Timeline dot on branch line offset
            svg_parts.append(
                f'  <circle cx="{x + 6}" cy="{y}" r="4" fill="{branch_color}" />'
            )
        else:
            bx = x + 14

            # Dot on the main timeline spine
            color = phase_colors.get(ev.event_type, "#999")
            svg_parts.append(
                f'  <circle cx="{x}" cy="{y}" r="5" fill="{color}" />'
            )

            # Horizontal connector from spine to label
            svg_parts.append(
                f'  <line x1="{x + 5}" y1="{y}" x2="{bx}" y2="{y}"'
                f'        stroke="{color}" stroke-width="1.5"'
                f'        marker-end="url(#arrow)" />'
            )

        # Timestamp
        ts = ev.timestamp[:19].replace("T", " ") if ev.timestamp else "—        "
        svg_parts.append(
            f'  <text x="{bx + 6}" y="{y + 4}" font-size="11" fill="#666">'
            f'{ts}</text>'
        )

        # Event label
        label_x = bx + 160
        svg_parts.append(
            f'  <text x="{label_x}" y="{y + 4}" font-size="13" fill="#333">'
            f'{_svg_escape(ev.label)}</text>'
        )

        # Branch tag if amendment
        if ev.branch == "pre_unmask_amend":
            tag = "CONFIRMATORY"
            tag_color = "#27AE60"
        elif ev.branch == "post_hoc_amend":
            tag = "EXPLORATORY_POST_HOC"
            tag_color = "#E74C3C"
        else:
            tag = ""
            tag_color = ""

        if tag:
            tag_x = label_x + len(ev.label) * 7.5 + 10
            svg_parts.append(
                f'  <text x="{tag_x}" y="{y + 4}" font-size="10"'
                f'        fill="{tag_color}" font-weight="bold">'
                f'[{tag}]</text>'
            )

        # Hash sub-line for relevant events
        hash_str = ""
        if ev.detail.get("content_hash"):
            hash_str = f"hash: {ev.detail['content_hash'][:16]}..."
        elif ev.detail.get("composite_hash"):
            hash_str = f"hash: {ev.detail['composite_hash'][:16]}..."
        if hash_str:
            svg_parts.append(
                f'  <text x="{label_x + 8}" y="{y + 18}" font-size="10" fill="#999">'
                f'{_svg_escape(hash_str)}</text>'
            )

    # Supersession arrows (rerun chains)
    for ev in events:
        if ev.detail.get("supersedes_result_id"):
            old_id = ev.detail["supersedes_result_id"]
            # Find Y positions of old and new result
            new_idx = events.index(ev)
            old_idx = next((i for i, e in enumerate(events)
                            if e.detail.get("result_id") == old_id), -1)
            if old_idx >= 0:
                y1 = top + old_idx * row_h + row_h // 2
                y2 = top + new_idx * row_h + row_h // 2
                # Draw dashed line from old to new, offset to right
                rx = margin + 8 + 14 + max(e.depth for e in events) * branch_indent + 550
                svg_parts.append(
                    f'  <line x1="{rx}" y1="{y1}" x2="{rx}" y2="{y2}"'
                    f'        stroke="#E74C3C" stroke-width="1" stroke-dasharray="4,3"'
                    f'        marker-end="url(#arrow)" />'
                )
                svg_parts.append(
                    f'  <text x="{rx + 8}" y="{(y1 + y2) // 2 + 4}" font-size="10" fill="#E74C3C">'
                    f'supersedes</text>'
                )

    # Legend
    legend_y = top + len(events) * row_h + 20
    svg_parts.append(
        f'  <text x="40" y="{legend_y}" font-family="sans-serif" font-weight="bold"'
        f'        font-size="12" fill="#555">Legend</text>'
    )
    lg_items = [
        ("● Ingest / ○ Classification / ◈ Plan lock / △ Seal (ingest-time)", "#333"),
        ("▽ Unmask / ◇ Amendment / ◆ Analysis / ■ Bundle", "#333"),
        ("— CONFIRMATORY = pre-unmask amendment", "#27AE60"),
        ("— EXPLORATORY_POST_HOC = post-unmask amendment", "#E74C3C"),
    ]
    for j, (text, color) in enumerate(lg_items):
        svg_parts.append(
            f'  <text x="40" y="{legend_y + 20 + j * 18}" font-size="11"'
            f'        fill="{color}">{_svg_escape(text)}</text>'
        )

    svg_parts.append("</svg>")
    svg = "\n".join(svg_parts)

    Path(output_path).write_text(svg)


def _svg_wrap(w: int, h: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg"'
        f'     width="{w}" height="{h}">\n'
        f'{body}\n'
        f'</svg>'
    )
