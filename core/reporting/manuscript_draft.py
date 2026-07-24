"""Manuscript draft generator — template-based IMRaD assembly.

All numbers come from AnalysisResult objects via the stats engine.
EXPLORATORY_POST_HOC results are clearly marked wherever they appear.
No LLM is involved in v1 — plain Python string templates only.
"""

from __future__ import annotations
from datetime import datetime
from pathlib import Path

from core.database import get_connection, DATA_ROOT
from core.reporting.strobe_checklist import check_study


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
    abstract = (
        f"**Background:** This study investigated {study_name} using a "
        f"{study_type.replace('_', ' ')} design.\n\n"
        f"**Methods:** {n_vars} variables were classified "
        f"({sum(1 for v in variables if v['role'] == 'baseline')} baseline, "
        f"{sum(1 for v in variables if v['role'] == 'outcome')} outcome). "
        f"Analysis included {n_analyses} pre-registered tests.\n\n"
        f"**Results:** [Results summary — not yet computed or will be filled during hydration]\n\n"
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
    discussion = (
        "## Discussion\n\n"
        "**Key results:** [Summarise key results with reference to study objectives]\n\n"
        "**Limitations:** [Discuss limitations — e.g., sample size, confounding, "
        "missing data, generalisability]\n\n"
        "**Interpretation:** [Cautious overall interpretation]\n\n"
        "**Generalisability:** [Discuss external validity]\n\n"
    )

    # ── Other Information ──────────────────────────────────────────────────
    other = (
        "## Other Information\n\n"
        "**Funding:** [To be declared by authors. The research tool itself "
        "is unfunded open-source software.]\n\n"
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
