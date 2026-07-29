"""Hash-verified study archive (bundle) for portable verification.

A bundle packages a completed study into a single `.tar.gz` that lets a
third party independently verify that the manuscript's numbers genuinely
came from the exact locked plan run against the exact raw data.

Composite hash: SHA-256 of raw_data_hash || locked_plan_hash || results_hash
(hash of hashes — if any component changes, the composite breaks).
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.database import get_connection, DATA_ROOT
from core.provenance.hashing import sha256 as _sha256, canonical_json as _canonical_json


SCHEMA_VERSION = "1.0.0"
HASH_ORDER = ("raw_data_hash", "locked_plan_hash", "results_hash")
COMPOSITE_SEPARATOR = "||"  # documented separator between hashes in the concatenation


def _compute_composite(*component_hashes: str) -> str:
    """SHA-256 of component hashes joined by the documented separator."""
    return _sha256(COMPOSITE_SEPARATOR.join(component_hashes))


def _export_raw_data(study_id: str) -> str:
    """Export the ingested raw data table as canonical JSON lines.

    Returns the entire table as a JSON array of row objects (sorted keys),
    so the hash is deterministic regardless of row_id insertion order.
    """
    conn = get_connection(study_id)
    raw_table = f"raw_{study_id}"
    cur = conn.execute(f"SELECT * FROM {raw_table} ORDER BY row_id")
    rows = cur.fetchall()
    conn.close()

    # Build a list of dicts with sorted keys for canonical JSON
    data = []
    for r in rows:
        row_dict = dict(r)
        # Ensure all values are JSON-compatible
        data.append({k: v for k, v in row_dict.items()})
    return _canonical_json(data)


def _export_analysis_results(study_id: str) -> str:
    """Export analysis results as canonical JSON array."""
    conn = get_connection(study_id)
    cur = conn.execute(
        "SELECT * FROM analysis_results WHERE study_id=? ORDER BY id", (study_id,)
    )
    rows = cur.fetchall()

    # Pre-fetch covariate results
    result_ids = [r["id"] for r in rows]
    covariate_map: dict[int, list[dict]] = {}
    if result_ids:
        placeholders = ",".join("?" for _ in result_ids)
        cur = conn.execute(
            f"SELECT * FROM analysis_covariate_results WHERE result_id IN ({placeholders}) ORDER BY id",
            result_ids,
        )
        for row in cur.fetchall():
            covariate_map.setdefault(row["result_id"], []).append(dict(row))

    conn.close()

    results = []
    for r in rows:
        row_dict = dict(r)
        # Decode JSON fields so they're proper objects, not strings
        for json_field in ("variable_ids_used", "effect_size_json",
                          "sample_counts_json", "status_json", "provenance_json",
                          "ph_diagnostics_json"):
            if row_dict.get(json_field):
                try:
                    row_dict[json_field] = json.loads(row_dict[json_field])
                except (TypeError, json.JSONDecodeError):
                    pass
        # Attach per-covariate results
        cr = covariate_map.get(r["id"])
        if cr:
            row_dict["covariate_results"] = cr
        results.append(row_dict)
    return _canonical_json(results)


def create_bundle(study_id: str) -> dict:
    """Create a hash-verified bundle archive for a completed study.

    Returns a dict with ``bundle_path`` (Path), ``composite_hash`` (str),
    and all component hashes.

    Raises RuntimeError if the study hasn't been unmasked and analyzed.
    """
    conn = get_connection(study_id)
    cur = conn.execute("SELECT * FROM studies WHERE id=?", (study_id,))
    study = cur.fetchone()
    if not study:
        conn.close()
        raise RuntimeError(f"Study '{study_id}' not found.")

    study_state = study["is_locked"]
    if study_state < 2:
        conn.close()
        raise RuntimeError(
            f"Cannot bundle study '{study_id}': study has not been unmasked "
            f"and analyzed. Run analyze first."
        )

    # Check for analysis results
    cur = conn.execute(
        "SELECT COUNT(*) as cnt FROM analysis_results WHERE study_id=?",
        (study_id,),
    )
    n_results = cur.fetchone()["cnt"]
    if n_results == 0:
        conn.close()
        raise RuntimeError(
            f"Cannot bundle study '{study_id}': no analysis results found. "
            f"Run analyze first."
        )
    conn.close()

    # ── Gather bundle contents ──────────────────────────────────────────
    raw_data_json = _export_raw_data(study_id)
    raw_data_hash = _sha256(raw_data_json)

    # Locked plan: find latest and reuse its content_hash
    locked_plans = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    if not locked_plans:
        raise RuntimeError(f"No locked plan found for study '{study_id}'.")
    locked_plan_path = locked_plans[-1]
    locked_plan_data = json.loads(locked_plan_path.read_text())
    locked_plan_hash = locked_plan_data["content_hash"]
    # Keep the locked plan JSON as-is (with content_hash) so verify_bundle
    # can read the hash back from it.
    locked_plan_json = _canonical_json(locked_plan_data)

    results_json = _export_analysis_results(study_id)
    results_hash = _sha256(results_json)

    composite_hash = _compute_composite(raw_data_hash, locked_plan_hash, results_hash)

    # ── Build manifest ──────────────────────────────────────────────────
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "study_id": study_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hash_algorithm": "SHA-256",
        "composite_separator": COMPOSITE_SEPARATOR,
        "composite_hash": composite_hash,
        "raw_data_hash": raw_data_hash,
        "locked_plan_hash": locked_plan_hash,
        "results_hash": results_hash,
        "verification_instructions": (
            "To verify this bundle, recompute each component hash from the "
            "bundle's own files and compare to the manifest:\n"
            f"  1. raw_data_hash (SHA-256 of raw_data.json): "
            f"this file is a canonical-JSON snapshot of the actual ingested "
            f"dataset as stored in the study database at bundling time "
            f"(post whitespace-stripping, post NA-value normalization). "
            f"It is NOT necessarily byte-identical to the originally uploaded "
            f"CSV, and should not be treated as a re-ingestable source file.\n"
            f"  2. locked_plan_hash: content_hash from "
            f"study_plan.locked.json\n"
            f"  3. results_hash: SHA-256 of analysis_results.json "
            f"(canonical JSON)\n"
            f"  4. composite_hash: SHA-256 of "
            f"{raw_data_hash}{COMPOSITE_SEPARATOR}"
            f"{locked_plan_hash}{COMPOSITE_SEPARATOR}"
            f"{results_hash}\n"
            "If the composite matches, all three components are authentic.\n"
            "If it doesn't, check each component hash individually to find "
            "which file was altered."
        ),
    }

    # ── STROBE report ───────────────────────────────────────────────────
    from core.reporting.strobe_checklist import generate_report
    strobe_text = generate_report(study_id)

    # ── Manuscript draft ────────────────────────────────────────────────
    # Check if it exists on disk; if not, generate it in-memory
    draft_path = DATA_ROOT / study_id / "manuscript_draft.md"
    if draft_path.exists():
        draft_text = draft_path.read_text()
    else:
        from core.reporting.manuscript_draft import generate_draft
        draft_text = generate_draft(study_id)

    # ── Write the bundle archive ────────────────────────────────────────
    bundle_filename = f"{study_id}_bundle.tar.gz"
    bundle_path = DATA_ROOT / study_id / bundle_filename

    with tarfile.open(str(bundle_path), "w:gz") as tar:
        # manifest.json
        manifest_bytes = _canonical_json(manifest).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

        # raw_data.csv — one JSON object per line (JSON Lines format)
        raw_lines = raw_data_json  # canonical JSON array
        raw_bytes = raw_lines.encode("utf-8")
        info = tarfile.TarInfo(name="raw_data.json")
        info.size = len(raw_bytes)
        tar.addfile(info, io.BytesIO(raw_bytes))

        # study_plan.locked.json
        plan_bytes = locked_plan_json.encode("utf-8")
        info = tarfile.TarInfo(name="study_plan.locked.json")
        info.size = len(plan_bytes)
        tar.addfile(info, io.BytesIO(plan_bytes))

        # analysis_results.json
        results_bytes = results_json.encode("utf-8")
        info = tarfile.TarInfo(name="analysis_results.json")
        info.size = len(results_bytes)
        tar.addfile(info, io.BytesIO(results_bytes))

        # manuscript_draft.md
        draft_bytes = draft_text.encode("utf-8")
        info = tarfile.TarInfo(name="manuscript_draft.md")
        info.size = len(draft_bytes)
        tar.addfile(info, io.BytesIO(draft_bytes))

        # strobe_report.txt
        strobe_bytes = strobe_text.encode("utf-8")
        info = tarfile.TarInfo(name="strobe_report.txt")
        info.size = len(strobe_bytes)
        tar.addfile(info, io.BytesIO(strobe_bytes))

    return {
        "bundle_path": bundle_path,
        "composite_hash": composite_hash,
        "raw_data_hash": raw_data_hash,
        "locked_plan_hash": locked_plan_hash,
        "results_hash": results_hash,
    }


def verify_bundle(bundle_path: str | Path) -> dict:
    """Verify a bundle archive's integrity by recomputing all hashes.

    Returns a dict with keys:
      - ``valid`` (bool): True if composite matches
      - ``composite_match`` (bool)
      - ``raw_data_match`` (bool)
      - ``locked_plan_match`` (bool)
      - ``results_match`` (bool)
      - ``manifest`` (dict): the manifest from the bundle
      - ``composite_hash`` (str): hash from manifest
      - ``computed_composite`` (str): hash computed from bundle contents

    Works from the bundle file ALONE — no dependency on the original
    study database.
    """
    bundle_path = Path(bundle_path)
    if not bundle_path.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_path}")

    # Extract bundle into memory
    contents: dict[str, bytes] = {}
    with tarfile.open(str(bundle_path), "r:gz") as tar:
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f is not None:
                contents[member.name] = f.read()

    if "manifest.json" not in contents:
        raise ValueError("Bundle is missing manifest.json")

    manifest = json.loads(contents["manifest.json"].decode("utf-8"))

    # Track which expected files are present vs missing
    expected_files = ["raw_data.json", "study_plan.locked.json", "analysis_results.json"]
    missing_files = [f for f in expected_files if f not in contents]

    # Recompute each component hash from the bundle's own file contents
    raw_data_present = "raw_data.json" in contents
    raw_data_bytes = contents.get("raw_data.json", b"")
    computed_raw_hash = _sha256(raw_data_bytes.decode("utf-8")) if raw_data_present else ""

    locked_plan_present = "study_plan.locked.json" in contents
    plan_bytes = contents.get("study_plan.locked.json", b"")
    if locked_plan_present:
        plan_data = json.loads(plan_bytes.decode("utf-8"))
        computed_plan_hash = plan_data.get("content_hash", "")
    else:
        computed_plan_hash = ""

    results_present = "analysis_results.json" in contents
    results_bytes = contents.get("analysis_results.json", b"")
    computed_results_hash = _sha256(results_bytes.decode("utf-8")) if results_present else ""

    # Composite: SHA-256 of the three component hashes
    computed_composite = _compute_composite(
        computed_raw_hash, computed_plan_hash, computed_results_hash,
    )

    reported_composite = manifest.get("composite_hash", "")

    return {
        "valid": computed_composite == reported_composite,
        "composite_match": computed_composite == reported_composite,
        "raw_data_match": raw_data_present and computed_raw_hash == manifest.get("raw_data_hash"),
        "locked_plan_match": locked_plan_present and computed_plan_hash == manifest.get("locked_plan_hash"),
        "results_match": results_present and computed_results_hash == manifest.get("results_hash"),
        "missing_files": missing_files,
        "manifest": manifest,
        "composite_hash": reported_composite,
        "computed_composite": computed_composite,
        "computed_raw_hash": computed_raw_hash,
        "computed_plan_hash": computed_plan_hash,
        "computed_results_hash": computed_results_hash,
    }


def format_verification_report(result: dict) -> str:
    """Format a verification result dict as a human-readable report."""
    lines = [
        "Bundle Verification Report",
        "=" * 50,
        f"Composite hash (manifest):  {result['composite_hash']}",
        f"Composite hash (computed):  {result['computed_composite']}",
        "",
    ]
    if result["valid"]:
        lines.append("✓ PASS — all hashes match. Bundle is authentic.")
    else:
        lines.append("✗ FAIL — composite hash mismatch.")
        lines.append("")

        missing = result.get("missing_files", [])
        for fname in missing:
            lines.append(f"  ✗ {fname} is missing from the archive.")

        if "raw_data.json" not in missing and not result.get("raw_data_match"):
            m = result["manifest"].get("raw_data_hash", "???")
            c = result["computed_raw_hash"]
            lines.append(f"  ✗ raw_data_hash: manifest={m}, computed={c}")
        if "study_plan.locked.json" not in missing and not result.get("locked_plan_match"):
            m = result["manifest"].get("locked_plan_hash", "???")
            c = result["computed_plan_hash"]
            lines.append(f"  ✗ locked_plan_hash: manifest={m}, computed={c}")
        if "analysis_results.json" not in missing and not result.get("results_match"):
            m = result["manifest"].get("results_hash", "???")
            c = result["computed_results_hash"]
            lines.append(f"  ✗ results_hash: manifest={m}, computed={c}")
        lines.append("")

        if "raw_data.json" not in missing and not result.get("raw_data_match"):
            lines.append("  → raw_data.json content has been altered.")
        elif "raw_data.json" in missing:
            lines.append("  → raw_data.json is missing from the archive.")

        if "study_plan.locked.json" not in missing and not result.get("locked_plan_match"):
            lines.append("  → study_plan.locked.json content has been altered.")
        elif "study_plan.locked.json" in missing:
            lines.append("  → study_plan.locked.json is missing from the archive.")

        if "analysis_results.json" not in missing and not result.get("results_match"):
            lines.append("  → analysis_results.json content has been altered.")
        elif "analysis_results.json" in missing:
            lines.append("  → analysis_results.json is missing from the archive.")

    return "\n".join(lines)
