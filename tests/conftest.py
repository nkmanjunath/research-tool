"""Synthetic patient data generator for tests.

Never commits real patient data.  This is the sole source of test data
for the entire project.
"""

from __future__ import annotations
import csv
import io
import random


def synthetic_patients(n: int = 30, seed: int = 42) -> str:
    """Return a CSV string of n synthetic patients.

    Schema mimics a retrospective myeloma study:
      age, sex, iss_stage, prior_lines, high_risk_cytogenetics,
      treatment_arm, response_category, pfs_days, pfs_event, os_days, os_event
    """
    rng = random.Random(seed)
    rows = [
        ["age", "sex", "iss_stage", "prior_lines", "high_risk_cytogenetics",
         "treatment_arm", "response_category", "pfs_days", "pfs_event", "os_days", "os_event"]
    ]
    for _ in range(n):
        age = rng.randint(45, 85)
        sex = rng.choice(["M", "F"])
        iss = rng.choice(["I", "II", "III"])
        prior = rng.randint(0, 6)
        high_risk = rng.choice(["yes", "no"])
        arm = rng.choice(["A", "B"])
        resp = rng.choices(
            ["CR", "PR", "MR", "SD", "PD"],
            weights=[15, 30, 20, 20, 15],
        )[0]
        pfs_days = max(0, int(rng.expovariate(1 / 300)))
        pfs_event = 1 if resp in ("PD",) or pfs_days < 200 else rng.choices([0, 1], weights=[40, 60])[0]
        os_days = max(0, int(rng.expovariate(1 / 500)))
        os_event = rng.choices([0, 1], weights=[30, 70])[0]
        rows.append([age, sex, iss, prior, high_risk, arm, resp, pfs_days, pfs_event, os_days, os_event])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows(rows)
    return buf.getvalue()
