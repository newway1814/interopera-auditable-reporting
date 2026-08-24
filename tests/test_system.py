from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from interopera.audit import AppendOnlyAuditLog
from interopera.computation import compute_figures
from interopera.extraction import build_graph, load_approved_manifest
from interopera.narrative import check_narrative_firewall, generate_narrative
from interopera.pipeline import run_pipeline
from interopera.queries import breach_action_query
from interopera.reconciliation import expected_answers, reconcile, traceability_check
from interopera.utils import load_json
from interopera.xlsx import read_first_sheet


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EngineTests(unittest.TestCase):
    def _figures(self, firm_id: str):
        config = load_json(PROJECT_ROOT / "config" / f"{firm_id}.json")
        graph, manifest = build_graph(PROJECT_ROOT, config)
        figures = compute_figures(graph, config, manifest["ingested_at"])
        return graph, figures

    def test_approved_extraction_and_provenance(self) -> None:
        manifest = load_approved_manifest(PROJECT_ROOT)
        graph, _ = build_graph(PROJECT_ROOT, load_json(PROJECT_ROOT / "config" / "firm_a.json"))
        self.assertEqual(manifest["schema_version"], 1)
        graph.assert_complete_provenance()
        self.assertGreater(len(graph.nodes), 50)
        self.assertGreater(len(graph.edges), 80)

    def test_firm_a_reconciles_exactly(self) -> None:
        _, figures = self._figures("firm_a")
        result = reconcile(figures, expected_answers(PROJECT_ROOT, "firm_a"))
        self.assertTrue(result["all_pass"], json.dumps(result, indent=2))

    def test_firm_b_changes_by_configuration_only(self) -> None:
        engine_hash_before = file_hash(PROJECT_ROOT / "src" / "interopera" / "computation.py")
        _, firm_a = self._figures("firm_a")
        _, firm_b = self._figures("firm_b")
        engine_hash_after = file_hash(PROJECT_ROOT / "src" / "interopera" / "computation.py")
        self.assertEqual(engine_hash_before, engine_hash_after)
        values_a = {figure.id: figure.value for figure in firm_a}
        values_b = {figure.id: figure.value for figure in firm_b}
        self.assertEqual(values_a["aggregate.non_ig"], "15.0%")
        self.assertEqual(values_b["aggregate.non_ig"], "21.0%")
        self.assertEqual(values_a["concentration.gre"], "7.0%")
        self.assertEqual(values_b["concentration.gre"], "13.0%")
        self.assertTrue(reconcile(firm_b, expected_answers(PROJECT_ROOT, "firm_b"))["all_pass"])

    def test_every_figure_resolves_to_guideline_and_holdings_sources(self) -> None:
        _, figures = self._figures("firm_b")
        result = traceability_check(figures)
        self.assertTrue(result["all_pass"], json.dumps(result, indent=2))

    def test_multi_hop_breach_action_query(self) -> None:
        graph, _ = self._figures("firm_a")
        result = breach_action_query(graph, "risk_metric:modified_duration")
        self.assertEqual(result["answer"]["action"], "PM notification within 1h")
        self.assertEqual(result["answer"]["owner"], "Portfolio Manager")
        self.assertIn("ON_BREACH", result["graph_path"])
        self.assertIn("NOTIFIES", result["graph_path"])

    def test_narrative_firewall_rejects_unseen_number(self) -> None:
        _, figures = self._figures("firm_a")
        safe = check_narrative_firewall(generate_narrative(figures), figures)
        unsafe = check_narrative_firewall("The model invented 42 widgets.", figures)
        self.assertTrue(safe.passed)
        self.assertFalse(unsafe.passed)
        self.assertEqual(unsafe.unexpected_numbers, ("42",))


class AuditTests(unittest.TestCase):
    def test_database_triggers_forbid_update_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.db"
            with AppendOnlyAuditLog(path) as audit:
                audit.append("run", "TEST", {"value": "original"})
                self.assertTrue(audit.verify_chain())
                with self.assertRaises(sqlite3.IntegrityError):
                    audit._connection.execute("UPDATE audit_events SET payload_json = '{}' WHERE sequence = 1")
                audit._connection.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    audit._connection.execute("DELETE FROM audit_events WHERE sequence = 1")
                audit._connection.rollback()
                self.assertTrue(audit.verify_chain())


class PipelineTests(unittest.TestCase):
    def test_two_runs_produce_byte_identical_numeric_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tmp") as directory:
            base = Path(directory)
            first = run_pipeline(PROJECT_ROOT, base / "first", base / "first.db", ["firm_a", "firm_b"])
            second = run_pipeline(PROJECT_ROOT, base / "second", base / "second.db", ["firm_a", "firm_b"])
            self.assertTrue(first["all_pass"])
            self.assertTrue(second["all_pass"])
            for firm_id in ("firm_a", "firm_b"):
                for filename in ("figures.json", "graph.json", "reconciliation.json", f"{firm_id}_report.xlsx"):
                    self.assertEqual(file_hash(base / "first" / firm_id / filename), file_hash(base / "second" / firm_id / filename), filename)

    def test_export_populates_every_template_row(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tmp") as directory:
            base = Path(directory)
            summary = run_pipeline(PROJECT_ROOT, base / "output", base / "audit.db", ["firm_a"])
            self.assertTrue(summary["all_pass"])
            rows = read_first_sheet(base / "output" / "firm_a" / "firm_a_report.xlsx")
            self.assertEqual(len(rows), 14)
            for row in rows[1:]:
                self.assertTrue(all(row[index] for index in range(2, 7)), row)
                self.assertIn("sample_fund_guidelines.pdf", row[6])
                self.assertIn("sample_holdings.csv", row[6])
                self.assertIn("chunk:holding:", row[6])


if __name__ == "__main__":
    unittest.main()
