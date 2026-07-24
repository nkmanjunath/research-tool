"""Manuscript draft generator — template-based IMRaD assembly.

All numbers come from AnalysisResult objects via the stats engine.
EXPLORATORY_POST_HOC results are clearly marked wherever they appear.
No LLM is involved in v1 — plain Python string templates only.
"""

from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path

from core.database import get_connection, DATA_ROOT
from core.reporting.strobe_checklist import check_study


def _json_field(row, field: str) -> dict:
    """Decode a JSON database field without making draft generation fragile."""
    raw = row[field]
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def generate_key_results(analyses) -> str:
    """Return factual analysis-status counts without editorial interpretation."""
    if not analyses:
        return "**Key results:** [Summarise key results with reference to study objectives]."
    statuses = [_json_field(a, "status_json").get("status", "completed") for a in analyses]
    n_completed = statuses.count("completed")
    n_skipped = statuses.count("skipped_assumption_violation")
    n_errors = len(statuses) - n_completed - n_skipped
    parts = [f"Of {len(statuses)} pre-registered test(s), {n_completed} completed successfully"]
    if n_skipped:
        parts.append(
            f"{n_skipped} {'was' if n_skipped == 1 else 'were'} skipped due to "
            f"{'an' if n_skipped == 1 else ''} assumption violation"
            f"{'s' if n_skipped != 1 else ''}"
        )
    if n_errors:
        parts.append(f"{n_errors} ended with an analysis error")
    return "**Key results:** " + " and ".join(parts) + "."


def _event_count(conn, study_id: str, time_variable: str) -> int | None:
    """Read event count from the unmasked table or its outcome shadow table."""
    prefix = time_variable.replace("_days", "").replace("_months", "").replace("_time", "")
    event_column = f"{prefix}_event"
    quoted = '"' + event_column.replace('"', '""') + '"'
    for table in (f"raw_{study_id}", f"raw_masked_{study_id}"):
        try:
            row = conn.execute(
                f'SELECT COUNT({quoted}) AS n_observed, '
                f'SUM(CASE WHEN CAST({quoted} AS INTEGER) = 1 THEN 1 ELSE 0 END) AS n_events '
                f'FROM {table}'
            ).fetchone()
        except Exception:
            continue
        if row and int(row["n_observed"] or 0) > 0:
            return int(row["n_events"] or 0)
    return None


def generate_limitations(study_id: str) -> str:
    """Generate factual limitation statements from study data.

    Every sentence traces to a specific fact in the study data.
    Never fabricates a limitation that isn't supported.
    """
    conn = get_connection(study_id)
    study = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,)).fetchone()
    if not study:
        conn.close()
        return ""

    locked_plans = list(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    plan_data = None
    if locked_plans:
        plan_data = json.loads(locked_plans[-1].read_text())

    analyses = conn.execute(
        "SELECT * FROM analysis_results WHERE study_id=? ORDER BY id", (study_id,)
    ).fetchall()
    limitations: list[str] = []

    # 1. Retrospective design (always true for this tool's scope)
    limitations.append(
        "As a retrospective analysis, this study is subject to "
        "the inherent limitations of retrospective designs, including "
        "potential information bias and residual confounding not captured "
        "by the covariates included."
    )

    # 2. Sample size — from analysis_results sample_counts
    total_n = 0
    for a in analyses:
        sc = json.loads(a["sample_counts_json"]) if a["sample_counts_json"] else {}
        n = sc.get("n_analyzed", 0) or sc.get("n_total", 0)
        total_n = max(total_n, n)
    n_covariates = len(plan_data.get("covariates", [])) if plan_data else 0
    if total_n > 0 and total_n < 50:
        if n_covariates > 0:
            limitations.append(
                f"This study's sample size (n={total_n}) limits statistical power, "
                f"particularly for the multivariable analysis involving "
                f"{n_covariates} covariate(s)."
            )
        else:
            limitations.append(
                f"The sample size (n={total_n}) is modest, which may limit "
                f"the precision of the reported estimates."
            )

    # 3. Skipped tests
    for a in analyses:
        status_data = json.loads(a["status_json"]) if a["status_json"] else {}
        if status_data.get("status") == "skipped_assumption_violation":
            reason = (status_data.get("reason", "assumption violation") or "assumption violation").rstrip(" .")
            limitations.append(
                f"The planned {a['test_name']} analysis could not be "
                f"reliably performed due to {reason}; "
                f"this comparison should be considered exploratory only."
            )

    # 4. Forced tests (ran despite warnings). Match warnings to the planned
    # test's variable rather than searching the test name for that variable.
    for a in analyses:
        status_data = json.loads(a["status_json"]) if a["status_json"] else {}
        if status_data.get("status") == "completed":
            if plan_data:
                planned = [
                    t for t in plan_data.get("planned_tests", [])
                    if t.get("test_name") == a["test_name"]
                ]
                for planned_test in planned:
                    warn_var = planned_test.get("variable_name", "")
                    warn_msg = plan_data.get("warnings", {}).get(warn_var)
                    if warn_msg:
                        limitations.append(
                            f"The {a['test_name']} analysis was performed "
                            f"despite a flagged assumption violation "
                            f"({warn_msg}); this result should be "
                            f"interpreted with caution."
                        )

    # 5. Cox EPV check. EPV is events per predictor, not observations per
    # predictor. Prefer a persisted event count; otherwise derive it from
    # the event indicator in the raw or masked table.
    for a in analyses:
        if a["test_name"] == "cox_proportional_hazards":
            sc = json.loads(a["sample_counts_json"]) if a["sample_counts_json"] else {}
            planned = next((
                t for t in (plan_data or {}).get("planned_tests", [])
                if t.get("test_name") == a["test_name"]
            ), None)
            n_events = sc.get("n_events")
            if n_events is None and planned:
                n_events = _event_count(conn, study_id, planned.get("variable_name", ""))
            if n_events is None:
                continue
            n_events = int(n_events)
            n_predictors = n_covariates + 1  # +1 for treatment_arm
            n_predictors = max(n_predictors, 1)
            epv = n_events / n_predictors
            if epv < 10:
                limitations.append(
                    f"With {n_events} events across {n_predictors} predictors "
                    f"in the Cox model (EPV={epv:.1f}), the "
                    f"events-per-variable ratio may be inadequate for "
                    f"reliable inference; findings should "
                    f"be considered preliminary."
                )

    # 6. Case-control without matching
    study_type = (study["study_type"] or "cohort") if study else "cohort"
    if study_type == "case_control":
        mc = plan_data.get("matching_criteria", []) if plan_data else []
        if not mc:
            limitations.append(
                "This case-control study did not employ explicit "
                "matching criteria, which may introduce confounding "
                "not addressed in the current analysis."
            )

    # Join non-empty, unique sentences
    seen: set[str] = set()
    result = []
    for line in limitations:
        if line not in seen:
            seen.add(line)
            result.append(line)

    conn.close()
    return "\n".join(f"- {s}" for s in result)


def generate_draft(study_id: str) -> str:
    """Assemble an IMRaD manuscript draft from study data.

    Numbers are pulled from AnalysisResult objects.  Missing data is
    indicated with placeholders like `[Not yet computed]`.
    """
    conn = get_connection(study_id)
    cur = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,))
    study = cur.fetchone()

    cur = conn.execute("SELECT * FROM variables WHERE study_id=?", (study_id,))
    variables = cur.fetchall()

    cur = conn.execute("SELECT * FROM analysis_results WHERE study_id=?", (study_id,))
    analyses = cur.fetchall()

    conn.close()

    study_name = study["name"] if study else study_id
    study_type = (study["study_type"] or "cohort") if study else "cohort"

    # ── Title ──────────────────────────────────────────────────────────────
    title = f"Title: A {study_type.replace('_', ' ')} study of {study_name}"

    # ── Abstract ────────────────────────────────────────────────────────────
    n_vars = len(variables)
    n_analyses = len(analyses)

    if n_analyses > 0:
        statuses = [
            (_json_field(a, "status_json").get("status", "completed"))
            for a in analyses
        ]
        n_completed = statuses.count("completed")
        n_total = len(statuses)
        results_line = (
            f"{n_completed} of {n_total} pre-registered tests completed"
            f"{'; see Results for details' if n_total > 0 else ''}."
        )
    else:
        results_line = "[Results summary — not yet computed or will be filled during hydration]"

    abstract = (
        f"**Background:** This study investigated {study_name} using a "
        f"{study_type.replace('_', ' ')} design.\n\n"
        f"**Methods:** {n_vars} variables were classified "
        f"({sum(1 for v in variables if v['role'] == 'baseline')} baseline, "
        f"{sum(1 for v in variables if v['role'] == 'outcome')} outcome). "
        f"Analysis included {n_analyses} pre-registered tests.\n\n"
        f"**Results:** {results_line}\n\n"
        f"**Conclusions:** [To be completed based on results]"
    )

    # ── Introduction ────────────────────────────────────────────────────────
    introduction = (
        "## Introduction\n\n"
        "**Background:** [Scientific background to be provided by author]\n\n"
        "**Objective:** This study aimed to test the association described in "
        "the locked study plan.\n\n"
    )

    # ── Methods ─────────────────────────────────────────────────────────────
    # Check for locked plan
    locked_plans = list(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    plan_section = "No locked plan found."
    if locked_plans:
        import json
        plan_data = json.loads(locked_plans[-1].read_text())
        plan_section = (
            f"**Study design:** {plan_data.get('study_type', 'cohort').replace('_', ' ')}\n\n"
            f"**Primary comparison:** {plan_data.get('primary_comparison', 'Not specified')}\n\n"
            f"**Primary outcome variable IDs:** "
            f"{', '.join(str(x) for x in plan_data.get('primary_outcome_variable_ids', []))}\n\n"
            f"**Covariates:** "
            f"{', '.join(str(x) for x in plan_data.get('covariates', [])) or 'None'}\n\n"
            f"**Pre-registered tests:**\n"
        )
        for t in plan_data.get("planned_tests", []):
            plan_section += f"  - {t.get('test_name', 'Unknown')}: {t.get('rationale', '')}\n"

    methods = (
        "## Methods\n\n"
        f"**Study design and setting:** {study_type.replace('_', ' ')} study. "
        f"Data stored locally. Variables classified at study setup.\n\n"
        f"**Study plan (pre-registered before outcome data access):**\n{plan_section}\n\n"
        f"**Statistical analysis:** Analyses were performed using the pre-registered "
        f"tests specified in the locked study plan. All tests were run deterministically "
        f"via the research tool's stats engine.\n\n"
    )

    # ── Results ─────────────────────────────────────────────────────────────
    results_section = "## Results\n\n"

    # Table 1 — pull real data from descriptive.py
    from core.stats.descriptive import generate_table1

    # Default to stratified by treatment_arm when a locked plan exists
    locked_plans = list(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    groupby = "treatment_arm" if locked_plans else None
    tbl = generate_table1(study_id, groupby=groupby)
    results_section += "**Table 1: Baseline Characteristics**\n\n"
    if not tbl.empty:
        # Flatten MultiIndex columns: ('Grouped by treatment_arm', 'A') → 'A'
        if hasattr(tbl.columns, 'levels') or (
            len(tbl.columns) > 0 and isinstance(tbl.columns[0], tuple)
        ):
            tbl.columns = [
                str(c[-1]).strip() if isinstance(c, tuple) else str(c)
                for c in tbl.columns
            ]
        results_section += tbl.to_markdown(index=False) + "\n\n"
    else:
        results_section += "[Table 1 not yet computed — run `research-tool table1`]\n\n"

    if analyses:
        for a in analyses:
            import json
            tag = ""
            if not a["is_pre_registered"]:
                tag = " **[EXPLORATORY_POST_HOC]**"
            es = json.loads(a["effect_size_json"]) if a["effect_size_json"] else None
            es_str = f" ({es['metric']}={es['value']:.3f})" if es else ""
            results_section += (
                f"**Test:** {a['test_name']}{tag}\n"
            )
            if a["statistic"] is not None:
                results_section += f"  Statistic: {a['statistic']:.4f}\n"
            if a["p_value"] is not None:
                sig = " (significant)" if a["p_value"] < 0.05 else ""
                results_section += f"  P-value: {a['p_value']:.4f}{sig}\n"
            if es:
                results_section += (
                    f"  Effect size: {es['metric']} = {es['value']:.3f}\n"
                )
            if a["ci_lower"] is not None and a["ci_upper"] is not None:
                results_section += (
                    f"  95% CI: ({a['ci_lower']:.3f}, {a['ci_upper']:.3f})\n"
                )
            if a["adjusted_p_value"] is not None:
                results_section += f"  Corrected p-value: {a['adjusted_p_value']:.4f}\n"
            results_section += "\n"
    else:
        results_section += (
            "**Primary analysis:** [Not yet computed — run `research-tool analyze`]\n\n"
            "**Exploratory analyses:** None recorded.\n\n"
        )

    # ── Discussion ──────────────────────────────────────────────────────────
    limitations_text = generate_limitations(study_id)
    key_results = generate_key_results(analyses) + "\n\n"
    limitations_section = (
        "**Limitations:**\n\n" + limitations_text + "\n\n"
    ) if limitations_text else (
        "**Limitations:** [Discuss limitations — e.g., sample size, confounding, "
        "missing data, generalisability]\n\n"
    )
    discussion = (
        "## Discussion\n\n"
        f"{key_results}"
        f"{limitations_section}"
        "**Interpretation:** [Cautious overall interpretation]\n\n"
        "**Generalisability:** [Discuss external validity]\n\n"
    )

    # ── Other Information ──────────────────────────────────────────────────
    other = (
        "## Other Information\n\n"
        "**Funding:** Not yet declared by study authors.\n\n"
    )

    # ── STROBE Checklist ───────────────────────────────────────────────────
    strobe_report = check_study(study_id)
    strobe_section = "## STROBE Checklist Status\n\n"
    for r in strobe_report:
        status = "✓" if r.satisfied else "✗"
        strobe_section += f"  [{status}] Item {r.item_id}: {r.evidence}\n"

    # ── Assemble ───────────────────────────────────────────────────────────
    draft = (
        f"# {title}\n\n"
        f"## Abstract\n\n{abstract}\n\n"
        f"{introduction}\n\n"
        f"{methods}\n\n"
        f"{results_section}\n\n"
        f"{discussion}\n\n"
        f"{other}\n\n"
        f"---\n\n"
        f"*Draft generated by the Retrospective Clinical Research Tool on "
        f"{datetime.utcnow().isoformat()}*\n\n"
        f"*Note: This is a template. All numeric values must be verified against "
        f"the original AnalysisResult objects in the provenance graph.*\n\n"
        f"---\n\n"
        f"{strobe_section}\n"
    )

    return draft


def write_draft(study_id: str) -> Path:
    """Generate and write the manuscript draft to disk."""
    draft = generate_draft(study_id)
    path = DATA_ROOT / study_id / "manuscript_draft.md"
    path.write_text(draft)
    return path
