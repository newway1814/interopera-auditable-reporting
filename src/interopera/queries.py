from __future__ import annotations

from typing import Any

from interopera.graph import PropertyGraph


def breach_action_query(graph: PropertyGraph, risk_metric_id: str) -> dict[str, Any]:
    metric = graph.nodes[risk_metric_id]
    actions = graph.targets(metric.id, "ON_BREACH")
    if len(actions) != 1:
        raise ValueError(f"Expected one breach action for {risk_metric_id}")
    owners = graph.targets(actions[0].id, "NOTIFIES")
    if len(owners) != 1:
        raise ValueError(f"Expected one breach owner for {risk_metric_id}")
    return {
        "question": f"What happens if {metric.properties['name']} breaches its limit, and who is notified?",
        "answer": {"action": actions[0].properties["action"], "owner": owners[0].properties["name"]},
        "graph_path": f"({metric.id})-[:ON_BREACH]->({actions[0].id})-[:NOTIFIES]->({owners[0].id})",
        "citations": [
            {
                "source_doc": actions[0].provenance.source_document,
                "page": actions[0].provenance.page,
                "chunk_id": actions[0].provenance.chunk_id,
            }
        ],
    }
