# Decisions

## Ponytail / ultra reductions

| What was skipped | Why | Add when |
|---|---|---|
| SQLAlchemy ORM | sqlite3 stdlib covers 4 simple tables | Tables get complex joins or need migrations |
| typer/click CLI | argparse is stdlib, same result | CLI grows beyond 12 commands |
| Pytest fixture framework | `make_patients()` helper is simpler and sufficient | Tests need complex fixture chains or session-scoped setup |
| `data/` directory in repo | Generated at runtime under `$PWD/data/studies/` | Need to ship study templates |
| Docker / cloud / multi-tenancy | Explicitly out of scope for v1 | — |

## Defensible choices made without explicit spec

- **Study ID format**: UUID4 hex string. Unique, non-sequential, no collision risk.
- **Outcome masking enforced at sqlite3 connection level**: `get_connection()` wraps the connection so outcome columns return NULL pre-lock. This catches raw SQL and ORM bypasses at the same point.
- **Time-to-event encoding**: PFS/OS stored as `(time_days, event_status)` — standard survival pair. The `time_to_event` data type expects two columns (duration + indicator).
- **Multiple comparisons**: Bonferroni as default (simplest). Holm-Bonferroni and BH offered as options.
- **Lock file naming**: `study_plan.v{N}.locked.json` starting at v1. Never overwritten.
