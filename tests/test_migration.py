from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import migrate_sqlite_to_supabase as migration


class MigrationPlanTests(unittest.TestCase):
    def test_client_frontend_aliases_are_projected_to_supabase_columns(self):
        parsed = {"jsa-clientes": {"payload": [{"id": "client-1", "nome": "Fixture", "canalOrigem": "Indicação", "statusFunil": "negociacao"}]}}
        report = {"errors": [], "destination_counts_estimated": {}}
        plan = migration.build_plan(parsed, report)
        self.assertEqual(plan["clients"][0]["source"], "Indicação")
        self.assertEqual(plan["clients"][0]["stage"], "negociacao")
        self.assertEqual(plan["clients"][0]["source_payload"]["canalOrigem"], "Indicação")

    def test_real_database_dry_run_counts_and_relations(self):
        parsed, report, plan = migration.build_report(ROOT / "backend" / "jsa.db")
        self.assertEqual(report["sqlite"]["integrity_check"], "ok")
        self.assertEqual(report["keys_preserved_raw"], 5)
        self.assertEqual(report["source_counts"]["jsa-clientes"], 1262)
        self.assertEqual(report["source_counts"]["jsa-produtos"], 5)
        self.assertEqual(report["source_counts"]["jsa-orcamentos"], 1)
        self.assertEqual(report["source_counts"]["jsa-vendas"], 0)
        self.assertEqual(len(plan["clients"]), 1262)
        self.assertEqual(len(plan["products"]), 5)
        self.assertEqual(len(plan["quotes"]), 1)
        self.assertEqual(len(plan["quote_items"]), 1)
        self.assertEqual(len(report["errors"]), 0)
        quote = parsed["jsa-orcamentos"]["payload"][0]
        self.assertEqual(plan["quotes"][0]["client_id"], quote["clienteId"])
        self.assertEqual(plan["quote_items"][0]["product_id"], quote["itens"][0]["produtoId"])
        self.assertEqual(plan["quotes"][0]["id"], quote["id"])

    def test_invalid_json_is_reported_not_silently_discarded(self):
        rows = [{"key": "bad", "value": "{not-json", "updated_at": "2026-01-01"}]
        parsed, report = migration.parse_json_rows(rows)
        self.assertEqual(parsed, {})
        self.assertFalse(report["keys"]["bad"]["json_valid"])
        self.assertEqual(len(report["errors"]), 1)

    def test_duplicate_ids_and_non_objects_are_flagged(self):
        rows = [{"key": "jsa-clientes", "value": json.dumps([{"id":"x"},{"id":"x"},7]), "updated_at":"2026-01-01"}]
        _, report = migration.parse_json_rows(rows)
        self.assertEqual(report["keys"]["jsa-clientes"]["duplicate_ids"], 1)
        self.assertEqual(report["keys"]["jsa-clientes"]["non_object_items"], 1)
        self.assertEqual(len(report["errors"]), 2)

    def test_repeatable_dry_run_backup_uses_sqlite_backup_and_logical_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            conn = sqlite3.connect(source)
            conn.execute("create table kv_store(key text primary key, value text not null, updated_at text not null)")
            conn.execute("insert into kv_store values ('x','[1]','2026-01-01')")
            conn.commit(); conn.close()
            backup_dir = migration.BACKUP_DIR
            try:
                migration.BACKUP_DIR = Path(tmp) / "backup"
                backup, file_hash = migration.verified_backup(source)
                self.assertTrue(backup.exists())
                self.assertEqual(migration.logical_sqlite_digest(source), migration.logical_sqlite_digest(backup))
                self.assertEqual(len(file_hash), 64)
            finally:
                migration.BACKUP_DIR = backup_dir


if __name__ == "__main__":
    unittest.main(verbosity=2)
