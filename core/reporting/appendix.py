"""Deterministic manuscript-appendix rendering for exported study results."""

from __future__ import annotations

from core.reporting.strobe_checklist import check_study


def _table(headers: list[str], rows: list[dict]) -> str:
    if not headers:
        return "_No data available._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines) + "\n"


def generate_appendix(export: dict, study_id: str) -> str:
    """Render Table 1, UROs, warnings, and STROBE from canonical export data."""
    table1 = export.get("table1") or {}
    metadata = export.get("study_metadata", {})
    sections = [
        f"# Statistical Appendix — {metadata.get('name', study_id)}",
        "",
        "## Table 1",
        "",
        _table(table1.get("headers", []), table1.get("rows", [])),
        "## URO Analysis Results",
        "",
    ]
    uro_rows = []
    for result in export.get("analysis_results", []):
        statistic = result.get("statistic") or {}
        uro_rows.append({
            "Test": result.get("test_name", ""),
            "Status": result.get("status", ""),
            "Statistic": statistic.get("value", "") if isinstance(statistic, dict) else statistic,
            "P-value": result.get("p_value", ""),
            "Adjusted P-value": result.get("adjusted_p_value", ""),
            "Reason": result.get("reason", "") or "",
        })
    sections.append(_table(["Test", "Status", "Statistic", "P-value", "Adjusted P-value", "Reason"], uro_rows))

    warnings = (export.get("locked_plan") or {}).get("warnings", {})
    sections.extend(["## Warnings", ""])
    if warnings:
        sections.extend(f"- {key}: {value}" for key, value in sorted(warnings.items()))
    else:
        sections.append("_No plan warnings recorded._")

    sections.extend(["", "## STROBE Checklist", ""])
    for item in check_study(study_id):
        mark = "x" if item.satisfied else " "
        sections.append(f"- [{mark}] **{item.item_id}** — {item.evidence}")
    sections.append("")
    return "\n".join(sections)
