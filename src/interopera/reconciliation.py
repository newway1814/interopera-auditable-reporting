from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from interopera.computation import Figure
from interopera.utils import load_json
from interopera.xlsx import answer_key_from_xlsx


def _expected_raw(metric: str, display: str) -> Decimal:
    cleaned = display.replace(",", "")
    if cleaned.endswith("%"):
        return Decimal(cleaned[:-1]) / Decimal("100")
    if metric == "Portfolio modified duration":
        return Decimal(cleaned.split()[0])
    if metric == "Portfolio DV01":
        match = re.search(r"SGD\s+([\d.]+)", cleaned)
        if not match:
            raise ValueError(f"Unparseable DV01 answer: {display}")
        return Decimal(match.group(1))
    raise ValueError(f"Unsupported answer-key value: {metric} = {display}")


def _tolerance(metric: str) -> Decimal:
    if metric == "Portfolio modified duration":
        return Decimal("0.005")
    if metric == "Portfolio DV01":
        return Decimal("0.5")
    return Decimal("0.0005")


def expected_answers(project_root: Path, firm_id: str) -> dict[str, dict[str, str]]:
    if firm_id == "firm_a":
        return answer_key_from_xlsx(project_root / "sample_docs" / "firm_A_answer_key.xlsx")
    if firm_id == "firm_b":
        return load_json(project_root / "config" / "firm_b_expected.json")
    raise ValueError(f"Unknown firm: {firm_id}")


def reconcile(figures: list[Figure], expected: dict[str, dict[str, str]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for figure in figures:
        answer = expected[figure.metric]
        expected_raw = _expected_raw(figure.metric, answer["value"])
        delta = figure.raw_value - expected_raw
        fields_match = all(
            (
                figure.value == answer["value"],
                figure.limit == answer["limit"],
                figure.utilization == answer["utilization"],
                figure.status == answer["status"],
            )
        )
        passed = abs(delta) <= _tolerance(figure.metric) and fields_match
        rows.append(
            {
                "figure": figure.id,
                "metric": figure.metric,
                "actual": {"value": figure.value, "limit": figure.limit, "utilization": figure.utilization, "status": figure.status},
                "expected": answer,
                "delta": format(delta, "f"),
                "tolerance": format(_tolerance(figure.metric), "f"),
                "pass": passed,
            }
        )
    missing = sorted(set(expected) - {figure.metric for figure in figures})
    return {"all_pass": all(row["pass"] for row in rows) and not missing, "missing_metrics": missing, "figures": rows}


def traceability_check(figures: list[Figure]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for figure in figures:
        valid_paths = bool(figure.graph_paths) and all(path[-1].get("type") == "SourceChunk" for path in figure.graph_paths)
        has_guideline = any(citation["source_doc"] == "sample_fund_guidelines.pdf" for citation in figure.citations)
        has_holdings = any(citation["source_doc"] == "sample_holdings.csv" for citation in figure.citations)
        rows.append({"figure": figure.id, "pass": valid_paths and has_guideline and has_holdings, "path_count": len(figure.graph_paths), "has_guideline_source": has_guideline, "has_holdings_source": has_holdings})
    return {"all_pass": all(row["pass"] for row in rows), "figures": rows}
