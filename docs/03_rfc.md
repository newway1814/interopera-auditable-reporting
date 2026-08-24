# RFC: Deterministic graph-backed compliance reporting

Status: Implemented for the synthetic Meridian Fixed Income Fund fixture.

## Context

The system must satisfy five coupled properties: reproducibility, graph-backed traceability, proof that an LLM did not produce or alter figures, exact Firm A reconciliation, and configuration-only Firm B switching. The important architectural consequence is that extraction, graph approval, computation, narrative, reconciliation, export, and audit cannot be a single generative workflow.

## Decision

Use a reviewed extraction manifest to build a small in-process property graph; compute figures with a deterministic Decimal engine that queries the graph; represent firm conventions in a constrained JSON mini-DSL; add each result back to the graph as a Figure node; require Figure-to-SourceChunk paths before emission; reconcile against independent answer-key artifacts; make narrative a downstream sidecar; and append all material events to a trigger-protected SQLite hash chain.

## Why the LLM cannot be the source of a number

The numeric call graph is `pipeline -> computation -> graph`. `computation.py` accepts only graph data and configuration and uses `Decimal`; it has no narrative-provider parameter, network client, prompt, or model response type. `xlsx.py` receives finalized Figure objects and only copies their strings into fixed cells. The optional model call occurs later and returns one string. That string is written only to `narrative.txt`; it is never parsed into a Figure or workbook numeric cell.

The structural controls are stronger than a prompt:

1. The calculation module cannot call a model.
2. The narrative module cannot mutate the frozen Figure dataclass or graph.
3. Reconciliation happens from deterministic figures, independent of narrative.
4. Export occurs only after a numeric-token subset check on commentary.
5. The audit event identifies the narrative provider and any rejected tokens.

The default evaluator path does not call an LLM at all. The optional Responses API adapter demonstrates the permitted boundary without making tests or report generation depend on a key or nondeterministic service.

## How a figure traces through the graph

During computation the engine adds a Figure node and edges:

- `COMPUTED_FROM` each Position used in the value.
- `GOVERNED_BY` the exact Limit node.
- `PRODUCED_BY` the active ConfigurationRule node.

Positions lead to holdings SourceChunks through `DERIVED_FROM`; limits lead to guideline SourceChunks; configuration rules lead to their config/brief SourceChunk. A breadth-first traversal from the Figure emits explicit alternating node/edge paths. Citations are derived from the terminal chunk provenance, not separately typed metadata. If the traversal returns no paths, `TraceabilityError` is raised before serialization or export.

Example conceptual paths:

```text
(Figure:aggregate.non_ig)-[:COMPUTED_FROM]->(Position:HY-01)-[:DERIVED_FROM]->(SourceChunk:holding:HY-01)
(Figure:aggregate.non_ig)-[:GOVERNED_BY]->(Limit:non_ig)-[:DERIVED_FROM]->(SourceChunk:allocations_p2)
(Figure:aggregate.non_ig)-[:PRODUCED_BY]->(ConfigurationRule:firm_b:aggregate_non_ig)-[:DERIVED_FROM]->(SourceChunk:config:firm_b)
```

This design makes the graph part of both computation and evidence. It is not an unused copy of extracted facts.

## Firm method expression and switching

Firm configuration contains three bounded dimensions:

- A position selector composed from `any`, `all`, `equals`, `in`, and `rating_below`.
- A concentration grouping key (`issuer_name` or `parent_or_issuer`).
- A utilization renderer (`percent_1dp` or `truncated_bps`).

The engine implements generic operators and validates fields/ratings/groupers. It contains no `if firm == ...` branch. Firm B's fallen-angel selection, parent rollup, and basis-point formatting are therefore data. The `CONFIGURATION_CHANGED` audit event proves which method was active.

The mini-DSL is deliberately small. Arbitrary expressions or embedded Python would weaken reviewability, make hashes less meaningful, and create an injection surface. Extending the vocabulary requires an engine release and tests; changing a firm's selection among supported operators does not.

## Reconciliation

Firm A expectations are read from the supplied workbook through a minimal OOXML reader. Firm B's brief contains only changed values and conventions; `firm_b_expected.json` records the full independently reviewed expected display set so automated reconciliation does not derive expectations by reusing the computation function.

Reconciliation checks exact display equality for value, limit, utilization, and status, plus raw numeric delta against a stated precision-derived tolerance. This prevents a method from passing because it happens to land near the expected number while formatting or status semantics differ. Current deltas are zero.

## Determinism

- Input, manifest, and configuration hashes form the stable run ID.
- Financial arithmetic uses `Decimal`, never binary float.
- Rounding is explicit (`ROUND_HALF_UP`) and Firm B truncation is explicit (`ROUND_DOWN`).
- Nodes, edges, paths, JSON keys, and position inputs are sorted.
- JSON is serialized with stable formatting.
- XLSX export preserves source ZIP entry metadata and order.
- Actual timestamps are confined to the persistent audit database, outside the compared numeric artifacts.

The automated replay test runs both firms twice into separate directories and compares hashes for figures, graph, reconciliation, and workbook files.

## Append-only audit design

SQLite is sufficient for the sample and easy for an examiner to inspect. Each event contains canonical payload JSON, previous hash, and event hash. Two database triggers abort every `UPDATE` and `DELETE`, even if a caller bypasses application code. There are no update/delete methods. Tests issue direct SQL and assert rejection, then re-verify the chain.

This is tamper-evident and mutation-resistant within the process boundary, not production WORM. A production deployment would periodically sign chain heads and write events to object-lock storage or an immutable ledger under separate administrative control.

## Human approval model

Extraction is error-prone, so the approved manifest is a first-class input. Approval binds reviewer status to the manifest SHA-256. The runtime also validates source hash, page count/terms, confidence threshold, and holdings ontology resolution. Any manifest edit invalidates approval. A production UI would show before/after graph diffs and require an authenticated reviewer signature; the checked-in approval demonstrates the same state transition for a fixed synthetic fixture.

## Alternatives considered

### Let an LLM extract and calculate in one prompt

Rejected. Reproducibility, numeric firewall proof, independent reconciliation, and durable lineage would depend on model behavior rather than program structure.

### Build a graph but calculate directly from CSV dataframes

Rejected. It would satisfy graph presence without satisfying the required figure-to-graph-to-source chain. Here, asset membership, contributor sets, limits, and source paths are all graph queries.

### Neo4j or another graph server

Deferred. It adds deployment risk to a thirteen-row sample without improving semantics. `PropertyGraph` has a narrow replacement seam; the exported graph is service-neutral JSON.

### Formulas inside the output workbook

Rejected as the authoritative computation layer. They reintroduce hidden mutable logic and complicate byte-level replay. The workbook is a presentation artifact populated from frozen computed results; JSON and the graph are the audit authority.

### General-purpose expression language for firm rules

Rejected. A constrained DSL is easier to review, hash, test, and secure.

## Failure behavior

The pipeline fails closed on changed or unapproved extraction, source hash drift, low confidence, unresolved asset classes, invalid configuration operators or ratings, missing graph paths, answer-key mismatch, narrative novel numbers, non-positive NAV, or a broken audit chain. It never substitutes an estimate for a missing trace.

## Production evolution

Replace the in-process graph and SQLite with durable services behind the same interfaces; sign approvals and chain heads; add effective-dated configs; ingest independently signed market data; enforce RBAC and four-eyes approvals; encrypt at rest; add WORM retention policies; version calculation operators; add schema migration tooling; introduce property-based tests and golden portfolios; and separate report creation from human-controlled distribution.
