from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from interopera.errors import TraceabilityError
from interopera.utils import canonical_json, sha256_bytes


@dataclass(frozen=True)
class Provenance:
    source_document: str
    page: int
    chunk_id: str
    ingested_at: str
    extraction_confidence: float
    source_sha256: str | None = None


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    properties: dict[str, Any]
    provenance: Provenance


@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    relation: str
    target: str
    properties: dict[str, Any]
    provenance: Provenance


class PropertyGraph:
    """Small deterministic property graph with explicit provenance everywhere."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self._outgoing: dict[str, list[str]] = {}

    def add_node(self, node: Node) -> None:
        existing = self.nodes.get(node.id)
        if existing and existing != node:
            raise ValueError(f"Conflicting node definition: {node.id}")
        self.nodes[node.id] = node
        self._outgoing.setdefault(node.id, [])

    def add_edge(
        self,
        source: str,
        relation: str,
        target: str,
        provenance: Provenance,
        properties: dict[str, Any] | None = None,
    ) -> Edge:
        if source not in self.nodes or target not in self.nodes:
            raise ValueError(f"Unknown edge endpoint: {source} -[{relation}]-> {target}")
        edge_id = f"edge:{source}:{relation}:{target}"
        edge = Edge(edge_id, source, relation, target, properties or {}, provenance)
        existing = self.edges.get(edge_id)
        if existing and existing != edge:
            raise ValueError(f"Conflicting edge definition: {edge_id}")
        self.edges[edge_id] = edge
        if edge_id not in self._outgoing[source]:
            self._outgoing[source].append(edge_id)
            self._outgoing[source].sort()
        return edge

    def outgoing(self, node_id: str, relation: str | None = None) -> list[Edge]:
        edges = [self.edges[edge_id] for edge_id in self._outgoing.get(node_id, [])]
        return [edge for edge in edges if relation is None or edge.relation == relation]

    def targets(self, node_id: str, relation: str) -> list[Node]:
        return [self.nodes[edge.target] for edge in self.outgoing(node_id, relation)]

    def nodes_of_type(self, node_type: str) -> list[Node]:
        return sorted((node for node in self.nodes.values() if node.type == node_type), key=lambda item: item.id)

    def paths_to_type(self, start: str, target_type: str = "SourceChunk", max_depth: int = 6) -> list[list[dict[str, str]]]:
        if start not in self.nodes:
            raise TraceabilityError(f"Unknown graph start node: {start}")
        paths: list[list[dict[str, str]]] = []
        queue: deque[tuple[str, list[dict[str, str]], frozenset[str]]] = deque()
        start_node = self.nodes[start]
        queue.append((start, [{"kind": "node", "id": start, "type": start_node.type}], frozenset({start})))
        while queue:
            current, path, visited = queue.popleft()
            if len(path) > max_depth * 2 + 1:
                continue
            for edge in self.outgoing(current):
                if edge.target in visited:
                    continue
                target = self.nodes[edge.target]
                next_path = path + [
                    {"kind": "edge", "id": edge.id, "relation": edge.relation},
                    {"kind": "node", "id": target.id, "type": target.type},
                ]
                if target.type == target_type:
                    paths.append(next_path)
                else:
                    queue.append((target.id, next_path, visited | {target.id}))
        return sorted(paths, key=canonical_json)

    def require_traceability(self, figure_id: str) -> list[list[dict[str, str]]]:
        paths = self.paths_to_type(figure_id)
        if not paths:
            raise TraceabilityError(f"Figure {figure_id} has no path to a source chunk")
        for path in paths:
            terminal = path[-1]
            if terminal["type"] != "SourceChunk":
                raise TraceabilityError(f"Figure {figure_id} has a malformed trace path")
        return paths

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": node.id,
                    "type": node.type,
                    "properties": node.properties,
                    "provenance": asdict(node.provenance),
                }
                for node in sorted(self.nodes.values(), key=lambda item: item.id)
            ],
            "edges": [
                {
                    "id": edge.id,
                    "source": edge.source,
                    "relation": edge.relation,
                    "target": edge.target,
                    "properties": edge.properties,
                    "provenance": asdict(edge.provenance),
                }
                for edge in sorted(self.edges.values(), key=lambda item: item.id)
            ],
        }

    def digest(self) -> str:
        return sha256_bytes(canonical_json(self.to_dict()).encode("utf-8"))

    def assert_complete_provenance(self) -> None:
        items: Iterable[Node | Edge] = [*self.nodes.values(), *self.edges.values()]
        for item in items:
            provenance = item.provenance
            if not all((provenance.source_document, provenance.chunk_id, provenance.ingested_at)):
                raise TraceabilityError(f"Missing provenance on {item.id}")
            if provenance.page < 0 or not 0 <= provenance.extraction_confidence <= 1:
                raise TraceabilityError(f"Invalid provenance on {item.id}")
