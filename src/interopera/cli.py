from __future__ import annotations

import argparse
import json
from pathlib import Path

from interopera.audit import AppendOnlyAuditLog
from interopera.extraction import build_graph
from interopera.pipeline import run_pipeline
from interopera.queries import breach_action_query
from interopera.utils import load_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic, graph-traceable portfolio compliance reporting")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Build graphs, compute figures, reconcile, and export reports")
    run.add_argument("--firm", choices=["firm_a", "firm_b", "all"], default="all")
    run.add_argument("--output", default="artifacts")
    run.add_argument("--audit-db", default="artifacts/audit.db")
    run.add_argument("--narrative-provider", choices=["deterministic", "openai"], default="deterministic")
    run.add_argument("--narrative-model", default="gpt-5.4")

    trace = subparsers.add_parser("trace", help="Replay a reported figure from saved artifacts")
    trace.add_argument("--firm", choices=["firm_a", "firm_b"], required=True)
    trace.add_argument("--figure", required=True)
    trace.add_argument("--output", default="artifacts")

    query = subparsers.add_parser("query-breach", help="Demonstrate a multi-hop breach-action graph query")
    query.add_argument("--firm", choices=["firm_a", "firm_b"], default="firm_a")
    query.add_argument("--metric", choices=["modified_duration", "dv01", "single_corporate", "gre"], default="modified_duration")

    verify = subparsers.add_parser("verify-audit", help="Verify the persistent audit hash chain")
    verify.add_argument("--audit-db", default="artifacts/audit.db")
    return parser


def _trace(project_root: Path, output: Path, firm_id: str, figure_id: str) -> dict[str, object]:
    figures_doc = load_json(output / firm_id / "figures.json")
    reconciliation = load_json(output / firm_id / "reconciliation.json")
    figure = next((item for item in figures_doc["figures"] if item["figure"] == figure_id), None)
    row = next((item for item in reconciliation["figures"] if item["figure"] == figure_id), None)
    if not figure or not row:
        raise SystemExit(f"Unknown figure {figure_id!r} for {firm_id}")
    return {
        "figure": figure_id,
        "value": figure["value"],
        "status": figure["status"],
        "formula": figure["formula"],
        "configuration_rule": figure["config_rule"],
        "graph_paths": figure["graph_paths"],
        "citations": figure["citations"],
        "reconciliation": {"delta": row["delta"], "tolerance": row["tolerance"], "pass": row["pass"]},
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = Path.cwd().resolve()
    if args.command == "run":
        firms = ["firm_a", "firm_b"] if args.firm == "all" else [args.firm]
        summary = run_pipeline(
            project_root,
            (project_root / args.output).resolve(),
            (project_root / args.audit_db).resolve(),
            firms,
            narrative_provider=args.narrative_provider,
            narrative_model=args.narrative_model,
        )
        print(json.dumps(summary, indent=2))
        return 0 if summary["all_pass"] else 1
    if args.command == "trace":
        print(json.dumps(_trace(project_root, (project_root / args.output).resolve(), args.firm, args.figure), indent=2))
        return 0
    if args.command == "query-breach":
        configuration = load_json(project_root / "config" / f"{args.firm}.json")
        graph, _ = build_graph(project_root, configuration)
        print(json.dumps(breach_action_query(graph, f"risk_metric:{args.metric}"), indent=2))
        return 0
    if args.command == "verify-audit":
        with AppendOnlyAuditLog((project_root / args.audit_db).resolve()) as audit:
            valid = audit.verify_chain()
            print(json.dumps({"valid": valid, "event_count": audit.count()}, indent=2))
        return 0 if valid else 1
    raise AssertionError("unreachable")
