# Architecture

```mermaid
flowchart TB
    subgraph Inputs[Immutable inputs]
        PDF[Guidelines PDF]
        CSV[Holdings CSV]
        FC[Firm JSON configuration]
        AK[Answer keys]
    end

    subgraph Control[Ingestion control plane]
        HV[SHA-256 and schema validation]
        PM[Reviewed extraction manifest]
        AP[Human approval record]
    end

    subgraph Graph[Property graph]
        SD[SourceDocument / SourceChunk]
        DM[Fund / AssetClass / Limit / Aggregate / RiskMetric]
        PO[Position / Issuer / IssuerGroup]
        BA[BreachAction / Owner / ConfigurationRule]
        FI[Figure]
    end

    subgraph Deterministic[Deterministic data plane - no LLM access]
        QE[Graph query engine]
        DE[Decimal calculation and formatter]
        TR[Trace resolver]
        RC[Reconciler]
        XL[OOXML template exporter]
    end

    subgraph Narrative[Narrative sidecar]
        NP[Deterministic default or optional frontier API]
        FW[Numeric-token firewall]
    end

    subgraph Evidence[Evidence and output]
        DB[(Append-only SQLite hash chain)]
        JS[Graph / figures / reconciliation JSON]
        WB[Populated XLSX]
    end

    PDF --> HV
    CSV --> HV
    PM --> HV
    AP --> HV
    HV --> Graph
    FC --> Graph
    Graph --> QE --> DE --> FI
    FI --> TR --> RC
    AK --> RC
    FI -. read-only snapshot .-> NP --> FW
    RC --> XL
    FW --> XL
    XL --> WB
    Graph --> JS
    QE --> DB
    RC --> DB
    FW --> DB
    XL --> DB
```

## Graph model

```mermaid
flowchart LR
    Figure -->|COMPUTED_FROM| Position
    Figure -->|GOVERNED_BY| Limit
    Figure -->|PRODUCED_BY| ConfigurationRule
    Position -->|BELONGS_TO| AssetClass
    Position -->|ISSUED_BY| Issuer
    Issuer -->|ROLLS_UP_TO| IssuerGroup
    AssetClass -->|HAS_LIMIT| Limit
    AssetClass -->|CONTRIBUTES_TO| Aggregate
    Aggregate -->|HAS_LIMIT| Limit
    RiskMetric -->|HAS_LIMIT| Limit
    RiskMetric -->|ON_BREACH| BreachAction
    BreachAction -->|NOTIFIES| Owner
    Position -->|DERIVED_FROM| HoldingsChunk[SourceChunk]
    Limit -->|DERIVED_FROM| GuidelineChunk[SourceChunk]
    ConfigurationRule -->|DERIVED_FROM| ConfigChunk[SourceChunk]
    SourceDocument -->|CONTAINS| SourceChunk
```

Every rectangle is a property-graph node. Every node and edge has the same provenance envelope:

```json
{
  "source_document": "sample_fund_guidelines.pdf",
  "page": 2,
  "chunk_id": "chunk:concentration_p2",
  "ingested_at": "2026-08-24T00:00:00Z",
  "extraction_confidence": 0.99,
  "source_sha256": "..."
}
```

The graph is not decorative. Computation discovers a position's asset class through `BELONGS_TO`, limits through `HAS_LIMIT`, liquid/non-IG contributors through `CONTRIBUTES_TO`, and figure evidence through Figure-originating traversals. Export is impossible until each Figure reaches SourceChunk nodes.

## Module boundaries

| Module | Responsibility | May produce reported numbers? |
|---|---|---|
| `extraction.py` | Approval gate and graph construction | Source values only |
| `graph.py` | Provenance-aware storage and traversal | No |
| `computation.py` | Decimal calculations, status, and formatting | Yes - sole authority |
| `reconciliation.py` | Answer-key comparison and trace checks | Deltas only |
| `narrative.py` | Commentary and firewall | No |
| `xlsx.py` | Copy computed strings into fixed template cells | No calculation |
| `audit.py` | Append-only evidence | No |
| `pipeline.py` | Ordered gates and fail-closed orchestration | No calculation |
