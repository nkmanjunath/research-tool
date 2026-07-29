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
from core.planning.diagnostics import check_violation
from core.reporting.strobe_checklist import check_study
from core.reporting import filter_superseded as _filter_superseded


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


def _format_analysis_result(a, covariate_rows: list[dict] | None = None) -> str:
    """Format a single analysis result as markdown."""
    import json
    tag = ""
    reason_line = ""
    rationale_suffix = ""
    if not a["is_pre_registered"]:
        tag = " **[EXPLORATORY_POST_HOC]**"
        prov = _json_field(a, "provenance_json")
        reason = prov.get("amendment_reason", "")
        if reason:
            reason_line = f"  Reason: {reason}\n"
        r = prov.get("rationale", "")
        if r:
            rationale_suffix = f" — {r}"
    es = json.loads(a["effect_size_json"]) if a["effect_size_json"] else None
    es_str = f" ({es['metric']}={es['value']:.3f})" if es else ""
    lines = [
        f"**Test:** {a['test_name']}{rationale_suffix}{tag}\n",
    ]
    if reason_line:
        lines.append(reason_line)
    if a["statistic"] is not None:
        lines.append(f"  Statistic: {a['statistic']:.4f}\n")
    if a["p_value"] is not None:
        sig = " (significant)" if a["p_value"] < 0.05 else ""
        lines.append(f"  P-value: {a['p_value']:.4f}{sig}\n")
    if es:
        lines.append(f"  Effect size: {es['metric']} = {es['value']:.3f}\n")
    if a["ci_lower"] is not None and a["ci_upper"] is not None:
        lines.append(f"  95% CI: ({a['ci_lower']:.3f}, {a['ci_upper']:.3f})\n")
    if a["adjusted_p_value"] is not None:
        lines.append(f"  Corrected p-value: {a['adjusted_p_value']:.4f}\n")
    if "lr_test_p" in a and a["lr_test_p"] is not None:
        lines.append(f"  Likelihood-ratio test p: {a['lr_test_p']:.4f}\n")
    if "concordance_index" in a and a["concordance_index"] is not None:
        lines.append(f"  Concordance index: {a['concordance_index']:.3f}\n")

    # Per-covariate table for Cox PH models
    if covariate_rows:
        lines.append("\n  | Covariate | HR | 95% CI | p |\n")
        lines.append("  |---|---|---|---|\n")
        for cr in covariate_rows:
            hr = f"{cr['hr']:.3f}" if cr.get("hr") is not None else "—"
            cl = f"{cr['ci_lower']:.3f}" if cr.get("ci_lower") is not None else "—"
            cu = f"{cr['ci_upper']:.3f}" if cr.get("ci_upper") is not None else "—"
            wp = f"{cr['wald_p']:.4f}" if cr.get("wald_p") is not None else "—"
            lines.append(f"  | {cr['covariate']} | {hr} | ({cl}, {cu}) | {wp} |\n")

    lines.append("\n")
    return "".join(lines)


def generate_key_results(analyses) -> str:
    """Return factual analysis-status counts without editorial interpretation.

    Counts only pre-registered results (is_pre_registered=1).
    Post-hoc results are reported separately elsewhere.
    """
    if not analyses:
        return "**Key results:** [Summarise key results with reference to study objectives]."
    pre_reg = [a for a in analyses if a["is_pre_registered"]]
    if not pre_reg:
        return "**Key results:** No pre-registered tests were completed."
    statuses = [_json_field(a, "status_json").get("status", "completed") for a in pre_reg]
    n_clean = statuses.count("completed")
    n_violated = statuses.count("assumption_violation")
    n_skipped = statuses.count("skipped_assumption_violation")
    n_errors = len(statuses) - n_clean - n_violated - n_skipped
    parts = [f"Of {len(statuses)} pre-registered test(s), {n_clean} completed successfully"]
    if n_violated:
        parts.append(
            f"{n_violated} {'was' if n_violated == 1 else 'were'} completed "
            f"with {'an' if n_violated == 1 else ''} assumption violation{'s' if n_violated != 1 else ''} "
            f"noted"
        )
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
    analyses = _filter_superseded(analyses)
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

    # 4. Completed-but-violated tests (post-unmask diagnostics)
    for a in analyses:
        has_viol, summary, details = check_violation(dict(a))
        if has_viol:
            limitations.append(
                f"The {a['test_name']} model completed but violated "
                f"{'its' if len(details) == 1 else 'multiple'} post-unmask "
                f"assumption check{'s' if len(details) != 1 else ''}: "
                f"{summary}. "
                f"The reported estimates should be interpreted with this caveat."
            )

    # 4. Forced tests (ran despite warnings). Match warnings to the planned
    # test's variable rather than searching the test name for that variable.
    for a in analyses:
        status_data = json.loads(a["status_json"]) if a["status_json"] else {}
        if status_data.get("status") in ("completed", "assumption_violation"):
            if plan_data:
                all_tests = plan_data.get("planned_tests", []) + plan_data.get("post_hoc_tests", [])
                planned = [
                    t for t in all_tests
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
        if a["test_name"] in ("cox_proportional_hazards", "cox_ph_model"):
            sc = json.loads(a["sample_counts_json"]) if a["sample_counts_json"] else {}
            all_tests = (plan_data or {}).get("planned_tests", []) + (plan_data or {}).get("post_hoc_tests", [])
            planned = next((
                t for t in all_tests
                if t.get("test_name") == a["test_name"]
            ), None)
            n_events = sc.get("n_events")
            if n_events is None and planned:
                n_events = _event_count(conn, study_id, planned.get("variable_name", ""))
            if n_events is None:
                continue
            n_events = int(n_events)
            if a["test_name"] == "cox_ph_model":
                # For cox_ph_model, predictor count = treatment + covariates from the plan
                n_predictors = n_covariates + 1
            else:
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

    cur = conn.execute("SELECT * FROM analysis_results WHERE study_id=? ORDER BY id", (study_id,))
    analyses = _filter_superseded(cur.fetchall())

    # Pre-fetch per-covariate results for Cox PH models
    covariate_map: dict[int, list[dict]] = {}
    if analyses:
        result_ids = [a["id"] for a in analyses]
        placeholders = ",".join("?" for _ in result_ids)
        cur = conn.execute(
            f"SELECT * FROM analysis_covariate_results WHERE result_id IN ({placeholders}) ORDER BY id",
            result_ids,
        )
        for row in cur.fetchall():
            covariate_map.setdefault(row["result_id"], []).append({
                "covariate": row["covariate"],
                "hr": row["hr"],
                "ci_lower": row["ci_lower"],
                "ci_upper": row["ci_upper"],
                "wald_p": row["wald_p"],
                "coef": row["coef"],
                "se": row["se"],
                "z": row["z"],
            })

    conn.close()

    study_name = study["name"] if study else study_id
    study_type = (study["study_type"] or "cohort") if study else "cohort"

    # ── Title ──────────────────────────────────────────────────────────────
    title = f"Title: A {study_type.replace('_', ' ')} study of {study_name}"

    # ── Abstract ────────────────────────────────────────────────────────────
    n_vars = len(variables)
    pre_reg = [a for a in analyses if a["is_pre_registered"]]
    n_pre_reg = len(pre_reg)

    if n_pre_reg > 0:
        statuses = [
            (_json_field(a, "status_json").get("status", "completed"))
            for a in pre_reg
        ]
        n_completed = statuses.count("completed")
        results_line = (
            f"{n_completed} of {n_pre_reg} pre-registered test"
            f"{'s' if n_pre_reg != 1 else ''} completed"
            f"{'; see Results for details' if n_pre_reg > 0 else ''}."
        )
    else:
        results_line = "[Results summary — not yet computed or will be filled during hydration]"

    abstract = (
        f"**Background:** This study investigated {study_name} using a "
        f"{study_type.replace('_', ' ')} design.\n\n"
        f"**Methods:** {n_vars} variables were classified "
        f"({sum(1 for v in variables if v['role'] == 'baseline')} baseline, "
        f"{sum(1 for v in variables if v['role'] == 'outcome')} outcome). "
        f"Analysis included {n_pre_reg} pre-registered test"
        f"{'s' if n_pre_reg != 1 else ''}.\n\n"
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

    # ── Protocol Amendments subsection (for v2+ plans) ──────────────────
    ph_plans = sorted(
        p for p in locked_plans
        if p.name.startswith("study_plan.v") and p.name.endswith(".locked.json")
    )
    amendments_to_log = []
    for p in ph_plans:
        try:
            ver_str = p.stem.split(".v")[1].split(".")[0]
            version = int(ver_str)
        except (IndexError, ValueError):
            continue
        if version <= 1:
            continue
        try:
            import json
            data = json.loads(p.read_text())
        except Exception:
            continue
        amendment_reason = data.get("amendment_reason", "")
        if not amendment_reason:
            continue
        locked_at = data.get("locked_at", "?")
        amendments_to_log.append((version, locked_at, amendment_reason))

    if amendments_to_log:
        methods += "**Protocol Amendments:**\n\n"
        for version, locked_at, reason in amendments_to_log:
            methods += (
                f"- Amendment v{version}: {reason} "
                f"(recorded {locked_at[:10]})\n"
            )
        methods += "\n"

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
        # Flatten MultiIndex columns and row index
        if hasattr(tbl.columns, 'levels') or (
            len(tbl.columns) > 0 and isinstance(tbl.columns[0], tuple)
        ):
            tbl.columns = [
                str(c[-1]).strip() if isinstance(c, tuple) else str(c)
                for c in tbl.columns
            ]
        if hasattr(tbl.index, 'levels'):
            tbl.index = [
                (str(i[0]).strip() + ": " + str(i[1]).strip())
                if isinstance(i, tuple) and str(i[1]).strip()
                else str(i[0]).strip() if isinstance(i, tuple)
                else str(i)
                for i in tbl.index
            ]
        results_section += tbl.to_markdown(index=True) + "\n\n"
    else:
        results_section += "[Table 1 not yet computed — run `research-tool table1`]\n\n"

    if analyses:
        # Separate pre-registered and post-hoc results
        pre_reg = [a for a in analyses if a["is_pre_registered"]]
        post_hoc = [a for a in analyses if not a["is_pre_registered"]]

        # ── Primary Pre-Registered Analysis ──────────────────────────────
        if pre_reg:
            results_section += "### Primary Pre-Registered Analysis\n\n"
            for a in pre_reg:
                results_section += _format_analysis_result(a, covariate_map.get(a["id"]))

        # ── Post-Hoc / Exploratory Analyses ──────────────────────────────
        if post_hoc:
            results_section += "### Post-Hoc / Exploratory Analyses\n\n"
            for a in post_hoc:
                results_section += _format_analysis_result(a, covariate_map.get(a["id"]))
        else:
            results_section += "**Exploratory analyses:** None recorded.\n\n"
    else:
        results_section += (
            "**Primary analysis:** [Not yet computed — run `research-tool analyze`]\n\n"
            "**Exploratory analyses:** None recorded.\n\n"
        )

    # ── Discussion ──────────────────────────────────────────────────────────
    limitations_text = generate_limitations(study_id)
    key_results = generate_key_results(analyses) + "\n\n"

    # Count-disclosure sentence for post-hoc analyses
    if analyses:
        post_hoc_results = [a for a in analyses if not a["is_pre_registered"]]
        if post_hoc_results:
            n_ph = len(post_hoc_results)
            n_ph_sig = sum(
                1 for a in post_hoc_results
                if a["p_value"] is not None and a["p_value"] < 0.05
            )
            key_results += (
                f"{n_ph} additional post-hoc/exploratory analyses were performed; "
                f"{n_ph_sig} of the {n_ph} post-hoc analyses reached p < 0.05. "
                f"Pre-registered and post-hoc analyses were each corrected for "
                f"multiple comparisons within their own family, not pooled "
                f"together, consistent with their different evidentiary status.\n\n"
            )
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

    # ── Enforcement: post-hoc results must have a post-hoc section header ──
    if analyses and any(not a["is_pre_registered"] for a in analyses):
        if "Post-Hoc / Exploratory Analyses" not in draft:
            raise ValueError(
                "Post-hoc/exploratory results exist but the draft is missing "
                "the 'Post-Hoc / Exploratory Analyses' section header — "
                "this must never be silently omitted."
            )

    return draft


def write_draft(study_id: str) -> Path:
    """Generate and write the manuscript draft to disk."""
    draft = generate_draft(study_id)
    path = DATA_ROOT / study_id / "manuscript_draft.md"
    path.write_text(draft)
    return path
