from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.database import DATA_ROOT, get_connection, init_db


# ── Known field bounds ───────────────────────────────────────────────────────

KNOWN_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "age": (0, 120),
    "bmi": (10, 60),
    "weight_kg": (20, 300),
    "weight_lbs": (44, 660),
    "height_cm": (50, 250),
    "height_m": (0.5, 2.5),
    "prior_lines": (0, None),
    "prior_therapies": (0, None),
    "num_lesions": (0, None),
    "num_metastases": (0, None),
    "systolic_bp": (50, 250),
    "diastolic_bp": (30, 150),
    "heart_rate": (20, 250),
    "temperature_c": (32, 43),
    "temperature_f": (89, 109),
    "creatinine": (0.1, 15),
    "hemoglobin_gdl": (3, 20),
    "platelet_count": (5, 1500),
    "wbc_count": (0.1, 100),
    "neutrophil_count": (0, 50),
    "lymphocyte_count": (0, 30),
    "albumin_gdl": (0.5, 6),
    "bmi": (10, 60),
    "follow_up_days": (0, None),
    "follow_up_months": (0, None),
}


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _fmt_pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def _col_header(text: str, width: int = 72) -> str:
    return f"\n{'─' * width}\n{text}\n{'─' * width}"


# ── Core ─────────────────────────────────────────────────────────────────────

def run_forensics(study_id: str) -> Path:
    """Run all forensics checks and write forensics_report.md.

    Returns the path to the written report.
    """
    study_dir = DATA_ROOT / study_id
    if not study_dir.exists():
        raise FileNotFoundError(f"Study directory not found: {study_dir}")

    conn = get_connection(study_id)
    init_db(conn)

    study_row = conn.execute(
        "SELECT id, name, is_locked, unmasked_at, created_at FROM studies WHERE id=?",
        (study_id,),
    ).fetchone()
    if not study_row:
        conn.close()
        raise FileNotFoundError(f"Study {study_id} not found in database.")

    study_info = dict(study_row)
    state = study_info["is_locked"]
    is_unmasked = state >= 2

    raw_table = f"raw_{study_id}"
    masked_table = f"raw_masked_{study_id}"

    col_info = _get_column_info(conn, study_id)
    n_rows = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM {raw_table}"
    ).fetchone()["cnt"]

    lines: list[str] = []
    lines.append(f"# Forensics Report — {study_info['name']}")
    lines.append(f"")
    lines.append(f"- **Study ID:** {study_id}")
    lines.append(f"- **Rows:** {n_rows}")
    lines.append(f"- **Columns:** {len(col_info)}")
    lines.append(f"- **State:** {['pre-lock', 'locked', 'unmasked'][state]}")
    lines.append(f"- **Unmasked:** {study_info['unmasked_at'] or 'N/A'}")
    lines.append(f"- **Generated:** {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"")

    # ── Tier 1: Impossible timelines ──
    lines.append(_col_header("Tier 1 — Impossible Timelines"))
    _check_impossible_timelines(conn, raw_table, masked_table, col_info,
                                n_rows, is_unmasked, lines)

    # ── Tier 1: Duplicate rows ──
    lines.append(_col_header("Tier 1 — Duplicate Rows"))
    _check_duplicates(conn, raw_table, col_info, lines)

    # ── Tier 1: Out-of-range values ──
    lines.append(_col_header("Tier 1 — Out-of-Range Values"))
    _check_out_of_range(conn, raw_table, col_info, lines)

    # ── Tier 2: Benford's Law ──
    lines.append(_col_header("Tier 2 — Benford's Law (First-Digit Analysis)"))
    _check_benford(conn, raw_table, col_info, n_rows, lines)

    # ── Tier 2: Digit-preference clustering ──
    lines.append(_col_header("Tier 2 — Digit-Preference / Rounding Clustering"))
    _check_digit_preference(conn, raw_table, col_info, n_rows, lines)

    # ── Summary ──
    lines.append(_col_header("Summary of Checks Run"))
    n_pass = sum(1 for l in lines if l.strip().startswith("- ✓"))
    n_flags = sum(1 for l in lines if l.strip().startswith("- ⚠"))
    n_blocked = sum(1 for l in lines if l.strip().startswith("- ⛔"))
    lines.append(f"")
    lines.append(f"- **Checks passing:** {n_pass}")
    lines.append(f"- **Flags raised:** {n_flags}")
    lines.append(f"- **Checks blocked/skipped:** {n_blocked}")
    lines.append(f"- **Note:** These findings are statistical screens, not proof of fabrication.")
    lines.append(f"")

    conn.close()

    report_path = study_dir / "forensics_report.md"
    report_path.write_text("\n".join(lines))
    return report_path


# ── Column helpers ───────────────────────────────────────────────────────────

def _get_column_info(conn, study_id: str) -> dict[str, dict]:
    """Return {col_name: {role, data_type, is_masked}}."""
    rows = conn.execute(
        "SELECT column_name, role, data_type, is_masked FROM variables WHERE study_id=?",
        (study_id,),
    ).fetchall()
    return {r["column_name"]: dict(r) for r in rows}


def _is_numeric_column(col_name: str, col_info: dict) -> bool:
    """Whether a column is expected to contain numeric values."""
    cinfo = col_info.get(col_name, {})
    dt = cinfo.get("data_type", "")
    return dt in ("continuous", "time_to_event")


def _get_numeric_values(conn, raw_table: str, col_name: str) -> list[float]:
    """Read numeric values from a TEXT column, skipping nulls/blanks."""
    rows = conn.execute(
        f"SELECT \"{col_name}\" FROM {raw_table} "
        f"WHERE \"{col_name}\" IS NOT NULL AND \"{col_name}\" != ''"
    ).fetchall()
    vals: list[float] = []
    for (v,) in rows:
        try:
            vals.append(float(v))
        except (ValueError, TypeError):
            pass
    return vals


# ── Tier 1: Impossible timelines ─────────────────────────────────────────────

def _find_time_pairs(col_info: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    time_cols = [
        c for c, info in col_info.items()
        if info.get("data_type") == "time_to_event"
    ]
    for i, a in enumerate(time_cols):
        for b in time_cols[i + 1:]:
            a_lower = a.lower().replace("_", "").replace("-", "")
            b_lower = b.lower().replace("_", "").replace("-", "")
            if "pfs" in a_lower and "os" in b_lower:
                pairs.append((a, b))
            elif "os" in a_lower and "pfs" in b_lower:
                pairs.append((b, a))
            elif "progressionfree" in a_lower and "overallsurvival" in b_lower:
                pairs.append((a, b))
            elif "overallsurvival" in a_lower and "progressionfree" in b_lower:
                pairs.append((b, a))
    return pairs


def _check_impossible_timelines(
    conn, raw_table: str, masked_table: str,
    col_info: dict, n_rows: int, is_unmasked: bool,
    lines: list[str],
) -> None:
    if not is_unmasked:
        masked_exists = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table' AND name=?",
            (masked_table,),
        ).fetchone()["cnt"] > 0
        if masked_exists:
            lines.append(f"⛔ Study is masked. Outcome data is in shadow table — "
                         f"unmask first or re-run with unmasked study.")
        else:
            lines.append(f"⛔ Study is masked and no shadow table found.")
        lines.append(f"   Skipped: impossible timeline checks need visible outcome data.")
        return

    time_pairs = _find_time_pairs(col_info)
    if not time_pairs:
        lines.append(f"→ No time-to-event column pairs found to compare.")
        return

    for shorter, longer in time_pairs:
        rows = conn.execute(
            f"SELECT CAST(\"{shorter}\" AS REAL) AS s, "
            f"       CAST(\"{longer}\" AS REAL) AS l "
            f"FROM {raw_table} "
            f"WHERE \"{shorter}\" IS NOT NULL AND \"{shorter}\" != '' "
            f"  AND \"{longer}\" IS NOT NULL AND \"{longer}\" != ''"
        ).fetchall()
        n_valid = len(rows)
        violations = [(i + 1, r["s"], r["l"]) for i, r in enumerate(rows)
                      if r["s"] > r["l"] > 0]
        zero_or_neg = [(i + 1, r["s"], r["l"]) for i, r in enumerate(rows)
                       if r["s"] <= 0 or r["l"] <= 0]

        lines.append(f"")
        lines.append(f"**{shorter} vs {longer}** (PFS should ≤ OS):")
        lines.append(f"- Valid rows: {n_valid} / {n_rows}")
        if violations:
            lines.append(f"- ⚠ **{len(violations)} row(s)** where {shorter} > {longer}:")
            for row_num, s, l in violations:
                lines.append(f"    Row {row_num}: {shorter}={s:.0f}, {longer}={l:.0f} (Δ {s - l:.0f})")
        else:
            lines.append(f"- ✓ No rows where {shorter} > {longer}.")
        if zero_or_neg:
            lines.append(f"- ⚠ **{len(zero_or_neg)} row(s)** with non-positive duration:")
            for row_num, s, l in zero_or_neg:
                lines.append(f"    Row {row_num}: {shorter}={s:.0f}, {longer}={l:.0f}")
        else:
            lines.append(f"- ✓ All durations positive.")


# ── Tier 1: Duplicate rows ───────────────────────────────────────────────────

def _check_duplicates(
    conn, raw_table: str, col_info: dict, lines: list[str],
) -> None:
    all_cols = list(col_info.keys())
    if not all_cols:
        lines.append(f"→ No columns to check.")
        return

    # Exact duplicates
    col_list = ", ".join(f'"{c}"' for c in all_cols)
    dup_rows = conn.execute(
        f"SELECT {col_list}, COUNT(*) AS cnt FROM {raw_table} "
        f"GROUP BY {col_list} HAVING cnt > 1"
    ).fetchall()
    if dup_rows:
        lines.append(f"- ⚠ **{len(dup_rows)} exact duplicate row group(s)** found:")
        for d in dup_rows:
            lines.append(f"    Appears {d['cnt']}×: " + ", ".join(
                f"{c}={d[c]}" for c in all_cols[:4]
            ) + ("..." if len(all_cols) > 4 else ""))
    else:
        lines.append(f"- ✓ No exact duplicate rows.")

    # Near-duplicates via patient_id
    raw_cols = [r[1] for r in conn.execute(
        f"PRAGMA table_info({raw_table})"
    ).fetchall() if r[1] != "row_id"]
    id_col = _find_patient_id_col(col_info, raw_cols)
    if not id_col:
        lines.append(f"- → No patient_id column found; near-duplicate check skipped.")
        return

    # For each patient_id with multiple rows, check for conflicting values
    pids = conn.execute(
        f"SELECT \"{id_col}\", COUNT(*) AS cnt FROM {raw_table} "
        f"GROUP BY \"{id_col}\" HAVING cnt > 1"
    ).fetchall()
    if not pids:
        lines.append(f"- ✓ No duplicate patient IDs (near-duplicates cannot occur).")
        return

    lines.append(f"- ⚠ **{len(pids)} patient ID(s)** appear more than once:")
    seen_ids: set[str] = set()
    for pid_row in pids:
        pid = pid_row[id_col]
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        rows_for_pid = conn.execute(
            f"SELECT * FROM {raw_table} "
            f"WHERE \"{id_col}\" = ? ORDER BY row_id",
            (pid,),
        ).fetchall()
        keys = [k for k in rows_for_pid[0].keys() if k != "row_id"]
        # Compare all non-id columns
        conflicts: list[str] = []
        first = dict(rows_for_pid[0])
        for row_dict in (dict(r) for r in rows_for_pid[1:]):
            for k in keys:
                if k == id_col:
                    continue
                if str(first.get(k, "")) != str(row_dict.get(k, "")):
                    conflicts.append(k)
        conflict_str = f", conflicting: {', '.join(conflicts)}" if conflicts else ""
        lines.append(
            f"    {id_col}={pid} appears {pid_row['cnt']}×{conflict_str}"
        )

    lines.append(f"- Note: Raw duplicate IDs are also caught by `lock --allow-duplicate-ids`.")


def _find_patient_id_col(col_info: dict, raw_cols: list[str] | None = None) -> str | None:
    candidates = ["patient_id", "patientid", "subject_id", "id", "ptid", "pt_id"]
    search_in = list(col_info.keys()) if col_info else []
    if raw_cols:
        search_in = list(set(search_in + raw_cols))
    for c in candidates:
        if c in search_in:
            return c
    for c in search_in:
        if "id" in c.lower() and c.lower() != "row_id":
            return c
    return None


# ── Tier 1: Out-of-range values ──────────────────────────────────────────────

def _check_out_of_range(
    conn, raw_table: str, col_info: dict, lines: list[str],
) -> None:
    checked_any = False
    for col_name, cinfo in col_info.items():
        if not _is_numeric_column(col_name, col_info):
            continue
        bounds = KNOWN_BOUNDS.get(col_name.lower())
        if bounds is None:
            continue
        lo, hi = bounds
        vals = _get_numeric_values(conn, raw_table, col_name)
        if not vals:
            continue
        checked_any = True
        violations: list[tuple[int, float]] = []
        for i, v in enumerate(vals, 1):
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                violations.append((i, v))
        if violations:
            lines.append(
                f"- ⚠ **{col_name}**: {len(violations)} value(s) outside "
                f"bounds [{_fmt(lo)}, {_fmt(hi)}]: "
                + ", ".join(f"row≈{r} ({_fmt(v)})" for r, v in violations[:5])
                + ("..." if len(violations) > 5 else "")
            )
        else:
            lines.append(f"- ✓ **{col_name}**: all {len(vals)} values within "
                         f"bounds [{_fmt(lo)}, {_fmt(hi)}].")
    if not checked_any:
        lines.append(f"- → No columns with declared bounds in this dataset.")


# ── Tier 2: Benford's Law ────────────────────────────────────────────────────

def _is_benford_eligible(
    vals: list[float], n_rows: int,
) -> tuple[bool, str]:
    """Check whether a column is eligible for Benford's Law analysis.

    Returns (eligible, explanation).
    """
    if not vals:
        return False, "No numeric values found."

    n = len(vals)
    if n < 30:
        return False, (
            f"Sample too small (N={n} < 30). Benford's Law requires "
            f"at least ~30–50 observations for a meaningful test."
        )

    # Must be naturally occurring (not bounded by artificial limits)
    mn, mx = min(vals), max(vals)
    if mn <= 0:
        return False, (
            f"Values include non-positive numbers (min={mn}). "
            f"Benford's Law applies to positive, naturally-occurring magnitudes."
        )

    # Must span at least 2 orders of magnitude
    ratio = mx / mn
    if ratio < 100:
        return False, (
            f"Insufficient range: max/mn={ratio:.1f} (< 100, i.e. < 2 orders of magnitude). "
            f"Benford's Law requires values spanning multiple orders of magnitude."
        )

    # Check for artificial constraints (e.g., bounded scales)
    uniq = len(set(vals))
    if uniq < 10:
        return False, (
            f"Too few unique values ({uniq}). "
            f"Likely an artificially constrained or categorical variable."
        )

    if n < 50:
        return True, (
            f"Marginal sample size (N={n} < 50). Results should be interpreted "
            f"with caution — Benford deviations at this N are often noise."
        )

    return True, (
        f"Eligible: N={n}, range {mn:.0f}–{mx:.0f} ({ratio:.0f}×, "
        f"{uniq} unique values)."
    )


def _benford_expected(n: int) -> list[float]:
    """Expected Benford proportion for first digits 1–9 given n observations."""
    return [n * math.log10(1 + 1 / d) for d in range(1, 10)]


def _chi_square_test(observed: list[int], expected: list[float]) -> float:
    """Pearson chi-square statistic."""
    stat = 0.0
    for o, e in zip(observed, expected):
        if e > 0:
            stat += (o - e) ** 2 / e
    return stat


def _first_digit_dist(vals: list[float]) -> Counter[int]:
    """Return counter of first digits (1–9)."""
    cnt: Counter[int] = Counter()
    for v in vals:
        s = f"{v:.10e}"  # scientific notation to avoid float issues
        first = s[0]
        if first in "123456789":
            cnt[int(first)] += 1
    return cnt


def _check_benford(
    conn, raw_table: str, col_info: dict, n_rows: int, lines: list[str],
) -> None:
    numeric_cols = [
        c for c, info in col_info.items()
        if _is_numeric_column(c, col_info)
    ]
    if not numeric_cols:
        lines.append(f"→ No numeric columns found to check.")
        return

    lines.append(f"")
    lines.append(f"**Benford's Law eligibility by column:**")
    lines.append(f"")

    for col_name in numeric_cols:
        vals = _get_numeric_values(conn, raw_table, col_name)
        eligible, reason = _is_benford_eligible(vals, n_rows)
        lines.append(f"**{col_name}**: {'✓' if eligible else '⛔'} {reason}")

        if eligible:
            fd = _first_digit_dist(vals)
            total = sum(fd.values())
            expected = _benford_expected(total)

            lines.append(f"")
            lines.append(f"  | Digit | Observed | Expected (Benford) | Δ% |")
            lines.append(f"  |-------|----------|-------------------|-----|")
            for d in range(1, 10):
                obs = fd.get(d, 0)
                exp = expected[d - 1]
                delta_pct = (obs - exp) / exp * 100 if exp > 0 else 0
                lines.append(
                    f"  | {d} | {obs} ({_fmt_pct(obs / total)}) | "
                    f"{exp:.1f} ({_fmt_pct(exp / total)}) | "
                    f"{delta_pct:+.1f}% |"
                )

            chi2 = _chi_square_test(
                [fd.get(d, 0) for d in range(1, 10)], expected
            )
            lines.append(f"")
            lines.append(f"  χ² statistic: {chi2:.2f} (8 df)")
            if chi2 > 15.5:
                lines.append(f"  ⚠ Chi-square test suggests deviation from "
                             f"Benford distribution (χ² > 15.5 at α=0.05).")
            else:
                lines.append(f"  ✓ First-digit distribution consistent with "
                             f"Benford's Law (χ² ≤ 15.5).")
            lines.append(f"")

    n_ineligible = sum(
        1 for c in numeric_cols
        if not _is_benford_eligible(_get_numeric_values(conn, raw_table, c), n_rows)[0]
    )
    if n_ineligible == len(numeric_cols):
        lines.append(f"*No Benford-eligible columns found in this dataset.*")


# ── Tier 2: Digit-preference clustering ──────────────────────────────────────

def _check_digit_preference(
    conn, raw_table: str, col_info: dict, n_rows: int, lines: list[str],
) -> None:
    numeric_cols = [
        c for c, info in col_info.items()
        if _is_numeric_column(c, col_info)
    ]
    if not numeric_cols:
        lines.append(f"→ No numeric columns found to check.")
        return

    if n_rows < 10:
        lines.append(f"⛔ Sample too small (N={n_rows} < 10) for digit-preference analysis.")
        return

    lines.append(f"")
    for col_name in numeric_cols:
        vals = _get_numeric_values(conn, raw_table, col_name)
        if len(vals) < 10:
            lines.append(f"**{col_name}**: ⛔ Only {len(vals)} valid values, skipping.")
            continue

        n = len(vals)
        # Last-digit distribution for decimal values
        has_decimals = any(v != int(v) for v in vals)
        if has_decimals:
            last_digit = Counter()
            for v in vals:
                s = f"{v:.10f}".rstrip("0")
                if "." in s:
                    last = s[-1]
                    if last.isdigit():
                        last_digit[last] += 1
                    else:
                        last_digit["0"] += 1
                else:
                    last_digit["0"] += 1

            # Check for .0/.5 clustering
            dot_zero = sum(
                v for d, v in last_digit.items() if d == "0"
            )
            dot_five = sum(
                v for d, v in last_digit.items() if d == "5"
            )
            expected_per_digit = n / 10 if n >= 10 else 0
            lines.append(f"**{col_name}**: {n} values")
            if dot_zero > expected_per_digit * 1.5:
                excess = dot_zero - expected_per_digit
                lines.append(
                    f"  ⚠ **{dot_zero} values ({_fmt_pct(dot_zero / n)}) end in .0** "
                    f"(expected ~{expected_per_digit:.0f}, excess ~{excess:.0f})"
                )
            else:
                lines.append(f"  ✓ No excess rounding to .0.")
            if dot_five > expected_per_digit * 1.5:
                excess = dot_five - expected_per_digit
                lines.append(
                    f"  ⚠ **{dot_five} values ({_fmt_pct(dot_five / n)}) end in .5** "
                    f"(expected ~{expected_per_digit:.0f}, excess ~{excess:.0f})"
                )
            else:
                lines.append(f"  ✓ No excess rounding to .5.")
        else:
            lines.append(f"**{col_name}**: All {n} values are whole numbers.")

    # Summary
    lines.append(f"")
    n_small = sum(
        1 for c in numeric_cols
        if len(_get_numeric_values(conn, raw_table, c)) < 10
    )
    if n_small > 0:
        lines.append(f"*{n_small} column(s) skipped due to insufficient valid values.*")
