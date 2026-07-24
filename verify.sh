#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════
# verify.sh — Manual end-to-end verification of research-tool
#
# Run with:  bash verify.sh
#
# This script prints EVERYTHING so you can read through it top to bottom
# with your own eyes.  Nothing is silently combined.
# ═══════════════════════════════════════════════════════════════════════

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SELF_DIR"

echo "================================================================"
echo "STEP 0: Clean slate (skip reinstall if tool already installed)"
echo "================================================================"
rm -rf data/ _test_emd.csv
echo "  data/ and _test_emd.csv removed."

echo ""
if command -v research-tool &>/dev/null; then
    echo "  research-tool already installed, skipping reinstall."
    echo "  (Run 'uv tool install --reinstall -e .' manually if you change the code.)"
else
    echo "  Running: uv tool install -e ."
    uv tool install -e . 2>&1 | tail -2
    echo "  Install complete."
fi

echo ""
echo "================================================================"
echo "STEP 1: Run full pytest suite"
echo "================================================================"
echo "  (Uses the project environment, including runtime and dev dependencies.)"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import pytest, scipy, tableone" >/dev/null 2>&1; then
    echo "  Project test dependencies missing; syncing the project environment."
    uv sync --dev
fi
set +e
pytest_output=$(.venv/bin/python -m pytest tests/ -v --tb=short 2>&1)
pytest_exit=$?
echo "$pytest_output"
echo ""
# Count pass/fail
passed=$(echo "$pytest_output" | grep -cE "^tests/.* PASSED" || true)
failed=$(echo "$pytest_output" | grep -cE "^tests/.* FAILED" || true)
total=$((passed + failed))
echo "  ---- SUITE RESULT: $passed / $total passed, $failed failed ----"
set -e
if [ "$pytest_exit" -ne 0 ]; then
    echo "  Pytest exited with status $pytest_exit." >&2
    exit "$pytest_exit"
fi

echo ""
echo "================================================================"
echo "STEP 2: Generate synthetic CSV (21 patients, EMD study schema)"
echo "================================================================"
/opt/homebrew/bin/python3 -c "
import csv, io
from random import Random
rng = Random(2024)
rows = [['patient_id','age','sex','iss_stage','prior_lines',
         'high_risk_cytogenetics','treatment_arm','response_category',
         'pfs_days','pfs_event','os_days','os_event']]
for i in range(21):
    rows.append([
        f'EMD_{i+1:03d}',
        rng.randint(40,85),
        rng.choice(['M','F']),
        rng.choices(['I','II','III'],[30,40,30])[0],
        rng.randint(0,4),
        rng.choice(['yes','no']),
        rng.choice(['A','B']),
        rng.choices(['CR','PR','MR','SD','PD'],[15,30,20,20,15])[0],
        max(30, int(rng.expovariate(1/250))),
        rng.choices([0,1],[35,65])[0],
        max(30, int(rng.expovariate(1/300))),
        rng.choices([0,1],[30,70])[0],
    ])
buf = io.StringIO()
csv.writer(buf).writerows(rows)
open('_test_emd.csv','w').write(buf.getvalue())
print('  Written _test_emd.csv  (21 rows of synthetic myeloma EMD data)')
"
echo "  Columns: patient_id, age, sex, iss_stage, prior_lines, high_risk_cytogenetics,"
echo "           treatment_arm, response_category, pfs_days, pfs_event, os_days, os_event"
echo ""

echo "================================================================"
echo "STEP 3: CLI — new-study"
echo "================================================================"
SID=$(research-tool new-study "Myeloma EMD")
echo "  Command: research-tool new-study \"Myeloma EMD\""
echo "  Study ID: $SID"
echo "  (This UUID is the study identifier used in all subsequent commands.)"
echo ""

echo "================================================================"
echo "STEP 4: CLI — ingest"
echo "================================================================"
echo "  Command: research-tool ingest $SID _test_emd.csv"
research-tool ingest "$SID" _test_emd.csv
echo ""

echo "================================================================"
echo "STEP 5: CLI — classify-variables"
echo "================================================================"
echo "  Command: research-tool classify-variables $SID"
echo "  (This also triggers seal_outcomes() — outcome values are moved to"
echo "   a shadow table and NULLed in the main raw_ table.)"
research-tool classify-variables "$SID"
echo ""

echo "================================================================"
echo "STEP 6: CLI — explore-baseline (masked)"
echo "================================================================"
echo "  Command: research-tool explore-baseline $SID --head 3"
echo "  (Only baseline columns shown; outcome columns are physically NULL.)"
research-tool explore-baseline "$SID" --head 3
echo ""

echo "================================================================"
echo "STEP 7: CLI — plan (declare pre-registered tests)"
echo "================================================================"
echo "  Command: research-tool plan $SID \\"
echo "           --type cohort \\"
echo "           --comparison \"PFS and response by treatment arm\" \\"
echo "           --outcome-var-ids \"7,8\" \\"
echo "           --test \"response_category:chi_square:Compare response\" \\"
echo "           --test \"pfs_days:kaplan_meier_logrank:Compare PFS between arms\" \\"
echo "           --covariates \"1,3,5\""
research-tool plan "$SID" \
  --type cohort \
  --comparison "PFS and response by treatment arm" \
  --outcome-var-ids "7,8" \
  --test "response_category:chi_square:Compare response" \
  --test "pfs_days:kaplan_meier_logrank:Compare PFS between arms" \
  --covariates "1,3,5"
echo ""

echo "================================================================"
echo "STEP 8: CLI — lock (immutable snapshot)"
echo "================================================================"
echo "  Command: research-tool lock $SID"
research-tool lock "$SID"
echo ""

echo "================================================================"
echo "STEP 9: ADVERSARIAL — manual sqlite3 check (YOU run this)"
echo "================================================================"
DB_PATH="$(pwd)/data/studies/$SID/study.db"
RAW_TABLE="raw_${SID}"
echo "  Study is LOCKED (state=1).  Outcome columns should be NULL."
echo ""
echo "  >>> RUN THIS COMMAND IN ANOTHER TERMINAL:"
echo ""
echo "      sqlite3 \"$DB_PATH\" \"SELECT row_id, age, response_category, pfs_days, os_days FROM ${RAW_TABLE} LIMIT 5;\""
echo ""
echo "  Expect: response_category, pfs_days, os_days are all NULL (empty)."
echo "  If you see values like 'CR', 'PR', '355', '239' — the gate is BROKEN."
echo ""
echo "  (Pausing 15 seconds for you to run the check.)"
echo ""

sleep 15

echo ""
echo "================================================================"
echo "STEP 10: CLI — unmask (irreversible)"
echo "================================================================"
echo "  Command: research-tool unmask $SID"
research-tool unmask "$SID"
echo ""

echo "================================================================"
echo "STEP 11: CLI — table1 (baseline characteristics)"
echo "================================================================"
echo "  Command: research-tool table1 $SID"
research-tool table1 "$SID"
echo ""

echo "================================================================"
echo "STEP 12: CLI — analyze (runs pre-registered tests)"
echo "================================================================"
echo "  Command: research-tool analyze $SID"
research-tool analyze "$SID"
echo ""

echo "================================================================"
echo "STEP 13: CLI — strobe-check"
echo "================================================================"
echo "  Command: research-tool strobe-check $SID"
research-tool strobe-check "$SID"
echo ""

echo "================================================================"
echo "STEP 14: CLI — draft"
echo "================================================================"
echo "  Command: research-tool draft $SID"
research-tool draft "$SID"
DRAFT_PATH="$(pwd)/data/studies/$SID/manuscript_draft.md"
echo "  Draft file: $DRAFT_PATH"
echo "  Sections found:"
grep '^##' "$DRAFT_PATH" 2>/dev/null || echo "  (no sections with ## header)"
echo ""

echo "================================================================"
echo "STEP 15: TAMPER TEST"
echo "================================================================"
echo "  The locked plan file was written with a content_hash (SHA-256)"
echo "  that is verified on every load_plan() call (e.g. analyze)."
echo ""
echo "  Locked file path:"
LOCKED_PATH="$(pwd)/data/studies/$SID/study_plan.v1.locked.json"
echo "    $LOCKED_PATH"
echo ""
echo "  >>> TO TEST TAMPER DETECTION:"
echo "    1. Open $LOCKED_PATH in a text editor."
echo "    2. Change the value of \"primary_comparison\" to something else"
echo "       (e.g. \"HACKED BY USER\")."
echo "    3. Save the file."
echo "    4. Run this command:"
echo ""
echo "        research-tool analyze $SID"
echo ""
echo "    5. Expected output:"
echo "       ValueError: Locked plan tampered: ... The file content has been modified since locking."
echo ""
echo "  After confirming the tamper is caught, restore the original value"
echo "  (or just run lock again to create v2) and analyze will work again."
echo ""

echo "================================================================"
echo "CLEANUP"
echo "================================================================"
echo "  Removing data/, _test_emd.csv, .venv/, .pytest_cache/..."
rm -rf data/ _test_emd.csv .venv/ .pytest_cache/
echo "  Done."
echo ""

echo "================================================================"
echo "COMPLETE"
echo "================================================================"
echo "  Study ID: $SID"
echo "  Data dir: $(pwd)/data/studies/$SID/  (now removed)"
echo "  Script artifacts cleaned up."
echo "  Remaining repo: source files, tests, docs only."
echo ""
