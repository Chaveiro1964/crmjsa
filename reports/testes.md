# Relatório de testes e pendências

Data: 28/10/2026. Resultados somente onde houve execução real. A suíte usa `unittest` da biblioteca padrão; não afirma testes de fluxos que não foram executados.

## Aprovados

| Teste executado | Resultado | Evidência |
|---|---|---|
| ZIP original íntegro | Aprovado | `unzip -t` sem erros |
| Backup do SQLite original | Aprovado | SHA-256 idêntico entre banco fornecido, `backend/jsa.db` e `backups/jsa-original.db`: `e7b57d94c0f7a7a177061436092cb8e3ecd763371d3eb491d82e7ab027d19405` |
| Integridade SQLite | Aprovado | `PRAGMA integrity_check = ok`; `PRAGMA foreign_key_check` vazio |
| JSON de cada chave de origem | Aprovado | 5/5 parseáveis; 0 JSON inválido no banco enviado |
| Instalação de dependências em venv limpa | Aprovado | `requirements.txt` instalado e `requirements.lock` gerado com freeze completo |
| Sintaxe Python | Aprovado | `python3 -m py_compile` nos arquivos da API, migrador e testes |
| Sintaxe JavaScript inline | Aprovado | Script inline extraído e aceito por `node --check`; Chromium também carregou sem erro de página |
| API HTTP, migração, SuperFrete e InfinitePay, 33 testes automatizados | Aprovado | `python3 -m unittest discover -s tests -v`: 33/33 OK; inclui validação do vínculo `sale_id` e recusa de venda inexistente antes de chamar a SuperFrete, host fixo, token server-side, cotação, `/cart` pendente e diagnóstico 422 sanitizado; InfinitePay valida preço derivado no servidor, centavos, payload sem handle, API Key, erro DNS em estado incerto, idempotência, segredos, retorno HTTPS, consulta por valor e recuperação explícita. Provedor mockado nessa suíte |
| Conflito de edição por versões | Aprovado | PUT sem precondição recebe `428`; gravação concorrente com versão antiga recebe `409`; conteúdo mais recente permanece no banco |
| API sem autenticação/chave errada/tamper | Aprovado | `401`; configuração ausente falha com `503`; token de sessão diferente do segredo |
| Limite de login | Aprovado | Cinco falhas dentro de 60 s foram registradas; tentativa seguinte retornou `429` |
| CRUD de uma chave e validação JSON | Aprovado | PUT/GET/DELETE em DB temporário; chave ausente retorna `value:null` |
| Exclusão em massa e confirmação | Aprovado | `DELETE /api/data` retorna `405`; frase errada retorna `400`; reset confirmado gera backup antes |
| Backup snapshot e restauração de lote | Aprovado | SHA-256, `PRAGMA integrity_check=ok`, operação em lote transacional; resposta não contém caminho de host |
| CORS | Aprovado | Origem não autorizada não recebe `Access-Control-Allow-Origin` |
| Smoke test E2E de Chromium | Aprovado | `python tests/browser_smoke.py`: além dos fluxos existentes, testa cotação iniciada pela venda, associação de etiqueta/rastreio ao pedido, custo real de frete, preservação do preço num link ativo e logo em desktop/mobile; todos os 17 módulos, view 390 px; 0 `page_errors`, 0 `console_errors`; requests externos são mockados e nenhuma chamada real InfinitePay é feita pelo smoke |
| API SuperFrete em Produção — consulta de conta | Aprovado, somente leitura | `GET /user` autenticado pelo backend: Produção aceita credencial; saldo no momento **R$ 7,43**. Nenhum dado pessoal foi impresso |
| API SuperFrete em Produção — cotação de exemplo | Aprovado, sem compra | `POST /api/superfrete/quote` autenticado, usando os serviços padrão `1,2,17,3,31`, sem enviar etiqueta: origem 08280-340, destino 20020-050, pacote 20×15×10 cm/1 kg/seguro zero. Retornou PAC R$ 21,68 (5 dias úteis), SEDEX R$ 30,96 (1 dia útil), Jadlog Econômico R$ 14,30 (2 dias úteis) e Loggi R$ 13,86 (2 dias úteis); Mini Envios não retornou opção |
| API SuperFrete em Produção — nova chave e criação pendente | Aprovado, sem pagamento | Autenticação da chave nova e cotação real retornaram PAC, SEDEX, Jadlog e Loggi. Um `POST /cart` por opção, via API local autenticada, retornou `pending` para todos: PAC `GjlVJrxwq0z3d99VxdyI` (R$ 21,15), SEDEX `qtvzFW1Ae0gy85Y4dDuM` (R$ 13,73), Jadlog `01M5ZV2BTJ04Q3WRN0R1X3FA49` (R$ 22,84), Loggi `01M5ZV2K01NZSVVKW384N740AN` (R$ 9,16). Consulta GET confirmou os quatro pendentes e o pedido PAC anterior `F52wi5KmHTbA5FaVnWfy`. Mini Envios não foi retornado nessa combinação. Nenhum `/checkout`; sem pagamento; banco local era cópia temporária |
| Correção reproduzida de Preços & Custos | Aprovado | E2E reproduziu perda de campos ainda não salvos ao clicar “Adicionar faixa”; correção captura e restaura todos os campos em re-render. Também prova persistência após reload, gravação atômica de `jsa-config` + `jsa-produtos`, cálculo por faixa Yale/Tetra, taxa de cartão e regra de frete grátis |
| Migração em dry-run | Aprovado | `scripts/migrate_sqlite_to_supabase.py --report reports/migration-dry-run.json`: backup lógico verificado, 0 erros; plano de 1.262 clientes, 5 produtos, 0 vendas, 1 orçamento, 1 item, 1 código pendente |
| Schema SQL base + addon SuperFrete — parser | Aprovado com limite | `pglast` analisou 94 instruções do schema e 19 do addon; checagem estrutural, não execução em PostgreSQL |
| Proteção do modo `--apply` | Aprovado | Sem `SUPABASE_DATABASE_URL`, encerrou com código 2 antes de conectar ou gravar |
| Entradas `index.html` e `Painel_JSA.html` | Aprovado | Arquivos iguais após a atualização logística/logo |
| Inicialização pelo comando documentado | Aprovado | `python backend/app.py` iniciou em `127.0.0.1` com DB temporário e retornou health check sem expor caminho |

O smoke test de navegador usa uma cópia temporária do banco e uma fixture localStorage fabricada somente para testar o fluxo de exportação. A fixture não foi mesclada; os dados originais permaneceram sem alteração.

## Bloqueados / não executados

| Item | Estado | Motivo/evidência necessária |
|---|---|---|
| Aplicar `schema.sql` num Supabase real | Bloqueado | Projeto/URL e acesso SQL não foram fornecidos; parser não equivale a execução |
| Testar RLS entre organizações/usuários | Bloqueado | Não há duas contas Auth/organizações para provar isolamento por consulta e mutação |
| Executar `--apply` e comparar registros de destino | Bloqueado | `SUPABASE_DATABASE_URL` e `SUPABASE_ORG_ID` vazios; o modo apply recusa sem credenciais |
| Fazer Supabase como fonte oficial do painel | Bloqueado/incompleto | Backend desta entrega ainda usa SQLite; falta adaptador runtime, login Supabase Auth e testes concorrentes/conflito |
| Estoque com quantidades controladas, devolução e reconciliação entre vendas | Não executado integralmente | O browser smoke criou uma venda e converteu um orçamento; o banco original não contém venda nem estoque controlado. São necessários cenários explícitos de baixa, reversão e concorrência de estoque |
| CRUD completo de produtos, contas, marketing, agenda e demais módulos | Parcial | A navegação e o render de todos os 17 módulos foram verificados; produto foi criado para prova XSS; preço-config/tiers e remetente foram criados/editados e recarregados. Os demais CRUDs/funções de agenda, marketing, contas, follow-up e atividades não tiveram cenário E2E integral |
| Importação CSV/Google Contacts e restauração por arquivo no browser | Não executado | API em lote testada, mas fluxo completo de upload/preview/commit no browser não foi percorrido |
| SuperFrete — checkout, pagamento e impressão reais | Não executado deliberadamente | Foram criados cinco pedidos de teste pendentes na conta (um PAC anterior e quatro serviços na rodada atual), todos com conteúdo/endereço sintéticos; nenhum checkout ou pagamento foi feito. Impressão/rastreio reais não foram executados. O operador deve cancelar os pedidos pendentes no painel se não quiser mantê-los |
| InfinitePay — criação e pagamento de links reais | Parcial | Quatro links de R$ 1,00 foram criados com sucesso (HTTP 200), pagamento único e validade de 24 horas; dois habilitaram Pix e dois não incluíram Pix no payload. O usuário informou que testou e pagou um link com sucesso. A API de criação retornou URLs; nenhum QR/copia-e-cola foi obtido. A chamada por linha de comando no sandbox expirou em TLS; isso não demonstra a conectividade do FastAPI na máquina local nem substitui confirmação de transação pela consulta autenticada no CRM |
| Aplicar addon Supabase e provar RLS em projeto real | Bloqueado | O addon foi validado por parser; URL/credenciais e projeto Supabase não foram fornecidos. A tabela não foi aplicada no banco remoto |
| Análise CVE abrangente de dependências | Não executada | Dependências foram fixadas e instaladas; não foi feita varredura externa de vulnerabilidades |
| Pentest XSS abrangente em todos os templates e importações externas | Parcial | Valores dinâmicos de texto/atributos recebem escaping HTML e o smoke provou que um nome de produto persistido contendo markup não cria um elemento. Não foi executado pentest integral nem avaliação de todas as futuras fontes de importação |
| Divergência do localStorage real da pessoa usuária | Bloqueado | O navegador e seu perfil real não são acessíveis nesta análise. O painel agora detecta e oferece exportação manual; o smoke test usou fixture temporária |
| HTTPS local com certificado confiável no Windows | Não executado neste sandbox | `scripts/run_https.ps1` foi criado para mkcert/Uvicorn, mas este ambiente Linux não tem PowerShell Windows nem acesso ao armazenamento de certificados da máquina do usuário. O HTTPS local é separado da TLS de saída para InfinitePay |

## Comandos reproduzíveis

Na raiz do projeto, com dependências instaladas:

```bash
python -m unittest discover -s tests -v
python scripts/migrate_sqlite_to_supabase.py --report reports/migration-dry-run.json
python tests/browser_smoke.py  # requer Playwright + Chromium instalados
```

Não execute `--apply` nem anuncie a base Supabase como validada até preencher credenciais de um projeto de teste, aplicar o schema, executar RLS com usuários em duas organizações e reconciliar contagens/IDs.
