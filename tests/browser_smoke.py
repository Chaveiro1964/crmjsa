"""Smoke test end-to-end opcional; requer `pip install playwright` e `playwright install chromium`.
Usa cópia temporária do banco para nunca alterar os dados comerciais incluídos.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOKEN = "browser-test-token-never-use-in-production-0123456789"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def json_request(url, token, path):
    req = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    with tempfile.TemporaryDirectory(prefix="jsa-browser-smoke-") as tmp:
        temp = Path(tmp)
        db_copy = temp / "jsa-browser.db"
        shutil.copy2(BACKEND / "jsa.db", db_copy)
        port = free_port()
        env = os.environ.copy()
        env.update({
            "APP_ENV": "test", "APP_HOST": "127.0.0.1", "APP_PORT": str(port),
            "APP_API_TOKEN": TOKEN, "APP_CORS_ORIGINS": f"http://127.0.0.1:{port}",
            "SUPERFRETE_TOKEN": "SMOKE_ONLY_DUMMY_NOT_A_REAL_CREDENTIAL_0123456789abcdef",
            "SUPERFRETE_ENV": "production",
            "INFINITEPAY_API_KEY": "", "INFINITEPAY_HANDLE": "",
            "SQLITE_PATH": str(db_copy), "BACKUPS_DIR": str(temp / "backups"),
            "PYTHONUNBUFFERED": "1",
        })
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)], cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        try:
            until = time.time() + 15
            while time.time() < until:
                if proc.poll() is not None:
                    raise RuntimeError("Servidor encerrou: " + (proc.stdout.read() if proc.stdout else ""))
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
                        if response.status == 200: break
                except Exception:
                    time.sleep(.1)
            else: raise RuntimeError("Servidor não ficou pronto")

            page_errors = []
            console_errors = []
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 950})
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.add_init_script("localStorage.setItem('jsa-clientes', JSON.stringify([{id:'legacy-only', nome:'Registro antigo local'}])); localStorage.setItem('jsa-sf-config', JSON.stringify({token:'SUPERFRETE-SECRET-TEST', ambiente:'sandbox'}));")
                page.goto(base + "/", wait_until="domcontentloaded")
                page.get_by_role("heading", name="Acesso ao JSA CRM").wait_for()
                expect(page.get_by_text("Encontramos 2 conjunto(s) antigo(s)")).to_be_visible()
                with page.expect_download() as download_info:
                    page.get_by_role("button", name="Baixar dados antigos deste navegador").click()
                legacy_download = download_info.value
                archive_path = Path(tmp) / "localStorage-export.json"
                legacy_download.save_as(archive_path)
                legacy_text = archive_path.read_text()
                legacy_copy = json.loads(legacy_text)
                assert legacy_copy["clientes"][0]["nome"] == "Registro antigo local"
                assert legacy_copy["sfConfig"]["token"] == ""
                assert legacy_copy["secretsOmitted"] == ["jsa-sf-config.token"]
                assert "SUPERFRETE-SECRET-TEST" not in legacy_text
                page.locator("#api-password").fill(TOKEN)
                page.get_by_role("button", name="Entrar no painel").click()
                page.locator("#auth-overlay").wait_for(state="hidden", timeout=20000)
                session_token = page.evaluate("sessionStorage.getItem('jsaApiToken')")
                assert session_token and session_token != TOKEN
                page.get_by_role("button", name="Painel").wait_for()
                page.get_by_text("Faturamento Últimos 7 Dias").wait_for(timeout=10000)
                brand_logo = page.locator(".sidebar-brand-logo")
                expect(brand_logo).to_be_visible()
                assert brand_logo.evaluate("img => img.complete && img.naturalWidth > 0")

                # Open the customer module, create a disposable customer, and verify persistence.
                page.get_by_role("button", name="Chaveiros").click()
                page.get_by_role("heading", name="Chaveiros").wait_for()
                page.get_by_role("button", name="+ Novo chaveiro").click()
                page.locator("#form-cliente input[name='nome']").fill("Chaveiro de teste automatizado")
                page.locator("#form-cliente input[name='telefone']").fill("(11) 90000-0101")
                page.locator("#form-cliente input[name='email']").fill("teste@example.invalid")
                page.locator("#form-cliente input[name='endereco']").fill("Rua do Teste")
                page.locator("#form-cliente input[name='numero']").fill("123")
                page.locator("#form-cliente input[name='complemento']").fill("Sala 2")
                page.locator("#form-cliente input[name='bairro']").fill("Centro")
                page.locator("#form-cliente input[name='cidade']").fill("São Paulo")
                page.locator("#form-cliente input[name='estado']").fill("SP")
                page.locator("#form-cliente input[name='cep']").fill("01001-000")
                page.locator("#form-cliente input[name='documento']").fill("12345678901")
                page.get_by_role("button", name="Salvar chaveiro").click()
                page.get_by_role("link", name="Chaveiro de teste automatizado").wait_for(timeout=10000)
                stored = json_request(base, session_token, "/api/data/jsa-clientes")["value"]
                client = next(row for row in stored if row.get("nome") == "Chaveiro de teste automatizado")
                assert client["cep"] == "01001-000" and client["numero"] == "123" and client["email"] == "teste@example.invalid"
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Chaveiros").wait_for(timeout=15000)
                page.get_by_role("button", name="Chaveiros").click()
                page.get_by_role("link", name="Chaveiro de teste automatizado").wait_for(timeout=10000)

                # Selecting a registered customer autofills sales and quotes; generated documents get public numbers.
                page.get_by_role("button", name="Vendas").click()
                page.get_by_role("button", name="+ Nova venda").click()
                sale_form = page.locator("#form-venda")
                sale_form.locator("select[name='clienteId']").select_option(client["id"])
                assert sale_form.locator("input[name='recipientPostalCode']").input_value() == "01001-000"
                assert sale_form.locator("input[name='recipientAddress']").input_value() == "Rua do Teste"
                assert sale_form.locator("input[name='recipientPhone']").input_value() == "(11) 90000-0101"
                sale_product = sale_form.locator(".item-produto").first
                product_id = sale_product.evaluate("el => [...el.options].find(option => option.value)?.value")
                if not product_id:
                    sale_form.get_by_role("button", name="+ Adicionar outro produto").click()
                    sale_product = sale_form.locator(".item-produto").last
                    product_id = sale_product.evaluate("el => [...el.options].find(option => option.value)?.value")
                sale_product.select_option(product_id)
                sale_form.locator(".item-qtd").first.fill("1")
                sale_form.get_by_role("button", name="Registrar").click()
                page.get_by_text("registrada!", exact=False).wait_for(timeout=10000)
                sales = json_request(base, session_token, "/api/data/jsa-vendas")["value"]
                direct_sale = next(row for row in sales if row.get("clienteId") == client["id"])
                assert direct_sale["numero"].startswith("VEN-") and direct_sale["recipient"]["postalCode"] == "01001-000"

                page.get_by_role("button", name="Orçamentos").click()
                page.get_by_role("button", name="+ Novo orçamento").click()
                quote_form = page.locator("#form-orcamento")
                quote_form.locator("select[name='clienteId']").select_option(client["id"])
                assert quote_form.locator("input[name='recipientPostalCode']").input_value() == "01001-000"
                assert quote_form.locator("input[name='recipientNumber']").input_value() == "123"
                quote_product = quote_form.locator(".item-produto").first
                quote_product.select_option(product_id)
                quote_form.locator(".item-qtd").first.fill("2")
                quote_form.get_by_role("button", name="Salvar orçamento").click()
                page.get_by_text("criado!", exact=False).wait_for(timeout=10000)
                quotes = json_request(base, session_token, "/api/data/jsa-orcamentos")["value"]
                new_quote = next(row for row in quotes if row.get("clienteId") == client["id"])
                assert new_quote["numero"].startswith("ORC-") and new_quote["recipient"]["postalCode"] == "01001-000"
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator(f"[data-convert-orc='{new_quote['id']}']").click()
                page.locator(f"[data-convert-orc='{new_quote['id']}']").wait_for(state="detached", timeout=10000)
                converted = json_request(base, session_token, "/api/data/jsa-orcamentos")["value"]
                converted_quote = next(row for row in converted if row["id"] == new_quote["id"])
                assert converted_quote["status"] == "fechado" and converted_quote["vendaNumero"].startswith("VEN-")
                sales = json_request(base, session_token, "/api/data/jsa-vendas")["value"]
                assert len({row["numero"] for row in sales}) == len(sales)
                assert page.locator("#app h1").inner_text().strip() == "Pagamentos InfinitePay"
                assert page.locator("#form-infinitepay-manual button[type='submit']").is_disabled()
                assert json_request(base, session_token, "/api/infinitepay/payments")["payments"] == []

                # The catalog keeps focus while typing, lets users add a code, and saves without changing pages.
                page.get_by_role("button", name="Catálogo").click()
                catalog_code = page.evaluate("ALL_DB[0].jsa")
                page.locator("#catalogo-search").click()
                for character in catalog_code:
                    page.keyboard.type(character)
                assert page.locator("#catalogo-search").input_value() == catalog_code
                add_code = page.locator("[data-catalog-add]").first
                assert add_code.get_attribute("data-catalog-add") == catalog_code
                catalog_dialogs = []
                page.once("dialog", lambda dialog: (catalog_dialogs.append((dialog.type, dialog.message)), dialog.accept("2")))
                add_code.click()
                try:
                    expect(page.locator("#catalog-quote-builder")).to_be_visible()
                except AssertionError:
                    print("CATALOG DEBUG", json.dumps({"dialogs": catalog_dialogs, "page_errors": page_errors, "console_errors": console_errors, "buttons": page.locator("[data-catalog-add]").count(), "state": page.locator("body").inner_text()[-1200:]}, ensure_ascii=False))
                    raise
                assert page.locator("[data-catalog-quote-qty='0']").input_value() == "2"
                page.locator("#catalog-quote-client").select_option(client["id"])
                assert page.locator("#catalog-quote-recipient input[name='recipientPostalCode']").input_value() == "01001-000"
                page.get_by_role("button", name="Salvar orçamento").click()
                page.get_by_text("criado!", exact=False).wait_for(timeout=10000)
                quick_quotes = json_request(base, session_token, "/api/data/jsa-orcamentos")["value"]
                assert any(row.get("clienteId") == client["id"] and row.get("numero", "").startswith("ORC-") for row in quick_quotes)

                # Visit every first-level module and ensure its page has a visible heading.
                modules = ["Painel", "Chaveiros", "Agenda", "Follow-up", "Atividades", "Vendas", "Orçamentos", "Conversor", "Catálogo", "Produtos", "Logística", "Códigos Pendentes", "Contas a Pagar", "Pagamentos", "Marketing", "Meta financeira", "Preços & Custos"]
                for label in modules:
                    page.get_by_role("button", name=label, exact=True).click()
                    page.locator("#app h1").wait_for(state="visible", timeout=5000)
                    assert page.locator("#app h1").inner_text().strip()

                # Reproduce the reported price-settings issue: save, verify API persistence, reload, and verify values.
                page.get_by_role("button", name="Preços & Custos", exact=True).click()
                price_form = page.locator("#form-config")
                for field, value in [("yaleCusto", "12.34"), ("yaleVenda", "23.45"), ("tetraCusto", "8.76"), ("tetraVenda", "19.87"), ("freteMedio", "17.65"), ("freteGratisAcima", "9"), ("metaVendasMensalValor", "4321.09"), ("metaVendasMensalQtd", "123")]:
                    price_form.locator(f"input[name='{field}']").fill(value)
                price_form.locator(".taxa-parcela-input[data-parcela='1']").fill("2.25")
                price_form.locator(".taxa-parcela-input[data-parcela='2']").fill("4.50")
                page.locator("#btn-add-tier").click()
                expect(page.locator("#form-config input[name='yaleCusto']")).to_have_value("12.34")
                tier = page.locator(".tier-row").last
                tier.locator(".tier-minqtd").fill("5")
                tier.locator(".tier-yale").fill("20")
                tier.locator(".tier-tetra").fill("30")
                price_form.get_by_role("button", name="Salvar configurações").click()
                page.get_by_text("Configurações salvas com sucesso!", exact=False).wait_for(timeout=10000)
                saved_prices = json_request(base, session_token, "/api/data/jsa-config")["value"]
                assert saved_prices["yaleCusto"] == 12.34 and saved_prices["yaleVenda"] == 23.45
                assert saved_prices["tetraCusto"] == 8.76 and saved_prices["tetraVenda"] == 19.87
                assert saved_prices["freteMedio"] == 17.65 and saved_prices["freteGratisAcima"] == 9
                assert saved_prices["taxaCartaoPorParcela"]["1"] == 2.25 and saved_prices["taxaCartaoPorParcela"]["2"] == 4.5
                assert saved_prices["tiers"][0]["minQtd"] == 5 and saved_prices["tiers"][0]["precoYale"] == 20
                saved_products = json_request(base, session_token, "/api/data/jsa-produtos")["value"]
                yale_product = next(row for row in saved_products if row["id"] == "yale")
                tetra_product = next(row for row in saved_products if row["id"] == "tetra")
                assert yale_product["custo"] == 12.34 and yale_product["venda"] == 23.45
                assert tetra_product["custo"] == 8.76 and tetra_product["venda"] == 19.87
                assert page.evaluate("calcPrecoVendaPadrao('yale', 5)") == 20
                assert page.evaluate("calcPrecoVendaPadrao('yale', 4)") == 23.45
                assert page.evaluate("calcTaxaCartao(100, 'cartao', 1)") == 2.25
                assert page.evaluate("calcFrete(9)") == 17.65 and page.evaluate("calcFreteAssumido(9)") == "empresa"
                totals_check = page.evaluate("calcItensTotals([{produtoId:'yale', qtd:5}])")
                assert totals_check["receita"] == 100 and round(totals_check["custoTotal"], 2) == 61.7
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Preços & Custos", exact=True).wait_for(timeout=15000)
                page.get_by_role("button", name="Preços & Custos", exact=True).click()
                expect(page.locator("#form-config input[name='yaleCusto']")).to_have_value("12.34")
                expect(page.locator("#form-config input[name='tetraVenda']")).to_have_value("19.87")
                expect(page.locator("#form-config input[name='freteMedio']")).to_have_value("17.65")

                # Save and reload sender settings; the API payload must never contain a token.
                page.get_by_role("button", name="Logística", exact=True).click()
                page.locator('[data-logistica-tab="config"]').click()
                sf_config_form = page.locator("#form-sf-config")
                for field, value in [("remetenteNome", "Loja Smoke JSA"), ("remetenteEndereco", "Rua de Teste"), ("remetenteNumero", "123"), ("remetenteComplemento", "Sala 4"), ("remetenteBairro", "Centro"), ("remetenteCidade", "São Paulo"), ("remetenteEstado", "SP"), ("remetenteTelefone", "11999990000"), ("margemPesoPercent", "5.5")]:
                    sf_config_form.locator(f"[name='{field}']").fill(value)
                sf_config_form.get_by_role("button", name="Salvar configurações da SuperFrete").click()
                page.get_by_text("Configurações da SuperFrete salvas!", exact=False).wait_for(timeout=10000)
                saved_sf = json_request(base, session_token, "/api/data/jsa-sf-config")["value"]
                assert saved_sf["remetenteNome"] == "Loja Smoke JSA" and saved_sf["remetenteBairro"] == "Centro"
                assert saved_sf["remetenteCidade"] == "São Paulo" and saved_sf["margemPesoPercent"] == 5.5
                assert not saved_sf.get("token")
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("button", name="Logística", exact=True).wait_for(timeout=15000)
                page.get_by_role("button", name="Logística", exact=True).click()
                page.locator('[data-logistica-tab="config"]').click()
                expect(page.locator("#form-sf-config input[name='remetenteNome']")).to_have_value("Loja Smoke JSA")
                expect(page.locator("#form-sf-config input[name='margemPesoPercent']")).to_have_value("5.5")

                page.get_by_role("button", name="Produtos").click()
                page.get_by_role("button", name="+ Novo produto").click()
                xss_name = '<b id="xss-smoke-marker">texto seguro</b>'
                page.locator("#form-produto input[name='nome']").fill(xss_name)
                page.locator("#form-produto input[name='codigo']").fill("XSS-TEST")
                page.locator("#form-produto input[name='custo']").fill("1")
                page.locator("#form-produto input[name='venda']").fill("2")
                page.locator("#form-produto").get_by_role("button", name="Adicionar", exact=True).click()
                page.get_by_text("adicionado com sucesso", exact=False).wait_for(timeout=10000)
                page.locator("#form-produto").wait_for(state="detached", timeout=10000)
                assert page.locator("#xss-smoke-marker").count() == 0
                product_table_text = page.locator("table").inner_text()
                assert "texto seguro" in product_table_text

                page.get_by_role("button", name="Vendas", exact=True).click()
                ship_sale_button = page.locator(f"[data-ship-sale='{direct_sale['id']}']")
                expect(ship_sale_button).to_be_visible(timeout=5000)
                ship_sale_button.click()
                page.get_by_role("heading", name="Logística").wait_for(timeout=5000)
                page.locator('[data-logistica-tab="cotacao"]').click()
                page.locator("#form-sf-quote").wait_for(timeout=5000)
                assert page.locator("#form-sf-quote select[name='quoteSaleId']").input_value() == direct_sale["id"]
                assert page.locator("#form-sf-quote select[name='quoteClientId']").input_value() == client["id"]
                assert page.locator("#form-sf-quote select[name='services']").input_value() == "1,2,17,3,31"
                assert "Jadlog" in page.locator("#form-sf-quote select[name='services']").inner_text()
                assert "Loggi" in page.locator("#form-sf-quote select[name='services']").inner_text()
                assert page.locator("#form-sf-quote input[name='length']").count() == 1
                assert page.locator("#form-sf-quote input[name='width']").count() == 1
                assert page.locator("#form-sf-quote input[name='height']").count() == 1
                assert page.locator("#form-sf-quote input[name='weight']").count() == 1
                assert page.locator("input[name='token']").count() == 0
                page.locator("#form-sf-quote input[name='to_postal_code']").fill("20020050")
                page.locator("#form-sf-quote input[name='length']").fill("20")
                page.locator("#form-sf-quote input[name='width']").fill("15")
                page.locator("#form-sf-quote input[name='height']").fill("10")
                page.locator("#form-sf-quote input[name='weight']").fill("1")
                page.locator("#form-sf-quote input[name='insurance_value']").fill("20")
                account_button = page.get_by_role("button", name="Testar conexão e consultar saldo")
                account_button.wait_for(timeout=5000)
                assert account_button.is_enabled()
                quote_requests = []
                def mock_quote(route):
                    quote_requests.append(json.loads(route.request.post_data or "{}"))
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "environment": "production", "quotes": [
                        {"company": "jadlog", "name": "JADLOG Econômico", "id": 3, "price": 14.30, "delivery_time": 2},
                        {"company": "loggi", "name": "LOGGI", "id": 31, "price": 13.86, "delivery_time": 2},
                    ]}))
                page.route("**/api/superfrete/quote", mock_quote)
                page.get_by_role("button", name="Consultar cotação").click()
                page.get_by_text("JADLOG Econômico").wait_for(timeout=5000)
                carrier_names = page.locator("#app strong").all_inner_texts()
                assert "Jadlog" in carrier_names and "Loggi" in carrier_names
                assert quote_requests and quote_requests[0]["services"] == "1,2,17,3,31"
                assert abs(quote_requests[0]["weight"] - 1.055) < 1e-8
                assert page.evaluate("state.sfQuoteRequest.saleId") == direct_sale["id"]

                # Verify the UI builds the same pending-shipment payload as the backend contract; no real POST is sent here.
                page.get_by_role("button", name="Preparar etiqueta").first.click()
                shipment_form = page.locator("#form-sf-shipment")
                shipment_form.wait_for(timeout=5000)
                for field, value in [("recipientName", "Destinatário Smoke"), ("recipientDocument", "52998224725"), ("recipientPhone", "11999990000"), ("recipientAddress", "Rua de Teste"), ("recipientNumber", "123"), ("recipientDistrict", "Centro"), ("recipientCity", "São Paulo"), ("recipientState", "SP"), ("recipientPostalCode", "20020050"), ("contentName", "Produto de teste"), ("contentQuantity", "1"), ("contentValue", "1.00")]:
                    shipment_form.locator(f"[name='{field}']").fill(value)
                shipment_requests = []
                def mock_shipment(route):
                    shipment_requests.append(json.loads(route.request.post_data or "{}"))
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "status": "pending", "paymentRequired": True, "shipment": {"order_id": "SMOKE-PENDING-001", "price": 10.0, "status": "pending"}}))
                page.route("**/api/superfrete/shipments", mock_shipment)
                shipment_form.get_by_role("button", name="Criar pedido pendente (sem cobrança)").click()
                page.get_by_text("Pedido pendente SMOKE-PENDING-001 criado", exact=False).wait_for(timeout=5000)
                assert len(shipment_requests) == 1
                assert shipment_requests[0]["service"] == 3
                assert abs(shipment_requests[0]["volume"]["weight"] - 1.055) < 1e-8
                assert shipment_requests[0]["recipient"]["document"] == "52998224725"
                assert shipment_requests[0]["sender"]["name"] == "Loja Smoke JSA"
                assert shipment_requests[0]["sale_id"] == direct_sale["id"]
                linked_sales = json_request(base, session_token, "/api/data/jsa-vendas")["value"]
                linked_sale = next(row for row in linked_sales if row["id"] == direct_sale["id"])
                assert linked_sale["freteLogistica"]["orderId"] == "SMOKE-PENDING-001"
                assert linked_sale["freteLogistica"]["quoteAmount"] == 14.30
                assert linked_sale["freteLogistica"]["actualAmount"] == 10.0
                assert linked_sale["frete"] == 10.0
                linked_labels = json_request(base, session_token, "/api/data/jsa-sf-etiquetas")["value"]
                linked_label = next(row for row in linked_labels if row["superfreteOrderId"] == "SMOKE-PENDING-001")
                assert linked_label["saleId"] == direct_sale["id"] and linked_label["saleNumber"] == direct_sale["numero"]
                assert page.evaluate("calcLucroVenda({receita:100,custoTotal:40,frete:15,freteAssumido:'cliente',freteLogistica:{actualAmount:20}})") == 55
                freight_guard = page.evaluate("""async saleId => {
                  const originalPayments = state.pagamentos;
                  const originalPaymentHistoryKnown = state.infinitePayPaymentHistoryKnown;
                  const source = state.vendas.find(sale => sale.id === saleId);
                  const guardId = 'SMOKE-FREIGHT-GUARD';
                  const guarded = {...source, id: guardId, freteAssumido:'cliente', frete:4.5, freteLogistica:null};
                  state.vendas = [...state.vendas, guarded];
                  await storSet(KEYS.vendas, state.vendas);
                  state.pagamentos = [{sale_id:guardId, status:'paid'}];
                  await sfPersistFreightForSale(guardId, {id:1, name:'PAC', price:18}, {order_id:'SMOKE-ORDER-GUARD', price:19, status:'pending'});
                  const saved = state.vendas.find(sale => sale.id === guardId);
                  const activePaymentResult = {chargedFreight:saved.frete, actualCarrierCost:saved.freteLogistica.actualAmount};
                  state.pagamentos = [];
                  state.infinitePayPaymentHistoryKnown = false;
                  await sfPersistFreightForSale(guardId, {id:1, name:'PAC', price:20}, {order_id:'SMOKE-ORDER-UNKNOWN', price:21, status:'pending'});
                  const unknownHistoryResult = {chargedFreight:state.vendas.find(sale => sale.id === guardId).frete, actualCarrierCost:state.vendas.find(sale => sale.id === guardId).freteLogistica.actualAmount};
                  state.pagamentos = originalPayments;
                  state.infinitePayPaymentHistoryKnown = originalPaymentHistoryKnown;
                  state.vendas = state.vendas.filter(sale => sale.id !== guardId);
                  await storSet(KEYS.vendas, state.vendas);
                  return {activePaymentResult, unknownHistoryResult};
                }""", direct_sale["id"])
                assert freight_guard == {
                    "activePaymentResult":{"chargedFreight":4.5,"actualCarrierCost":19},
                    "unknownHistoryResult":{"chargedFreight":4.5,"actualCarrierCost":21}
                }
                page.get_by_role("button", name="Vendas", exact=True).click()
                expect(page.get_by_text("SMOKE-PENDING-001")).to_be_visible(timeout=5000)

                page.get_by_role("button", name="Logística", exact=True).click()
                page.locator('[data-logistica-tab="etiquetas"]').wait_for(timeout=5000)
                page.locator('[data-logistica-tab="embalagens"]').click()
                page.get_by_role("button", name="+ Nova embalagem").click()
                package_form = page.locator("#form-sf-embalagem")
                package_form.locator("input[name='nome']").fill("Caixa Smoke")
                package_form.locator("input[name='comprimento']").fill("20")
                package_form.locator("input[name='largura']").fill("15")
                package_form.locator("input[name='altura']").fill("10")
                package_form.locator("input[name='pesoEmbalagem']").fill("0.2")
                page.get_by_role("button", name="Adicionar embalagem").click()
                expect(page.get_by_text("Caixa Smoke")).to_be_visible()
                page.locator("[data-edit-embalagem]").first.click()
                page.locator("#form-sf-embalagem input[name='comprimento']").fill("21")
                page.get_by_role("button", name="Salvar alterações").click()
                page.locator("#form-sf-embalagem").wait_for(state="detached", timeout=10000)
                expect(page.get_by_text("21×15×10")).to_be_visible()
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("[data-del-embalagem]").first.click()
                page.get_by_text("Caixa Smoke", exact=True).wait_for(state="detached", timeout=10000)

                page.locator('[data-logistica-tab="pesos"]').click()
                page.get_by_role("button", name="+ Novo peso de produto").click()
                weight_form = page.locator("#form-sf-peso")
                weight_form.locator("input[name='codigo']").fill("SMOKE-01")
                weight_form.locator("input[name='nome']").fill("Peso de teste")
                weight_form.locator("input[name='pesoUnitario']").fill("0.3")
                page.get_by_role("button", name="Adicionar peso").click()
                expect(page.get_by_text("SMOKE-01")).to_be_visible()
                page.locator("[data-edit-peso]").first.click()
                page.locator("#form-sf-peso input[name='pesoUnitario']").fill("0.35")
                page.get_by_role("button", name="Salvar alterações").click()
                page.locator("#form-sf-peso").wait_for(state="detached", timeout=10000)
                expect(page.get_by_text("0.35 kg")).to_be_visible()
                stored_weights = json_request(base, session_token, "/api/data/jsa-sf-peso-produtos")["value"]
                assert any(row.get("codigo") == "SMOKE-01" and row.get("pesoUnitario") == 0.35 for row in stored_weights)
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator("[data-del-peso]").first.click()
                page.get_by_text("SMOKE-01", exact=True).wait_for(state="detached", timeout=10000)

                # Mobile viewport: verify the application remains rendered and key navigation exists.
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("button", name="Painel").click()
                assert page.locator("#app").is_visible()
                expect(page.locator(".sidebar-brand-logo")).to_be_visible()
                assert page.locator("body").evaluate("el => el.scrollWidth") <= page.locator("body").evaluate("el => el.clientWidth") + 10
                result = {
                    "browser": "Chromium " + browser.version,
                    "legacy_storage_detect_export_no_merge": "PASS", "login": "PASS", "dashboard": "PASS",
                    "client_create_and_reload": "PASS", "sale_quote_autofill_and_numbering": "PASS",
                    "quote_conversion_atomic": "PASS", "infinitepay_page_unconfigured_no_call": "PASS",
                    "catalog_search_and_quick_quote": "PASS", "stored_xss_rendered_as_text": "PASS",
                    "all_17_modules_navigation": "PASS", "price_settings_persist_after_reload": "PASS",
                    "yale_tetra_price_sync_and_volume_tiers": "PASS", "card_rate_and_sale_calculation": "PASS",
                    "superfrete_sender_settings_persist_after_reload": "PASS",
                    "superfrete_weight_margin_applied_to_quote_and_shipment": "PASS",
                    "shipment_ui_payload_contract": "PASS", "logistica_package_and_weight_create_edit_delete": "PASS",
                    "superfrete_freight_linked_to_sale_and_payment_amount_protected": "PASS",
                    "company_logo_visible_on_desktop_and_mobile": "PASS",
                    "navigation": "PASS", "logistica_safe_ui": "PASS", "mobile_viewport": "PASS",
                    "page_errors": page_errors, "console_errors": console_errors[:10]
                }
                browser.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if page_errors:
                return 1
            return 0
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
