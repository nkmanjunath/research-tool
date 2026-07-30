"""Lock/unmask mechanism — writes immutable versioned plan snapshots.

Once locked, a plan file must never be edited in place.  Any change requires
creating a new version.  Old versions are never deleted.

Integrity: every locked file includes a SHA-256 hash of its own content
(excluding the hash field itself).  load_plan() verifies the hash on every
read and raises if the file was tampered with.
"""

from __future__ import annotations
import hashlib
import json
from pathlib import Path

from core.database import DATA_ROOT, get_connection, init_db
from core.planning.study_plan import StudyPlan
from core.masking.gate import lock_study, is_masked, unmask_study as gate_unmask


def _plan_path(study_id: str, version: int) -> Path:
    return DATA_ROOT / study_id / f"study_plan.v{version}.locked.json"


def _next_version(study_id: str) -> int:
    """Find the highest existing version + 1."""
    existing = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
    if not existing:
        return 1
    max_v = 0
    for p in existing:
        try:
            v = int(p.stem.split(".v")[1].split(".")[0])
            max_v = max(max_v, v)
        except (IndexError, ValueError):
            continue
    return max_v + 1


def _compute_hash(data: dict) -> str:
    """SHA-256 of the canonical JSON, excluding the content_hash field."""
    d = {k: v for k, v in data.items() if k != "content_hash"}
    canonical = json.dumps(d, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def lock_plan(study_id: str, plan: StudyPlan) -> Path:
    """Write an immutable, versioned, timestamped plan to disk.

    The file includes a content_hash that is verified on every load.
    Must be called before unmasking (is_masked must be True).

    Returns
    -------
    Path to the written lock file.
    """
    if not is_masked(study_id):
        raise RuntimeError("Cannot lock a plan after unmasking.")

    plan.version = _next_version(study_id)
    plan.locked_at = None  # will be set by to_dict()
    data = plan.to_dict()
    data["content_hash"] = _compute_hash(data)

    path = _plan_path(study_id, plan.version)
    path.write_text(json.dumps(data, indent=2))

    # Update the study record in DB
    conn = get_connection(study_id)
    init_db(conn)
    conn.execute(
        "UPDATE studies SET is_locked=1, study_type=? WHERE id=?",
        (plan.study_type, study_id),
    )
    conn.commit()
    conn.close()

    return path


def verify_hash(path: Path) -> bool:
    """Verify the content_hash in a locked plan file."""
    data = json.loads(path.read_text())
    stored_hash = data.get("content_hash", "")
    if not stored_hash:
        return False
    computed = _compute_hash(data)
    return computed == stored_hash


def load_plan(study_id: str, version: int | None = None) -> StudyPlan:
    """Load the latest (or specific version) plan file.

    Raises ValueError if the content hash doesn't match (tampered file).
    """
    if version is not None:
        path = _plan_path(study_id, version)
    else:
        versions = sorted(DATA_ROOT.glob(f"{study_id}/study_plan.v*.locked.json"))
        if not versions:
            raise FileNotFoundError(f"No locked plan found for study {study_id}")
        path = versions[-1]

    data = json.loads(path.read_text())

    if not verify_hash(path):
        raise ValueError(
            f"Locked plan tampered: {path}. "
            "The file content has been modified since locking."
        )

    # Strip internal fields before deserializing
    data.pop("content_hash", None)
    return StudyPlan.from_dict(data)


def unmask_study(study_id: str) -> None:
    """Unmask the study — irreversibly reveals outcome data."""
    gate_unmask(study_id)


def lock_amendment(
    study_id: str,
    *,
    amendment_reason: str,
    planned_tests: list[dict] | None = None,
    post_hoc_tests: list[dict] | None = None,
) -> Path:
    """Write a new versioned plan file for an amendment.

    This is a separate function from ``lock_plan()`` — do not modify
    ``lock_plan()``.  ``lock_plan()``'s unconditional refusal to lock after
    unmasking is the tool's core HARKing prevention guarantee and must not
    accept any bypass parameter.

    Parameters
    ----------
    study_id : str
    amendment_reason : str
        Required human-readable explanation for the amendment.
    planned_tests : list[dict], optional
        New pre-registered tests (pre-unmask amendment).  Only one of
        ``planned_tests`` or ``post_hoc_tests`` may be set.
    post_hoc_tests : list[dict], optional
        New post-hoc/exploratory tests (post-unmask amendment).

    Returns
    -------
    Path to the written lock file.

    Raises
    ------
    ValueError
        If ``amendment_reason`` is empty, or both/both-none of the test
        lists are provided, or state constraints are violated.
    RuntimeError
        If study has already been unmasked when trying a pre-unmask
        amendment (planned_tests), or if study has NOT been unmasked when
        trying a post-hoc amendment (post_hoc_tests).
    """
    if not amendment_reason:
        raise ValueError("amendment_reason is required for any amendment.")

    if not planned_tests and not post_hoc_tests:
        raise ValueError("Must provide either planned_tests (pre-unmask) or post_hoc_tests (post-hoc).")
    if planned_tests and post_hoc_tests:
        raise ValueError("Cannot provide both planned_tests and post_hoc_tests in one amendment.")

    # Load the latest locked plan to preserve existing state
    latest = load_plan(study_id)

    # ── State enforcement ─────────────────────────────────────────────
    conn = get_connection(study_id)
    cur = conn.execute("SELECT is_locked FROM studies WHERE id=?", (study_id,))
    row = cur.fetchone()
    state = row["is_locked"] if row else 0
    conn.close()

    if planned_tests is not None:
        # Pre-unmask amendment: study must still be locked/masked (state < 2)
        if state >= 2:
            raise RuntimeError(
                f"Cannot amend study '{study_id}' with pre-registered tests "
                f"after unmasking. The study has already been unmasked — "
                f"use lock_amendment(..., post_hoc_tests=...) for post-hoc "
                f"amendments instead."
            )
        new_planned = list(planned_tests)
        new_post_hoc = list(latest.post_hoc_tests)  # preserve any existing post-hoc tests
    else:
        # Post-hoc amendment: study must be unmasked (state == 2)
        if state < 2:
            raise RuntimeError(
                f"Cannot add post-hoc tests to study '{study_id}' before "
                f"unmasking. Post-hoc amendments require unmasked data."
            )
        new_planned = list(latest.planned_tests)
        new_post_hoc = list(latest.post_hoc_tests)
        # Dedup: only add tests not already present (by test_name + rationale)
        existing_keys = {(t.get("test_name"), t.get("rationale", "")) for t in new_post_hoc}
        for t in post_hoc_tests:
            key = (t.get("test_name"), t.get("rationale", ""))
            if key not in existing_keys:
                new_post_hoc.append(t)
                existing_keys.add(key)

    # ── Build the new plan ────────────────────────────────────────────
    plan = StudyPlan(
        study_id=study_id,
        study_type=latest.study_type,
        primary_comparison=latest.primary_comparison,
        primary_outcome_variable_ids=list(latest.primary_outcome_variable_ids),
        planned_tests=new_planned,
        covariates=list(latest.covariates),
        matching_criteria=list(latest.matching_criteria),
        warnings=dict(latest.warnings),
        role_overrides=dict(latest.role_overrides),
        audit=dict(latest.audit),
        post_hoc_tests=new_post_hoc,
        amendment_reason=amendment_reason,
        cox_ph_models=list(latest.cox_ph_models),
        diagnostic_results=list(latest.diagnostic_results),
    )

    plan.version = _next_version(study_id)
    plan.locked_at = None
    data = plan.to_dict()
    data["content_hash"] = _compute_hash(data)

    path = _plan_path(study_id, plan.version)
    path.write_text(json.dumps(data, indent=2))
    return path
