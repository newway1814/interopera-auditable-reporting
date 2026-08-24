from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interopera import __version__
from interopera.audit import AppendOnlyAuditLog
from interopera.computation import Figure, compute_figures
from interopera.extraction import build_graph
from interopera.narrative import check_narrative_firewall, generate_narrative, generate_openai_narrative
from interopera.reconciliation import expected_answers, reconcile, traceability_check
from interopera.utils import canonical_json, load_json, sha256_bytes, sha256_file, write_json
from interopera.xlsx import export_report


@dataclass(frozen=True)
class FirmRunResult:
    firm_id: str
    run_id: str
    output_directory: Path
    reconciliation_passed: bool
    traceability_passed: bool
    firewall_passed: bool
    report_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "firm_id": self.firm_id,
            "run_id": self.run_id,
            "output_directory": str(self.output_directory),
            "reconciliation_passed": self.reconciliation_passed,
            "traceability_passed": self.traceability_passed,
            "firewall_passed": self.firewall_passed,
            "report_path": str(self.report_path),
        }


def _run_id(project_root: Path, configuration_path: Path) -> str:
    inputs = {
        "engine_version": __version__,
        "guidelines": sha256_file(project_root / "sample_docs" / "sample_fund_guidelines.pdf"),
        "holdings": sha256_file(project_root / "sample_docs" / "sample_holdings.csv"),
        "configuration": sha256_file(configuration_path),
        "manifest": sha256_file(project_root / "config" / "rules_manifest.json"),
    }
    return sha256_bytes(canonical_json(inputs).encode("utf-8"))[:20]


def run_firm(
    project_root: Path,
    output_root: Path,
    audit: AppendOnlyAuditLog,
    firm_id: str,
    narrative_provider: str = "deterministic",
    narrative_model: str = "gpt-5.4",
) -> FirmRunResult:
    config_path = project_root / "config" / f"{firm_id}.json"
    configuration = load_json(config_path)
    run_id = _run_id(project_root, config_path)
    firm_output = output_root / firm_id
    firm_output.mkdir(parents=True, exist_ok=True)

    previous = audit.latest_payload("CONFIGURATION_ACTIVATED")
    if previous and previous.get("firm_id") != firm_id:
        audit.append(run_id, "CONFIGURATION_CHANGED", {"from": previous.get("firm_id"), "to": firm_id, "configuration_sha256": sha256_file(config_path)})
    audit.append(run_id, "CONFIGURATION_ACTIVATED", {"firm_id": firm_id, "configuration_sha256": sha256_file(config_path)})
    audit.append(run_id, "GRAPH_CONSTRUCTION_STARTED", {"guidelines_sha256": sha256_file(project_root / "sample_docs" / "sample_fund_guidelines.pdf"), "holdings_sha256": sha256_file(project_root / "sample_docs" / "sample_holdings.csv")})

    graph, manifest = build_graph(project_root, configuration)
    audit.append(run_id, "GRAPH_CONSTRUCTION_COMPLETED", {"node_count": len(graph.nodes), "edge_count": len(graph.edges), "graph_sha256_before_figures": graph.digest(), "extraction_approval": "APPROVED"})

    figures = compute_figures(graph, configuration, manifest["ingested_at"])
    for figure in figures:
        audit.append(
            run_id,
            "FIGURE_COMPUTED",
            {
                "figure": figure.id,
                "raw_value": format(figure.raw_value, "f"),
                "display_value": figure.value,
                "formula": figure.formula,
                "config_rule": figure.config_rule,
                "input_nodes": list(figure.input_node_ids),
                "graph_path_count": len(figure.graph_paths),
            },
        )

    traceability = traceability_check(figures)
    audit.append(run_id, "TRACEABILITY_CHECKED", {"passed": traceability["all_pass"], "figures_checked": len(figures)})
    if not traceability["all_pass"]:
        raise RuntimeError("Traceability gate failed; report export is forbidden")

    reconciliation = reconcile(figures, expected_answers(project_root, firm_id))
    audit.append(run_id, "RECONCILIATION_COMPLETED", {"passed": reconciliation["all_pass"], "figures_checked": len(figures), "failed_figures": [row["figure"] for row in reconciliation["figures"] if not row["pass"]]})

    narrative = generate_openai_narrative(figures, narrative_model) if narrative_provider == "openai" else generate_narrative(figures)
    firewall = check_narrative_firewall(narrative, figures)
    audit.append(run_id, "NARRATIVE_FIREWALL_CHECKED", {"passed": firewall.passed, "unexpected_numbers": list(firewall.unexpected_numbers), "provider": narrative_provider, "model": narrative_model if narrative_provider == "openai" else None})
    if not firewall.passed:
        raise RuntimeError("Narrative firewall failed; report export is forbidden")

    figures_document = {"schema_version": 1, "firm_id": firm_id, "run_id": run_id, "engine": "deterministic_decimal", "llm_in_numeric_path": False, "figures": [figure.to_dict() for figure in figures]}
    write_json(firm_output / "figures.json", figures_document)
    write_json(firm_output / "graph.json", graph.to_dict())
    write_json(firm_output / "reconciliation.json", reconciliation)
    write_json(firm_output / "traceability.json", traceability)
    write_json(firm_output / "narrative_firewall.json", firewall.to_dict())
    (firm_output / "narrative.txt").write_text(narrative + "\n", encoding="utf-8")

    report_path = firm_output / f"{firm_id}_report.xlsx"
    export_report(project_root / "sample_docs" / "report_template.xlsx", report_path, figures)
    try:
        audit_report_path = str(report_path.relative_to(project_root))
    except ValueError:
        audit_report_path = report_path.name
    audit.append(run_id, "REPORT_EXPORTED", {"firm_id": firm_id, "report_path": audit_report_path, "report_sha256": sha256_file(report_path), "reconciliation_passed": reconciliation["all_pass"]})

    return FirmRunResult(
        firm_id=firm_id,
        run_id=run_id,
        output_directory=firm_output,
        reconciliation_passed=bool(reconciliation["all_pass"]),
        traceability_passed=bool(traceability["all_pass"]),
        firewall_passed=firewall.passed,
        report_path=report_path,
    )


def run_pipeline(
    project_root: Path,
    output_root: Path,
    audit_path: Path,
    firms: list[str],
    narrative_provider: str = "deterministic",
    narrative_model: str = "gpt-5.4",
) -> dict[str, Any]:
    results: list[FirmRunResult] = []
    with AppendOnlyAuditLog(audit_path) as audit:
        for firm_id in firms:
            results.append(run_firm(project_root, output_root, audit, firm_id, narrative_provider, narrative_model))
        audit_valid = audit.verify_chain()
        audit_count = audit.count()
    summary = {
        "schema_version": 1,
        "all_pass": audit_valid and all(result.reconciliation_passed and result.traceability_passed and result.firewall_passed for result in results),
        "audit_chain_valid": audit_valid,
        "audit_event_count": audit_count,
        "firms": [result.to_dict() for result in results],
    }
    write_json(output_root / "run_summary.json", summary)
    return summary
