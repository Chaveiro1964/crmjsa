from __future__ import annotations

import json
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOKEN = "test-only-token-never-use-in-production-0123456789abcdef"
sys.path.insert(0, str(BACKEND))
import app as backend_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="jsa-api-test-")
        cls.temp_path = Path(cls.temp.name)
        cls.port = free_port()
        env = os.environ.copy()
        env.update({
            "APP_ENV": "test",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(cls.port),
            "APP_API_TOKEN": TOKEN,
            "APP_CORS_ORIGINS": f"http://127.0.0.1:{cls.port}",
            "SQLITE_PATH": str(cls.temp_path / "test.db"),
            "BACKUPS_DIR": str(cls.temp_path / "backups"),
            "PYTHONUNBUFFERED": "1",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                output = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise RuntimeError(f"API encerrou ao iniciar:\n{output}")
            try:
                status, body, _ = cls.request("GET", "/api/health")
                if status == 200 and body.get("ok"):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            cls.proc.terminate()
            output = cls.proc.stdout.read() if cls.proc.stdout else ""
            raise RuntimeError(f"API não ficou pronta:\n{output}")
        status, body, _ = cls.request("POST", "/api/auth/session", {"password": TOKEN})
        if status != 200:
            raise RuntimeError(f"Login de teste falhou: {status} {body}")
        cls.auth = body["access_token"]
        if cls.auth == TOKEN:
            raise RuntimeError("A API retornou a chave administrativa em vez de uma sessão curta.")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    @classmethod
    def request(cls, method: str, path: str, body=None, token: str | None = None, origin: str | None = None, extra_headers: dict[str, str] | None = None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        if extra_headers:
            headers.update(extra_headers)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(cls.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                try:
                    parsed = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    parsed = raw.decode(errors="replace")
                return response.status, parsed, dict(response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = raw.decode(errors="replace")
            return error.code, parsed, dict(error.headers)

    def test_health_does_not_disclose_local_path(self):
        status, body, headers = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["authConfigured"])
        self.assertNotIn("db", body)
        self.assertNotIn("path", json.dumps(body).lower())
        self.assertEqual({k.lower(): v for k, v in headers.items()}.get("x-content-type-options"), "nosniff")

    def test_authentication_and_session(self):
        self.assertEqual(self.request("GET", "/api/auth/check")[0], 401)
        self.assertEqual(self.request("GET", "/api/data/jsa-clientes")[0], 401)
        status, body, _ = self.request("POST", "/api/auth/session", {"password": "wrong"})
        self.assertEqual(status, 401)
        self.assertNotIn("access_token", body)
        status, body, _ = self.request("GET", "/api/auth/check", token=self.auth)
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])
        tampered = self.auth[:-1] + ("0" if self.auth[-1] != "0" else "1")
        self.assertEqual(self.request("GET", "/api/auth/check", token=tampered)[0], 401)

    def test_missing_auth_secret_fails_closed(self):
        with patch.object(backend_app, "APP_API_TOKEN", ""):
            with self.assertRaises(HTTPException) as error:
                backend_app.require_admin(None)
        self.assertEqual(error.exception.status_code, 503)

    def test_login_rate_limit(self):
        # test_authentication_and_session already records one failed login from this client.
        for _ in range(4):
            self.assertEqual(self.request("POST", "/api/auth/session", {"password": "wrong-again"})[0], 401)
        status, body, _ = self.request("POST", "/api/auth/session", {"password": "wrong-again"})
        self.assertEqual(status, 429)

    def test_create_read_update_delete_and_json_validation(self):
        self.request("DELETE", "/api/data/jsa-clientes", token=self.auth)
        status, body, _ = self.request("GET", "/api/data/jsa-clientes", token=self.auth)
        self.assertEqual(status, 200)
        self.assertIsNone(body["value"])
        sample = [{"id": "c-1", "nome": "Cliente Teste"}]
        status, _, _ = self.request("PUT", "/api/data/jsa-clientes", {"value": sample}, token=self.auth)
        self.assertEqual(status, 428)
        status, body, _ = self.request("PUT", "/api/data/jsa-clientes", {"value": sample}, token=self.auth, extra_headers={"If-None-Match":"*"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body, _ = self.request("GET", "/api/data/jsa-clientes", token=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], sample)
        self.assertTrue(body["updated_at"])
        first_version = body["updated_at"]
        newer = [{"id": "c-1", "nome": "Cliente atualizado"}]
        status, _, _ = self.request("PUT", "/api/data/jsa-clientes", {"value": newer}, token=self.auth, extra_headers={"If-Match":first_version})
        self.assertEqual(status, 200)
        status, _, _ = self.request("PUT", "/api/data/jsa-clientes", {"value": sample}, token=self.auth, extra_headers={"If-Match":first_version})
        self.assertEqual(status, 409)
        self.assertEqual(self.request("GET", "/api/data/jsa-clientes", token=self.auth)[1]["value"], newer)
        status, delete_body, _ = self.request("DELETE", "/api/data/jsa-clientes", token=self.auth)
        self.assertEqual(status, 200)
        self.assertTrue(delete_body["backup_sha256"])
        status, body, _ = self.request("GET", "/api/data/jsa-clientes", token=self.auth)
        self.assertEqual(status, 200)
        self.assertIsNone(body["value"])
        status, _, _ = self.request("GET", "/api/data/invalid%2Fkey", token=self.auth)
        self.assertIn(status, (400, 404))

    def test_bulk_restore_is_atomic_and_creates_verified_backup(self):
        values = {"jsa-clientes": [{"id": "1"}], "jsa-config": {"freteMedio": 25}}
        status, body, _ = self.request("PUT", "/api/data", {"values": values}, token=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(body["keys_written"], 2)
        self.assertTrue(body["backup_sha256"])
        self.assertNotIn(str(self.temp_path), json.dumps(body))
        backup = self.temp_path / "backups" / body["backup_id"]
        self.assertTrue(backup.is_file())
        import sqlite3
        conn = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM kv_store").fetchone()[0], 0)
        finally:
            conn.close()

    def test_bulk_write_checks_versions_when_requested(self):
        sample = {"jsa-vendas": [], "jsa-orcamentos": []}
        for key in sample:
            self.request("DELETE", f"/api/data/{key}", token=self.auth)
            status, _, _ = self.request("PUT", f"/api/data/{key}", {"value": []}, token=self.auth, extra_headers={"If-None-Match": "*"})
            self.assertEqual(status, 200)
        versions = {key: self.request("GET", f"/api/data/{key}", token=self.auth)[1]["updated_at"] for key in sample}
        updated = {"jsa-vendas": [{"id": "v-1"}], "jsa-orcamentos": [{"id": "o-1"}]}
        status, body, _ = self.request("PUT", "/api/data", {"values": updated, "expected_versions": versions}, token=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual(body["keys_written"], 2)
        stale = {"jsa-vendas": [{"id": "stale"}], "jsa-orcamentos": [{"id": "o-1"}]}
        status, _, _ = self.request("PUT", "/api/data", {"values": stale, "expected_versions": versions}, token=self.auth)
        self.assertEqual(status, 409)
        self.assertEqual(self.request("GET", "/api/data/jsa-vendas", token=self.auth)[1]["value"], updated["jsa-vendas"])

    def test_snapshot_hides_path_and_returns_checksum(self):
        status, body, _ = self.request("POST", "/api/backup/snapshot", {}, token=self.auth)
        self.assertEqual(status, 200)
        self.assertTrue(body["backup_id"].startswith("jsa-"))
        self.assertEqual(len(body["sha256"]), 64)
        self.assertNotIn(str(self.temp_path), json.dumps(body))

    def test_bulk_delete_is_disabled_and_admin_reset_requires_phrase_and_backup(self):
        status, body, _ = self.request("DELETE", "/api/data", token=self.auth)
        self.assertEqual(status, 405)
        self.assertEqual(self.request("POST", "/api/admin/reset", {"confirmation": "wrong"}, token=self.auth)[0], 400)
        status, body, _ = self.request("POST", "/api/admin/reset", {"confirmation": "APAGAR TODOS OS DADOS"}, token=self.auth)
        self.assertEqual(status, 200)
        self.assertTrue(body["backup_sha256"])
        status, body, _ = self.request("GET", "/api/data/jsa-clientes", token=self.auth)
        self.assertEqual(status, 200)
        self.assertIsNone(body["value"])

    def test_cors_rejects_unlisted_origin(self):
        status, _, headers = self.request("GET", "/api/health", origin="https://evil.example")
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "https://evil.example")

    def test_frontend_is_served_with_login_and_no_localstorage_save_fallback(self):
        status, body, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Acesso ao JSA CRM", body)
        self.assertIn("Não foi salvo no banco", body)
        self.assertNotIn("localStorage.setItem", body)
        self.assertIn("APAGAR TODOS OS DADOS", body)
        self.assertNotIn("state.sfConfig.token.trim()", body)
        self.assertIn("/api/superfrete/quote", body)
        self.assertIn("logo-chaves-jsa.jpg", body)
        self.assertIn("data-ship-sale", body)
        self.assertIn("quoteSaleId", body)
        self.assertIn("Pagamentos InfinitePay", body)
        self.assertIn("/api/infinitepay/links", body)
        self.assertNotIn("INFINITEPAY_API_KEY=", body)


class SuperFreteUnitTests(unittest.TestCase):
    def test_provider_proxy_uses_fixed_production_host_and_server_bearer(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"balance":7.0}'
        with patch.object(backend_app, "SUPERFRETE_ENV", "production"), patch.object(
            backend_app, "SUPERFRETE_TOKEN", "unit-test-secret-not-for-production"
        ), patch("app.urllib.request.urlopen", return_value=Response()) as urlopen:
            result = backend_app._superfrete_request("/user", method="GET")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.superfrete.com/api/v0/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer unit-test-secret-not-for-production")
        self.assertEqual(result["balance"], 7.0)
        self.assertNotIn("unit-test-secret-not-for-production", json.dumps(result))

    def test_provider_422_reports_field_names_without_echoing_private_values(self):
        private_document = "12345678901"
        error_body = json.dumps({
            "message": f"Invalid document {private_document}",
            "errors": {"package.weight": ["invalid for CEP 08280340"], "recipient.document": [f"invalid {private_document}"]},
        }).encode("utf-8")
        provider_error = urllib.error.HTTPError(
            "https://api.superfrete.com/api/v0/cart", 422, "Unprocessable Content", {}, io.BytesIO(error_body)
        )
        with patch.object(backend_app, "SUPERFRETE_ENV", "production"), patch.object(
            backend_app, "SUPERFRETE_TOKEN", "unit-test-secret-not-for-production"
        ), patch("app.urllib.request.urlopen", side_effect=provider_error):
            with self.assertRaises(HTTPException) as raised:
                backend_app._superfrete_request("/cart", {"package": {"weight": 1}})
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("package.weight", raised.exception.detail)
        self.assertIn("recipient.document", raised.exception.detail)
        self.assertNotIn(private_document, raised.exception.detail)
        self.assertNotIn("08280340", raised.exception.detail)

    def test_quote_uses_server_origin_and_correct_package_units(self):
        from_postal_code = "08280340"
        with patch.object(backend_app, "SUPERFRETE_FROM_POSTAL_CODE", from_postal_code), patch.object(
            backend_app, "_superfrete_request", return_value=[{"id": 1, "price": 12.5}]
        ) as remote:
            payload = backend_app.SuperFreteQuotePayload(
                to_postal_code="20020050", width=15, height=10, length=20, weight=0.75,
                insurance_value=100, services="1,2,17,3,31",
            )
            result = backend_app.superfrete_quote(payload)
        self.assertTrue(result["ok"])
        self.assertEqual(result["quotes"][0]["price"], 12.5)
        args, kwargs = remote.call_args
        self.assertEqual(args[0], "/calculator")
        self.assertEqual(args[1]["from"]["postal_code"], from_postal_code)
        self.assertEqual(args[1]["to"]["postal_code"], "20020050")
        self.assertEqual(args[1]["package"], {"width": 15, "height": 10, "length": 20, "weight": 0.75})
        self.assertEqual(args[1]["options"]["insurance_value"], 100)
        self.assertEqual(args[1]["services"], "1,2,17,3,31")
        default_payload = backend_app.SuperFreteQuotePayload(to_postal_code="20020050", width=15, height=10, length=20, weight=0.75)
        self.assertEqual(default_payload.services, "1,2,17,3,31")

    def test_quote_fails_when_server_origin_is_missing(self):
        with patch.object(backend_app, "SUPERFRETE_FROM_POSTAL_CODE", ""), patch.object(backend_app, "_superfrete_request") as remote:
            payload = backend_app.SuperFreteQuotePayload(to_postal_code="20020050", width=15, height=10, length=20, weight=0.75)
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_quote(payload)
        self.assertEqual(error.exception.status_code, 503)
        remote.assert_not_called()

    def test_account_only_returns_balance_and_limits_not_personal_data(self):
        provider = {"firstname": "Pessoa", "document": "12345678900", "email": "private@example.com", "balance": 7.0, "shipments": 1, "shipments_available": 4, "limits": {"private": "sensitive"}}
        with patch.object(backend_app, "_superfrete_request", return_value=provider) as remote:
            result = backend_app.superfrete_account()
        self.assertEqual(result["account"]["balance"], 7.0)
        self.assertNotIn("document", json.dumps(result))
        self.assertNotIn("private@example.com", json.dumps(result))
        self.assertNotIn("sensitive", json.dumps(result))
        remote.assert_called_once_with("/user", method="GET")

    def test_create_shipment_stages_pending_order_without_checkout(self):
        sender = backend_app.SuperFreteAddress(
            name="Loja JSA", address="Avenida Antonio de Sousa Queiroz", district="Centro", city="Sao Paulo",
            state_abbr="SP", postal_code="08280-340", number="656",
        )
        recipient = backend_app.SuperFreteAddress(
            name="Cliente Teste", address="Rua Exemplo", district="Centro", city="Rio de Janeiro",
            state_abbr="RJ", postal_code="20020-050", document="12345678901", phone="21999998888",
        )
        payload = backend_app.SuperFreteShipmentPayload(
            sender=sender, recipient=recipient, service=1,
            volume=backend_app.SuperFreteVolume(width=15, height=10, length=20, weight=0.75),
            contents=[backend_app.SuperFreteContentsItem(name="Produto teste", quantity=1, unitary_value=20)],
            insurance_value=20, sale_id="sale-test-001",
        )
        with patch.object(backend_app, "_find_crm_sale", return_value={"id": "sale-test-001", "numero": "VEN-000123", "clienteId": "client-test-001"}), patch.object(
            backend_app, "SUPERFRETE_FROM_POSTAL_CODE", "08280340"
        ), patch.object(
            backend_app, "_superfrete_request", side_effect=[{"id": "ORD123456789", "price": 6.25}, {"id": "ORD123456789", "price": 6.25, "status": "pending"}]
        ) as remote, patch.object(backend_app, "_save_pending_superfrete_order") as save:
            result = backend_app.superfrete_create_pending_shipment(payload)
        self.assertEqual(result["shipment"]["order_id"], "ORD123456789")
        self.assertEqual(result["shipment"]["sale_id"], "sale-test-001")
        self.assertEqual(result["shipment"]["sale_number"], "VEN-000123")
        self.assertEqual(result["shipment"]["customer_id"], "client-test-001")
        self.assertTrue(result["paymentRequired"])
        self.assertEqual([call.args[0] for call in remote.call_args_list], ["/cart", "/order/info/ORD123456789"])
        self.assertFalse(any(call.args[0] == "/checkout" for call in remote.call_args_list))
        cart_payload = remote.call_args_list[0].args[1]
        self.assertEqual(cart_payload["volumes"], {"width": 15, "height": 10, "length": 20, "weight": 0.75})
        self.assertNotIn("package", cart_payload)
        self.assertNotIn("volume", cart_payload)
        self.assertEqual(cart_payload["to"]["document"], "12345678901")
        self.assertNotIn("sale_id", cart_payload)
        self.assertEqual(save.call_args.args[1]["sale_id"], "sale-test-001")
        self.assertNotIn("document", json.dumps(save.call_args.args[1]))
        self.assertEqual(save.call_args.args[0], "ORD123456789")

    def test_create_shipment_refuses_unknown_sale_before_provider_call(self):
        sender = backend_app.SuperFreteAddress(
            name="Loja JSA", address="Avenida Antonio de Sousa Queiroz", district="Centro", city="Sao Paulo",
            state_abbr="SP", postal_code="08280-340", number="656",
        )
        recipient = backend_app.SuperFreteAddress(
            name="Cliente Teste", address="Rua Exemplo", district="Centro", city="Rio de Janeiro",
            state_abbr="RJ", postal_code="20020-050", document="12345678901",
        )
        payload = backend_app.SuperFreteShipmentPayload(
            sender=sender, recipient=recipient, service=1,
            volume=backend_app.SuperFreteVolume(width=15, height=10, length=20, weight=0.75),
            contents=[backend_app.SuperFreteContentsItem(name="Produto teste", quantity=1, unitary_value=20)],
            sale_id="sale-missing-001",
        )
        with patch.object(backend_app, "_find_crm_sale", return_value=None), patch.object(backend_app, "_superfrete_request") as remote:
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_create_pending_shipment(payload)
        self.assertEqual(error.exception.status_code, 404)
        self.assertIn("venda selecionada", error.exception.detail)
        remote.assert_not_called()

    def test_checkout_refuses_bad_confirmation_or_untracked_order(self):
        payload = backend_app.SuperFreteCheckoutPayload(order_id="ORD123456789", confirmation="not the exact phrase", expected_price=6.25)
        with patch.object(backend_app, "_superfrete_request") as remote:
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_checkout(payload)
        self.assertEqual(error.exception.status_code, 400)
        remote.assert_not_called()
        payload = backend_app.SuperFreteCheckoutPayload(order_id="ORD123456789", confirmation="PAGAR ORD123456789", expected_price=6.25)
        with patch.object(backend_app, "_pending_superfrete_orders", return_value={} ), patch.object(backend_app, "_superfrete_request") as remote:
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_checkout(payload)
        self.assertEqual(error.exception.status_code, 404)
        remote.assert_not_called()
        payload = backend_app.SuperFreteCheckoutPayload(order_id="ORD123456789", confirmation="PAGAR ORD123456789", expected_price=6.25)
        with patch.object(backend_app, "_pending_superfrete_orders", return_value={"ORD123456789": {"status": "pending", "price": None}}), patch.object(backend_app, "_superfrete_request") as remote:
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_checkout(payload)
        self.assertEqual(error.exception.status_code, 409)
        remote.assert_not_called()
        payload = backend_app.SuperFreteCheckoutPayload(order_id="ORD123456789", confirmation="PAGAR ORD123456789", expected_price=6.24)
        with patch.object(backend_app, "_pending_superfrete_orders", return_value={"ORD123456789": {"status": "pending", "price": 6.25}}), patch.object(backend_app, "_superfrete_request") as remote:
            with self.assertRaises(HTTPException) as error:
                backend_app.superfrete_checkout(payload)
        self.assertEqual(error.exception.status_code, 409)
        remote.assert_not_called()


class InfinitePayUnitTests(unittest.TestCase):
    def _seed(self, key, value):
        with backend_app.get_conn() as conn:
            conn.execute(
                "INSERT INTO kv_store(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, json.dumps(value), backend_app._now_iso()),
            )
            conn.commit()

    def test_config_never_discloses_credentials(self):
        api_key = "inchk-test-secret-do-not-return"
        handle = "test-handle-private"
        with patch.object(backend_app, "INFINITEPAY_API_KEY", api_key), patch.object(backend_app, "INFINITEPAY_HANDLE", ""):
            result = backend_app.infinitepay_config()
        serialized = json.dumps(result)
        self.assertTrue(result["configured"])
        self.assertTrue(result["linkConfigured"])
        self.assertFalse(result["handleConfigured"])
        self.assertFalse(result["paymentCheckConfigured"])
        self.assertEqual(result["paymentMethods"], ["pix", "credit_card"])
        self.assertNotIn(api_key, serialized)
        self.assertNotIn(handle, serialized)

    def test_redirect_url_requires_public_https_callback_path(self):
        with patch.object(backend_app, "INFINITEPAY_REDIRECT_URL", "http://crm.example/api/infinitepay/return"):
            self.assertFalse(backend_app._valid_infinitepay_redirect_url())
        with patch.object(backend_app, "INFINITEPAY_REDIRECT_URL", "https://localhost/api/infinitepay/return"):
            self.assertFalse(backend_app._valid_infinitepay_redirect_url())
        with patch.object(backend_app, "INFINITEPAY_REDIRECT_URL", "https://crm.example/api/infinitepay/return"):
            self.assertTrue(backend_app._valid_infinitepay_redirect_url())

    def test_sale_link_uses_server_snapshot_cents_method_and_reuses_active_link(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"url":"https://checkout.infinitepay.io/pay/demo"}'

        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-test-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(backend_app, "INFINITEPAY_API_KEY", "inchk-test-only-secret"), patch.object(
            backend_app, "INFINITEPAY_HANDLE", ""
        ), patch("app.urllib.request.urlopen", return_value=Response()) as remote:
            self._seed("jsa-vendas", [{
                "id": "sale-123456", "numero": "VEN-000001", "clienteId": "client-123",
                "itens": [{"produtoNome": "Produto de teste", "qtd": 2, "precoVenda": 10.50}],
                "freteAssumido": "cliente", "frete": 5.0,
            }])
            self._seed("jsa-clientes", [{"id": "client-123", "nome": "Cliente Teste", "email": "cliente@example.test", "telefone": "11999998888"}])
            payload = backend_app.InfinitePayLinkPayload(
                source="sale", sale_id="sale-123456", payment_method="pix", request_id="sale-sale-123456-a1"
            )
            result = backend_app.infinitepay_create_link(payload)
            request = remote.call_args.args[0]
            provider_payload = json.loads(request.data.decode("utf-8"))
            second = backend_app.infinitepay_create_link(backend_app.InfinitePayLinkPayload(
                source="sale", sale_id="sale-123456", payment_method="credit_card", request_id="sale-sale-123456-a2"
            ))

        self.assertEqual(result["payment"]["status"], "pending")
        self.assertEqual(result["payment"]["amount_cents"], 2600)
        self.assertEqual(result["payment"]["checkout_url"], "https://checkout.infinitepay.io/pay/demo")
        self.assertNotIn("handle", provider_payload)
        self.assertEqual(provider_payload["order_nsu"], result["payment"]["order_nsu"])
        self.assertEqual(provider_payload["items"], [
            {"quantity": 2, "price": 1050, "description": "Produto de teste"},
            {"quantity": 1, "price": 500, "description": "Frete de entrega"},
        ])
        self.assertEqual(provider_payload["options"]["payment_methods_enabled"], ["credit_card", "pix"])
        self.assertTrue(provider_payload["options"]["single_payment"])
        self.assertEqual(provider_payload["customer"]["phone_number"], "+5511999998888")
        self.assertEqual(request.full_url, "https://api.checkout.infinitepay.io/links")
        self.assertEqual(request.headers.get("X-api-key"), "inchk-test-only-secret")
        self.assertNotIn("inchk-test-only-secret", json.dumps(result))
        self.assertTrue(second["reused"])
        self.assertEqual(second["payment"]["id"], result["payment"]["id"])
        self.assertEqual(remote.call_count, 1)

    def test_payment_check_does_not_mark_wrong_amount_as_paid(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return b'{"success":true,"paid":true,"amount":1999,"capture_method":"pix"}'

        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-check-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(backend_app, "INFINITEPAY_API_KEY", "inchk-test-only-secret"), patch.object(
            backend_app, "INFINITEPAY_HANDLE", "jsa-test"
        ), patch("app.urllib.request.urlopen", return_value=Response()) as remote:
            self._seed("jsa-pagamentos", [{
                "id": "payment-123456", "order_nsu": "PAG-12345678901234567890", "source": "manual",
                "payment_method": "pix", "amount_cents": 2000, "status": "pending",
            }])
            payload = backend_app.InfinitePayCheckPayload(
                payment_id="payment-123456", slug="invoice-demo", transaction_nsu="transaction-demo-123"
            )
            result = backend_app.infinitepay_payment_check(payload)
            request_payload = json.loads(remote.call_args.args[0].data.decode("utf-8"))

        self.assertEqual(result["payment"]["status"], "review")
        self.assertIn("valor não confere", result["message"])
        self.assertEqual(request_payload["handle"], "jsa-test")
        self.assertEqual(request_payload["order_nsu"], "PAG-12345678901234567890")
        self.assertEqual(request_payload["transaction_nsu"], "transaction-demo-123")

    def test_provider_validation_error_is_sanitized_and_recorded_as_failed(self):
        provider_body = b'{"message":"private response body that must not be echoed"}'
        error = urllib.error.HTTPError("https://api.checkout.infinitepay.io/links", 422, "Unprocessable", {}, io.BytesIO(provider_body))
        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-error-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(backend_app, "INFINITEPAY_API_KEY", "inchk-test-only-secret"), patch.object(
            backend_app, "INFINITEPAY_HANDLE", "jsa-test"
        ), patch("app.urllib.request.urlopen", side_effect=error):
            payload = backend_app.InfinitePayLinkPayload(
                source="manual", payment_method="credit_card", request_id="manual-attempt-123456",
                amount_cents=1000, description="Cobrança de teste",
            )
            with self.assertRaises(HTTPException) as raised:
                backend_app.infinitepay_create_link(payload)
            with backend_app.get_conn() as conn:
                records = backend_app._payment_records_from_conn(conn)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("private response body", raised.exception.detail)
        self.assertEqual(records[0]["status"], "failed")

    def test_dns_failure_is_explained_and_kept_uncertain(self):
        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-dns-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(backend_app, "INFINITEPAY_API_KEY", "inchk-test-only-secret"), patch.object(
            backend_app, "INFINITEPAY_HANDLE", ""
        ), patch("app.urllib.request.urlopen", side_effect=urllib.error.URLError(socket.gaierror(-2, "private DNS diagnostic"))):
            payload = backend_app.InfinitePayLinkPayload(
                source="manual", payment_method="pix", request_id="manual-dns-attempt-12345",
                amount_cents=1000, description="Teste DNS",
            )
            with self.assertRaises(HTTPException) as raised:
                backend_app.infinitepay_create_link(payload)
            with backend_app.get_conn() as conn:
                records = backend_app._payment_records_from_conn(conn)
        self.assertIn("DNS", raised.exception.detail)
        self.assertNotIn("private DNS diagnostic", raised.exception.detail)
        self.assertEqual(records[0]["status"], "uncertain")

    def test_uncertain_attempt_requires_exact_operator_confirmation_to_unlock(self):
        payment_id = "a" * 32
        order_nsu = "PAG-12345678901234567890"
        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-resolve-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ):
            self._seed("jsa-pagamentos", [{
                "id": payment_id, "order_nsu": order_nsu, "source": "manual", "payment_method": "pix",
                "amount_cents": 1000, "status": "uncertain", "created_at": backend_app._now_iso(),
            }])
            with self.assertRaises(HTTPException) as wrong:
                backend_app.infinitepay_resolve_uncertain(payment_id, backend_app.InfinitePayResolvePayload(confirmation="nao existe, pode tentar"))
            self.assertEqual(wrong.exception.status_code, 400)
            result = backend_app.infinitepay_resolve_uncertain(
                payment_id, backend_app.InfinitePayResolvePayload(confirmation=f"CONFIRMO NAO CRIADO {order_nsu}")
            )
        self.assertEqual(result["payment"]["status"], "failed")
        self.assertNotIn("resolution", result["payment"])

    def test_fresh_creating_attempt_cannot_be_unlocked(self):
        payment_id = "b" * 32
        order_nsu = "PAG-12345678901234567891"
        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-resolve-fresh-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ):
            self._seed("jsa-pagamentos", [{
                "id": payment_id, "order_nsu": order_nsu, "source": "manual", "payment_method": "pix",
                "amount_cents": 1000, "status": "creating", "created_at": backend_app._now_iso(),
            }])
            with self.assertRaises(HTTPException) as error:
                backend_app.infinitepay_resolve_uncertain(
                    payment_id, backend_app.InfinitePayResolvePayload(confirmation=f"CONFIRMO NAO CRIADO {order_nsu}")
                )
        self.assertEqual(error.exception.status_code, 409)

    def test_public_return_only_displays_server_verified_payment_status(self):
        payment_id = "c" * 32
        order_nsu = "PAG-12345678901234567892"
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "https", "path": "/api/infinitepay/return", "raw_path": b"/api/infinitepay/return",
            "query_string": b"", "headers": [(b"host", b"crm.example")], "client": ("203.0.113.10", 12345),
            "server": ("crm.example", 443),
        }
        with tempfile.TemporaryDirectory(prefix="jsa-infinitepay-return-") as directory, patch.object(
            backend_app, "DB_PATH", Path(directory) / "test.db"
        ), patch.object(backend_app, "INFINITEPAY_API_KEY", "inchk-test-secret"), patch.object(
            backend_app, "INFINITEPAY_HANDLE", "jsa-test"
        ), patch.object(backend_app, "INFINITEPAY_REDIRECT_URL", "https://crm.example/api/infinitepay/return"), patch.object(
            backend_app, "_allow_infinitepay_return", return_value=True
        ), patch.object(backend_app, "infinitepay_payment_check", return_value={"ok": True, "payment": {"status": "paid"}}) as check:
            self._seed("jsa-pagamentos", [{
                "id": payment_id, "order_nsu": order_nsu, "status": "pending", "amount_cents": 1000,
            }])
            response = backend_app.infinitepay_public_return(
                Request(scope), order_nsu=order_nsu, slug="invoice-demo", transaction_nsu="transaction-demo-123"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pagamento confirmado pela InfinitePay", response.body.decode())
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("inchk-test-secret", response.body.decode())
        check.assert_called_once()


if __name__ == "__main__":
        unittest.main(verbosity=2)
