from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable

from interopera.errors import ConfigurationError, TraceabilityError
from interopera.graph import Node, PropertyGraph, Provenance
from interopera.utils import decimal_string


RATING_ORDER = {
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7,
    "BBB+": 8, "BBB": 9, "BBB-": 10,
    "BB+": 11, "BB": 12, "BB-": 13,
    "B+": 14, "B": 15, "B-": 16,
    "CCC+": 17, "CCC": 18, "CCC-": 19, "CC": 20, "C": 21, "D": 22,
}


@dataclass(frozen=True)
class Figure:
    id: str
    section: str
    metric: str
    raw_value: Decimal
    value: str
    limit: str
    utilization: str
    status: str
    formula: str
    config_rule: str
    input_node_ids: tuple[str, ...]
    graph_paths: tuple[tuple[dict[str, str], ...], ...]
    citations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "figure": self.id,
            "section": self.section,
            "metric": self.metric,
            "raw_value": decimal_string(self.raw_value),
            "value": self.value,
            "limit": self.limit,
            "utilization": self.utilization,
            "status": self.status,
            "formula": self.formula,
            "config_rule": self.config_rule,
            "input_node_ids": list(self.input_node_ids),
            "graph_path": _compact_path(self.graph_paths),
            "graph_paths": [list(path) for path in self.graph_paths],
            "citations": list(self.citations),
        }


def _percent(value: Decimal) -> str:
    percentage = (value * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{percentage}%"


def _limit_percent(value: Decimal) -> str:
    percentage = (value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{percentage}%"


def _utilization(value: Decimal | None, format_name: str) -> str:
    if value is None:
        return "n/a"
    if format_name == "percent_1dp":
        return _percent(value)
    if format_name == "truncated_bps":
        basis_points = (value * Decimal("10000")).to_integral_value(rounding=ROUND_DOWN)
        return f"{basis_points} bps"
    raise ConfigurationError(f"Unsupported utilization format: {format_name}")


def _limit_status(value: Decimal, minimum: Decimal | None, maximum: Decimal | None) -> str:
    if minimum is not None and value < minimum:
        return "BREACH"
    if maximum is not None and value > maximum:
        return "BREACH"
    if value == minimum or value == maximum:
        return "AT LIMIT"
    return "OK"


def _matches(position: Node, predicate: dict[str, Any]) -> bool:
    if "any" in predicate:
        return any(_matches(position, child) for child in predicate["any"])
    if "all" in predicate:
        return all(_matches(position, child) for child in predicate["all"])
    field = predicate.get("field")
    if field not in position.properties:
        raise ConfigurationError(f"Unknown position field in selector: {field}")
    actual = position.properties[field]
    if "equals" in predicate:
        return actual == predicate["equals"]
    if "in" in predicate:
        return actual in predicate["in"]
    if "rating_below" in predicate:
        threshold = predicate["rating_below"]
        if not actual:
            return False
        if actual not in RATING_ORDER or threshold not in RATING_ORDER:
            raise ConfigurationError(f"Unsupported credit rating comparison: {actual} vs {threshold}")
        return RATING_ORDER[actual] > RATING_ORDER[threshold]
    raise ConfigurationError(f"Unsupported selector: {predicate}")


def _group_key(position: Node, group_by: str) -> str:
    if group_by == "issuer_name":
        return str(position.properties["issuer_name"])
    if group_by == "parent_or_issuer":
        return str(position.properties.get("parent_issuer") or position.properties["issuer_name"])
    raise ConfigurationError(f"Unsupported grouping key: {group_by}")


def _dec(node: Node, property_name: str) -> Decimal:
    return Decimal(str(node.properties[property_name]))


def _target(graph: PropertyGraph, node_id: str, relation: str) -> Node:
    targets = graph.targets(node_id, relation)
    if len(targets) != 1:
        raise TraceabilityError(f"Expected one {relation} target for {node_id}; found {len(targets)}")
    return targets[0]


def _compact_path(paths: tuple[tuple[dict[str, str], ...], ...]) -> str:
    if not paths:
        return "ERROR: no source path"
    preferred = sorted(
        paths,
        key=lambda path: (0 if "guidelines" in path[-1]["id"] or "allocation" in path[-1]["id"] or "risk" in path[-1]["id"] else 1, str(path)),
    )[0]
    tokens: list[str] = []
    for item in preferred:
        if item["kind"] == "node":
            tokens.append(f"({item['type']}:{item['id'].split(':', 1)[-1]})")
        else:
            tokens.append(f"-[:{item['relation']}]->")
    return "".join(tokens)


class FigureEngine:
    def __init__(self, graph: PropertyGraph, configuration: dict[str, Any], ingested_at: str) -> None:
        self.graph = graph
        self.configuration = configuration
        self.ingested_at = ingested_at
        self.positions = graph.nodes_of_type("Position")
        self.nav = sum((_dec(position, "market_value_sgd") for position in self.positions), Decimal("0"))
        if self.nav <= 0:
            raise ConfigurationError("Portfolio NAV must be positive")
        self.utilization_format = configuration["utilization"]["format"]

    def compute_all(self) -> list[Figure]:
        figures: list[Figure] = []
        allocation_specs = [
            ("allocation.sgs", "Singapore Government Securities", "asset_class:sgs"),
            ("allocation.mas_bills", "MAS Bills", "asset_class:mas_bills"),
            ("allocation.ig_corporate", "Investment Grade Corporate Bonds", "asset_class:ig_corporate"),
            ("allocation.high_yield", "High Yield Bonds", "asset_class:high_yield"),
            ("allocation.foreign_currency", "Foreign Currency Bonds (hedged)", "asset_class:foreign_currency"),
            ("allocation.structured_credit", "Structured Credit (ABS/MBS)", "asset_class:structured_credit"),
            ("allocation.cash", "Cash & Cash Equivalents", "asset_class:cash"),
        ]
        figures.extend(self._allocation(*spec) for spec in allocation_specs)
        figures.append(self._aggregate_non_ig())
        figures.append(self._concentration("corporate"))
        figures.append(self._concentration("gre"))
        figures.append(self._liquidity())
        figures.append(self._duration())
        figures.append(self._dv01())
        return figures

    def _positions_for_asset(self, asset_id: str) -> list[Node]:
        selected: list[Node] = []
        for position in self.positions:
            asset = _target(self.graph, position.id, "BELONGS_TO")
            if asset.id == asset_id:
                selected.append(position)
        return selected

    def _allocation(self, figure_id: str, metric: str, asset_id: str) -> Figure:
        positions = self._positions_for_asset(asset_id)
        value = sum((_dec(position, "market_value_sgd") for position in positions), Decimal("0")) / self.nav
        asset = self.graph.nodes[asset_id]
        limit = _target(self.graph, asset_id, "HAS_LIMIT")
        minimum = Decimal(limit.properties["min"])
        maximum = Decimal(limit.properties["max"])
        minimum_only = asset.properties.get("report_limit") == "minimum_only"
        if minimum_only:
            limit_display = f"min {_limit_percent(minimum)}"
            utilization_value = None
            status = _limit_status(value, minimum, None)
        else:
            limit_display = f"{_limit_percent(minimum).removesuffix('%')}–{_limit_percent(maximum)}"
            utilization_value = value / maximum if maximum else None
            status = _limit_status(value, minimum, maximum)
        return self._emit(
            figure_id=figure_id,
            section="Allocation",
            metric=metric,
            raw_value=value,
            value_display=_percent(value),
            limit_display=limit_display,
            utilization_value=utilization_value,
            status=status,
            formula="sum(position.market_value_sgd where position-[:BELONGS_TO]->asset_class) / NAV",
            config_rule="utilization",
            inputs=positions,
            governed_by=limit,
        )

    def _aggregate_non_ig(self) -> Figure:
        selector = self.configuration["aggregate_non_ig"]["selector"]
        positions = [position for position in self.positions if _matches(position, selector)]
        value = sum((_dec(position, "market_value_sgd") for position in positions), Decimal("0")) / self.nav
        aggregate = self.graph.nodes["aggregate:non_ig"]
        limit = _target(self.graph, aggregate.id, "HAS_LIMIT")
        maximum = Decimal(limit.properties["max"])
        return self._emit(
            "aggregate.non_ig", "Aggregate", "Aggregate non-IG exposure", value, _percent(value),
            f"max {_limit_percent(maximum)}", value / maximum, _limit_status(value, None, maximum),
            "sum(position.market_value_sgd where configured non-IG selector matches) / NAV",
            "aggregate_non_ig", positions, limit,
        )

    def _concentration(self, kind: str) -> Figure:
        rule = self.configuration["concentration"][kind]
        positions = [position for position in self.positions if _matches(position, rule["selector"])]
        grouped: dict[str, list[Node]] = {}
        for position in positions:
            grouped.setdefault(_group_key(position, rule["group_by"]), []).append(position)
        largest_key, largest_positions = max(
            grouped.items(),
            key=lambda item: (sum((_dec(position, "market_value_sgd") for position in item[1]), Decimal("0")), item[0]),
        )
        value = sum((_dec(position, "market_value_sgd") for position in largest_positions), Decimal("0")) / self.nav
        if kind == "corporate":
            figure_id, metric, risk_id, rule_name = "concentration.corporate", "Largest single corporate issuer", "risk_metric:single_corporate", "corporate_concentration"
        else:
            figure_id, metric, risk_id, rule_name = "concentration.gre", "Largest GRE issuer", "risk_metric:gre", "gre_concentration"
        limit = _target(self.graph, risk_id, "HAS_LIMIT")
        maximum = Decimal(limit.properties["max"])
        return self._emit(
            figure_id, "Concentration", metric, value, _percent(value), f"max {_limit_percent(maximum)}",
            value / maximum, _limit_status(value, None, maximum),
            f"max(group sum(position.market_value_sgd), group_by={rule['group_by']}) / NAV; winner={largest_key}",
            rule_name, largest_positions, limit,
        )

    def _liquidity(self) -> Figure:
        aggregate = self.graph.nodes["aggregate:liquid_assets"]
        contributor_ids = {edge.source for edge in self.graph.edges.values() if edge.relation == "CONTRIBUTES_TO" and edge.target == aggregate.id}
        positions = [position for position in self.positions if _target(self.graph, position.id, "BELONGS_TO").id in contributor_ids]
        value = sum((_dec(position, "market_value_sgd") for position in positions), Decimal("0")) / self.nav
        limit = _target(self.graph, aggregate.id, "HAS_LIMIT")
        minimum = Decimal(limit.properties["min"])
        return self._emit(
            "liquidity.liquid_assets", "Liquidity", "Liquid assets ratio", value, _percent(value),
            f"min {_limit_percent(minimum)}", value / minimum, _limit_status(value, minimum, None),
            "sum(position.market_value_sgd for asset_class-[:CONTRIBUTES_TO]->liquid_assets) / NAV",
            "utilization", positions, limit,
        )

    def _duration(self) -> Figure:
        weighted_sum = sum((_dec(position, "market_value_sgd") * _dec(position, "modified_duration") for position in self.positions), Decimal("0"))
        value = weighted_sum / self.nav
        limit = _target(self.graph, "risk_metric:modified_duration", "HAS_LIMIT")
        minimum = Decimal(limit.properties["min"])
        maximum = Decimal(limit.properties["max"])
        display = f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} yrs"
        return self._emit(
            "market_risk.modified_duration", "Market risk", "Portfolio modified duration", value, display,
            f"{minimum}–{maximum} yrs", None, _limit_status(value, minimum, maximum),
            "sum(position.market_value_sgd * position.modified_duration) / NAV",
            "utilization", self.positions, limit,
        )

    def _dv01(self) -> Figure:
        value = sum((_dec(position, "market_value_sgd") * _dec(position, "modified_duration") * Decimal("0.0001") for position in self.positions), Decimal("0"))
        limit = _target(self.graph, "risk_metric:dv01", "HAS_LIMIT")
        maximum = Decimal(limit.properties["max"])
        display = f"SGD {value.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,.0f} / bp"
        return self._emit(
            "market_risk.dv01", "Market risk", "Portfolio DV01", value, display,
            f"max {maximum:,.0f}", value / maximum, _limit_status(value, None, maximum),
            "sum(position.market_value_sgd * position.modified_duration * 0.0001)",
            "utilization", self.positions, limit,
        )

    def _emit(
        self,
        figure_id: str,
        section: str,
        metric: str,
        raw_value: Decimal,
        value_display: str,
        limit_display: str,
        utilization_value: Decimal | None,
        status: str,
        formula: str,
        config_rule: str,
        inputs: list[Node],
        governed_by: Node,
    ) -> Figure:
        graph_id = f"figure:{figure_id}"
        provenance = Provenance("deterministic_engine", 0, f"formula:{figure_id}", self.ingested_at, 1.0)
        self.graph.add_node(Node(graph_id, "Figure", {"metric": metric, "formula": formula, "raw_value": decimal_string(raw_value)}, provenance))
        for position in sorted(inputs, key=lambda item: item.id):
            self.graph.add_edge(graph_id, "COMPUTED_FROM", position.id, position.provenance)
        self.graph.add_edge(graph_id, "GOVERNED_BY", governed_by.id, governed_by.provenance)
        config_node = f"config_rule:{self.configuration['firm_id']}:{config_rule}"
        self.graph.add_edge(graph_id, "PRODUCED_BY", config_node, self.graph.nodes[config_node].provenance)
        paths_raw = self.graph.require_traceability(graph_id)
        paths = tuple(tuple(item for item in path) for path in paths_raw)
        citations_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
        for path in paths_raw:
            terminal = self.graph.nodes[path[-1]["id"]]
            provenance = terminal.provenance
            key = (provenance.source_document, provenance.page, provenance.chunk_id)
            citations_by_key[key] = {
                "source_doc": provenance.source_document,
                "page": provenance.page,
                "chunk_id": provenance.chunk_id,
                "passage_summary": terminal.properties.get("summary") or terminal.properties.get("section"),
            }
        return Figure(
            id=figure_id,
            section=section,
            metric=metric,
            raw_value=raw_value,
            value=value_display,
            limit=limit_display,
            utilization=_utilization(utilization_value, self.utilization_format),
            status=status,
            formula=formula,
            config_rule=config_node,
            input_node_ids=tuple(sorted(position.id for position in inputs)),
            graph_paths=paths,
            citations=tuple(citations_by_key[key] for key in sorted(citations_by_key)),
        )


def compute_figures(graph: PropertyGraph, configuration: dict[str, Any], ingested_at: str) -> list[Figure]:
    return FigureEngine(graph, configuration, ingested_at).compute_all()
