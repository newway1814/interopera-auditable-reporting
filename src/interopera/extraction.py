from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from interopera.errors import ConfigurationError, ExtractionApprovalError
from interopera.graph import Node, PropertyGraph, Provenance
from interopera.utils import load_json, sha256_file, slugify


def _provenance(
    manifest: dict[str, Any], chunk: dict[str, Any], source_sha256: str | None = None
) -> Provenance:
    return Provenance(
        source_document=manifest["source_document"]["name"],
        page=int(chunk["page"]),
        chunk_id=chunk["id"],
        ingested_at=manifest["ingested_at"],
        extraction_confidence=float(chunk["extraction_confidence"]),
        source_sha256=source_sha256 or manifest["source_document"]["sha256"],
    )


def _normalise_text(value: str) -> str:
    return " ".join(value.replace("&amp;", "&").split()).casefold()


def load_approved_manifest(project_root: Path) -> dict[str, Any]:
    manifest_path = project_root / "config" / "rules_manifest.json"
    approval = load_json(project_root / "config" / "extraction_approval.json")
    manifest = load_json(manifest_path)
    if approval.get("status") != "APPROVED":
        raise ExtractionApprovalError("Extraction manifest has not passed the human approval gate")
    actual_manifest_hash = sha256_file(manifest_path)
    if actual_manifest_hash != approval.get("rules_manifest_sha256"):
        raise ExtractionApprovalError("Extraction manifest changed after human approval")
    guideline_path = project_root / "sample_docs" / manifest["source_document"]["name"]
    if sha256_file(guideline_path) != manifest["source_document"]["sha256"]:
        raise ExtractionApprovalError("Guidelines PDF does not match the approved source hash")
    minimum = float(approval["minimum_confidence"])
    low_confidence = [chunk["id"] for chunk in manifest["chunks"] if float(chunk["extraction_confidence"]) < minimum]
    if low_confidence:
        raise ExtractionApprovalError(f"Chunks below review threshold: {', '.join(low_confidence)}")

    pages = [page.extract_text() or "" for page in PdfReader(guideline_path).pages]
    for chunk in manifest["chunks"]:
        page_number = int(chunk["page"])
        if page_number < 1 or page_number > len(pages):
            raise ExtractionApprovalError(f"Invalid page for {chunk['id']}")
        page_text = _normalise_text(pages[page_number - 1])
        missing = [term for term in chunk["match_terms"] if _normalise_text(term) not in page_text]
        if missing:
            raise ExtractionApprovalError(f"Approved chunk {chunk['id']} no longer matches PDF terms: {missing}")
    return manifest


def _add_guideline_graph(graph: PropertyGraph, manifest: dict[str, Any]) -> dict[str, str]:
    chunks = {chunk["id"]: chunk for chunk in manifest["chunks"]}
    doc_prov = _provenance(manifest, chunks["chunk:allocations_p1"])
    graph.add_node(Node("doc:guidelines", "SourceDocument", {"name": manifest["source_document"]["name"], "sha256": manifest["source_document"]["sha256"]}, doc_prov))
    for chunk in manifest["chunks"]:
        provenance = _provenance(manifest, chunk)
        graph.add_node(Node(chunk["id"], "SourceChunk", {"section": chunk["section"], "summary": chunk["summary"]}, provenance))
        graph.add_edge("doc:guidelines", "CONTAINS", chunk["id"], provenance)

    graph.add_node(Node("fund:meridian", "Fund", {"name": "Meridian Fixed Income Fund", "base_currency": "SGD"}, doc_prov))
    asset_lookup: dict[str, str] = {}
    for spec in manifest["asset_classes"]:
        provenance = _provenance(manifest, chunks[spec["chunk_id"]])
        properties = {key: value for key, value in spec.items() if key not in {"id", "chunk_id"}}
        graph.add_node(Node(spec["id"], "AssetClass", properties, provenance))
        limit_id = f"limit:allocation:{spec['id'].split(':', 1)[1]}"
        graph.add_node(Node(limit_id, "Limit", {"kind": "allocation", "min": spec["min"], "max": spec["max"], "unit": "NAV"}, provenance))
        graph.add_edge(spec["id"], "HAS_LIMIT", limit_id, provenance)
        graph.add_edge(limit_id, "DERIVED_FROM", spec["chunk_id"], provenance)
        graph.add_edge("fund:meridian", "PERMITS", spec["id"], provenance)
        for name in [spec["name"], *spec.get("aliases", [])]:
            asset_lookup[slugify(name)] = spec["id"]

    for spec in manifest["aggregates"]:
        provenance = _provenance(manifest, chunks[spec["chunk_id"]])
        graph.add_node(Node(spec["id"], "Aggregate", {"name": spec["name"]}, provenance))
        limit_id = f"limit:{spec['id'].split(':', 1)[1]}"
        limit_props = {key: value for key, value in spec.items() if key in {"min", "max"}}
        limit_props.update({"kind": "aggregate", "unit": "NAV"})
        graph.add_node(Node(limit_id, "Limit", limit_props, provenance))
        graph.add_edge(spec["id"], "HAS_LIMIT", limit_id, provenance)
        graph.add_edge(limit_id, "DERIVED_FROM", spec["chunk_id"], provenance)
        for contributor in spec["contributors"]:
            graph.add_edge(contributor, "CONTRIBUTES_TO", spec["id"], provenance)

    for spec in manifest["risk_metrics"]:
        provenance = _provenance(manifest, chunks[spec["chunk_id"]])
        graph.add_node(Node(spec["id"], "RiskMetric", {"name": spec["name"], "unit": spec["unit"]}, provenance))
        limit_id = f"limit:{spec['id'].split(':', 1)[1]}"
        limit_props = {key: value for key, value in spec.items() if key in {"min", "max", "unit"}}
        limit_props["kind"] = "risk"
        graph.add_node(Node(limit_id, "Limit", limit_props, provenance))
        action_id = f"breach_action:{spec['id'].split(':', 1)[1]}"
        owner_id = f"owner:{slugify(spec['owner'])}"
        graph.add_node(Node(action_id, "BreachAction", {"action": spec["breach_action"]}, provenance))
        if owner_id not in graph.nodes:
            graph.add_node(Node(owner_id, "Owner", {"name": spec["owner"]}, provenance))
        graph.add_edge(spec["id"], "HAS_LIMIT", limit_id, provenance)
        graph.add_edge(spec["id"], "ON_BREACH", action_id, provenance)
        graph.add_edge(action_id, "NOTIFIES", owner_id, provenance)
        graph.add_edge(limit_id, "DERIVED_FROM", spec["chunk_id"], provenance)
        graph.add_edge(action_id, "DERIVED_FROM", spec["chunk_id"], provenance)
        graph.add_edge(owner_id, "DERIVED_FROM", spec["chunk_id"], provenance)
    return asset_lookup


def _add_holdings_graph(graph: PropertyGraph, project_root: Path, manifest: dict[str, Any], asset_lookup: dict[str, str]) -> None:
    holdings_path = project_root / "sample_docs" / "sample_holdings.csv"
    holdings_hash = sha256_file(holdings_path)
    ingested_at = manifest["ingested_at"]
    doc_prov = Provenance("sample_holdings.csv", 1, "chunk:holdings_header", ingested_at, 1.0, holdings_hash)
    graph.add_node(Node("doc:holdings", "SourceDocument", {"name": "sample_holdings.csv", "sha256": holdings_hash}, doc_prov))
    with holdings_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream), start=2):
            instrument_id = row["instrument_id"]
            chunk_id = f"chunk:holding:{instrument_id}"
            provenance = Provenance("sample_holdings.csv", 1, chunk_id, ingested_at, 1.0, holdings_hash)
            graph.add_node(Node(chunk_id, "SourceChunk", {"row": row_number, "summary": f"Holdings row for {instrument_id}"}, provenance))
            graph.add_edge("doc:holdings", "CONTAINS", chunk_id, provenance)
            asset_id = asset_lookup.get(slugify(row["asset_class"]))
            if not asset_id:
                raise ConfigurationError(f"Unresolved asset class in holdings row {row_number}: {row['asset_class']}")
            issuer_id = f"issuer:{slugify(row['issuer_name'])}"
            if issuer_id not in graph.nodes:
                graph.add_node(Node(issuer_id, "Issuer", {"name": row["issuer_name"], "issuer_type": row["issuer_type"]}, provenance))
                graph.add_edge(issuer_id, "DERIVED_FROM", chunk_id, provenance)
            parent_name = row["parent_issuer"].strip()
            if parent_name:
                parent_id = f"issuer_group:{slugify(parent_name)}"
                if parent_id not in graph.nodes:
                    graph.add_node(Node(parent_id, "IssuerGroup", {"name": parent_name}, provenance))
                    graph.add_edge(parent_id, "DERIVED_FROM", chunk_id, provenance)
                graph.add_edge(issuer_id, "ROLLS_UP_TO", parent_id, provenance)
            position_id = f"position:{instrument_id}"
            properties = {
                **row,
                "asset_class_id": asset_id,
                "market_value_sgd": row["market_value_sgd"],
                "modified_duration": row["modified_duration"],
            }
            graph.add_node(Node(position_id, "Position", properties, provenance))
            graph.add_edge(position_id, "BELONGS_TO", asset_id, provenance)
            graph.add_edge(position_id, "ISSUED_BY", issuer_id, provenance)
            graph.add_edge(position_id, "DERIVED_FROM", chunk_id, provenance)
            graph.add_edge("fund:meridian", "HOLDS", position_id, provenance)


def _add_configuration_graph(graph: PropertyGraph, project_root: Path, manifest: dict[str, Any], configuration: dict[str, Any]) -> None:
    source = configuration["source"]
    source_path = project_root / source["document"]
    source_hash = sha256_file(source_path)
    firm_id = configuration["firm_id"]
    doc_id = f"doc:config:{firm_id}"
    chunk_id = f"chunk:config:{firm_id}"
    provenance = Provenance(source["document"], int(source.get("page", 1)), chunk_id, manifest["ingested_at"], 1.0, source_hash)
    graph.add_node(Node(doc_id, "SourceDocument", {"name": source["document"], "sha256": source_hash}, provenance))
    graph.add_node(Node(chunk_id, "SourceChunk", {"section": source["section"], "summary": f"House conventions for {configuration['display_name']}"}, provenance))
    graph.add_edge(doc_id, "CONTAINS", chunk_id, provenance)
    rules = {
        "utilization": configuration["utilization"],
        "aggregate_non_ig": configuration["aggregate_non_ig"],
        "corporate_concentration": configuration["concentration"]["corporate"],
        "gre_concentration": configuration["concentration"]["gre"],
    }
    for rule_name, rule_value in rules.items():
        rule_id = f"config_rule:{firm_id}:{rule_name}"
        graph.add_node(Node(rule_id, "ConfigurationRule", {"name": rule_name, "definition": rule_value}, provenance))
        graph.add_edge(rule_id, "DERIVED_FROM", chunk_id, provenance)


def build_graph(project_root: Path, configuration: dict[str, Any]) -> tuple[PropertyGraph, dict[str, Any]]:
    manifest = load_approved_manifest(project_root)
    graph = PropertyGraph()
    asset_lookup = _add_guideline_graph(graph, manifest)
    _add_holdings_graph(graph, project_root, manifest, asset_lookup)
    _add_configuration_graph(graph, project_root, manifest, configuration)
    graph.assert_complete_provenance()
    return graph, manifest
