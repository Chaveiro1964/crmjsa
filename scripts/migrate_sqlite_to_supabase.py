#!/usr/bin/env python3
"""Migra o JSA CRM SQLite para as tabelas normalizadas do Supabase.

Seguro por padrão: sem --apply somente valida e relata; nunca altera SQLite.
Reexecuções são idempotentes por (organization_id, id) e chave original.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "jsa.db"
BACKUP_DIR = ROOT / "backups"
REPORT_DIR = ROOT / "reports"
KNOWN_KEYS = {
    "jsa-clientes": "clients",
    "jsa-vendas": "sales",
    "jsa-orcamentos": "quotes",
    "jsa-produtos": "products",
    "jsa-codigos-nao-encontrados": "missing_codes",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logical_sqlite_digest(path: Path) -> str:
    """Hash schema and table rows independent of SQLite page layout."""
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        schema = connection.execute(
            "select type,name,sql from sqlite_master where name not like 'sqlite_%' order by type,name"
        ).fetchall()
        digest = hashlib.sha256(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode())
        for table_row in connection.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        ):
            table = table_row[0]
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            normalized = sorted(json.dumps(tuple(row), ensure_ascii=False, default=str, separators=(",", ":")) for row in rows)
            digest.update(table.encode())
            digest.update("\n".join(normalized).encode())
        return digest.hexdigest()
    finally:
        connection.close()


def verified_backup(source: Path) -> tuple[Path, str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_DIR / f"jsa-pre-supabase-{stamp}-{uuid.uuid4().hex[:6]}.sqlite3"
    src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        dst.commit()
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup SQLite não passou integrity_check: {result}")
    finally:
        dst.close()
        src.close()
    source_hash, backup_hash = logical_sqlite_digest(source), logical_sqlite_digest(destination)
    if source_hash != backup_hash:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Hash lógico do backup difere do SQLite original; migração cancelada.")
    return destination, sha256_file(destination)


def read_source(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        tables = [row[0] for row in connection.execute("select name from sqlite_master where type='table' order by name")]
        if "kv_store" not in tables:
            raise RuntimeError("Tabela kv_store não encontrada no SQLite.")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite original não passou integrity_check: {integrity}")
        rows = [dict(row) for row in connection.execute("select key, value, updated_at from kv_store order by key")]
        result = {"tables": tables, "integrity_check": integrity, "kv_rows": len(rows)}
        return rows, result
    finally:
        connection.close()


def parse_json_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    parsed: dict[str, Any] = {}
    report = {"keys": {}, "errors": [], "warnings": [], "source_counts": {}, "destination_counts_estimated": {}}
    for row in rows:
        key = row["key"]
        try:
            payload = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError) as exc:
            report["errors"].append({"key": key, "error": f"JSON inválido: {exc}"})
            report["keys"][key] = {"json_valid": False, "raw_bytes": len((row["value"] or "").encode())}
            continue
        parsed[key] = {"payload": payload, "value_raw": row["value"], "updated_at": row["updated_at"]}
        count = len(payload) if isinstance(payload, list) else 1
        report["keys"][key] = {"json_valid": True, "root_type": type(payload).__name__, "items": count}
        report["source_counts"][key] = count if isinstance(payload, list) else 1
        if not isinstance(payload, list):
            report["warnings"].append({"key": key, "warning": "Raiz JSON não é array; conteúdo será preservado sem normalização."})
            continue
        seen_ids: set[str] = set()
        duplicates = 0
        bad_objects = 0
        for obj in payload:
            if not isinstance(obj, dict):
                bad_objects += 1
                continue
            record_id = obj.get("id")
            if record_id is not None:
                value = str(record_id)
                if value in seen_ids:
                    duplicates += 1
                seen_ids.add(value)
        report["keys"][key].update({"objects": len(payload) - bad_objects, "non_object_items": bad_objects, "duplicate_ids": duplicates})
        if bad_objects:
            report["errors"].append({"key": key, "error": f"{bad_objects} itens não são objetos; raw preservado, normalização incompleta."})
        if duplicates:
            report["errors"].append({"key": key, "error": f"{duplicates} IDs duplicados; upsert não pode preservar ambas as linhas normalizadas."})
    return parsed, report


def parse_decimal(value: Any, key: str, field: str, errors: list[dict[str, str]], default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, TypeError):
        errors.append({"key": key, "error": f"Valor numérico inválido em {field}; usado {default} na projeção (original preservado em JSON)."})
        return Decimal(default)


def parse_date(value: Any, key: str, field: str, errors: list[dict[str, str]]) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        errors.append({"key": key, "error": f"Data inválida em {field}; mantida apenas no payload de origem."})
        return None


def obj_str(row: dict[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None:
            return str(value)
    return default


def id_of(row: dict[str, Any], key: str, errors: list[dict[str, str]], index: int) -> str | None:
    value = row.get("id")
    if value is None or str(value) == "":
        errors.append({"key": key, "error": f"Registro no índice {index} sem id; preservado no legado e ignorado na tabela normalizada."})
        return None
    return str(value)


def build_plan(parsed: dict[str, Any], report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    plan: dict[str, list[dict[str, Any]]] = {name: [] for name in ("clients", "products", "sales", "sale_items", "quotes", "quote_items", "missing_codes")}
    clients = parsed.get("jsa-clientes", {}).get("payload", [])
    products = parsed.get("jsa-produtos", {}).get("payload", [])
    quotes = parsed.get("jsa-orcamentos", {}).get("payload", [])
    sales = parsed.get("jsa-vendas", {}).get("payload", [])
    missing = parsed.get("jsa-codigos-nao-encontrados", {}).get("payload", [])
    errors = report["errors"]

    if isinstance(clients, list):
        for i, row in enumerate(clients):
            if not isinstance(row, dict): continue
            rid = id_of(row, "jsa-clientes", errors, i)
            if not rid: continue
            plan["clients"].append({"id": rid, "name": obj_str(row,"nome","name"), "phone": obj_str(row,"telefone","phone"), "address": obj_str(row,"endereco","address"), "document": obj_str(row,"documento","document"), "city": obj_str(row,"cidade","city"), "state": obj_str(row,"estado","state"), "postal_code": obj_str(row,"cep","postal_code"), "source": obj_str(row,"canalOrigem","origem","source"), "stage": obj_str(row,"statusFunil","etapa","stage"), "tags": row.get("tags") if isinstance(row.get("tags"), list) else [], "next_return": row.get("proximoRetorno"), "source_payload": row})
    client_ids = {r["id"] for r in plan["clients"]}

    if isinstance(products, list):
        for i, row in enumerate(products):
            if not isinstance(row, dict): continue
            rid = id_of(row, "jsa-produtos", errors, i)
            if not rid: continue
            plan["products"].append({"id": rid, "code": obj_str(row,"codigo","code"), "name": obj_str(row,"nome","name"), "description": obj_str(row,"descricao","description"), "cost_price": parse_decimal(row.get("custo", row.get("cost_price")),"jsa-produtos","custo",errors), "sale_price": parse_decimal(row.get("venda", row.get("sale_price")),"jsa-produtos","venda",errors), "active": bool(row.get("ativo", True)), "stock": None if row.get("estoque") is None else parse_decimal(row.get("estoque"),"jsa-produtos","estoque",errors), "minimum_stock": parse_decimal(row.get("estoqueMinimo", row.get("minimum_stock")),"jsa-produtos","estoqueMinimo",errors), "source_payload": row})
    product_ids = {r["id"] for r in plan["products"]}

    if isinstance(quotes, list):
        for i, row in enumerate(quotes):
            if not isinstance(row, dict): continue
            rid = id_of(row, "jsa-orcamentos", errors, i)
            if not rid: continue
            client_ref = obj_str(row,"clienteId","client_id") or None
            if client_ref and client_ref not in client_ids:
                errors.append({"key":"jsa-orcamentos", "error":f"Orçamento {rid}: clienteId sem correspondência; vínculo normalizado nulo, original preservado."})
                client_ref = None
            quote = {"id":rid, "client_id":client_ref, "client_name_snapshot":obj_str(row,"clienteNome","clientName"), "quote_date":parse_date(row.get("data",row.get("date")),"jsa-orcamentos","data",errors), "postal_code":obj_str(row,"cep","postal_code"), "note":obj_str(row,"observacao","note"), "shipping_amount":parse_decimal(row.get("frete",row.get("shipping_amount")),"jsa-orcamentos","frete",errors), "shipping_paid_by":obj_str(row,"freteAssumido","shipping_paid_by"), "payment_method":obj_str(row,"formaPagamento","payment_method"), "installments":int(row["parcelas"]) if str(row.get("parcelas","")).isdigit() else None, "total_amount":parse_decimal(row.get("valorTotal",row.get("total_amount")),"jsa-orcamentos","valorTotal",errors), "status":obj_str(row,"status"), "loss_reason":obj_str(row,"motivoPerda","loss_reason"), "source_payload":row}
            plan["quotes"].append(quote)
            items = row.get("itens", row.get("items", []))
            if not isinstance(items, list):
                errors.append({"key":"jsa-orcamentos", "error":f"Orçamento {rid}: itens não é array; payload original preservado."})
                continue
            for line_no, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    errors.append({"key":"jsa-orcamentos", "error":f"Orçamento {rid}, item {line_no}: item inválido, payload original preservado."})
                    continue
                product_id = obj_str(item,"produtoId","product_id") or None
                if product_id and product_id not in product_ids:
                    errors.append({"key":"jsa-orcamentos", "error":f"Orçamento {rid}, item {line_no}: produtoId sem correspondência; vínculo normalizado nulo."})
                    product_id = None
                quantity = parse_decimal(item.get("qtd", item.get("quantity")),"jsa-orcamentos","item.qtd",errors)
                if quantity <= 0:
                    errors.append({"key":"jsa-orcamentos", "error":f"Orçamento {rid}, item {line_no}: quantidade não positiva; não projetado."})
                    continue
                plan["quote_items"].append({"id":str(uuid.uuid5(uuid.NAMESPACE_URL, f"jsa:{rid}:quote-item:{line_no}")), "quote_id":rid, "line_no":line_no, "product_id":product_id, "product_name_snapshot":obj_str(item,"produtoNome","product_name"), "model":obj_str(item,"modelo","model"), "quantity":quantity, "unit_cost":parse_decimal(item.get("custoUnit",item.get("unit_cost")),"jsa-orcamentos","item.custoUnit",errors), "unit_price":parse_decimal(item.get("precoVenda",item.get("unit_price")),"jsa-orcamentos","item.precoVenda",errors), "source_payload":item})

    if isinstance(sales, list):
        for i, row in enumerate(sales):
            if not isinstance(row, dict): continue
            rid = id_of(row, "jsa-vendas", errors, i)
            if not rid: continue
            client_ref = obj_str(row,"clienteId","clientId","client_id") or None
            if client_ref and client_ref not in client_ids:
                errors.append({"key":"jsa-vendas", "error":f"Venda {rid}: referência de cliente não encontrada; vínculo nulo, original preservado."})
                client_ref = None
            sale = {"id":rid, "client_id":client_ref, "client_name_snapshot":obj_str(row,"clienteNome","clientName","client_name"), "sale_date":parse_date(row.get("data",row.get("date")),"jsa-vendas","data",errors), "status":obj_str(row,"status"), "shipping_amount":parse_decimal(row.get("frete",row.get("shipping")),"jsa-vendas","frete",errors), "shipping_paid_by":obj_str(row,"freteAssumido","shipping_paid_by"), "payment_method":obj_str(row,"formaPagamento","paymentMethod"), "installments":int(row["parcelas"]) if str(row.get("parcelas","")).isdigit() else None, "total_amount":parse_decimal(row.get("valorTotal",row.get("total")),"jsa-vendas","valorTotal",errors), "source_payload":row}
            plan["sales"].append(sale)
            items = row.get("itens", row.get("items", []))
            if isinstance(items, list):
                for line_no, item in enumerate(items, start=1):
                    if not isinstance(item, dict): continue
                    product_id = obj_str(item,"produtoId","product_id") or None
                    if product_id and product_id not in product_ids:
                        errors.append({"key":"jsa-vendas", "error":f"Venda {rid}, item {line_no}: referência de produto não encontrada; vínculo nulo."})
                        product_id = None
                    quantity = parse_decimal(item.get("qtd",item.get("quantity")),"jsa-vendas","item.qtd",errors)
                    if quantity <= 0: continue
                    plan["sale_items"].append({"id":str(uuid.uuid5(uuid.NAMESPACE_URL,f"jsa:{rid}:sale-item:{line_no}")),"sale_id":rid,"line_no":line_no,"product_id":product_id,"product_name_snapshot":obj_str(item,"produtoNome","product_name"),"model":obj_str(item,"modelo","model"),"quantity":quantity,"unit_cost":parse_decimal(item.get("custoUnit",item.get("unit_cost")),"jsa-vendas","item.custoUnit",errors),"unit_price":parse_decimal(item.get("precoVenda",item.get("unit_price")),"jsa-vendas","item.precoVenda",errors),"source_payload":item})

    if isinstance(missing, list):
        for i, row in enumerate(missing):
            if not isinstance(row, dict): continue
            rid = id_of(row, "jsa-codigos-nao-encontrados", errors, i)
            if not rid: continue
            plan["missing_codes"].append({"id":rid,"code":obj_str(row,"codigo","code"),"occurrences":max(0,int(row.get("vezes",row.get("occurrences",0)) or 0)),"first_seen":parse_date(row.get("primeiraVez",row.get("first_seen")),"jsa-codigos-nao-encontrados","primeiraVez",errors),"last_seen":parse_date(row.get("ultimaVez",row.get("last_seen")),"jsa-codigos-nao-encontrados","ultimaVez",errors),"source_payload":row})

    for table, records in plan.items():
        report["destination_counts_estimated"][table] = len(records)
    return plan


def build_report(source_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]]:
    rows, sqlite_report = read_source(source_path)
    parsed, report = parse_json_rows(rows)
    plan = build_plan(parsed, report)
    clients = parsed.get("jsa-clientes",{}).get("payload",[])
    if isinstance(clients,list):
        names = Counter(str(r.get("nome","")).strip().casefold() for r in clients if isinstance(r,dict) and str(r.get("nome","")).strip())
        phones = Counter(str(r.get("telefone","")).strip() for r in clients if isinstance(r,dict) and str(r.get("telefone","")).strip())
        report["duplicate_name_groups"] = sum(1 for n in names.values() if n > 1)
        report["duplicate_phone_groups"] = sum(1 for n in phones.values() if n > 1)
        report["warnings"].append({"key":"jsa-clientes","warning":"Nomes/telefones repetidos foram contabilizados, não mesclados nem descartados."})
    report["source_sha256"] = sha256_file(source_path)
    report["source_file_name"] = source_path.name
    report["sqlite"] = sqlite_report
    report["unknown_keys_preserved_in_legacy"] = sorted(k for k in parsed if k not in KNOWN_KEYS)
    report["keys_preserved_raw"] = len(parsed)
    report["normalized_plan"] = {table: len(records) for table, records in plan.items()}
    return parsed, report, plan


def migrate(parsed: dict[str, Any], report: dict[str, Any], plan: dict[str, list[dict[str, Any]]], org_id: str, db_url: str) -> None:
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("Dependência psycopg não instalada; rode `pip install -r requirements.txt`.") from exc

    org_uuid = uuid.UUID(org_id)
    source_name = str(report.get("source_file_name", DEFAULT_DB.name))
    with psycopg.connect(db_url) as conn:
        exists = conn.execute("select 1 from public.organizations where id = %s", (org_uuid,)).fetchone()
        if not exists:
            raise RuntimeError("A organização informada não existe em public.organizations. Crie-a no Supabase e confira o UUID antes de aplicar.")
        for key, item in parsed.items():
            conn.execute(
                """insert into public.legacy_kv_store (organization_id,key,value_raw,payload,updated_at_raw)
                   values (%s,%s,%s,%s,%s)
                   on conflict (organization_id,key) do update set value_raw=excluded.value_raw,
                   payload=excluded.payload, updated_at_raw=excluded.updated_at_raw, migrated_at=now()""",
                (org_uuid,key,item["value_raw"],Jsonb(item["payload"]),str(item["updated_at"] or "")),
            )
            if key not in KNOWN_KEYS:
                conn.execute(
                    """insert into public.app_state (organization_id,key,value,source_updated_at)
                       values (%s,%s,%s,%s) on conflict (organization_id,key) do update
                       set value=excluded.value,source_updated_at=excluded.source_updated_at,updated_at=now()""",
                    (org_uuid,key,Jsonb(item["payload"]),str(item["updated_at"] or "")),
                )

        def upsert(table: str, row: dict[str, Any], columns: list[str], updates: list[str]) -> None:
            names = ["organization_id"] + columns
            values = [org_uuid] + [row.get(c) for c in columns]
            placeholders = ",".join(["%s"] * len(names))
            update_clause = ",".join(f"{name}=excluded.{name}" for name in updates)
            sql = f"insert into public.{table} ({','.join(names)}) values ({placeholders}) on conflict (organization_id,id) do update set {update_clause}"
            conn.execute(sql, values)

        for row in plan["clients"]:
            data = dict(row); data["tags"] = Jsonb(data["tags"]); data["next_return"] = Jsonb(data["next_return"]) if data["next_return"] is not None else None; data["source_payload"] = Jsonb(data["source_payload"])
            upsert("clients",data,["id","name","phone","address","document","city","state","postal_code","source","stage","tags","next_return","source_payload"],["name","phone","address","document","city","state","postal_code","source","stage","tags","next_return","source_payload","updated_at"])
        for row in plan["products"]:
            data=dict(row); data["source_payload"]=Jsonb(data["source_payload"])
            upsert("products",data,["id","code","name","description","cost_price","sale_price","active","stock","minimum_stock","source_payload"],["code","name","description","cost_price","sale_price","active","stock","minimum_stock","source_payload","updated_at"])
        for row in plan["sales"]:
            data=dict(row); data["source_payload"]=Jsonb(data["source_payload"])
            upsert("sales",data,["id","client_id","client_name_snapshot","sale_date","status","shipping_amount","shipping_paid_by","payment_method","installments","total_amount","source_payload"],["client_id","client_name_snapshot","sale_date","status","shipping_amount","shipping_paid_by","payment_method","installments","total_amount","source_payload","updated_at"])
        for row in plan["quotes"]:
            data=dict(row); data["source_payload"]=Jsonb(data["source_payload"])
            upsert("quotes",data,["id","client_id","client_name_snapshot","quote_date","postal_code","note","shipping_amount","shipping_paid_by","payment_method","installments","total_amount","status","loss_reason","source_payload"],["client_id","client_name_snapshot","quote_date","postal_code","note","shipping_amount","shipping_paid_by","payment_method","installments","total_amount","status","loss_reason","source_payload","updated_at"])
        for row in plan["missing_codes"]:
            data=dict(row); data["source_payload"]=Jsonb(data["source_payload"])
            upsert("missing_codes",data,["id","code","occurrences","first_seen","last_seen","source_payload"],["code","occurrences","first_seen","last_seen","source_payload","updated_at"])
        for table, rows in (("quote_items",plan["quote_items"]),("sale_items",plan["sale_items"])):
            for row in rows:
                vals=[org_uuid,row["id"],row["quote_id"] if table=="quote_items" else row["sale_id"],row["line_no"],row["product_id"],row["product_name_snapshot"],row["model"],row["quantity"],row["unit_cost"],row["unit_price"],Jsonb(row["source_payload"])]
                if table=="quote_items":
                    conn.execute("""insert into public.quote_items (organization_id,id,quote_id,line_no,product_id,product_name_snapshot,model,quantity,unit_cost,unit_price,source_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set product_id=excluded.product_id,product_name_snapshot=excluded.product_name_snapshot,model=excluded.model,quantity=excluded.quantity,unit_cost=excluded.unit_cost,unit_price=excluded.unit_price,source_payload=excluded.source_payload""", vals)
                else:
                    conn.execute("""insert into public.sale_items (organization_id,id,sale_id,line_no,product_id,product_name_snapshot,model,quantity,unit_cost,unit_price,source_payload) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do update set product_id=excluded.product_id,product_name_snapshot=excluded.product_name_snapshot,model=excluded.model,quantity=excluded.quantity,unit_cost=excluded.unit_cost,unit_price=excluded.unit_price,source_payload=excluded.source_payload""", vals)

        # Post-write counts and staging key count are checked inside the same transaction.
        counts = {}
        for table in ("clients","products","sales","sale_items","quotes","quote_items","missing_codes","legacy_kv_store"):
            counts[table] = conn.execute(f"select count(*) from public.{table} where organization_id=%s", (org_uuid,)).fetchone()[0]
        expected = {"clients":len(plan["clients"]),"products":len(plan["products"]),"sales":len(plan["sales"]),"sale_items":len(plan["sale_items"]),"quotes":len(plan["quotes"]),"quote_items":len(plan["quote_items"]),"missing_codes":len(plan["missing_codes"]),"legacy_kv_store":len(parsed)}
        report["destination_counts_after_apply"] = counts
        report["expected_counts"] = expected
        report["integrity_validation"] = {key: counts[key] == expected[key] for key in expected}
        expected_ids = {
            "clients": {str(row["id"]) for row in plan["clients"]},
            "products": {str(row["id"]) for row in plan["products"]},
            "sales": {str(row["id"]) for row in plan["sales"]},
            "quotes": {str(row["id"]) for row in plan["quotes"]},
            "missing_codes": {str(row["id"]) for row in plan["missing_codes"]},
            "quote_items": {str(row["id"]) for row in plan["quote_items"]},
            "sale_items": {str(row["id"]) for row in plan["sale_items"]},
            "legacy_kv_store": set(parsed),
        }
        id_validation = {}
        for table, expected_set in expected_ids.items():
            column = "key" if table == "legacy_kv_store" else "id"
            actual_set = {str(row[0]) for row in conn.execute(f"select {column} from public.{table} where organization_id=%s", (org_uuid,)).fetchall()}
            id_validation[table] = actual_set == expected_set
        report["id_validation"] = id_validation
        report["integrity_validation"].update({f"{key}_ids": ok for key, ok in id_validation.items()})
        if not all(report["integrity_validation"].values()):
            raise RuntimeError("Validação pós-carga detectou divergência de contagem/IDs; transação será revertida. Use uma organização de migração vazia e investigue dados preexistentes.")
        conn.execute(
            """insert into public.migration_runs (organization_id, source_name, source_sha256, report, status, completed_at)
               values (%s,%s,%s,%s,'completed',now())""",
            (org_uuid, source_name, report["source_sha256"], Jsonb(report)),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_DB, help="Arquivo SQLite (padrão: backend/jsa.db)")
    parser.add_argument("--apply", action="store_true", help="Aplica a migração. Sem esta opção apenas simula e valida.")
    parser.add_argument("--org-id", default=os.getenv("SUPABASE_ORG_ID", ""), help="UUID da organização já criada no schema Supabase.")
    parser.add_argument("--report", type=Path, help="Caminho do relatório JSON (por padrão reports/).")
    args = parser.parse_args()
    source = args.sqlite.resolve()
    if not source.is_file():
        print(f"ERRO: SQLite não encontrado: {source}", file=sys.stderr)
        return 2
    if args.apply and not args.org_id:
        print("ERRO: --apply exige --org-id ou SUPABASE_ORG_ID.", file=sys.stderr)
        return 2
    if args.apply and not os.getenv("SUPABASE_DATABASE_URL"):
        print("ERRO: --apply exige SUPABASE_DATABASE_URL (credencial somente do lado servidor).", file=sys.stderr)
        return 2
    try:
        backup, backup_hash = verified_backup(source)
        parsed, report, plan = build_report(source)
        report.update({"mode":"apply" if args.apply else "dry_run", "backup_file":str(backup.relative_to(ROOT)) if backup.is_relative_to(ROOT) else backup.name, "backup_file_sha256":backup_hash, "backup_logically_verified":True, "started_at":datetime.now(timezone.utc).isoformat(), "applied":False})
        if args.apply:
            if report.get("errors"):
                raise RuntimeError("Dry-run encontrou erros de dados; nenhuma gravação foi enviada. Revise o relatório e corrija a origem antes de aplicar.")
            migrate(parsed, report, plan, args.org_id, os.environ["SUPABASE_DATABASE_URL"])
            report["applied"] = True
            report["status"] = "completed"
        else:
            report["status"] = "dry_run_validated"
            report["note"] = "Nenhuma gravação foi feita no Supabase; backup SQLite criado e validado. Passe --apply após revisar o relatório."
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = args.report.resolve() if args.report else REPORT_DIR / f"sqlite-supabase-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+"\n", encoding="utf-8")
        print(json.dumps({"status":report["status"],"backup":report["backup_file"],"backup_sha256":backup_hash,"source_counts":report.get("source_counts",{}),"normalized_plan":report.get("normalized_plan",{}),"errors":len(report.get("errors",[])),"report":str(report_path)},ensure_ascii=False,indent=2))
        return 0 if not report.get("errors") else 1
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
