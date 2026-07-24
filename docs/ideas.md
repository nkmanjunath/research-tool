# Ideas — explorations not yet ready for implementation

## Three-Layer Transparency for Assumption Warnings

A staged disclosure model for statistical assumption checks, designed so the
main workflow stays clean while offering a breadcrumb trail for users who want
to understand *why* a recommendation was made.

### Layer 1 — Inline Actionable Warning (the main workflow)

Concise, zero-fluff status update. Printed directly in the CLI at plan time.

> ⚠️ Chi-square expects at least 5 counts per cell (Group B expected count is 1.1). Consider using Fisher's Exact Test instead.

Constraints:
- Must name the count and the offending cell, but never outcome category labels
  (the existing chi-square check already enforces this — the cell identified
  must be by group, never by outcome value).
- Recommend the alternative test and move on.

### Layer 2 — The "Why?" Drawer (optional expansion)

A lightweight expandable explanation triggered by a `--why` flag or an
interactive prompt (`?`). Not shown by default.

Content structure (3-part schema):
1. **What happened**: Chi-square relies on mathematical approximations that
   break down when numbers are small.
2. **Why it matters**: A cell count under 5 makes the p-value unreliable (it
   can falsely flag or miss real effects).
3. **What the alternative does**: Fisher's Exact Test calculates the exact
   probability instead of approximating it.

This is the "competent-but-rusty" user path — they know enough to want the
rationale but don't need to open a textbook.

### Layer 3 — External Documentation (v2)

A link printed at the end of the layer-2 explanation pointing to a knowledge-
base page or interactive explainer for users who realise they need a refresher
on the underlying concept (hypothesis testing, contingency tables, etc.).

### Key design rule

Never force Layer 2 or Layer 3 onto someone who just wants Layer 1. The main
workflow stays clean — the breadcrumb trail is visible but opt-in only.

### Open questions

- How does Layer 2 work in a CLI? `--why` flag per test? Interactive mode
  (`?` key during plan declaration)?
- Should the explanation text live in `test_selector.py` (on each
  `AssumptionCheck` subclass) or in a separate knowledge-base file?
- Are the 3-part explanations themselves testable? (They should be — stale
  explanations that drift from the actual check logic are worse than none.)
