# InterOpera Auditable Portfolio Reporting

A runnable reference implementation for the InterOpera Senior Software Engineer take-home. It converts the supplied guidelines and holdings into one provenance-rich property graph, computes every report figure by graph traversal, reconciles both firms, exports the supplied Excel template, and records an append-only audit trail.

## Start with one command

Python 3.11 or newer is required.

```bash
pip install -r requirements.txt && python -m interopera run --firm all
```

The command exits non-zero if extraction approval, traceability, reconciliation, the narrative firewall, or the audit hash chain fails. It does not require an API key.

## What it produces

`artifacts/firm_a/` and `artifacts/firm_b/` each contain:

- `<firm>_report.xlsx` - the populated supplied template.
- `figures.json` - exact values, formula identifiers, input nodes, graph paths, and citations.
- `graph.json` - the complete graph, including computed Figure nodes.
- `reconciliation.json` - expected/actual/delta/tolerance/pass for every figure.
- `traceability.json` - proof that every figure reaches both guideline and holdings source chunks.
- `narrative.txt` and `narrative_firewall.json` - commentary and the no-new-numbers proof.

`artifacts/audit.db` is persistent and append-only. `artifacts/run_summary.json` gives the overall result.

## Expected reconciliation

Firm A reproduces all supplied answer-key displays exactly. Firm B changes only configuration:

| Figure | Firm A | Firm B | Configuration effect |
|---|---:|---:|---|
| Aggregate non-IG | 15.0% / OK | 21.0% / BREACH | Include ratings below BBB-, including fallen angels |
| Largest GRE | 7.0% / OK | 13.0% / BREACH | Group GREs by parent-or-issuer |
| Utilization | 58.3% style | 5833 bps style | Truncate ratio multiplied by 10,000 |

Everything else is unchanged. The engine uses `Decimal`, stable ordering, canonical JSON, and deterministic ZIP metadata. Tests run the whole system twice and compare the report and numeric artifact hashes.

## Design in one minute

1. `config/rules_manifest.json` is the reviewed extraction of the PDF. The runtime re-extracts page text, verifies identifying terms, verifies the PDF hash, and verifies the manifest against `config/extraction_approval.json`. A changed or low-confidence extraction is blocked.
2. `src/interopera/extraction.py` builds SourceDocument, SourceChunk, Fund, AssetClass, Limit, Aggregate, RiskMetric, BreachAction, Owner, Issuer, IssuerGroup, Position, and ConfigurationRule nodes. Every node and edge carries document, page, chunk, ingestion time, confidence, and source hash.
3. `src/interopera/computation.py` obtains holdings, membership, contributors, limits, and thresholds only through graph queries. It adds a Figure node and `COMPUTED_FROM`, `GOVERNED_BY`, and `PRODUCED_BY` edges before emitting a result.
4. A figure without a path from Figure to SourceChunk raises an error and cannot reach export.
5. `src/interopera/audit.py` appends hash-chained events to SQLite. Database triggers reject `UPDATE` and `DELETE`, including direct SQL.

Detailed flows, architecture, and rationale are in [docs/01_flow_and_audit_events.md](docs/01_flow_and_audit_events.md), [docs/02_architecture.md](docs/02_architecture.md), and [docs/03_rfc.md](docs/03_rfc.md).

## Firm configuration mini-DSL

Firm behavior is data in `config/firm_a.json` and `config/firm_b.json`. The engine supports composable `any`/`all` predicates, `equals`, `in`, and `rating_below`; issuer grouping keys; and utilization formatters.

```json
{
  "aggregate_non_ig": {
    "selector": {
      "any": [
        {"field": "asset_class_id", "in": ["asset_class:high_yield", "asset_class:structured_credit"]},
        {"field": "credit_rating", "rating_below": "BBB-"}
      ]
    }
  },
  "concentration": {
    "gre": {"selector": {"field": "issuer_type", "equals": "GRE"}, "group_by": "parent_or_issuer"}
  },
  "utilization": {"format": "truncated_bps"}
}
```

Switch with `--firm firm_a`, `--firm firm_b`, or `--firm all`; no source edit is needed.

## Useful audit commands

Replay one figure, including all graph paths, citations, configuration rule, and answer-key delta:

```bash
python -m interopera trace --firm firm_b --figure aggregate.non_ig
```

Run a multi-hop domain query without re-reading the PDF:

```bash
python -m interopera query-breach --metric modified_duration
```

Verify the persistent audit chain:

```bash
python -m interopera verify-audit
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers exact Firm A reconciliation, config-only Firm B changes, byte-identical reruns, graph traceability, a multi-hop breach-action query, narrative-number rejection, populated workbook output, provenance completeness, and database-enforced audit immutability.

## Tolerances

Displayed values, limits, utilization, and statuses must match exactly. Raw-value deltas also must be within:

- Allocations, aggregates, concentrations, and liquidity: `0.0005` of NAV, equivalent to 0.05 percentage points.
- Modified duration: `0.005` years, consistent with a two-decimal display.
- DV01: `SGD 0.50 / bp`, consistent with whole-dollar display.

These are display-derived tolerances, not cushions for different methods. Current deltas are all zero.

## Language-model boundary

The default commentary provider is deterministic and contains no numbers. An optional OpenAI Responses API adapter is available:

```bash
OPENAI_API_KEY=... python -m interopera run --firm all --narrative-provider openai --narrative-model gpt-5.4
```

The adapter is downstream of finalized figures and receives read-only computed output. Its text has no route back into graph properties, calculations, reconciliation, or workbook numeric cells. Export is blocked unless the firewall proves every numeric token in the narrative already appears in deterministic output. The adapter follows the official [Responses API create-response contract](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).

## Deliberate scope and production hardening

This sample uses an in-process property graph and SQLite because the data is small and replayability matters more than scale. Production work would add authenticated approvals, signed source/manifest hashes, object-lock/WORM storage, role-based access, KMS-backed signing, market-data lineage, observability, recovery drills, and durable graph/database services. See the RFC for tradeoffs and migration seams.
