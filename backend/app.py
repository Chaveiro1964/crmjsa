"""JSA CRM API: SQLite local protegido, compatível com o painel existente.

O modo SQLite permanece como modo local inicial. A migração para Supabase é
opt-in e documentada; nenhuma credencial Supabase é necessária para abrir a
interface local, mas APP_API_TOKEN deve ser configurado para habilitar dados.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import base64
import time
import threading
import math
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
BACKUPS_DIR = Path(os.getenv("BACKUPS_DIR", str(BASE_DIR / "backups"))).expanduser().resolve()
BACKUPS_DIR.mkdir(exist_ok=True)


def _read_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding an existing environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[0]):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_read_dotenv(BASE_DIR.parent / ".env")
APP_ENV = os.getenv("APP_ENV", "development").lower()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
DB_PATH = Path(os.getenv("SQLITE_PATH", str(BASE_DIR / "jsa.db"))).expanduser().resolve()
APP_API_TOKEN = os.getenv("APP_API_TOKEN", "")
SUPERFRETE_TOKEN = os.getenv("SUPERFRETE_TOKEN", "").strip()
SUPERFRETE_ENV = os.getenv("SUPERFRETE_ENV", "production").strip().lower()
SUPERFRETE_FROM_POSTAL_CODE = os.getenv("SUPERFRETE_FROM_POSTAL_CODE", "").strip()
INFINITEPAY_API_KEY = os.getenv("INFINITEPAY_API_KEY", "").strip()
INFINITEPAY_HANDLE = os.getenv("INFINITEPAY_HANDLE", "").strip().lstrip("$")
INFINITEPAY_REDIRECT_URL = os.getenv("INFINITEPAY_REDIRECT_URL", "").strip()
INFINITEPAY_API_BASE = "https://api.checkout.infinitepay.io"
SUPERFRETE_BASE_URLS = {
    "sandbox": "https://sandbox.superfrete.com/api/v0",
    "production": "https://api.superfrete.com/api/v0",
}
APP_CORS_ORIGINS = [
    item.strip().rstrip("/")
    for item in os.getenv(
        "APP_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"
    ).split(",")
    if item.strip()
]
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_WINDOW_SECONDS = 60
_AUTH_MAX_FAILURES = 5
_INFINITEPAY_RETURN_ATTEMPTS: dict[str, list[float]] = {}
_INFINITEPAY_RETURN_LOCK = threading.Lock()

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_JSON_BYTES = 8 * 1024 * 1024

app = FastAPI(
    title="JSA CRM API",
    version="2.0.0",
    docs_url="/api/docs" if APP_ENV == "development" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if APP_ENV == "development" else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=APP_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "If-Match", "If-None-Match"],
    max_age=600,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_JSON_BYTES:
        return Response(status_code=413, content="Payload too large")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


class AuthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=512)


class ValuePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


class ResetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=1, max_length=64)


class BulkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any]
    expected_versions: dict[str, str | None] | None = None


class SuperFreteQuotePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to_postal_code: str = Field(min_length=8, max_length=9)
    width: float = Field(gt=0, le=300)
    height: float = Field(gt=0, le=300)
    length: float = Field(gt=0, le=300)
    weight: float = Field(gt=0, le=1000, description="Peso do pacote em kg")
    insurance_value: float = Field(default=0, ge=0, le=1000000)
    services: str = Field(default="1,2,17,3,31", max_length=100)


class SuperFreteAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=50)
    address: str = Field(min_length=2, max_length=50)
    district: str = Field(min_length=2, max_length=60)
    city: str = Field(min_length=2, max_length=50)
    state_abbr: str = Field(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    postal_code: str = Field(min_length=8, max_length=9)
    number: str = Field(default="", max_length=10)
    complement: str = Field(default="", max_length=20)
    document: str | None = Field(default=None, max_length=18)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=254)


class SuperFreteVolume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: float = Field(gt=0, le=300)
    height: float = Field(gt=0, le=300)
    length: float = Field(gt=0, le=300)
    weight: float = Field(gt=0, le=1000)


class SuperFreteContentsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=10000)
    unitary_value: float = Field(ge=0, le=1000000)


class SuperFreteShipmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender: SuperFreteAddress
    recipient: SuperFreteAddress
    service: int = Field(ge=1, le=100)
    volume: SuperFreteVolume
    contents: list[SuperFreteContentsItem] = Field(min_length=1, max_length=100)
    insurance_value: float = Field(default=0, ge=0, le=1000000)
    tag: str = Field(default="", max_length=100)
    commercial_invoice: bool = False
    sale_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class SuperFreteCheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=6, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    confirmation: str = Field(min_length=6, max_length=100)
    expected_price: float = Field(ge=0, le=1000000)


class InfinitePayLinkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["sale", "manual"]
    payment_method: Literal["pix", "credit_card"]
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    sale_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    client_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    amount_cents: int | None = Field(default=None, gt=0, le=100000000)
    description: str | None = Field(default=None, min_length=1, max_length=120)


class InfinitePayCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    slug: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
    transaction_nsu: str = Field(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")


class InfinitePayResolvePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(min_length=12, max_length=100)


bearer = HTTPBearer(auto_error=False)


def _require_configured_token() -> str:
    if not APP_API_TOKEN or len(APP_API_TOKEN) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A API de dados está bloqueada. Configure APP_API_TOKEN com pelo menos 32 caracteres aleatórios no arquivo .env e reinicie o servidor.",
        )
    return APP_API_TOKEN


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    secret = _require_configured_token()
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Autenticação necessária.", headers={"WWW-Authenticate": "Bearer"})
    supplied = credentials.credentials
    try:
        body, signature = supplied.split(".", 1)
        expected_signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("invalid signature")
        padded = body + "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if claims.get("sub") != "admin" or int(claims.get("exp", 0)) <= int(time.time()):
            raise ValueError("expired")
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Credencial inválida.", headers={"WWW-Authenticate": "Bearer"}) from exc
    return "admin"


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        yield conn
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=503, detail="Falha temporária ao acessar o banco local.") from exc
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_json(value: str, key: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        # Do not reveal the contents of potentially sensitive database records.
        raise HTTPException(status_code=500, detail=f"Registro '{key}' contém JSON inválido; restaure um backup ou corrija o dado.") from exc


def _validate_key(key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="Identificador de dado inválido.")


def _create_backup() -> tuple[str, str]:
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="O banco local ainda não existe.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"jsa-{stamp}-{secrets.token_hex(3)}.sqlite3"
    destination = BACKUPS_DIR / name
    try:
        source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
            target.commit()
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError("Backup integrity check failed")
        finally:
            target.close()
            source.close()
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return name, digest
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Não foi possível criar e validar o backup.") from exc


def _superfrete_error_fields(raw: bytes) -> list[str]:
    """Extract only schema field names from provider validation errors; never echo values or messages."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    field_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_.\[\]-]{0,79}$")
    field_labels = {"field", "path", "attribute", "parameter", "param"}
    error_containers = {"errors", "fields", "violations", "details", "invalid_params", "validation_errors"}
    reserved = field_labels | error_containers | {"message", "error", "code", "description", "type", "status"}
    found: set[str] = set()

    def walk(node: Any, in_field_map: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key)
                lowered = name.lower()
                if in_field_map and lowered not in reserved and field_pattern.fullmatch(name):
                    found.add(name)
                if lowered in field_labels and isinstance(value, str) and field_pattern.fullmatch(value):
                    found.add(value)
                walk(value, in_field_map or lowered in error_containers)
        elif isinstance(node, list):
            for item in node:
                walk(item, in_field_map)

    walk(parsed)
    return sorted(found)[:8]


def _superfrete_request(path: str, payload: dict[str, Any] | None = None, method: str = "POST") -> Any:
    """Call only a fixed SuperFrete API host; the bearer token never leaves this server."""
    if SUPERFRETE_ENV not in SUPERFRETE_BASE_URLS:
        raise HTTPException(status_code=503, detail="SUPERFRETE_ENV deve ser sandbox ou production.")
    if not SUPERFRETE_TOKEN:
        raise HTTPException(status_code=503, detail="Configure SUPERFRETE_TOKEN no .env do servidor e reinicie o backend.")
    if not path.startswith("/") or path.startswith("//") or ".." in path:
        raise HTTPException(status_code=400, detail="Operação SuperFrete inválida.")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        SUPERFRETE_BASE_URLS[SUPERFRETE_ENV] + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {SUPERFRETE_TOKEN}",
            "User-Agent": "JSA CRM/2.1 (contato@chavesjsa.com.br)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise HTTPException(status_code=502, detail="Resposta SuperFrete excedeu o limite de tamanho.")
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=502, detail="Resposta SuperFrete não está em JSON válido.") from exc
    except urllib.error.HTTPError as exc:
        # Do not forward raw provider bodies or any authorization information.
        if exc.code == 401:
            raise HTTPException(status_code=502, detail="SuperFrete recusou a credencial. Confira se o token é de Produção.") from exc
        if exc.code in (400, 422):
            try:
                provider_body = exc.read(65536)
            except OSError:
                provider_body = b""
            fields = _superfrete_error_fields(provider_body)
            detail = "SuperFrete rejeitou os dados da operação."
            if fields:
                detail += " Campos citados pela API: " + ", ".join(fields) + "."
            else:
                detail += " Confira os dados de remetente, destinatário, serviço e pacote."
            raise HTTPException(status_code=422, detail=detail) from exc
        if exc.code == 429:
            raise HTTPException(status_code=503, detail="Limite temporário de chamadas SuperFrete atingido; aguarde e tente novamente.") from exc
        raise HTTPException(status_code=502, detail=f"Falha na API SuperFrete (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Não foi possível conectar à SuperFrete; tente novamente.") from exc


def _pending_superfrete_orders() -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM kv_store WHERE key = ?", ("jsa-sf-pending-orders",)).fetchone()
    if not row:
        return {}
    value = _safe_json(row["value"], "jsa-sf-pending-orders")
    return value if isinstance(value, dict) else {}


def _save_pending_superfrete_order(order_id: str, summary: dict[str, Any]) -> None:
    orders = _pending_superfrete_orders()
    orders[order_id] = summary
    serialized = json.dumps(orders, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv_store(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("jsa-sf-pending-orders", serialized, _now_iso()),
        )
        conn.commit()


def _find_order_summary(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            result = _find_order_summary(item)
            if result:
                return result
    if isinstance(value, dict):
        raw_id = value.get("id") or value.get("order_id") or value.get("orderId")
        if isinstance(raw_id, (str, int)) and re.fullmatch(r"[A-Za-z0-9_-]{6,80}", str(raw_id)):
            price = next((value.get(key) for key in ("price", "total", "value", "amount") if isinstance(value.get(key), (int, float, str))), None)
            try:
                price = float(price) if price is not None else None
                if price is not None and (not math.isfinite(price) or price < 0):
                    price = None
            except (ValueError, TypeError):
                price = None
            service = value.get("service") or value.get("name")
            if isinstance(service, dict):
                service = service.get("name") or service.get("id")
            if not isinstance(service, (str, int, float)):
                service = None
            return {"order_id": str(raw_id), "price": price, "service": service}
        for item in value.values():
            result = _find_order_summary(item)
            if result:
                return result
    return None


def _find_order_info(value: Any, order_id: str) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            found = _find_order_info(item, order_id)
            if found:
                return found
    elif isinstance(value, dict):
        candidate = value.get("id") or value.get("order_id") or value.get("orderId")
        if str(candidate or "") == order_id:
            return value
        for item in value.values():
            found = _find_order_info(item, order_id)
            if found:
                return found
    return None


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        db_ok = True
    except HTTPException:
        db_ok = False
    return {
        "ok": db_ok,
        "service": "jsa-crm",
        "version": app.version,
        "database": "sqlite-local",
        "authConfigured": bool(APP_API_TOKEN and len(APP_API_TOKEN) >= 32),
    }


@app.post("/api/auth/session")
def create_session(payload: AuthPayload, request: Request) -> dict[str, str]:
    expected = _require_configured_token()
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _AUTH_FAILURES_LOCK:
        recent = [stamp for stamp in _AUTH_FAILURES.get(client_ip, []) if now - stamp < _AUTH_WINDOW_SECONDS]
        _AUTH_FAILURES[client_ip] = recent
        if len(recent) >= _AUTH_MAX_FAILURES:
            raise HTTPException(status_code=429, detail="Muitas tentativas de acesso. Aguarde 60 segundos e tente novamente.")
    if not hmac.compare_digest(payload.password.encode("utf-8"), expected.encode("utf-8")):
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES.setdefault(client_ip, []).append(now)
        raise HTTPException(status_code=401, detail="Senha incorreta.")
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES.pop(client_ip, None)
    # Return a short-lived signed session, never the long-lived configured secret.
    claims = {"sub": "admin", "exp": int(time.time()) + 8 * 60 * 60, "nonce": secrets.token_urlsafe(18)}
    body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(expected.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return {"access_token": f"{body}.{signature}", "token_type": "bearer", "expires_in": str(8 * 60 * 60)}


@app.get("/api/auth/check", dependencies=[Depends(require_admin)])
def check_session() -> dict[str, bool]:
    return {"authenticated": True}


@app.get("/api/data", dependencies=[Depends(require_admin)])
def get_all_data() -> dict[str, Any]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM kv_store ORDER BY key").fetchall()
    return {row["key"]: _safe_json(row["value"], row["key"]) for row in rows}


@app.put("/api/data", dependencies=[Depends(require_admin)])
def set_many_data(payload: BulkPayload) -> dict[str, Any]:
    if not payload.values:
        raise HTTPException(status_code=400, detail="Nenhum dado foi enviado para restauração.")
    serialized: dict[str, str] = {}
    for key, value in payload.values.items():
        _validate_key(key)
        try:
            text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"O registro '{key}' não é JSON válido.") from exc
        if len(text.encode("utf-8")) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail=f"O registro '{key}' excede o limite de 8 MB.")
        serialized[key] = text
    if payload.expected_versions is not None and set(payload.expected_versions) != set(serialized):
        raise HTTPException(status_code=400, detail="expected_versions deve conter exatamente as chaves enviadas.")
    backup_name, backup_sha256 = _create_backup()
    now = _now_iso()
    try:
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if payload.expected_versions is not None:
                current_rows = conn.execute(
                    "SELECT key, updated_at FROM kv_store WHERE key IN (" + ",".join("?" for _ in serialized) + ")",
                    list(serialized),
                ).fetchall()
                current_versions = {row["key"]: row["updated_at"] for row in current_rows}
                conflicts = [key for key, expected in payload.expected_versions.items() if current_versions.get(key) != expected]
                if conflicts:
                    conn.rollback()
                    raise HTTPException(status_code=409, detail="Conflito: uma das coleções foi alterada em outra aba. Recarregue e tente novamente.")
            conn.executemany(
                """INSERT INTO kv_store (key,value,updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                [(key, value, now) for key, value in serialized.items()],
            )
            conn.commit()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="A restauração falhou; os dados anteriores foram mantidos.") from exc
    return {"ok": True, "keys_written": len(serialized), "updated_at": now, "backup_id": backup_name, "backup_sha256": backup_sha256}


@app.get("/api/data/{key}", dependencies=[Depends(require_admin)])
def get_data(key: str) -> dict[str, Any]:
    _validate_key(key)
    with get_conn() as conn:
        row = conn.execute("SELECT value, updated_at FROM kv_store WHERE key = ?", (key,)).fetchone()
    if row is None:
        return {"key": key, "value": None, "updated_at": None}
    return {"key": key, "value": _safe_json(row["value"], key), "updated_at": row["updated_at"]}


@app.put("/api/data/{key}", dependencies=[Depends(require_admin)])
def set_data(key: str, payload: ValuePayload, request: Request) -> dict[str, Any]:
    _validate_key(key)
    try:
        serialized = json.dumps(payload.value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="O conteúdo não pode ser convertido em JSON válido.") from exc
    if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
        raise HTTPException(status_code=413, detail="O registro excede o limite permitido de 8 MB.")
    now = _now_iso()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT updated_at FROM kv_store WHERE key = ?", (key,)).fetchone()
        if current is None:
            if request.headers.get("If-None-Match") != "*":
                raise HTTPException(status_code=428, detail="Versão ausente. Recarregue o painel antes de salvar.")
        else:
            supplied = request.headers.get("If-Match", "").strip().strip('"')
            if not supplied or not hmac.compare_digest(supplied, current["updated_at"]):
                raise HTTPException(status_code=409, detail="Este registro foi alterado em outra aba. Recarregue os dados antes de tentar novamente.")
        conn.execute(
            """INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, serialized, now),
        )
        conn.commit()
    return {"ok": True, "key": key, "updated_at": now}


@app.delete("/api/data/{key}", dependencies=[Depends(require_admin)])
def delete_data(key: str) -> dict[str, Any]:
    _validate_key(key)
    backup_name, backup_sha256 = _create_backup()
    with get_conn() as conn:
        cursor = conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))
        conn.commit()
    return {"ok": True, "deleted": cursor.rowcount == 1, "backup_id": backup_name, "backup_sha256": backup_sha256}


@app.delete("/api/data", status_code=405)
def refuse_delete_all() -> None:
    raise HTTPException(status_code=405, detail="Exclusão em massa desativada. Use a ação administrativa com confirmação e backup obrigatório.")


@app.post("/api/admin/reset", dependencies=[Depends(require_admin)])
def reset_all_data(payload: ResetPayload) -> dict[str, Any]:
    if payload.confirmation != "APAGAR TODOS OS DADOS":
        raise HTTPException(status_code=400, detail="Confirmação administrativa não corresponde.")
    backup_name, backup_sha256 = _create_backup()
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM kv_store")
        conn.commit()
    return {"ok": True, "backup": backup_name, "backup_sha256": backup_sha256}


@app.post("/api/backup/snapshot", dependencies=[Depends(require_admin)])
def backup_snapshot() -> dict[str, Any]:
    name, digest = _create_backup()
    # Only an opaque basename and digest are returned; never the host path.
    return {"ok": True, "backup_id": name, "sha256": digest}


@app.get("/api/superfrete/config", dependencies=[Depends(require_admin)])
def superfrete_config() -> dict[str, Any]:
    return {
        "configured": bool(SUPERFRETE_TOKEN),
        "environment": SUPERFRETE_ENV if SUPERFRETE_ENV in SUPERFRETE_BASE_URLS else "invalid",
        "originPostalCode": re.sub(r"\D", "", SUPERFRETE_FROM_POSTAL_CODE),
        "tokenSource": ".env/server environment",
    }


@app.post("/api/superfrete/quote", dependencies=[Depends(require_admin)])
def superfrete_quote(payload: SuperFreteQuotePayload) -> dict[str, Any]:
    origin = re.sub(r"\D", "", SUPERFRETE_FROM_POSTAL_CODE)
    destination = re.sub(r"\D", "", payload.to_postal_code)
    if len(origin) != 8:
        raise HTTPException(status_code=503, detail="Configure SUPERFRETE_FROM_POSTAL_CODE (CEP de origem) no .env do servidor.")
    if len(origin) != 8 or len(destination) != 8:
        raise HTTPException(status_code=422, detail="CEP de origem e destino precisam conter 8 dígitos.")
    numeric_values = (payload.width, payload.height, payload.length, payload.weight, payload.insurance_value)
    if not all(math.isfinite(value) for value in numeric_values):
        raise HTTPException(status_code=422, detail="Dimensões, peso e valor declarado devem ser números finitos.")
    allowed_services = {"1", "2", "3", "17", "31", "33"}
    services = [value.strip() for value in payload.services.split(",") if value.strip()]
    if not services or not set(services).issubset(allowed_services):
        raise HTTPException(status_code=422, detail="Serviços permitidos: 1, 2, 3, 17, 31 e 33.")
    remote_payload = {
        "from": {"postal_code": origin},
        "to": {"postal_code": destination},
        "package": {"width": payload.width, "height": payload.height, "length": payload.length, "weight": payload.weight},
        "services": ",".join(services),
        "options": {"insurance_value": payload.insurance_value, "use_insurance_value": payload.insurance_value > 0},
    }
    data = _superfrete_request("/calculator", remote_payload)
    return {"ok": True, "environment": SUPERFRETE_ENV, "quotes": data}


@app.get("/api/superfrete/account", dependencies=[Depends(require_admin)])
def superfrete_account() -> dict[str, Any]:
    data = _superfrete_request("/user", method="GET")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Resposta inesperada ao consultar conta SuperFrete.")
    # Return only finite numeric balance/capacity fields; never forward nested provider account objects.
    allowed: dict[str, int | float] = {}
    for key in ("balance", "shipments", "shipments_available"):
        try:
            number = float(data[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            allowed[key] = number if key == "balance" or not number.is_integer() else int(number)
    return {"ok": True, "environment": SUPERFRETE_ENV, "account": allowed}


def _postal_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _sf_address_json(address: SuperFreteAddress, *, recipient: bool) -> dict[str, Any]:
    postal = _postal_digits(address.postal_code)
    if len(postal) != 8:
        raise HTTPException(status_code=422, detail="O CEP precisa conter 8 dígitos.")
    document = _postal_digits(address.document or "")
    if recipient and len(document) not in (11, 14):
        raise HTTPException(status_code=422, detail="Informe CPF ou CNPJ válido do destinatário para a etiqueta.")
    phone = _postal_digits(address.phone or "")
    normalized = {
        "name": address.name.strip(),
        "address": address.address.strip(),
        "district": address.district.strip(),
        "city": address.city.strip(),
        "state_abbr": address.state_abbr.upper(),
        "postal_code": postal,
        "number": address.number.strip(),
        "complement": address.complement.strip(),
    }
    if document:
        normalized["document"] = document
    if recipient:
        if address.email:
            normalized["email"] = address.email.strip() or None
        if phone:
            if len(phone) != 11:
                raise HTTPException(status_code=422, detail="Telefone do destinatário deve ter 11 dígitos, incluindo DDD.")
            normalized["phone"] = phone
    return normalized


def _find_crm_sale(sale_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        sales = _load_kv_value(conn, "jsa-vendas", [])
    if not isinstance(sales, list):
        return None
    return next((sale for sale in sales if isinstance(sale, dict) and sale.get("id") == sale_id), None)


@app.post("/api/superfrete/shipments", dependencies=[Depends(require_admin)])
def superfrete_create_pending_shipment(payload: SuperFreteShipmentPayload) -> dict[str, Any]:
    if payload.service not in {1, 2, 17, 3, 31, 33}:
        raise HTTPException(status_code=422, detail="Serviço SuperFrete inválido.")
    linked_sale: dict[str, str] = {}
    if payload.sale_id:
        sale = _find_crm_sale(payload.sale_id)
        if not sale:
            raise HTTPException(status_code=404, detail="A venda selecionada não foi encontrada no CRM; nenhum pedido foi criado.")
        linked_sale = {
            "sale_id": payload.sale_id,
            "sale_number": str(sale.get("numero") or "")[:80],
            "customer_id": str(sale.get("clienteId") or "")[:128],
        }
    if payload.service == 33 and len(_postal_digits(payload.recipient.phone or "")) != 11:
        raise HTTPException(status_code=422, detail="J&T exige telefone do destinatário com DDD e 11 dígitos.")
    sender = _sf_address_json(payload.sender, recipient=False)
    recipient = _sf_address_json(payload.recipient, recipient=True)
    origin = _postal_digits(SUPERFRETE_FROM_POSTAL_CODE)
    if origin and sender["postal_code"] != origin:
        raise HTTPException(status_code=422, detail="O CEP de origem deve corresponder ao SUPERFRETE_FROM_POSTAL_CODE do .env.")
    volume = payload.volume.model_dump()
    if not all(math.isfinite(float(value)) for value in volume.values()):
        raise HTTPException(status_code=422, detail="Dimensões e peso precisam ser números finitos.")
    products = [item.model_dump() for item in payload.contents]
    remote_payload = {
        "from": sender,
        "to": recipient,
        "service": payload.service,
        "volumes": volume,
        "products": products,
        "options": {
            "insurance_value": payload.insurance_value if payload.insurance_value > 0 else None,
            "receipt": False,
            "own_hand": False,
            "non_commercial": True,
        },
        "tag": payload.tag.strip() or "JSA CRM",
        "platform": "JSA CRM",
    }
    result = _superfrete_request("/cart", remote_payload)
    summary = _find_order_summary(result)
    if not summary:
        raise HTTPException(status_code=502, detail="SuperFrete respondeu, mas não foi possível identificar o ID da etiqueta pendente. Confira sua conta antes de repetir para evitar duplicidade.")
    summary.setdefault("service_id", payload.service)
    try:
        _save_pending_superfrete_order(summary["order_id"], {**summary, **linked_sale, "status": "pending", "created_at": _now_iso()})
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail=f"A SuperFrete criou o pedido {summary['order_id']}, mas o CRM não conseguiu gravá-lo localmente. Não repita a criação; confira esse ID no painel SuperFrete.") from exc
    try:
        detail = _superfrete_request(f"/order/info/{summary['order_id']}", method="GET")
        detail_summary = _find_order_summary(detail)
        if detail_summary and detail_summary.get("price") is not None:
            summary["price"] = detail_summary["price"]
        if isinstance(detail, dict):
            for key in ("status", "tracking", "delivery", "company", "service"):
                if key in detail:
                    summary[key] = detail[key]
    except HTTPException:
        # Cart creation already succeeded; the order remains recoverable by ID, but without a verified amount.
        pass
    summary.update(linked_sale)
    _save_pending_superfrete_order(summary["order_id"], {**summary, "status": "pending", "created_at": _now_iso()})
    # The address/document payload is not echoed back to the browser or logged.
    return {"ok": True, "environment": SUPERFRETE_ENV, "shipment": summary, "status": "pending", "paymentRequired": True}


@app.post("/api/superfrete/checkout", dependencies=[Depends(require_admin)])
def superfrete_checkout(payload: SuperFreteCheckoutPayload) -> dict[str, Any]:
    if not secrets.compare_digest(payload.confirmation, f"PAGAR {payload.order_id}"):
        raise HTTPException(status_code=400, detail="Confirmação de pagamento não corresponde ao ID do pedido.")
    pending = _pending_superfrete_orders()
    order = pending.get(payload.order_id)
    if not isinstance(order, dict) or order.get("status") != "pending":
        raise HTTPException(status_code=404, detail="Pedido pendente não foi criado por esta instalação; pagamento bloqueado.")
    if not isinstance(order.get("price"), (int, float)) or not math.isfinite(float(order["price"])) or float(order["price"]) < 0:
        raise HTTPException(status_code=409, detail="O preço real da etiqueta ainda não foi confirmado pela API. Atualize os dados do pedido antes do checkout.")
    if not math.isfinite(payload.expected_price) or round(float(order["price"]), 2) != round(payload.expected_price, 2):
        raise HTTPException(status_code=409, detail="O preço exibido mudou. Atualize o pedido e confirme novamente o valor exato.")
    # Mark first: if the provider/network result is uncertain, block duplicate payment.
    order["status"] = "checkout_in_progress"
    pending[payload.order_id] = order
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv_store(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("jsa-sf-pending-orders", json.dumps(pending, ensure_ascii=False, allow_nan=False, separators=(",", ":")), _now_iso()),
        )
        conn.commit()
    # The API performs the debit. This endpoint is deliberately separate from cart creation.
    result = _superfrete_request("/checkout", {"orders": [payload.order_id]})
    order["status"] = "checkout_requested"
    order["checkout_at"] = _now_iso()
    pending[payload.order_id] = order
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv_store(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("jsa-sf-pending-orders", json.dumps(pending, ensure_ascii=False, allow_nan=False, separators=(",", ":")), _now_iso()),
        )
        conn.commit()
    return {"ok": True, "environment": SUPERFRETE_ENV, "order_id": payload.order_id}


@app.get("/api/superfrete/pending", dependencies=[Depends(require_admin)])
def superfrete_pending_orders() -> dict[str, Any]:
    orders = _pending_superfrete_orders()
    # Only opaque CRM references, order metadata and timestamps are returned; never addresses/documents.
    safe = [{"order_id": order_id, **{key: item.get(key) for key in ("price", "service", "service_id", "status", "created_at", "sale_id", "sale_number", "customer_id")}}
            for order_id, item in orders.items() if isinstance(item, dict)]
    return {"ok": True, "orders": safe}


@app.get("/api/superfrete/orders/{order_id}", dependencies=[Depends(require_admin)])
def superfrete_order_info(order_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", order_id):
        raise HTTPException(status_code=422, detail="ID de etiqueta inválido.")
    data = _superfrete_request(f"/order/info/{order_id}", method="GET")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Resposta inesperada ao consultar etiqueta.")
    info = _find_order_info(data, order_id) or data
    safe = {key: info[key] for key in ("id", "status", "tracking", "price", "delivery", "company", "service") if key in info}
    pending = _pending_superfrete_orders()
    if order_id in pending and isinstance(pending[order_id], dict):
        order = pending[order_id]
        if isinstance(info.get("price"), (int, float, str)):
            try:
                price = float(info["price"])
                if math.isfinite(price) and price >= 0:
                    order["price"] = price
            except (TypeError, ValueError):
                pass
        provider_status = str(info.get("status") or "").lower()
        if provider_status in {"pending", "released", "posted", "delivered", "cancelled", "canceled"}:
            order["status"] = "pending" if provider_status == "pending" else provider_status
        pending[order_id] = order
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO kv_store(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                ("jsa-sf-pending-orders", json.dumps(pending, ensure_ascii=False, allow_nan=False, separators=(",", ":")), _now_iso()),
            )
            conn.commit()
    return {"ok": True, "environment": SUPERFRETE_ENV, "order": safe}


@app.post("/api/superfrete/orders/{order_id}/print", dependencies=[Depends(require_admin)])
def superfrete_print_label(order_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", order_id):
        raise HTTPException(status_code=422, detail="ID de etiqueta inválido.")
    data = _superfrete_request("/tag/print", {"orders": [order_id]})
    urls: list[str] = []
    def collect_urls(item: Any) -> None:
        if isinstance(item, dict):
            for value in item.values():
                collect_urls(value)
        elif isinstance(item, list):
            for value in item:
                collect_urls(value)
        elif isinstance(item, str) and item.startswith("https://"):
            from urllib.parse import urlparse
            if urlparse(item).hostname in {"api.superfrete.com", "web.superfrete.com", "superfrete.com"}:
                urls.append(item)
    collect_urls(data)
    if not urls:
        raise HTTPException(status_code=502, detail="A SuperFrete não retornou um link PDF reconhecido.")
    return {"ok": True, "url": urls[0]}


INFINITEPAY_PAYMENTS_KEY = "jsa-pagamentos"
INFINITEPAY_ACTIVE_STATUSES = {"creating", "uncertain", "pending", "paid", "review"}


class InfinitePayProviderError(Exception):
    def __init__(self, public_detail: str, definitive: bool):
        super().__init__(public_detail)
        self.public_detail = public_detail
        self.definitive = definitive


def _infinitepay_transport_error(endpoint: str, exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    reason_type = type(reason).__name__
    error_number = getattr(reason, "errno", None)
    logging.getLogger(__name__).warning(
        "InfinitePay outbound network failure endpoint=%s reason_type=%s errno=%s",
        endpoint, reason_type, error_number,
    )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "A InfinitePay excedeu o tempo limite de 20 segundos. O resultado pode ser incerto; confira o painel antes de tentar novamente."
    if isinstance(reason, socket.gaierror):
        return "Falha de DNS ao localizar a API da InfinitePay. Verifique a conexão, o DNS ou o firewall do computador servidor."
    if isinstance(reason, ssl.SSLError):
        return "Falha na conexão HTTPS com a InfinitePay. Verifique certificados, relógio do computador e inspeção TLS do antivírus/proxy."
    if isinstance(reason, ConnectionRefusedError):
        return "A conexão com a API da InfinitePay foi recusada pela rede. Verifique proxy, firewall ou política de saída HTTPS."
    return f"Falha de rede ao acessar a InfinitePay ({reason_type}). Confira a conexão e o firewall; verifique a conta antes de repetir."


def _infinitepay_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call the fixed InfinitePay Checkout host; the API key stays server-side."""
    if not INFINITEPAY_API_KEY:
        raise HTTPException(status_code=503, detail="Configure INFINITEPAY_API_KEY no .env do servidor e reinicie o backend.")
    if endpoint not in {"/links", "/payment_check"}:
        raise HTTPException(status_code=400, detail="Operação InfinitePay inválida.")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        INFINITEPAY_API_BASE + endpoint,
        data=body,
        method="POST",
        headers={
            "X-Api-Key": INFINITEPAY_API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "JSA CRM/2.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise InfinitePayProviderError("A resposta da InfinitePay excedeu o limite de tamanho.", definitive=False)
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(parsed, dict):
            raise InfinitePayProviderError("Resposta inesperada da InfinitePay.", definitive=False)
        return parsed
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 422):
            detail = "A InfinitePay recusou os dados. Confira handle, itens e método de pagamento."
            definitive = True
        elif exc.code in (401, 403):
            detail = "A InfinitePay recusou a credencial. Confira a API Key e a ativação do Checkout Integrado."
            definitive = True
        elif exc.code == 429:
            detail = "Limite temporário da InfinitePay atingido. Aguarde antes de criar outro link."
            definitive = True
        else:
            detail = f"Falha temporária na InfinitePay (HTTP {exc.code}); confirme o resultado no painel antes de tentar novamente."
            definitive = False
        raise InfinitePayProviderError(detail, definitive=definitive) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InfinitePayProviderError(_infinitepay_transport_error(endpoint, exc), definitive=False) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InfinitePayProviderError(
            "Resposta inválida da InfinitePay; confirme no painel se a cobrança foi criada antes de repetir.",
            definitive=False,
        ) from exc


def _payment_records_from_conn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (INFINITEPAY_PAYMENTS_KEY,)).fetchone()
    if not row:
        return []
    value = _safe_json(row["value"], INFINITEPAY_PAYMENTS_KEY)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _save_payment_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    serialized = json.dumps(records, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > MAX_JSON_BYTES:
        raise HTTPException(status_code=413, detail="O histórico de pagamentos excede o limite permitido.")
    conn.execute(
        "INSERT INTO kv_store(key, value, updated_at) VALUES(?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (INFINITEPAY_PAYMENTS_KEY, serialized, _now_iso()),
    )


def _amount_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Preço de item inválido.") from exc
    if not amount.is_finite() or amount < 0:
        raise HTTPException(status_code=422, detail="Preço de item inválido.")
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents > 100000000:
        raise HTTPException(status_code=422, detail="O valor excede o limite permitido para uma cobrança.")
    return cents


def _infinitepay_customer(client: Any) -> dict[str, str] | None:
    if not isinstance(client, dict):
        return None
    customer: dict[str, str] = {}
    name = str(client.get("nome") or "").strip()[:120]
    email = str(client.get("email") or "").strip()[:254]
    if name:
        customer["name"] = name
    if email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        customer["email"] = email
    phone = re.sub(r"\D", "", str(client.get("telefone") or ""))
    if phone.startswith("55") and len(phone) in (12, 13):
        customer["phone_number"] = "+" + phone
    elif len(phone) in (10, 11):
        customer["phone_number"] = "+55" + phone
    return customer or None


def _load_kv_value(conn: sqlite3.Connection, key: str, fallback: Any) -> Any:
    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    return _safe_json(row["value"], key) if row else fallback


def _find_infinitepay_checkout_url(value: Any) -> str | None:
    allowed_keys = {"url", "checkout_url", "checkouturl", "checkout_link", "checkoutlink", "payment_url", "paymenturl", "link"}
    from urllib.parse import urlparse

    if isinstance(value, dict):
        for key, candidate in value.items():
            if str(key).lower() in allowed_keys and isinstance(candidate, str) and len(candidate) <= 2048:
                parsed = urlparse(candidate)
                host = (parsed.hostname or "").lower()
                if parsed.scheme == "https" and parsed.username is None and parsed.password is None and (host == "infinitepay.io" or host.endswith(".infinitepay.io")):
                    return candidate
        for candidate in value.values():
            found = _find_infinitepay_checkout_url(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_infinitepay_checkout_url(candidate)
            if found:
                return found
    return None


def _valid_infinitepay_redirect_url() -> bool:
    if not INFINITEPAY_REDIRECT_URL:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(INFINITEPAY_REDIRECT_URL)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".test", ".invalid")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return False
    except ValueError:
        pass
    return bool(
        parsed.scheme == "https" and host and parsed.username is None
        and parsed.password is None and parsed.path.rstrip("/") == "/api/infinitepay/return"
        and not parsed.query and not parsed.fragment and len(INFINITEPAY_REDIRECT_URL) <= 2048
    )


def _payment_public_view(payment: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id", "request_id", "order_nsu", "source", "sale_id", "sale_number", "description",
        "payment_method", "amount_cents", "status", "checkout_url", "created_at", "expires_at",
        "checked_at", "capture_method", "installments", "slug", "transaction_nsu",
    )
    return {key: payment[key] for key in allowed if key in payment}


def _reserve_infinitepay_link(payload: InfinitePayLinkPayload) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        records = _payment_records_from_conn(conn)
        existing_request = next((row for row in records if row.get("request_id") == payload.request_id), None)
        if existing_request:
            if existing_request.get("status") in {"pending", "paid"} and existing_request.get("checkout_url"):
                conn.rollback()
                return existing_request, None, True
            conn.rollback()
            raise HTTPException(status_code=409, detail="Esta tentativa já existe e não pode ser repetida automaticamente. Verifique o histórico e o painel InfinitePay.")

        items: list[dict[str, Any]]
        amount_cents: int
        sale: dict[str, Any] | None = None
        client: dict[str, Any] | None = None
        if payload.source == "sale":
            if not payload.sale_id or payload.amount_cents is not None or payload.description is not None:
                conn.rollback()
                raise HTTPException(status_code=422, detail="Para criar pelo pedido, informe apenas o ID da venda e o método.")
            active = next((row for row in records if row.get("sale_id") == payload.sale_id and row.get("status") in INFINITEPAY_ACTIVE_STATUSES), None)
            if active:
                conn.rollback()
                if active.get("checkout_url"):
                    return active, None, True
                raise HTTPException(status_code=409, detail="Já existe uma tentativa de pagamento sem resultado confirmado. Verifique o painel InfinitePay antes de gerar outra.")
            sales = _load_kv_value(conn, "jsa-vendas", [])
            sale = next((row for row in sales if isinstance(row, dict) and row.get("id") == payload.sale_id), None) if isinstance(sales, list) else None
            if not sale:
                conn.rollback()
                raise HTTPException(status_code=404, detail="Venda não encontrada no CRM.")
            items = []
            for item in sale.get("itens", []) if isinstance(sale.get("itens"), list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    quantity_value = Decimal(str(item.get("qtd", 0)))
                    quantity = int(quantity_value)
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if quantity_value != quantity or quantity < 1 or quantity > 10000:
                    continue
                price_cents = _amount_to_cents(item.get("precoVenda", 0))
                if price_cents < 1:
                    continue
                description = str(item.get("produtoNome") or item.get("modelo") or "Produto")[:120]
                items.append({"quantity": quantity, "price": price_cents, "description": description})
            if not items:
                conn.rollback()
                raise HTTPException(status_code=422, detail="A venda não possui itens e preços válidos para cobrança.")
            amount_cents = sum(row["quantity"] * row["price"] for row in items)
            if sale.get("freteAssumido") == "cliente":
                freight_cents = _amount_to_cents(sale.get("frete", 0))
                if freight_cents:
                    items.append({"quantity": 1, "price": freight_cents, "description": "Frete de entrega"})
                    amount_cents += freight_cents
            if amount_cents < 1 or amount_cents > 100000000:
                conn.rollback()
                raise HTTPException(status_code=422, detail="O total da venda excede o limite permitido para uma cobrança.")
            if payload.client_id and payload.client_id != sale.get("clienteId"):
                conn.rollback()
                raise HTTPException(status_code=422, detail="O cliente informado não corresponde à venda.")
            client_id = sale.get("clienteId")
            clients = _load_kv_value(conn, "jsa-clientes", [])
            client = next((row for row in clients if isinstance(row, dict) and row.get("id") == client_id), None) if isinstance(clients, list) else None
        else:
            if payload.sale_id or payload.amount_cents is None or not payload.description:
                conn.rollback()
                raise HTTPException(status_code=422, detail="Para cobrança avulsa, informe descrição e valor positivo em centavos.")
            amount_cents = payload.amount_cents
            items = [{"quantity": 1, "price": amount_cents, "description": payload.description.strip()}]
            if payload.client_id:
                clients = _load_kv_value(conn, "jsa-clientes", [])
                client = next((row for row in clients if isinstance(row, dict) and row.get("id") == payload.client_id), None) if isinstance(clients, list) else None
                if not client:
                    conn.rollback()
                    raise HTTPException(status_code=404, detail="Cliente não encontrado no CRM.")

        if payload.source == "sale" and sale:
            sale_number = str(sale.get("numero") or "")[:80]
            description = f"Venda {sale_number}" if sale_number else "Venda JSA CRM"
        else:
            sale_number = ""
            description = payload.description.strip()[:120]
        payment_id = uuid.uuid4().hex
        order_nsu = "PAG-" + uuid.uuid4().hex.upper()
        expires_at = (now + timedelta(days=7)).isoformat(timespec="seconds")
        record = {
            "id": payment_id,
            "request_id": payload.request_id,
            "order_nsu": order_nsu,
            "source": payload.source,
            "sale_id": sale.get("id") if sale else None,
            "sale_number": sale_number or None,
            "description": description,
            "payment_method": payload.payment_method,
            "amount_cents": amount_cents,
            "items": items,
            "status": "creating",
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": expires_at,
        }
        records.append(record)
        _save_payment_records(conn, records)
        conn.commit()

    checkout_options: dict[str, Any] = {
        "single_payment": True,
        "valid_until": expires_at,
    }
    # InfinitePay keeps credit card enabled; include both entries when adding Pix.
    if payload.payment_method == "pix":
        checkout_options["payment_methods_enabled"] = ["credit_card", "pix"]
    checkout_payload: dict[str, Any] = {
        "order_nsu": order_nsu,
        "items": items,
        "options": checkout_options,
    }
    if INFINITEPAY_REDIRECT_URL:
        checkout_payload["redirect_url"] = INFINITEPAY_REDIRECT_URL
    customer = _infinitepay_customer(client)
    if customer:
        checkout_payload["customer"] = customer
    return record, checkout_payload, False


def _update_payment(payment_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        records = _payment_records_from_conn(conn)
        target = next((row for row in records if row.get("id") == payment_id), None)
        if not target:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Registro de pagamento não encontrado.")
        target.update(updates)
        _save_payment_records(conn, records)
        conn.commit()
        return target


@app.get("/api/infinitepay/config", dependencies=[Depends(require_admin)])
def infinitepay_config() -> dict[str, Any]:
    return {
        "configured": bool(INFINITEPAY_API_KEY),
        "linkConfigured": bool(INFINITEPAY_API_KEY),
        "apiKeyConfigured": bool(INFINITEPAY_API_KEY),
        "handleConfigured": bool(INFINITEPAY_HANDLE),
        "paymentCheckConfigured": bool(INFINITEPAY_API_KEY and INFINITEPAY_HANDLE),
        "paymentMethods": ["pix", "credit_card"] if INFINITEPAY_API_KEY else [],
        "checkoutHost": INFINITEPAY_API_BASE,
        "redirectUrlConfigured": bool(INFINITEPAY_API_KEY and INFINITEPAY_HANDLE and _valid_infinitepay_redirect_url()),
        "webhookConfigured": False,
    }


@app.get("/api/infinitepay/payments", dependencies=[Depends(require_admin)])
def infinitepay_payments() -> dict[str, Any]:
    with get_conn() as conn:
        records = _payment_records_from_conn(conn)
    safe = [_payment_public_view(row) for row in records]
    safe.sort(key=lambda row: row.get("created_at", ""), reverse=True)
    return {"ok": True, "payments": safe}


@app.post("/api/infinitepay/links", dependencies=[Depends(require_admin)])
def infinitepay_create_link(payload: InfinitePayLinkPayload) -> dict[str, Any]:
    if not INFINITEPAY_API_KEY:
        raise HTTPException(status_code=503, detail="Configure INFINITEPAY_API_KEY no .env do servidor; esta integração usa opções avançadas do Checkout.")
    if INFINITEPAY_REDIRECT_URL and not _valid_infinitepay_redirect_url():
        raise HTTPException(status_code=503, detail="INFINITEPAY_REDIRECT_URL precisa ser uma URL HTTPS pública; deixe vazia para desativar o retorno automático.")
    record, provider_payload, reused = _reserve_infinitepay_link(payload)
    if reused:
        return {"ok": True, "reused": True, "payment": _payment_public_view(record)}
    try:
        provider_result = _infinitepay_request("/links", provider_payload or {})
    except InfinitePayProviderError as exc:
        _update_payment(record["id"], {"status": "failed" if exc.definitive else "uncertain", "error": exc.public_detail})
        raise HTTPException(status_code=502, detail=exc.public_detail) from exc
    checkout_url = _find_infinitepay_checkout_url(provider_result)
    if not checkout_url:
        detail = "A InfinitePay respondeu sem uma URL de checkout reconhecida. Confira o painel antes de tentar novamente."
        _update_payment(record["id"], {"status": "uncertain", "error": detail})
        raise HTTPException(status_code=502, detail=detail)
    updated = _update_payment(record["id"], {"status": "pending", "checkout_url": checkout_url})
    return {"ok": True, "reused": False, "payment": _payment_public_view(updated)}


@app.post("/api/infinitepay/payment-check", dependencies=[Depends(require_admin)])
def infinitepay_payment_check(payload: InfinitePayCheckPayload) -> dict[str, Any]:
    if not INFINITEPAY_API_KEY or not INFINITEPAY_HANDLE:
        raise HTTPException(status_code=503, detail="Configure INFINITEPAY_API_KEY e INFINITEPAY_HANDLE no .env para consultar pagamentos.")
    with get_conn() as conn:
        records = _payment_records_from_conn(conn)
    payment = next((row for row in records if row.get("id") == payload.payment_id), None)
    if not payment:
        raise HTTPException(status_code=404, detail="Registro de pagamento não encontrado.")
    if payment.get("status") not in {"pending", "review", "paid"}:
        raise HTTPException(status_code=409, detail="Este pagamento ainda não tem link confirmado para consulta.")
    try:
        result = _infinitepay_request("/payment_check", {
            "handle": INFINITEPAY_HANDLE,
            "order_nsu": payment["order_nsu"],
            "transaction_nsu": payload.transaction_nsu,
            "slug": payload.slug,
        })
    except InfinitePayProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.public_detail) from exc
    if not result.get("success"):
        raise HTTPException(status_code=502, detail="A InfinitePay não confirmou a consulta. Confira os dados de retorno do checkout.")
    paid = result.get("paid") is True
    provider_amount = result.get("amount")
    try:
        provider_amount_cents = int(provider_amount) if provider_amount is not None else None
    except (TypeError, ValueError):
        provider_amount_cents = None
    checked_status = "pending"
    if paid:
        checked_status = "paid" if provider_amount_cents == payment.get("amount_cents") else "review"
    elif payment.get("status") == "paid":
        checked_status = "paid"
    capture_method = result.get("capture_method")
    updates: dict[str, Any] = {
        "status": checked_status,
        "checked_at": _now_iso(),
        "slug": payload.slug,
        "transaction_nsu": payload.transaction_nsu,
    }
    if capture_method in {"pix", "credit_card"}:
        updates["capture_method"] = capture_method
    installments = result.get("installments")
    if isinstance(installments, int) and 1 <= installments <= 12:
        updates["installments"] = installments
    updated = _update_payment(payment["id"], updates)
    if checked_status == "review":
        return {"ok": True, "payment": _payment_public_view(updated), "message": "A InfinitePay informou pagamento, mas o valor não confere com o CRM. Revise antes de marcar como pago."}
    return {"ok": True, "payment": _payment_public_view(updated)}


@app.post("/api/infinitepay/payments/{payment_id}/resolve-uncertain", dependencies=[Depends(require_admin)])
def infinitepay_resolve_uncertain(payment_id: str, payload: InfinitePayResolvePayload) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Fa-f0-9]{32}", payment_id):
        raise HTTPException(status_code=422, detail="ID de pagamento inválido.")
    with get_conn() as conn:
        records = _payment_records_from_conn(conn)
    payment = next((row for row in records if row.get("id") == payment_id), None)
    if not payment:
        raise HTTPException(status_code=404, detail="Registro de pagamento não encontrado.")
    if payment.get("status") not in {"creating", "uncertain"}:
        raise HTTPException(status_code=409, detail="Somente tentativas sem resultado confirmado podem ser liberadas por esta ação.")
    if payment.get("status") == "creating":
        try:
            created = datetime.fromisoformat(str(payment.get("created_at", "")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created).total_seconds() < 120:
                raise HTTPException(status_code=409, detail="A tentativa ainda está em andamento. Aguarde pelo menos dois minutos e confira o painel InfinitePay.")
        except ValueError:
            raise HTTPException(status_code=409, detail="A data da tentativa não pôde ser validada; não libere nova cobrança automaticamente.")
    expected = "CONFIRMO NAO CRIADO " + str(payment.get("order_nsu", ""))
    if not hmac.compare_digest(payload.confirmation.strip(), expected):
        raise HTTPException(status_code=400, detail=f"Para liberar, digite exatamente: {expected}")
    updated = _update_payment(payment_id, {
        "status": "failed",
        "resolution": "operator_verified_absent",
        "resolved_at": _now_iso(),
    })
    return {"ok": True, "payment": _payment_public_view(updated)}


def _allow_infinitepay_return(client_ip: str) -> bool:
    now = time.monotonic()
    with _INFINITEPAY_RETURN_LOCK:
        for key, attempts in list(_INFINITEPAY_RETURN_ATTEMPTS.items()):
            kept = [stamp for stamp in attempts if now - stamp < 60]
            if kept:
                _INFINITEPAY_RETURN_ATTEMPTS[key] = kept
            else:
                _INFINITEPAY_RETURN_ATTEMPTS.pop(key, None)
        attempts = _INFINITEPAY_RETURN_ATTEMPTS.setdefault(client_ip, [])
        if len(attempts) >= 10:
            return False
        attempts.append(now)
        return True


def _infinitepay_return_page(message: str, code: int = 200) -> HTMLResponse:
    safe_message = {
        "confirmed": "Pagamento confirmado pela InfinitePay. Você pode fechar esta janela.",
        "pending": "O retorno foi recebido, mas o pagamento ainda não consta como aprovado. Atualize o JSA CRM ou confira a InfinitePay.",
        "review": "O pagamento requer conferência manual. Não considere o pedido como pago antes de revisar o valor no JSA CRM.",
        "invalid": "Não foi possível associar este retorno a um pedido válido.",
        "error": "Não foi possível confirmar o pagamento agora. Verifique o JSA CRM ou o painel InfinitePay.",
    }.get(message, "Não foi possível confirmar o pagamento agora.")
    html = (
        "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Retorno de pagamento</title><style>body{font:16px system-ui,sans-serif;max-width:620px;margin:12vh auto;padding:24px;color:#172033}"
        ".box{border:1px solid #d8dee9;border-radius:12px;padding:24px}h1{font-size:1.4rem}</style><body><main class='box'>"
        "<h1>JSA CRM · Pagamento</h1><p>" + safe_message + "</p></main></body></html>"
    )
    return HTMLResponse(html, status_code=code, headers={
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    })


@app.get("/api/infinitepay/return", response_class=HTMLResponse)
def infinitepay_public_return(
    request: Request,
    order_nsu: str = Query(min_length=6, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
    slug: str = Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
    transaction_nsu: str = Query(min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
) -> HTMLResponse:
    if not _valid_infinitepay_redirect_url() or not INFINITEPAY_API_KEY or not INFINITEPAY_HANDLE:
        raise HTTPException(status_code=404, detail="Rota de retorno indisponível.")
    from urllib.parse import urlparse
    configured_host = (urlparse(INFINITEPAY_REDIRECT_URL).hostname or "").lower()
    if (request.url.hostname or "").lower() != configured_host:
        raise HTTPException(status_code=404, detail="Rota de retorno indisponível.")
    client_ip = request.client.host if request.client else "unknown"
    if not _allow_infinitepay_return(client_ip):
        return _infinitepay_return_page("error", code=429)
    with get_conn() as conn:
        records = _payment_records_from_conn(conn)
    payment = next((row for row in records if row.get("order_nsu") == order_nsu), None)
    if not payment:
        return _infinitepay_return_page("invalid", code=404)
    if payment.get("status") == "paid":
        return _infinitepay_return_page("confirmed")
    if payment.get("status") not in {"pending", "review"}:
        return _infinitepay_return_page("invalid", code=409)
    try:
        result = infinitepay_payment_check(InfinitePayCheckPayload(
            payment_id=payment["id"], slug=slug, transaction_nsu=transaction_nsu
        ))
    except HTTPException:
        return _infinitepay_return_page("error", code=502)
    result_status = result.get("payment", {}).get("status")
    if result_status == "paid":
        return _infinitepay_return_page("confirmed")
    if result_status == "review":
        return _infinitepay_return_page("review", code=202)
    return _infinitepay_return_page("pending", code=202)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=APP_HOST, port=APP_PORT, reload=False, proxy_headers=False)
