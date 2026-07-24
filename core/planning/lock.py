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
