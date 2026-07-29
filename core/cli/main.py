"""CLI entrypoint — argparse dispatch.

Commands (Phase 1):
  new-study           create a new study
  ingest              load CSV/Excel
  classify-variables  tag columns with role and data type
  explore-baseline    peek at baseline data (outcomes masked)
"""

from __future__ import annotations
import argparse
import json
import math
import sys
import uuid
from datetime import datetime, timezone

from core.database import get_connection, init_db, study_dir, DATA_ROOT
from core.ingestion.csv_loader import load_file
from core.ingestion.variable_classifier import classify_variables_interactive, _classify_batch
from core.masking.gate import seal_outcomes, is_masked
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, lock_amendment, load_plan, unmask_study
from core.planning.test_selector import check_assumptions
from core.stats.descriptive import generate_table1
from core.stats.inferential import run_test
from core.reporting.strobe_checklist import generate_report
from core.reporting.manuscript_draft import write_draft
from core.reporting.flowchart import render_flowchart


def _model_field(model, key: str, default=""):
    """Read field from dict or dataclass."""
    if isinstance(model, dict):
        return model.get(key, default)
    return getattr(model, key, default)


def cmd_new_study(args: argparse.Namespace) -> None:
    study_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        "INSERT INTO studies (id, name, created_at, data_dir) VALUES (?, ?, ?, ?)",
        (study_id, args.name, now, str(study_dir(study_id))),
    )
    conn.commit()
    conn.close()
    # Create study dir
    study_dir(study_id).mkdir(parents=True, exist_ok=True)
    print(study_id)


def cmd_ingest(args: argparse.Namespace) -> None:
    na_vals = None
    if getattr(args, "na_values", None):
        na_vals = [v.strip() for v in args.na_values.split(",")]
        print(f"Treating values as missing: {na_vals}")
    force = getattr(args, "force_reingest", False)
    columns = load_file(args.study_id, args.file, na_values=na_vals, force=force)
    print(f"Ingested {len(columns)} columns: {', '.join(columns)}")


def cmd_classify_variables(args: argparse.Namespace) -> None:
    """Auto-classify and write to DB. In v1 this uses heuristics."""
    conn = get_connection(args.study_id)
    init_db(conn)
    raw_table = f"raw_{args.study_id}"
    cur = conn.execute(f"PRAGMA table_info({raw_table})")
    cols_info = cur.fetchall()
    columns = [r["name"] for r in cols_info if r["name"] != "row_id"]
    conn.close()

    suggestions = classify_variables_interactive(args.study_id, columns)
    _classify_batch(args.study_id, suggestions)

    # After classification, seal outcome values into shadow table
    seal_outcomes(args.study_id)

    # Query back to get DB-assigned IDs
    conn = get_connection(args.study_id)
    rows = conn.execute(
        "SELECT id, column_name, role, data_type FROM variables WHERE study_id=? ORDER BY id",
        (args.study_id,),
    ).fetchall()
    conn.close()
    for r in rows:
        print(f"  {r['id']:<4} {r['column_name']:<30} → {r['role']:<10} {r['data_type']}")


def cmd_explore_baseline(args: argparse.Namespace) -> None:
    """Show summary of baseline variables only (outcomes masked — physically NULL)."""
    conn = get_connection(args.study_id)
    cur = conn.execute("SELECT column_name, role, data_type FROM variables WHERE study_id=?", (args.study_id,))
    vars_info = cur.fetchall()

    baseline_cols = [r["column_name"] for r in vars_info if r["role"] == "baseline"]
    if not baseline_cols:
        print("No baseline variables classified yet.")
        conn.close()
        return

    col_list = ", ".join(baseline_cols)
    raw_table = f"raw_{args.study_id}"
    cur2 = conn.execute(f"SELECT {col_list} FROM {raw_table}")
    rows = cur2.fetchall()
    conn.close()
    print(f"Baseline variables ({len(baseline_cols)}): {', '.join(baseline_cols)}")
    print(f"  {len(rows)} rows available")
    if args.head:
        for r in rows[:args.head]:
            r = {k: v for k, v in dict(r).items() if k in baseline_cols}
            print(r)


def cmd_list_variables(args: argparse.Namespace) -> None:
    """List all classified variables with their DB IDs."""
    conn = get_connection(args.study_id)
    rows = conn.execute(
        "SELECT id, column_name, role, data_type FROM variables WHERE study_id=? ORDER BY id",
        (args.study_id,),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"No variables classified for study '{args.study_id}'. Run 'classify-variables' first.")
        return
    print(f"  {'ID':<4} {'Column':<30} {'Role':<12} {'Type'}")
    print(f"  {'──':<4} {'──────':<30} {'────':<12} ────")
    for r in rows:
        print(f"  {r['id']:<4} {r['column_name']:<30} {r['role']:<12} {r['data_type']}")


def cmd_plan(args: argparse.Namespace) -> None:
    """Declare a study plan."""

    # Load from JSON if --from-json is provided
    json_path = getattr(args, "from_json", None)
    if json_path:
        from pathlib import Path
        import json as _json
        data = _json.loads(Path(json_path).read_text())
        # Override args with JSON values (CLI flags still win if provided)
        if "comparison" in data:
            args.comparison = data["comparison"]
        if "outcome_var_ids" in data:
            v = data["outcome_var_ids"]
            args.outcome_var_ids = ",".join(str(x) for x in v) if isinstance(v, list) else v
        if "study_type" in data:
            args.study_type = data["study_type"]
        if "tests" in data:
            args.tests = data["tests"]
        if "covariates" in data:
            v = data["covariates"]
            args.covariates = ",".join(str(x) for x in v) if isinstance(v, list) else v
        if "cox_ph_models" in data:
            args.cox_ph_models = data["cox_ph_models"]
        if "interaction_terms" in data:
            args.interaction_terms = data["interaction_terms"]
        if "matching_criteria" in data:
            v = data["matching_criteria"]
            args.matching_criteria = ",".join(str(x) for x in v) if isinstance(v, list) else v
        if "overrides" in data:
            args.overrides = data["overrides"]

    outcome_ids = [int(x) for x in args.outcome_var_ids.split(",")]
    tests = []
    if args.tests:
        for t in args.tests:
            if isinstance(t, dict):
                # Native JSON dict (from --from-json)
                tests.append({
                    "variable_name": t.get("variable_name", ""),
                    "test_name": t.get("test_name", ""),
                    "rationale": t.get("rationale", ""),
                })
            else:
                parts = t.split(":", 2)
                # Format: variable_name:test_name:[rationale]
                var_name = parts[0] if len(parts) > 0 else ""
                test_name = parts[1] if len(parts) > 1 else ""
                rationale = parts[2] if len(parts) > 2 else ""
                tests.append({
                    "variable_name": var_name,
                    "test_name": test_name,
                    "rationale": rationale,
                })
    covariates = [int(x) for x in args.covariates.split(",")] if args.covariates else []
    matching_criteria = [int(x) for x in args.matching_criteria.split(",")] if getattr(args, "matching_criteria", None) else []

    # Parse Cox PH models
    cox_ph_models = []
    if getattr(args, "cox_ph_models", None):
        for m in args.cox_ph_models:
            if isinstance(m, dict):
                # Native JSON dict (from --from-json)
                cox_ph_models.append({
                    "model_name": m.get("model_name", ""),
                    "survival_time_col": m.get("survival_time_col", ""),
                    "event_col": m.get("event_col", ""),
                    "primary_treatment_col": m.get("primary_treatment_col", ""),
                    "covariate_cols": m.get("covariate_cols", []),
                    "rationale": m.get("rationale", ""),
                    "interaction_terms": m.get("interaction_terms", []),
                })
            else:
                # Format: model_name:survival_time_col:event_col:primary_treatment_col[:covariate_cols[:rationale]]
                parts = m.split(":")
                if len(parts) < 4:
                    print(f"Error: invalid Cox PH model format '{m}'. Use model_name:survival_time_col:event_col:primary_treatment_col[:covariate_cols[:rationale]]", file=sys.stderr)
                    sys.exit(1)
                model_name = parts[0]
                survival_time_col = parts[1]
                event_col = parts[2]
                primary_treatment_col = parts[3]
                if len(parts) >= 6:
                    covariate_cols = [c.strip() for c in parts[4].split(",")] if parts[4] else []
                    rationale = parts[5]
                elif len(parts) == 5:
                    covariate_cols = [c.strip() for c in parts[4].split(",")] if parts[4] else []
                    rationale = ""
                else:
                    covariate_cols = []
                    rationale = ""
                cox_ph_models.append({
                    "model_name": model_name,
                    "survival_time_col": survival_time_col,
                    "event_col": event_col,
                    "primary_treatment_col": primary_treatment_col,
                    "covariate_cols": covariate_cols,
                    "rationale": rationale,
                    "interaction_terms": [],
                })

    # Parse interaction terms and attach to the named model
    if getattr(args, "interaction_terms", None):
        for spec in args.interaction_terms:
            if isinstance(spec, dict):
                model_name = spec.get("model_name", "")
                var_a = spec.get("var_a", "")
                var_b = spec.get("var_b", "")
            else:
                parts = spec.split(":")
                if len(parts) != 3:
                    print(f"Error: invalid interaction term format '{spec}'. Use model_name:var_a:var_b", file=sys.stderr)
                    sys.exit(1)
                model_name, var_a, var_b = parts
            found = False
            for m in cox_ph_models:
                if m["model_name"] == model_name:
                    m["interaction_terms"].append([var_a, var_b])
                    found = True
                    break
            if not found:
                print(f"Error: interaction term '{spec}' references model '{model_name}' which was not declared with --cox-ph-models", file=sys.stderr)
                sys.exit(1)

    # Role overrides are plan metadata. They do not modify the ingested rows
    # or erase the classifier's original suggestion.
    overrides: dict[int, str] = {}
    override_audit: list[dict] = []
    for raw_override in getattr(args, "overrides", []) or []:
        parts = raw_override.split(":")
        if len(parts) != 2 or not parts[0].startswith("id=") or not parts[1].startswith("role="):
            print(f"Error: invalid override '{raw_override}'. Use id=<variable_id>:role=<role>.", file=sys.stderr)
            sys.exit(1)
        try:
            variable_id = int(parts[0][len("id="):])
        except ValueError:
            print(f"Error: invalid override variable ID in '{raw_override}'.", file=sys.stderr)
            sys.exit(1)
        role = parts[1][len("role="):]
        if role not in {"baseline", "outcome", "covariate"}:
            print(f"Error: invalid override role '{role}'. Choose baseline, outcome, or covariate.", file=sys.stderr)
            sys.exit(1)
        if variable_id in overrides:
            print(f"Error: duplicate override for variable ID {variable_id}.", file=sys.stderr)
            sys.exit(1)
        overrides[variable_id] = role
        override_audit.append({"variable_id": variable_id, "role": role})

    if overrides and list(DATA_ROOT.glob(f"{args.study_id}/study_plan.v*.locked.json")):
        print("Error: cannot apply role overrides after the study plan is locked. Create a new plan version before locking.", file=sys.stderr)
        sys.exit(1)

    # ── Validate classified variables ─────────────────────────────────────
    conn = get_connection(args.study_id)
    cur = conn.execute(
        "SELECT id, column_name, role FROM variables WHERE study_id=?",
        (args.study_id,),
    )
    classified = {r["id"]: {"name": r["column_name"], "role": r["role"]} for r in cur.fetchall()}
    conn.close()

    if not classified:
        print(
            f"Error: no variables classified for study '{args.study_id}'. "
            f"Run 'research-tool classify-variables {args.study_id}' after ingesting data first.",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown_overrides = [str(i) for i in overrides if i not in classified]
    if unknown_overrides:
        print(f"Error: override variable ID(s) {', '.join(unknown_overrides)} not found among classified variables.", file=sys.stderr)
        sys.exit(1)
    for variable_id, role in overrides.items():
        classified[variable_id]["role"] = role

    # outcome-var-ids
    missing_outcome = [str(i) for i in outcome_ids if i not in classified]
    if missing_outcome:
        print(
            f"Error: outcome variable ID(s) {', '.join(missing_outcome)} not found "
            f"among classified variables. Run 'research-tool classify-variables' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    wrong_role = [str(i) for i in outcome_ids if classified[i]["role"] != "outcome"]
    if wrong_role:
        print(
            f"Error: variable ID(s) {', '.join(wrong_role)} "
            f"({'/'.join(classified[int(i)]['name'] for i in wrong_role)}) "
            f"is classified as '{classified[int(wrong_role[0])]['role']}', not 'outcome'. "
            f"Use a variable classified as outcome, or reclassify with "
            f"'research-tool classify-variables {args.study_id}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # covariates
    missing_cov = [str(i) for i in covariates if i not in classified]
    if missing_cov:
        print(
            f"Error: covariate variable ID(s) {', '.join(missing_cov)} not found "
            f"among classified variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate Cox PH model columns exist and are classified correctly
    if cox_ph_models:
        conn = get_connection(args.study_id)
        cur = conn.execute(
            "SELECT column_name, role, data_type FROM variables WHERE study_id=?",
            (args.study_id,),
        )
        var_catalog = {r["column_name"]: {"role": r["role"], "data_type": r["data_type"]} for r in cur.fetchall()}
        conn.close()

        for model in cox_ph_models:
            model_name = model["model_name"]
            survival_time_col = model["survival_time_col"]
            event_col = model["event_col"]
            primary_treatment_col = model["primary_treatment_col"]
            covariate_cols = model["covariate_cols"]

            # Check survival_time_col
            if survival_time_col not in var_catalog:
                print(f"Error: Cox PH model '{model_name}': survival_time_col '{survival_time_col}' not found among classified variables.", file=sys.stderr)
                sys.exit(1)
            if var_catalog[survival_time_col]["data_type"] != "time_to_event":
                print(f"Error: Cox PH model '{model_name}': survival_time_col '{survival_time_col}' is not classified as time_to_event (got {var_catalog[survival_time_col]['data_type']}).", file=sys.stderr)
                sys.exit(1)

            # Check event_col
            if event_col not in var_catalog:
                print(f"Error: Cox PH model '{model_name}': event_col '{event_col}' not found among classified variables.", file=sys.stderr)
                sys.exit(1)
            if var_catalog[event_col]["role"] != "outcome":
                print(f"Error: Cox PH model '{model_name}': event_col '{event_col}' must be classified as outcome.", file=sys.stderr)
                sys.exit(1)

            # Check primary_treatment_col
            if primary_treatment_col not in var_catalog:
                print(f"Error: Cox PH model '{model_name}': primary_treatment_col '{primary_treatment_col}' not found among classified variables.", file=sys.stderr)
                sys.exit(1)

            # Check covariate_cols
            for cov in covariate_cols:
                if cov not in var_catalog:
                    print(f"Error: Cox PH model '{model_name}': covariate '{cov}' not found among classified variables.", file=sys.stderr)
                    sys.exit(1)

            # Check interaction term columns
            for pair in model.get("interaction_terms", []):
                for var in pair:
                    if var not in var_catalog:
                        print(f"Error: Cox PH model '{model_name}': interaction term variable '{var}' not found among classified variables.", file=sys.stderr)
                        sys.exit(1)

            # Validate event_col is binary (0/1)
            conn2 = get_connection(args.study_id)
            raw_table = f"raw_{args.study_id}"
            cur2 = conn2.execute(f'SELECT DISTINCT "{event_col}" AS val FROM {raw_table} WHERE "{event_col}" IS NOT NULL')
            event_vals = [str(r["val"]) for r in cur2.fetchall() if r["val"] is not None and str(r["val"]).strip() != ""]
            non_binary = [v for v in event_vals if v not in ("0", "1")]
            if non_binary:
                print(f"Error: Cox PH model '{model_name}': event_col '{event_col}' must be binary (0/1), found values: {non_binary}", file=sys.stderr)
                conn2.close()
                sys.exit(1)
            # Validate survival_time_col is numeric and non-negative
            cur2 = conn2.execute(f'SELECT "{survival_time_col}" AS val FROM {raw_table} WHERE "{survival_time_col}" IS NOT NULL')
            for r in cur2.fetchall():
                v = r["val"]
                if v is None or str(v).strip() == "":
                    continue
                try:
                    fv = float(v)
                    if not math.isfinite(fv):
                        print(f"Error: Cox PH model '{model_name}': survival_time_col '{survival_time_col}' has non-finite value '{v}'", file=sys.stderr)
                        conn2.close()
                        sys.exit(1)
                    if fv < 0:
                        print(f"Error: Cox PH model '{model_name}': survival_time_col '{survival_time_col}' has negative value ({v})", file=sys.stderr)
                        conn2.close()
                        sys.exit(1)
                except (ValueError, TypeError):
                    print(f"Error: Cox PH model '{model_name}': survival_time_col '{survival_time_col}' is not numeric (found '{v}')", file=sys.stderr)
                    conn2.close()
                    sys.exit(1)
            conn2.close()

    # Check assumptions before building plan
    # Enrich tests with covariate count for Cox EPV check
    n_covariates = len(covariates)
    for t in tests:
        t["n_covariates"] = n_covariates

    # Collect all warnings including Cox PH model warnings
    all_warnings = check_assumptions(args.study_id, tests)

    # Add Cox PH model warnings
    from core.planning.test_selector import check_cox_ph_model_assumptions
    cox_model_warnings = check_cox_ph_model_assumptions(args.study_id, cox_ph_models)
    all_warnings.extend(cox_model_warnings)

    for w in all_warnings:
        print(w, file=sys.stderr)

    # Warn if a matching criterion overlaps the comparison variable
    if matching_criteria:
        comp_lower = args.comparison.lower().replace("_", " ")
        for vid, info in classified.items():
            col_name = info["name"]
            col_normalized = col_name.lower().replace("_", " ")
            if col_normalized in comp_lower and vid in matching_criteria:
                print(
                    f"Warning: variable '{col_name}' is declared as both a "
                    f"matching criterion and part of the primary comparison. "
                    f"Matching on the comparison variable can obscure the effect "
                    f"being studied — confirm this is intentional.",
                    file=sys.stderr,
                )

    # Map warnings to test names for enforcement at analyze time
    warning_map: dict[str, str] = {}
    for test in tests:
        tn = test.get("test_name", "")
        var = test.get("variable_name", "")
        for w in all_warnings:
            if f"{tn} on '{var}'" in w:
                warning_map[var] = w

    # Also map Cox PH model warnings
    for model in cox_ph_models:
        model_name = model["model_name"]
        for w in cox_model_warnings:
            if model_name in w:
                warning_map[f"cox_ph_model:{model_name}"] = w

    plan = StudyPlan(
        study_id=args.study_id,
        study_type=args.study_type,
        primary_comparison=args.comparison,
        primary_outcome_variable_ids=outcome_ids,
        planned_tests=tests,
        covariates=covariates,
        matching_criteria=matching_criteria,
        warnings=warning_map,
        role_overrides=overrides,
        audit={"role_overrides": override_audit},
        cox_ph_models=cox_ph_models,
    )

    # Write provisional plan
    mcd = DATA_ROOT / args.study_id
    mcd.mkdir(parents=True, exist_ok=True)
    prov_path = mcd / "study_plan.provisional.json"
    import json
    prov_path.write_text(json.dumps(plan.to_dict(), indent=2))
    print(f"Provisional plan saved to {prov_path}")
    print(f"Run 'research-tool lock {args.study_id}' to lock it (immutable).")


def cmd_lock(args: argparse.Namespace) -> None:
    """Lock the study plan — writes immutable versioned JSON snapshot."""
    prov_path = DATA_ROOT / args.study_id / "study_plan.provisional.json"
    if not prov_path.exists():
        print("No provisional plan found. Run 'research-tool plan' first.",
              file=sys.stderr)
        sys.exit(1)

    # ── Block locking on duplicate patient IDs unless explicitly allowed ──
    if not getattr(args, "allow_duplicate_ids", False):
        from core.ingestion.csv_loader import find_duplicate_patient_ids
        dupes = find_duplicate_patient_ids(args.study_id)
        if dupes:
            dupe_desc = ", ".join("'" + str(pid) + "' (" + str(n) + "x)" for pid, n in dupes)
            print(
                f"Error: duplicate patient identifiers found — {dupe_desc}. "
                f"The study cannot be locked because duplicate IDs violate the "
                f"independence assumption of all planned tests. "
                f"Either:\n"
                f"  (a) remove or deduplicate the offending rows and re-ingest, or\n"
                f"  (b) pass --allow-duplicate-ids to lock if this is a "
                f"genuinely longitudinal/repeated-measures design.",
                file=sys.stderr,
            )
            sys.exit(1)

    import json
    plan_data = json.loads(prov_path.read_text())
    plan = StudyPlan.from_dict(plan_data)
    path = lock_plan(args.study_id, plan)
    print(f"Plan locked: {path}")


def cmd_amend(args: argparse.Namespace) -> None:
    """Amend a locked study plan (pre-unmask or post-hoc)."""
    reason = getattr(args, "reason", "")
    if not reason:
        print("Error: --reason is required for any amendment.", file=sys.stderr)
        sys.exit(1)

    is_post_hoc = getattr(args, "post_hoc", False)

    # Parse tests from --test flags
    tests = []
    raw_tests = getattr(args, "tests", []) or []
    for t in raw_tests:
        parts = t.split(":", 2)
        var_name = parts[0] if len(parts) > 0 else ""
        test_name = parts[1] if len(parts) > 1 else ""
        rationale = parts[2] if len(parts) > 2 else ""
        tests.append({"variable_name": var_name, "test_name": test_name, "rationale": rationale})

    try:
        if is_post_hoc:
            path = lock_amendment(
                args.study_id,
                amendment_reason=reason,
                post_hoc_tests=tests,
            )
            print(f"Post-hoc amendment saved as version {path.stem.split('.v')[1]}: {reason}")
        else:
            path = lock_amendment(
                args.study_id,
                amendment_reason=reason,
                planned_tests=tests,
            )
            print(f"Amendment saved as version {path.stem.split('.v')[1]}: {reason}")
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_unmask(args: argparse.Namespace) -> None:
    """Unmask outcome data (irreversible).  Gates on pre-unmask diagnostics."""
    # Load plan to check diagnostic results
    from core.planning.lock import load_plan
    from core.database import get_connection
    from datetime import datetime, timezone
    import json

    try:
        plan = load_plan(args.study_id)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading plan: {e}", file=sys.stderr)
        print("Unmask requires a locked plan. Run 'research-tool lock' first.",
              file=sys.stderr)
        sys.exit(1)

    diag_results = getattr(plan, "diagnostic_results", [])
    pre_unmask = [d for d in diag_results if d.get("stage") == "pre_unmask"]

    forceable_fails = [d for d in pre_unmask if d.get("status") == "fail" and d.get("forceable") == True]
    non_forceable_fails = [d for d in pre_unmask if d.get("status") == "fail" and d.get("forceable") != True]

    # Tier 1: non-forceable fails → always block, no flag bypasses
    if non_forceable_fails:
        print(
            f"Error: cannot unmask — {len(non_forceable_fails)} non-overridable "
            f"diagnostic check(s) failed:\n",
            file=sys.stderr,
        )
        for d in non_forceable_fails:
            print(f"  [{d['check_name']}] {d['message']}", file=sys.stderr)
        print(
            "\nThese checks assess fundamental data integrity for the planned analyses.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Tier 2: forceable fails → block unless user provides override justification
    force_justification = getattr(args, "force", None)
    if forceable_fails:
        if not force_justification:
            print(
                f"Error: {len(forceable_fails)} diagnostic check(s) require review:\n",
                file=sys.stderr,
            )
            for d in forceable_fails:
                print(f"  [{d['check_name']}] {d['message']}", file=sys.stderr)
            print(
                "\nThese do not block unmask unconditionally, but require a documented "
                "justification.\n"
                "Provide one with:  research-tool unmask <study_id> --force '<your reason>'\n"
                "The justification is permanently recorded in the study's audit trail.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Log the override to the DB
        conn = get_connection(args.study_id)
        override_events = []
        for d in forceable_fails:
            override_events.append({
                "check_name": d["check_name"],
                "message": d["message"],
                "justification": force_justification,
                "overridden_at": datetime.now(timezone.utc).isoformat(),
            })
        existing = conn.execute(
            "SELECT unmask_audit_json FROM studies WHERE id=?",
            (args.study_id,),
        ).fetchone()
        audit_log = []
        if existing and existing["unmask_audit_json"]:
            audit_log = json.loads(existing["unmask_audit_json"])
        audit_log.extend(override_events)
        conn.execute(
            "UPDATE studies SET unmask_audit_json=? WHERE id=?",
            (json.dumps(audit_log), args.study_id),
        )
        conn.commit()
        conn.close()

    unmask_study(args.study_id)
    msg = "Study unmasked. Outcome data now visible."
    if forceable_fails:
        msg += (
            f" {len(forceable_fails)} diagnostic override(s) recorded "
            f"in unmask_audit_json."
        )
    print(msg)


def cmd_table1(args: argparse.Namespace) -> None:
    """Generate Table 1.

    When a locked plan exists, defaults to stratified output grouped by the
    plan's comparison variable (treatment_arm).  Pass --overall for the
    unstratified single-column view.  No baseline p-values (CONSORT).
    """
    groupby = args.groupby
    if groupby is None and not getattr(args, "overall", False):
        locked_paths = sorted(DATA_ROOT.glob(f"{args.study_id}/study_plan.v*.locked.json"))
        if locked_paths:
            groupby = "treatment_arm"

    tbl = generate_table1(args.study_id, groupby=groupby)
    if tbl.empty if hasattr(tbl, 'empty') else len(tbl) == 0:
        print("Table 1 is empty. Classify variables first.")
        return
    print(tbl.to_string())


def cmd_analyze(args: argparse.Namespace) -> None:
    """Run pre-registered analyses from locked plan."""
    if is_masked(args.study_id):
        print("Study is still masked. Unmask first with 'research-tool unmask'.",
              file=sys.stderr)
        sys.exit(1)
    try:
        plan = load_plan(args.study_id)
    except FileNotFoundError:
        print("No locked plan found. Lock a plan first.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(args.study_id)
    raw_table = f"raw_{args.study_id}"
    import pandas as pd
    df = pd.read_sql_query(f"SELECT * FROM {raw_table}", conn)
    conn.close()

    from core.stats.multiple_comparisons import correct
    from datetime import datetime, timezone
    import json

    results = []
    conn = get_connection(args.study_id)
    init_db(conn)

    # Parse --force, --rerun flags
    force = getattr(args, "force", False)
    rerun = getattr(args, "rerun", False)

    # Build combined test list: planned tests (pre-registered) + post-hoc tests (exploratory)
    # The --post-hoc flag on analyze is legacy; analyze now runs both lists automatically.
    test_runs: list[tuple[list[dict], int, str]] = []  # (tests, is_pre_registered, label)
    if plan.planned_tests:
        test_runs.append((plan.planned_tests, 1, "pre-registered"))
    if plan.post_hoc_tests:
        test_runs.append((plan.post_hoc_tests, 0, "post-hoc"))

    # Enforce assumption warnings from plan time
    plan_warnings = getattr(plan, "warnings", {})

    # Run all test lists
    for test_list, is_pre_registered, _label in test_runs:
        for t in test_list:
            var_name = t.get("variable_name", "")
            test_name = t.get("test_name", "")
            test_rationale = t.get("rationale", "")
            if not test_name or not var_name:
                continue

            # Dedup: skip if completed result already exists for this exact test
            if not rerun:
                existing = conn.execute(
                    """SELECT id, computed_at FROM analysis_results
                       WHERE study_id=? AND test_name=? AND variable_ids_used=? AND
                             study_plan_version=? AND is_pre_registered=?
                             AND json_extract(status_json, '$.status') = 'completed'
                             AND superseded_previous_result_id IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (args.study_id, test_name, json.dumps([]),
                     plan.version, is_pre_registered),
                ).fetchone()
                if existing:
                    print(
                        f"Test '{test_name}' on '{var_name}' already completed "
                        f"under plan v{plan.version} (result id {existing['id']}, "
                        f"computed {existing['computed_at']}). "
                        f"Skipping — use --rerun to force recomputation."
                    )
                    continue

            # For post-hoc tests, scan forward to find which amendment version
            # first declared this specific test (test_name + rationale match)
            # and record its amendment_reason and declaring version.
            ph_reason = ""
            declaring_version = plan.version
            if is_pre_registered == 0:
                for v in range(1, plan.version + 1):
                    try:
                        p = load_plan(args.study_id, version=v)
                    except Exception:
                        continue
                    reason = getattr(p, "amendment_reason", "")
                    if not reason:
                        continue
                    for pt in p.post_hoc_tests:
                        if pt.get("test_name") == test_name and (
                            not test_rationale
                            or not pt.get("rationale", "")
                            or pt.get("rationale", "") == test_rationale
                        ):
                            ph_reason = reason
                            declaring_version = v
                            break
                    if ph_reason:
                        break
            if var_name in plan_warnings and not force:
                # Suggest the appropriate alternative based on test type
                alt_test = {"chi_square": "fisher_exact",
                            "t_test": "mann_whitney",
                            "paired_t_test": "wilcoxon_signed_rank"}.get(test_name, "")
                alt_hint = ""
                if alt_test:
                    alt_hint = (
                        f"Redeclare the plan with an alternative test via:\n"
                        f"  research-tool plan ... --test \"{var_name}:{alt_test}:...\"\n"
                    )
                print(
                    f"Skipping '{test_name}' on '{var_name}' — "
                    f"plan time warning recorded:\n"
                    f"  {plan_warnings[var_name]}\n"
                    f"Use '--force' to run despite warnings, or {alt_hint}",
                    file=sys.stderr,
                )
                # Record the skipped test in the DB so the audit trail is complete
                skipped_uro = {
                    "test_name": test_name,
                    "is_pre_registered": is_pre_registered,
                    "statistic": None,
                    "p_value": None,
                    "ci_lower": None,
                    "ci_upper": None,
                    "params": {},
                    "effect_size": None,
                    "sample_counts": {"n_total": len(df), "n_analyzed": 0, "n_excluded": len(df)},
                    "status": "skipped_assumption_violation",
                    "reason": plan_warnings[var_name],
                    "rationale": test_rationale,
                    "amendment_reason": ph_reason,
                    "declaring_version": declaring_version,
                }
                results.append(skipped_uro)
                continue

            kwargs = {"outcome_col": var_name, "group_col": "treatment_arm"}
            if test_name in ("kaplan_meier_logrank", "cox_proportional_hazards"):
                # Convention: time_to_event variable named e.g. "pfs_days"
                # has its event indicator in a column with the same prefix + "_event"
                # or the same prefix minus "_days"/"_months" + "_event"
                prefix = var_name.replace("_days", "").replace("_months", "").replace("_time", "")
                kwargs["time_col"] = var_name
                kwargs["event_col"] = f"{prefix}_event"
            result = run_test(test_name, df, **kwargs)
            result["status"] = "completed"
            result["reason"] = None
            result["rationale"] = t.get("rationale", "")
            result["variable_name"] = var_name
            result["is_pre_registered"] = is_pre_registered
            result["amendment_reason"] = ph_reason
            result["declaring_version"] = declaring_version
            if result.get("params", {}).get("error"):
                result["status"] = "error"
                result["reason"] = result["params"]["error"]
            results.append(result)

    # Run Cox PH models if declared in the plan
    cox_ph_models = getattr(plan, "cox_ph_models", [])
    if cox_ph_models:
        for model in cox_ph_models:
            model_name = _model_field(model, "model_name")
            survival_time_col = _model_field(model, "survival_time_col")
            event_col = _model_field(model, "event_col")
            primary_treatment_col = _model_field(model, "primary_treatment_col")
            covariate_cols = _model_field(model, "covariate_cols", [])
            model_rationale = _model_field(model, "rationale")

            if not model_name or not survival_time_col:
                continue

            # Check for assumption warnings
            warning_key = f"cox_ph_model:{model_name}"
            if warning_key in plan_warnings and not force:
                print(
                    f"Skipping Cox PH model '{model_name}' — "
                    f"plan time warning recorded:\n"
                    f"  {plan_warnings[warning_key]}\n"
                    f"Use '--force' to run despite warnings.",
                    file=sys.stderr,
                )
                skipped_uro = {
                    "test_name": "cox_ph_model",
                    "statistic": None,
                    "p_value": None,
                    "ci_lower": None,
                    "ci_upper": None,
                    "params": {},
                    "effect_size": None,
                    "sample_counts": {"n_total": len(df), "n_analyzed": 0, "n_excluded": len(df)},
                    "status": "skipped_assumption_violation",
                    "reason": plan_warnings[warning_key],
                    "rationale": model_rationale,
                    "amendment_reason": "",
                    "declaring_version": plan.version,
                }
                results.append(skipped_uro)
                continue

            # Dedup check
            if not rerun:
                existing = conn.execute(
                    """SELECT id, computed_at FROM analysis_results
                       WHERE study_id=? AND test_name=? AND variable_ids_used=? AND
                             study_plan_version=? AND is_pre_registered=?
                             AND json_extract(status_json, '$.status') = 'completed'
                             AND superseded_previous_result_id IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (args.study_id, "cox_ph_model", json.dumps([]),
                     plan.version, 1),  # Cox PH models are always pre-registered
                ).fetchone()
                if existing:
                    print(
                        f"Cox PH model '{model_name}' already completed "
                        f"under plan v{plan.version} (result id {existing['id']}, "
                        f"computed {existing['computed_at']}). "
                        f"Skipping — use --rerun to force recomputation."
                    )
                    continue

            # Look up variable types from classifier
            cur3 = conn.execute(
                "SELECT column_name, data_type FROM variables WHERE study_id=?",
                (args.study_id,),
            )
            var_types = {r["column_name"]: r["data_type"] for r in cur3.fetchall()}

            interaction_terms = _model_field(model, "interaction_terms", [])

            # Run the multivariable Cox PH model
            result = run_test(
                "cox_ph_model",
                df,
                outcome_col=survival_time_col,
                group_col=primary_treatment_col,
                time_col=survival_time_col,
                event_col=event_col,
                covariates=covariate_cols,
                var_types=var_types,
                interaction_terms=interaction_terms,
            )
            result["status"] = "completed"
            result["reason"] = None
            result["rationale"] = model_rationale
            result["variable_name"] = model_name
            result["test_name"] = "cox_ph_model"
            result["amendment_reason"] = ""
            result["declaring_version"] = plan.version
            if result.get("params", {}).get("error"):
                result["status"] = "error"
                result["reason"] = result["params"]["error"]
            results.append(result)

    # Promote completed Cox PH results with Schoenfeld violations BEFORE
    # multiple-testing correction (M3 fix: correction must see final statuses)
    for r in results:
        if r.get("status") != "completed":
            continue
        params = r.get("params", {})
        ph_diag = params.get("assumption_diagnostics") if params else None
        if ph_diag:
            covs = ph_diag.get("covariates", [])
            violations = [c for c in covs if c.get("p_value", 1) < 0.05]
            if violations:
                names = "; ".join(f"{v['covariate']} (p={v['p_value']:.4f})" for v in violations)
                r["status"] = "assumption_violation"
                r["reason"] = f"Proportional hazards assumption violated: {names}"

    # Apply multiple-testing correction to completed tests only
    completed = [r for r in results if r["status"] == "completed"]
    completed_p = [r["p_value"] for r in completed if r["p_value"] is not None]
    if len(completed_p) > 1:
        corrected = correct(completed_p)
        for r, cp in zip(completed, corrected):
            r["adjusted_p_value"] = cp
    elif len(completed_p) == 1:
        completed[0]["adjusted_p_value"] = completed[0]["p_value"]
    # Skipped/error results keep adjusted_p_value = None (already set by _uro)

    # Track superseded results for --rerun
    supersede_map: dict[str, int] = {}
    if rerun:
        for test_list, is_pre_registered, _label in test_runs:
            for t in test_list:
                var_name = t.get("variable_name", "")
                test_name = t.get("test_name", "")
                if not test_name or not var_name:
                    continue
                existing = conn.execute(
                    """SELECT id FROM analysis_results
                       WHERE study_id=? AND test_name=? AND variable_ids_used=? AND
                             study_plan_version=? AND is_pre_registered=?
                             AND json_extract(status_json, '$.status') = 'completed'
                             AND superseded_previous_result_id IS NULL
                       ORDER BY id DESC LIMIT 1""",
                    (args.study_id, test_name, json.dumps([]),
                     plan.version, is_pre_registered),
                ).fetchone()
                if existing:
                    supersede_map[test_name] = existing["id"]

        # Also check for Cox PH models
        if cox_ph_models:
            existing = conn.execute(
                """SELECT id FROM analysis_results
                   WHERE study_id=? AND test_name=? AND variable_ids_used=? AND
                         study_plan_version=? AND is_pre_registered=?
                         AND json_extract(status_json, '$.status') = 'completed'
                         AND superseded_previous_result_id IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (args.study_id, "cox_ph_model", json.dumps([]),
                 plan.version, 1),
            ).fetchone()
            if existing:
                supersede_map["cox_ph_model"] = existing["id"]

    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        status_record = {"status": r.get("status", "completed")}
        if r.get("reason"):
            status_record["reason"] = r["reason"]

        stored_version = r.get("declaring_version", plan.version)
        prov = {"plan_version": stored_version}
        ph_reason = r.get("amendment_reason", "")
        if ph_reason:
            prov["amendment_reason"] = ph_reason
        rationale = r.get("rationale", "")
        if rationale:
            prov["rationale"] = rationale
        superseded_id = supersede_map.get(r.get("test_name", ""))
        params = r.get("params", {})
        lr_test_p = params.get("lr_test_p_value") if params else None
        concordance = params.get("concordance_index") if params else None
        ph_diag = params.get("assumption_diagnostics") if params else None
        cursor = conn.execute(
            """INSERT INTO analysis_results
               (study_id, study_plan_version, variable_ids_used, test_name,
                statistic, p_value, adjusted_p_value, ci_lower, ci_upper,
                effect_size_json, sample_counts_json, status_json,
                is_pre_registered, provenance_json, computed_at,
                superseded_previous_result_id,
                lr_test_p, concordance_index, ph_diagnostics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (args.study_id, stored_version, json.dumps([]),
             r["test_name"], r["statistic"], r["p_value"],
             r.get("adjusted_p_value"), r.get("ci_lower"), r.get("ci_upper"),
             json.dumps(r["effect_size"]) if r.get("effect_size") else None,
             json.dumps(r["sample_counts"]) if r.get("sample_counts") else None,
             json.dumps(status_record),
             r.get("is_pre_registered", 1),
             json.dumps(prov), now,
             superseded_id,
             lr_test_p, concordance,
             json.dumps(ph_diag) if ph_diag else None),
        )
        result_id = cursor.lastrowid

        # Insert per-covariate results for Cox PH models
        if r.get("test_name") == "cox_ph_model" and params:
            cov_results = params.get("per_covariate_results", [])
            for cr in cov_results:
                conn.execute(
                    """INSERT INTO analysis_covariate_results
                       (result_id, covariate, hr, ci_lower, ci_upper,
                        wald_p, coef, se, z, reference_level, tested_level)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (result_id,
                     cr.get("covariate"), cr.get("hr"),
                     cr.get("ci_lower"), cr.get("ci_upper"),
                     cr.get("wald_p"), cr.get("coef"),
                     cr.get("se"), cr.get("z"),
                     cr.get("reference_level"),
                     cr.get("tested_level")),
                )
    conn.commit()
    conn.close()

    for r in results:
        p_str = f"p={r['p_value']:.4f}" if r['p_value'] is not None else "error"
        print(f"  {r['test_name']}: stat={r['statistic']}, {p_str}")


def cmd_strobe_check(args: argparse.Namespace) -> None:
    """Generate STROBE compliance report."""
    report = generate_report(args.study_id)
    print(report)


def cmd_draft(args: argparse.Namespace) -> None:
    """Generate manuscript draft."""
    path = write_draft(args.study_id)
    print(f"Manuscript draft written to {path}")


def cmd_bundle(args: argparse.Namespace) -> None:
    """Create a hash-verified portable study archive."""
    from core.reporting.bundle import create_bundle, format_verification_report
    try:
        result = create_bundle(args.study_id)
        print(f"Bundle created: {result['bundle_path']}")
        print(f"Composite hash: {result['composite_hash']}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_verify_bundle(args: argparse.Namespace) -> None:
    """Verify a bundle archive's integrity."""
    from core.reporting.bundle import verify_bundle, format_verification_report
    try:
        result = verify_bundle(args.bundle_path)
        print(format_verification_report(result))
        if not result["valid"]:
            sys.exit(1)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_plot_km(args: argparse.Namespace) -> None:
    """Generate a Kaplan-Meier survival curve plot for a completed KM test."""
    from core.reporting.plots import generate_km_plot
    from pathlib import Path

    fmt = getattr(args, "format", "svg")
    show_risk_table = not getattr(args, "no_risk_table", False)
    show_medians = False if getattr(args, "no_medians", False) else None
    base_output = getattr(args, "output", None)
    time_unit = getattr(args, "time_unit", "months")
    style = getattr(args, "style", "clean")

    styles_to_generate = ["clean", "scientific", "presentation"] if style == "all" else [style]

    generated: list[Path] = []
    for s in styles_to_generate:
        if base_output is not None:
            base = Path(base_output)
            if style == "all":
                out = base.with_name(base.stem + f"_{s}" + base.suffix)
            else:
                out = base
        else:
            # Always include the style name in the default filename so
            # sequential invocations (--style clean, --style scientific, ...)
            # don't silently overwrite each other.  This applies even when
            # the user omits --style entirely (defaults to "clean") — the
            # resulting km_plot_1_clean.svg is unambiguous and safe.
            from core.database import DATA_ROOT
            out = DATA_ROOT / args.study_id / f"km_plot_{args.test_id}_{s}.{fmt}"

        try:
            path = generate_km_plot(
                args.study_id, args.test_id,
                output_path=out,
                fmt=fmt,
                show_risk_table=show_risk_table,
                show_medians=show_medians,
                time_unit_display=time_unit,
                style=s,
            )
            generated.append(path)
        except (ValueError, FileNotFoundError) as e:
            print(f"Error generating {s} style: {e}", file=sys.stderr)
            if style != "all":
                sys.exit(1)

    for p in generated:
        print(f"Kaplan-Meier plot saved to {p}")


def cmd_export_excel(args: argparse.Namespace) -> None:
    """Generate a publication-ready Excel report with KM plot, Table 1, and audit hashes."""
    from core.reporting.excel_export import generate_excel_report
    output_path = getattr(args, "output", None)
    try:
        path = generate_excel_report(args.study_id, output_path=output_path)
        print(f"Excel report saved to {path}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_export(args: argparse.Namespace) -> None:
    """Export study as study_result.v1.json (portable, reviewer-ready)."""
    # Check for post-unmask diagnostic violations before export
    from core.planning.diagnostics import check_violation
    conn = get_connection(args.study_id)
    violations: list[str] = []
    for a in conn.execute(
        "SELECT status_json, ph_diagnostics_json FROM analysis_results WHERE study_id=?",
        (args.study_id,),
    ).fetchall():
        has_v, summary, _ = check_violation(dict(a))
        if has_v:
            violations.append(summary)
    if violations and not getattr(args, "acknowledge_violations", False):
        print(
            f"Error: {len(violations)} post-unmask diagnostic violation(s) found:\n",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  • {v}", file=sys.stderr)
        print(
            "\nExported results would include these violations — they are disclosed in output\n"
            "artifacts. Pass --acknowledge-violations to confirm you have reviewed them\n"
            "and wish to proceed with export.",
            file=sys.stderr,
        )
        conn.close()
        sys.exit(1)
    conn.close()

    import json
    from datetime import datetime, timezone
    import pandas as pd

    conn = get_connection(args.study_id)
    raw_table = f"raw_{args.study_id}"

    # ── study metadata ──────────────────────────────────────────────────
    cur = conn.execute("SELECT * FROM studies WHERE id=?", (args.study_id,))
    study = cur.fetchone()
    if not study:
        print(f"Error: study '{args.study_id}' not found.", file=sys.stderr)
        sys.exit(1)

    export = {
        "schema_version": "1.0.0",
        "export_mode": args.mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "study_metadata": {
            "study_id": args.study_id,
            "name": study["name"],
            "design_type": study["study_type"] or "cohort",
            "created_at": study["created_at"],
            "study_state": {0: "pre_locked", 1: "locked", 2: "unmasked"}.get(study["is_locked"], "unknown"),
            "unmasked_at": study["unmasked_at"] if study["is_locked"] >= 2 else None,
        },

        # ── variable catalog ─────────────────────────────────────────────
        "variable_catalog": [],

        # ── locked plan ──────────────────────────────────────────────────
        "locked_plan": None,

        # ── URO analysis results ─────────────────────────────────────────
        "analysis_results": [],

        # ── Table 1 (baseline) ───────────────────────────────────────────
        "table1": None,

        # ── Analysis summary ─────────────────────────────────────────────
        "analysis_summary": None,
    }

    # Variables
    cur = conn.execute(
        "SELECT id, column_name, role, data_type FROM variables WHERE study_id=? ORDER BY id",
        (args.study_id,),
    )
    for r in cur.fetchall():
        export["variable_catalog"].append({
            "id": r["id"],
            "column": r["column_name"],
            "role": r["role"],
            "data_type": r["data_type"],
        })

    # Locked plan
    locked_paths = list(DATA_ROOT.glob(f"{args.study_id}/study_plan.v*.locked.json"))
    if locked_paths:
        latest = sorted(locked_paths)[-1]
        export["locked_plan"] = json.loads(latest.read_text())
        export["study_metadata"]["plan_version"] = latest.stem.split(".")[1]
    else:
        export["study_metadata"]["plan_version"] = None

    # Analysis results (UROs)
    cur = conn.execute(
        """SELECT id, test_name, statistic, p_value, adjusted_p_value,
                  ci_lower, ci_upper, effect_size_json, sample_counts_json,
                  status_json,
                  is_pre_registered, computed_at
           FROM analysis_results WHERE study_id=? ORDER BY id""",
        (args.study_id,),
    )
    for r in cur.fetchall():
        status_data = json.loads(r["status_json"]) if r["status_json"] else {"status": "completed"}
        uro = {
            "test_id": f"t{r['id']}",
            "test_name": r["test_name"],
            "status": status_data.get("status", "completed"),
            "reason": status_data.get("reason"),
            "statistic": {"name": r["test_name"], "value": r["statistic"]} if r["statistic"] is not None else None,
            "p_value": r["p_value"],
            "adjusted_p_value": r["adjusted_p_value"],
            "confidence_interval": {
                "level": 0.95,
                "low": r["ci_lower"],
                "high": r["ci_upper"],
            } if r["ci_lower"] is not None else None,
            "effect_size": json.loads(r["effect_size_json"]) if r["effect_size_json"] else None,
            "sample_counts": json.loads(r["sample_counts_json"]) if r["sample_counts_json"] else None,
            "is_pre_registered": bool(r["is_pre_registered"]),
            "computed_at": r["computed_at"],
        }
        export["analysis_results"].append(uro)

    # Table 1
    export_groupby = "treatment_arm" if locked_paths else None
    tbl = generate_table1(args.study_id, groupby=export_groupby)
    if tbl is not None and not tbl.empty:
        # Flatten MultiIndex columns: ('Grouped by treatment_arm', 'A') → 'A'
        headers = []
        for col in tbl.columns:
            if isinstance(col, tuple):
                # Take the last non-empty element
                parts = [str(p).strip() for p in col if str(p).strip()]
                headers.append(parts[-1] if parts else str(col))
            else:
                headers.append(str(col).strip())

        rows = []
        for idx, row in tbl.iterrows():
            parts = [str(p).strip() for p in (idx if isinstance(idx, tuple) else [idx])]
            characteristic = parts[0] if parts else ""

            # Skip the grouping column itself (redundant when groupby is active)
            if export_groupby and characteristic.lower().startswith(export_groupby.lower()):
                continue

            category_level = parts[1] if len(parts) > 1 else None
            vals = {}
            for h, raw_col in zip(headers, tbl.columns):
                vals[h] = str(row[raw_col]).strip()
            entry = {"label": characteristic, **vals}
            if category_level:
                entry["category_level"] = category_level
            rows.append(entry)
        export["table1"] = {
            "headers": ["Characteristic"] + headers,
            "rows": rows,
        }

    # Analysis summary
    pre_reg = [r for r in export["analysis_results"] if r.get("is_pre_registered")]
    post_hoc = [r for r in export["analysis_results"] if not r.get("is_pre_registered")]
    export["analysis_summary"] = {
        "n_pre_registered": len(pre_reg),
        "n_pre_registered_completed": sum(
            1 for r in pre_reg if r.get("status") == "completed"
        ),
        "n_post_hoc": len(post_hoc),
        "n_post_hoc_significant": sum(
            1 for r in post_hoc
            if r.get("p_value") is not None and r["p_value"] < 0.05
        ),
    }

    # Data hash for authenticity (SHA-256 of raw data, canonical JSON)
    try:
        from core.provenance.hashing import compute_raw_data_hash
        export["study_metadata"]["data_hash_sha256"] = compute_raw_data_hash(args.study_id)
    except Exception:
        export["study_metadata"]["data_hash_sha256"] = None

    conn.close()

    # Write
    version_label = args.study_plan_version or "v1"
    out_path = DATA_ROOT / args.study_id / f"study_result.{version_label}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2, default=str))
    print(f"Study result exported to {out_path}")
    if getattr(args, "format", "json") == "appendix":
        from core.reporting.appendix import generate_appendix

        appendix_path = out_path.with_name(out_path.stem + ".appendix.md")
        appendix_path.write_text(generate_appendix(export, args.study_id))
        print(f"Appendix exported to {appendix_path}")
    if args.mode == "supplementary":
        print("  (supplementary mode — no row-level data included)")


def cmd_lineage(args: argparse.Namespace) -> None:
    """Render study provenance DAG to stdout (text) or file (SVG)."""
    from core.reporting.lineage import assemble_events, render_text, render_svg

    events = assemble_events(args.study_id)
    if args.svg:
        render_svg(events, args.svg)
        print(f"Lineage DAG written to {args.svg}")
    else:
        print(render_text(events))


def cmd_forensics(args: argparse.Namespace) -> None:
    """Run anomaly-detection checks on study data."""
    from core.reporting.forensics import run_forensics

    report_path = run_forensics(args.study_id)
    print(f"Forensics report written to {report_path}")


def cmd_forest_plot(args: argparse.Namespace) -> None:
    """Render publication-ready forest plot for Cox PH results."""
    from core.reporting.forest_plot import render_forest

    try:
        result = render_forest(args.study_id, args.output, ascii=args.ascii)
    except ValueError as e:
        print(f"Error: {e}\nRun 'plan', 'lock', 'unmask', and 'analyze' first.", file=sys.stderr)
        sys.exit(1)
    if args.ascii:
        print(result)
    else:
        print(f"Forest plot written to {result}")


def cmd_flowchart(args: argparse.Namespace) -> None:
    """Render CONSORT/STROBE patient flow diagram."""
    result = render_flowchart(args.study_id, args.output, ascii=args.ascii,
                              show_title=not args.no_title,
                              show_watermark=args.watermark,
                              verbose=args.verbose,
                              show_study_name=args.show_study_name)
    if args.ascii:
        print(result)
    else:
        print(f"Flowchart written to {result}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research-tool", description="Retrospective clinical research tool")
    sub = p.add_subparsers(dest="command")

    # new-study
    sp = sub.add_parser("new-study", help="Create a new study")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_new_study)

    # ingest
    sp = sub.add_parser("ingest", help="Load CSV/Excel into a study")
    sp.add_argument("study_id")
    sp.add_argument("file")
    sp.add_argument("--na-values",
                    help="Comma-separated strings to treat as missing (e.g. 'unknown,missing,N/A,?'). "
                         "Added on top of pandas' default NA representations.")
    sp.add_argument("--force-reingest", action="store_true",
                    help="Re-ingest even if study already has data (clears variables, plans, results).")
    sp.set_defaults(func=cmd_ingest)

    # classify-variables
    sp = sub.add_parser("classify-variables", help="Classify column roles and data types")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_classify_variables)

    # explore-baseline
    sp = sub.add_parser("explore-baseline", help="Explore baseline data (outcomes masked)")
    sp.add_argument("study_id")
    sp.add_argument("--head", type=int, default=5)
    sp.set_defaults(func=cmd_explore_baseline)

    # list-variables
    sp = sub.add_parser("list-variables", help="List classified variables with their DB IDs")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_list_variables)

    # plan
    sp = sub.add_parser("plan", help="Declare the study plan (declare intent before seeing outcomes)")
    sp.add_argument("study_id")
    sp.add_argument("--type", dest="study_type", default="cohort",
                    choices=["cohort", "case_control", "cross_sectional"])
    sp.add_argument("--comparison", required=True, help="Primary comparison or association being tested")
    sp.add_argument("--outcome-var-ids", required=True, help="Comma-separated variable IDs of primary outcomes")
    sp.add_argument("--test", action="append", dest="tests", help="Planned test in format 'var_id:test_name:rationale'")
    sp.add_argument("--covariates", help="Comma-separated variable IDs for covariates")
    sp.add_argument("--cox-ph-models", action="append", dest="cox_ph_models",
                    help="Multivariable Cox PH model in format 'model_name:survival_time_col:event_col:primary_treatment_col:covariate_col1,covariate_col2,...:rationale'")
    sp.add_argument("--matching-criteria", help="Comma-separated variable IDs used for matching (case-control studies)")
    sp.add_argument("--interaction-terms", action="append", dest="interaction_terms",
                    help="Interaction term for a Cox PH model in format 'model_name:var_a:var_b'")
    sp.add_argument("--override", action="append", dest="overrides", default=[],
                    help="Override a classified role before lock: id=<variable_id>:role=<role>")
    sp.add_argument("--from-json", type=str, default=None,
                    help="Load plan from a JSON file instead of CLI flags. "
                         "Expected keys: comparison, outcome_var_ids, study_type, tests, "
                         "covariates, cox_ph_models, interaction_terms, matching_criteria, overrides")
    sp.set_defaults(func=cmd_plan)

    # lock
    sp = sub.add_parser("lock", help="Lock the study plan (immutable snapshot)")
    sp.add_argument("study_id")
    sp.add_argument("--allow-duplicate-ids", action="store_true",
                    help="Allow locking even when duplicate patient IDs are present "
                         "(use for longitudinal/repeated-measures designs where "
                         "the same patient legitimately appears in multiple rows)")
    sp.set_defaults(func=cmd_lock)

    # amend
    sp = sub.add_parser("amend", help="Amend a locked study plan")
    sp.add_argument("study_id")
    sp.add_argument("--post-hoc", action="store_true",
                    help="Post-hoc/exploratory amendment (requires unmasked study)")
    sp.add_argument("--reason", required=True,
                    help="Required human-readable reason for this amendment")
    sp.add_argument("--test", action="append", dest="tests",
                    help="Test to add in format 'var_name:test_name:rationale'")
    sp.set_defaults(func=cmd_amend)

    # unmask
    sp = sub.add_parser("unmask", help="Unmask outcome data (irreversible)")
    sp.add_argument("study_id")
    sp.add_argument("--force", nargs="?", const="", default=None,
                    help="Override forceable diagnostic warnings. Provide a justification string: "
                         "--force 'your reason'")
    sp.set_defaults(func=cmd_unmask)

    # table1
    sp = sub.add_parser("table1", help="Generate Table 1 (baseline characteristics)")
    sp.add_argument("study_id")
    sp.add_argument("--groupby", help="Column to group by (overrides locked plan default)")
    sp.add_argument("--overall", action="store_true",
                    help="Force unstratified single-column view (ignores locked plan grouping)")
    sp.set_defaults(func=cmd_table1)

    # analyze
    sp = sub.add_parser("analyze", help="Run pre-registered analyses from locked plan")
    sp.add_argument("study_id")
    sp.add_argument("--force", action="store_true",
                    help="Run tests even when the plan has recorded assumption warnings")
    sp.add_argument("--post-hoc", action="store_true",
                    help="Run post-hoc/exploratory tests instead of pre-registered tests")
    sp.add_argument("--rerun", action="store_true",
                    help="Force recomputation even if a completed result already exists")
    sp.set_defaults(func=cmd_analyze)

    # strobe-check
    sp = sub.add_parser("strobe-check", help="Check STROBE checklist compliance")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_strobe_check)

    # draft
    sp = sub.add_parser("draft", help="Generate manuscript draft")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_draft)

    # bundle
    sp = sub.add_parser("bundle", help="Create a hash-verified portable study archive")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_bundle)

    # verify-bundle
    sp = sub.add_parser("verify-bundle", help="Verify a bundle archive's integrity")
    sp.add_argument("bundle_path", help="Path to the .tar.gz bundle file")
    sp.set_defaults(func=cmd_verify_bundle)

    # plot-km
    sp = sub.add_parser("plot-km", help="Generate a Kaplan-Meier survival curve plot")
    sp.add_argument("study_id")
    sp.add_argument("test_id", type=int, help="ID of the completed kaplan_meier_logrank analysis result")
    sp.add_argument("--format", choices=["svg", "pdf"], default="svg",
                    help="Output format (default: svg)")
    sp.add_argument("--no-risk-table", action="store_true",
                    help="Hide the at-risk table subplot")
    sp.add_argument("--no-medians", action="store_true",
                    help="Hide median survival reference lines and callouts")
    sp.add_argument("--output", type=str,
                    help="Custom output file path (overrides default naming)")
    sp.add_argument("--time-unit", choices=["days", "months"], default="months",
                    help="Display unit for the x-axis (default: months)")
    sp.add_argument("--style", choices=["clean", "scientific", "presentation", "all"], default="clean",
                    help="Visual preset for the plot (default: clean; use 'all' to generate all three)")
    sp.set_defaults(func=cmd_plot_km)

    # export
    sp = sub.add_parser("export", help="Export study as JSON (portable reviewer format)")
    sp.add_argument("study_id")
    sp.add_argument("--mode", default="supplementary", choices=["supplementary", "internal_full"])
    sp.add_argument("--version", dest="study_plan_version", help="Plan version label (e.g. v1)")
    sp.add_argument("--format", choices=["json", "appendix"], default="json",
                    help="Export JSON, or JSON plus a manuscript appendix Markdown file")
    sp.add_argument("--acknowledge-violations", action="store_true",
                    help="Acknowledge post-unmask diagnostic violations and proceed with export")
    sp.set_defaults(func=cmd_export)

    # export-excel
    sp = sub.add_parser("export-excel",
                        help="Generate a publication-ready Excel report with KM plot, Table 1, and audit")
    sp.add_argument("study_id")
    sp.add_argument("--output", type=str, help="Custom output file path")
    sp.set_defaults(func=cmd_export_excel)

    # lineage
    sp = sub.add_parser("lineage", help="Render study provenance DAG")
    sp.add_argument("study_id")
    sp.add_argument("--svg", type=str, default=None,
                    help="Output path for SVG file (omit for ASCII terminal output)")
    sp.set_defaults(func=cmd_lineage)

    # forensics
    sp = sub.add_parser("forensics",
                        help="Run data anomaly-detection checks")
    sp.add_argument("study_id")
    sp.set_defaults(func=cmd_forensics)

    # plot-forest
    sp = sub.add_parser("plot-forest",
                        help="Render forest plot for Cox PH results")
    sp.add_argument("study_id")
    sp.add_argument("--svg", dest="output", type=str, default=None,
                    help="Output SVG path (default: data/studies/<id>/forest_plot.svg)")
    sp.add_argument("--ascii", action="store_true",
                    help="Render as ASCII text instead of SVG")
    sp.set_defaults(func=cmd_forest_plot)

    # flowchart
    sp = sub.add_parser("flowchart",
                        help="Render CONSORT/STROBE patient flow diagram")
    sp.add_argument("study_id")
    sp.add_argument("--svg", dest="output", type=str, default=None,
                    help="Output SVG path (default: data/studies/<id>/flowchart.svg)")
    sp.add_argument("--ascii", action="store_true",
                    help="Render as ASCII text instead of SVG")
    sp.add_argument("--no-title", action="store_true",
                    help="Omit title from SVG")
    sp.add_argument("--watermark", action="store_true",
                    help="Show 'Generated by' footer in SVG")
    sp.add_argument("--verbose", action="store_true",
                    help="Show developer annotations in SVG")
    sp.add_argument("--show-study-name", action="store_true",
                    help="Show dataset name subtitle below main title")
    sp.set_defaults(func=cmd_flowchart)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
