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
import sys
import uuid
from datetime import datetime, timezone

from core.database import get_connection, init_db, study_dir, DATA_ROOT
from core.ingestion.csv_loader import load_file
from core.ingestion.variable_classifier import classify_variables_interactive, _classify_batch
from core.masking.gate import seal_outcomes, is_masked
from core.planning.study_plan import StudyPlan
from core.planning.lock import lock_plan, load_plan, unmask_study
from core.planning.test_selector import check_assumptions
from core.stats.descriptive import generate_table1
from core.stats.inferential import run_test
from core.reporting.strobe_checklist import generate_report
from core.reporting.manuscript_draft import write_draft


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
    columns = load_file(args.study_id, args.file, na_values=na_vals)
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

    for s in suggestions:
        print(f"  {s['column']:<30} → {s['role']:<10} {s['data_type']}")


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


def cmd_plan(args: argparse.Namespace) -> None:
    """Declare a study plan."""
    outcome_ids = [int(x) for x in args.outcome_var_ids.split(",")]
    tests = []
    if args.tests:
        for t in args.tests:
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

    # Check assumptions before building plan
    # Enrich tests with covariate count for Cox EPV check
    n_covariates = len(covariates)
    for t in tests:
        t["n_covariates"] = n_covariates

    warnings = check_assumptions(args.study_id, tests)
    for w in warnings:
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
        for w in warnings:
            if f"{tn} on '{var}'" in w:
                warning_map[var] = w

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


def cmd_unmask(args: argparse.Namespace) -> None:
    """Unmask outcome data (irreversible)."""
    unmask_study(args.study_id)
    print("Study unmasked. Outcome data now visible (outcome values restored from shadow table).")


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

    # Parse --force flag
    force = getattr(args, "force", False)

    for t in plan.planned_tests:
        var_name = t.get("variable_name", "")
        test_name = t.get("test_name", "")
        if not test_name or not var_name:
            continue

        # Enforce assumption warnings from plan time
        plan_warnings = getattr(plan, "warnings", {})
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
                "statistic": None,
                "p_value": None,
                "ci_lower": None,
                "ci_upper": None,
                "params": {},
                "effect_size": None,
                "sample_counts": {"n_total": len(df), "n_analyzed": 0, "n_excluded": len(df)},
                "status": "skipped_assumption_violation",
                "reason": plan_warnings[var_name],
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
        if result.get("params", {}).get("error"):
            result["status"] = "error"
            result["reason"] = result["params"]["error"]
        results.append(result)

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

    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        status_record = {"status": r.get("status", "completed")}
        if r.get("reason"):
            status_record["reason"] = r["reason"]
        conn.execute(
            """INSERT INTO analysis_results
               (study_id, study_plan_version, variable_ids_used, test_name,
                statistic, p_value, adjusted_p_value, ci_lower, ci_upper,
                effect_size_json, sample_counts_json, status_json,
                is_pre_registered, provenance_json, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (args.study_id, plan.version, json.dumps([]),
             r["test_name"], r["statistic"], r["p_value"],
             r.get("adjusted_p_value"), r.get("ci_lower"), r.get("ci_upper"),
             json.dumps(r["effect_size"]) if r.get("effect_size") else None,
             json.dumps(r["sample_counts"]) if r.get("sample_counts") else None,
             json.dumps(status_record),
             json.dumps({"plan_version": plan.version}), now),
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


def cmd_export(args: argparse.Namespace) -> None:
    """Export study as study_result.v1.json (portable, reviewer-ready)."""
    import json
    from datetime import datetime, timezone
    from hashlib import sha256
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

    # Data hash for authenticity (SHA-256 of raw_data JSON)
    try:
        cur = conn.execute(f"SELECT json_row FROM raw_data WHERE study_id=? ORDER BY row_id", (args.study_id,))
        raw_json = json.dumps([dict(r) for r in cur.fetchall()]).encode()
        export["study_metadata"]["data_hash_sha256"] = sha256(raw_json).hexdigest()
    except Exception:
        # raw_data table may not exist in older studies — try the raw_ table
        try:
            df = pd.read_sql_query(f"SELECT * FROM {raw_table}", conn)
            raw_json = df.to_json(orient="records").encode()
            export["study_metadata"]["data_hash_sha256"] = sha256(raw_json).hexdigest()
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

    # plan
    sp = sub.add_parser("plan", help="Declare the study plan (declare intent before seeing outcomes)")
    sp.add_argument("study_id")
    sp.add_argument("--type", dest="study_type", default="cohort",
                    choices=["cohort", "case_control", "cross_sectional"])
    sp.add_argument("--comparison", required=True, help="Primary comparison or association being tested")
    sp.add_argument("--outcome-var-ids", required=True, help="Comma-separated variable IDs of primary outcomes")
    sp.add_argument("--test", action="append", dest="tests", help="Planned test in format 'var_id:test_name:rationale'")
    sp.add_argument("--covariates", help="Comma-separated variable IDs for covariates")
    sp.add_argument("--matching-criteria", help="Comma-separated variable IDs used for matching (case-control studies)")
    sp.add_argument("--override", action="append", dest="overrides", default=[],
                    help="Override a classified role before lock: id=<variable_id>:role=<role>")
    sp.set_defaults(func=cmd_plan)

    # lock
    sp = sub.add_parser("lock", help="Lock the study plan (immutable snapshot)")
    sp.add_argument("study_id")
    sp.add_argument("--allow-duplicate-ids", action="store_true",
                    help="Allow locking even when duplicate patient IDs are present "
                         "(use for longitudinal/repeated-measures designs where "
                         "the same patient legitimately appears in multiple rows)")
    sp.set_defaults(func=cmd_lock)

    # unmask
    sp = sub.add_parser("unmask", help="Unmask outcome data (irreversible)")
    sp.add_argument("study_id")
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

    # export
    sp = sub.add_parser("export", help="Export study as JSON (portable reviewer format)")
    sp.add_argument("study_id")
    sp.add_argument("--mode", default="supplementary", choices=["supplementary", "internal_full"])
    sp.add_argument("--version", dest="study_plan_version", help="Plan version label (e.g. v1)")
    sp.add_argument("--format", choices=["json", "appendix"], default="json",
                    help="Export JSON, or JSON plus a manuscript appendix Markdown file")
    sp.set_defaults(func=cmd_export)

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
