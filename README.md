# JSA CRM — entrega auditada e endurecida

Esta entrega preserva o painel e os dados SQLite recebidos, corrige riscos críticos da API local e prepara o esquema PostgreSQL e o importador de dados para Supabase. O banco original não foi alterado.

> **Limite importante:** não foram fornecidos acesso, URL, senha PostgreSQL nem usuário Supabase. Portanto, a criação do schema em um projeto real, a execução de RLS contra duas organizações e a importação remota ainda não puderam ser realizadas. O painel desta entrega continua usando SQLite local como fonte ativa; **não configure uma migração como produção nem considere o Supabase como fonte oficial antes de implementar/testar o adaptador de runtime e concluir os testes descritos em `reports/testes.md`**. O schema e o importador estão preparados, mas a conexão de produção não foi simulada.

## Conteúdo

- `backend/app.py`: API FastAPI protegida, servindo o painel.
- `backend/jsa.db`: cópia intacta do banco recebido.
- `backend/static/index.html` e `Painel_JSA.html`: entradas preservadas e iguais, com autenticação, persistência sem fallback silencioso, navegação agrupada, logística SuperFrete e Pagamentos InfinitePay.
- `supabase/schema.sql`: schema PostgreSQL normalizado, isolamento por organização e políticas RLS.
- `supabase/migrations/20261026_001_initial_schema.sql`: versão inicial para histórico de migrações.
- `supabase/superfrete.sql` e `supabase/migrations/20261026_002_superfrete_logistics.sql`: addon idempotente para metadados de etiquetas com RLS.
- `scripts/migrate_sqlite_to_supabase.py`: simulação por padrão; importação remota somente com `--apply`.
- `tests/`: testes HTTP/API, plano de migração e smoke test opcional em Chromium.
- `backups/`: cópia original do banco e do painel, além do backup criado e validado na simulação.
- `reports/`: auditoria, matriz funcional, testes, dry-run e evidência/contratos das integrações SuperFrete e InfinitePay.

## Requisitos e instalação

Testado nesta entrega com Python 3.12.3, FastAPI 0.162.0, Uvicorn 0.54.0, Pydantic 2.14.1 e psycopg 3.3.6. Use Python 3.12 ou uma versão compatível com as dependências pinadas.

```bash
unzip JSA_CRM_NextGen_Final.zip
cd JSA_CRM_NextGen
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
cp .env.example .env             # Windows: Copy-Item .env.example .env
```

Gere uma chave local longa e aleatória, depois cole-a no campo `APP_API_TOKEN=` do `.env` (não a envie nem a publique):

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

A API permanece desabilitada para dados se a chave estiver vazia ou tiver menos de 32 caracteres. Após preencher o `.env`, rode a partir da raiz do projeto:

```bash
python backend/app.py
```

Abra <http://127.0.0.1:8000>. O painel pedirá a chave configurada. A chave gera uma sessão assinada com validade de 8 horas; somente essa sessão curta fica em `sessionStorage`. A API escuta em `127.0.0.1` por padrão. Não a exponha à internet sem proxy HTTPS, controles adicionais e revisão de segurança.

### HTTPS local opcional no Windows

No PowerShell, a partir da pasta do projeto, execute `powershell -ExecutionPolicy Bypass -File .\scripts\run_https.ps1`. O script instala o `mkcert` pelo WinGet (ou Chocolatey se WinGet não estiver disponível), cria uma CA confiável para o usuário e gera certificado para `localhost` e `127.0.0.1`. Ele inicia o Uvicorn somente em loopback; abra <https://127.0.0.1:8000> (ou a porta `APP_PORT` definida no `.env`). Os arquivos do certificado e da chave privada ficam em `%LOCALAPPDATA%\JSA_CRM\localhost-https`, fora do projeto e do ZIP. É uma configuração apenas de desenvolvimento: não compartilhe a chave da CA `rootCA-key.pem` nem a chave local. O `mkcert` é descrito no [repositório oficial](https://github.com/FiloSottile/mkcert); o Uvicorn documenta as opções `--ssl-certfile` e `--ssl-keyfile` em [Settings](https://uvicorn.dev/settings/).

Esse certificado protege navegador → JSA CRM em `localhost`; não altera nem corrige a conexão de saída backend → API InfinitePay. Se aparecer “Falha na conexão HTTPS com a InfinitePay”, investigue o relógio, as autoridades confiáveis, proxy/antivírus, DNS e firewall do computador onde o backend roda. Depois de instalar a CA, feche e reabra o navegador para ele recarregar a confiança local.

Verificação básica em outro terminal:

```bash
curl http://127.0.0.1:8000/api/health
```

A resposta de saúde não revela caminhos locais nem dados pessoais. A API de dados exige autenticação; o acesso à documentação OpenAPI só fica ligado em `APP_ENV=development`.

## Testes

```bash
python -m unittest discover -s tests -v
```

Para o teste de navegador, além do pacote Playwright e do Chromium:

```bash
python -m pip install playwright
python -m playwright install chromium
python tests/browser_smoke.py
```

O smoke test cria uma cópia temporária do SQLite e valida exportação segura de dados legados, login, cadastro/reload de cliente, autofill, venda e orçamento numerados, conversão atômica, busca e orçamento rápido do Catálogo, XSS armazenado, navegação dos 17 módulos, tela Pagamentos sem credenciais, persistência após recarga dos campos de Preços & Custos e remetente, sincronização dos preços Yale/Tetra com Produtos, faixas por quantidade, taxa de cartão, regra de frete grátis, margem de peso SuperFrete aplicada à cotação e etiqueta, cotação iniciada pela venda, associação de pedido e rastreio ao pedido, preservação de valor em link InfinitePay ativo, lucro baseado no custo de transporte real, logo em desktop/mobile, CRUD de embalagem/peso e viewport mobile. Usa tokens fictícios e rotas externas mockadas. Não faz chamadas externas nem modifica `backend/jsa.db`.

## API e salvamento

Rotas de dados exigem `Authorization: Bearer <sessão>`; o front-end obtém a sessão por `POST /api/auth/session` ao digitar a chave administrativa. As gravações recusadas ou sem conexão produzem aviso explícito; não há escrita automática no `localStorage` nem mensagem de sucesso falso. O `localStorage` legado é somente inspecionado para oferecer uma exportação manual, sem merge automático com o banco.

Gravações individuais usam controle otimista: versão ausente recebe `428`; versão desatualizada recebe `409`, é recusada e não sobrescreve o dado mais recente (ex.: outra aba aberta). O painel indica o conflito e pede recarga. Chamadas abortam após 15 segundos. Restauração em lote é intencionalmente separada, transacional e precedida por backup.

`DELETE /api/data` está desativado. A operação administrativa `POST /api/admin/reset` exige autenticação, a frase `APAGAR TODOS OS DADOS` e cria/valida um backup antes de excluir. A restauração JSON em lote faz backup e gravação transacional. O snapshot de backup retorna apenas um nome opaco e SHA-256, nunca um caminho do servidor.

## Logística SuperFrete

Configure somente no `.env` do servidor, nunca na página:

```dotenv
SUPERFRETE_TOKEN=cole_o_token_aqui
SUPERFRETE_ENV=production
SUPERFRETE_FROM_POSTAL_CODE=08280-340
```

O ZIP final não inclui `.env` nem token. Depois de extrair, copie `.env.example` para `.env`, preencha `APP_API_TOKEN` e estas variáveis SuperFrete, e reinicie o backend. Use tokens do ambiente correspondente (`production` ou `sandbox`). O CEP `08280-340` é a origem informada para esta instalação; ajuste se o local de postagem mudar. Rua/número e os dados do remetente são preenchidos em Logística e enviados apenas ao backend/provedor quando se prepara um pedido.

Na aba Vendas, “Cotação de frete” abre a Logística com a venda selecionada e preenche CEP, embalagem, pesos cadastrados e valor declarado quando disponíveis; revise os dados antes de cotar. O pedido pendente SuperFrete fica vinculado à venda, e transportadora, custo real, status e rastreio são mantidos no pedido e recuperados após recarga. A etiqueta não envia `sale_id` nem dados do CRM ao provedor. Se já existir pagamento InfinitePay ativo, ou se o histórico de cobranças não puder ser consultado, o valor comercial do frete não é alterado nem um link é recriado automaticamente: o custo real da etiqueta é registrado separadamente; confira a cobrança e a margem e trate eventual diferença manualmente. Na aba Logística, “Testar conexão e consultar saldo” usa somente leitura. Por padrão, a cotação solicita PAC, SEDEX, Mini Envios, Jadlog e Loggi; o seletor também permite reduzir a lista. Jadlog e Loggi só aparecem quando há ponto de postagem próximo do CEP de origem; a Loggi também precisa estar ativa nas configurações do token SuperFrete. A cotação usa CEP destino, medidas (cm), peso (kg), seguro e CEP de origem do `.env`; a margem configurada é aplicada uma vez ao peso efetivamente enviado e esse mesmo peso é reutilizado na etiqueta. Consultar frete não cria pedido nem debita saldo. “Criar pedido pendente” registra uma etiqueta pendente via `/cart`; não executa pagamento. Somente “Confirmar e pagar” chama `/checkout` e exige a revisão do preço e a frase `PAGAR <ID>`. Não repita um checkout cujo resultado de rede seja incerto; primeiro atualize o status. Impressão/rastreio são passos separados. A recarga de saldo permanece manual no painel SuperFrete. Em Preços & Custos, custos/preços de Yale/Tetra são sincronizados com Produtos e faixas de volume afetam o preço padrão da venda.

O teste real de Produção com a chave nova confirmou quatro opções disponíveis no CEP/pacote de teste e criou quatro pedidos pendentes: PAC **GjlVJrxwq0z3d99VxdyI** (R$ 21,15), SEDEX **qtvzFW1Ae0gy85Y4dDuM** (R$ 13,73), Jadlog **01M5ZV2BTJ04Q3WRN0R1X3FA49** (R$ 22,84) e Loggi **01M5ZV2K01NZSVVKW384N740AN** (R$ 9,16). Mini Envios não apareceu nessa cotação. Havia também um pedido PAC pendente de teste anterior: **F52wi5KmHTbA5FaVnWfy** (R$ 21,15). Nenhum `/checkout` foi chamado e nenhum pagamento foi feito; esses pedidos estão na conta SuperFrete para cancelamento pelo operador. A gravação local dos testes reais ficou isolada. Evidências sem dados pessoais estão em [`reports/superfrete.md`](reports/superfrete.md).

## Pagamentos InfinitePay

Configure `INFINITEPAY_API_KEY` no `.env` do backend para gerar links com as opções avançadas usadas pelo CRM. `INFINITEPAY_HANDLE` é necessário para consulta de pagamento e conciliação, mas não é enviado ao criar o link. Reinicie o servidor após editar o arquivo. As variáveis ficam vazias no `.env.example` e o ZIP não contém credenciais. Veja as referências do contrato em [`reports/infinitepay.md`](reports/infinitepay.md).

Ao converter um orçamento, o CRM primeiro persiste a venda, o fechamento do orçamento e a baixa de estoque em uma gravação atômica; depois tenta criar um link InfinitePay conforme a opção escolhida no CRM. Se a integração estiver indisponível, a venda permanece registrada e o erro aparece na tela Pagamentos. Uma venda com tentativa ativa reutiliza o link existente ou bloqueia nova emissão se o resultado anterior for incerto. Só depois de conferir no painel InfinitePay que não existe pedido é possível liberar uma tentativa incerta, digitando a frase de confirmação que inclui seu `order_nsu`. O preço é reconstruído no servidor a partir da venda persistida, em centavos; frete só é cobrado quando foi marcado como pago pelo cliente. A etiqueta vinculada depois não altera nem recria um link já ativo; confira separadamente o frete cobrado e o custo real de transporte.

A aba Pagamentos também gera cobrança avulsa sem criar venda ou alterar estoque. Cada link usa um pagamento e validade de sete dias. Conforme o contrato atual da InfinitePay, o cartão permanece disponível; selecionar Pix habilita Pix adicionalmente, portanto o CRM não promete um link exclusivo de Pix. O CRM mantém histórico e só marca como Pago quando a consulta autenticada `/payment_check` confirma `order_nsu`, `transaction_nsu`, `slug` e o valor exato. Não são coletados dados de cartão. Em instalação publicada, configure opcionalmente `INFINITEPAY_REDIRECT_URL=https://seu-dominio/api/infinitepay/return`; essa página pública mínima consulta a API da InfinitePay no servidor, sem expor o painel administrativo nem confiar cegamente nos parâmetros do navegador. Em `localhost`, não configure retorno automático: o endereço não é acessível pelo comprador fora da máquina e o pagamento pode ser conciliado manualmente. Webhook não está habilitado nesta versão. Os testes automatizados usam mocks; quatro links de teste reais de R$ 1,00 foram gerados anteriormente, e o usuário informou que concluiu com sucesso o pagamento de um deles. Isso não substitui a conciliação por `payment_check` no banco de produção; nenhum pagamento novo foi feito nesta atualização.

## Configurar Supabase

1. Crie um projeto Supabase e um usuário em **Authentication**.
2. Execute uma vez `supabase/schema.sql` no SQL Editor do projeto. A mesma versão base está em `supabase/migrations/20261026_001_initial_schema.sql`. Para a logística, execute depois `supabase/superfrete.sql` (ou a migração aditiva `supabase/migrations/20261026_002_superfrete_logistics.sql`), não é necessário executar ambos.
3. Crie uma organização vinculada ao UUID do usuário Auth, usando SQL Editor com o UUID real da conta (não use o valor de exemplo):

```sql
insert into public.organizations (name, created_by)
values ('JSA', '<UUID_REAL_DO_USUARIO_AUTH>')
returning id;

insert into public.organization_members (organization_id, user_id, role)
values ('<UUID_DA_ORGANIZACAO_RETORNADO>', '<UUID_REAL_DO_USUARIO_AUTH>', 'owner');
```

4. Mantenha a URL de conexão PostgreSQL apenas no `.env` do servidor. Preencha `SUPABASE_DATABASE_URL` e `SUPABASE_ORG_ID`. A URL pode conter uma senha de banco; trate-a como segredo. O navegador não recebe `service_role` nem senha PostgreSQL.
5. Rode primeiro a simulação, sem `--apply`:

```bash
python scripts/migrate_sqlite_to_supabase.py --report reports/migration-dry-run.json
```

6. Revise o relatório, os backups e a organização. Somente então a importação remota explícita seria:

```bash
python scripts/migrate_sqlite_to_supabase.py --apply --org-id UUID_DA_ORGANIZACAO --report reports/migration-apply.json
```

O script cria backup SQLite com integridade checada e igualdade lógica de schema/linhas; preserva cada valor bruto em `legacy_kv_store`; projeta clientes, produtos, vendas, orçamentos, itens e códigos pendentes nas tabelas normalizadas; preserva os IDs e os payloads de origem; detecta erros e repetições; e usa upsert idempotente. O SQLite nunca é apagado nem substituído. Sem Supabase configurado, `--apply` recusa a operação.

> A carga remota não foi executada nesta entrega. Além disso, o backend atualmente usa SQLite em runtime; o painel não faz leitura/escrita nas tabelas Supabase. Faça a integração do adaptador de persistência antes de tornar o Supabase a fonte oficial. Este limite está registrado para impedir uma troca de banco enganosa ou perda de dados.

## Backup e restauração

- O original está preservado em `backups/jsa-original.db` e em `backend/jsa.db`, com SHA-256 igual ao SQLite recebido: `e7b57d94c0f7a7a177061436092cb8e3ecd763371d3eb491d82e7ab027d19405`.
- `POST /api/backup/snapshot` gera cópia SQLite consistente, verifica `PRAGMA integrity_check` e retorna identificador + hash.
- Cópias JSON do painel não incluem o token SuperFrete; após restauração, reconfigure-o manualmente.
- Não apague o arquivo original até confirmar a migração, a reconciliação e o aceite do operador.

## Segurança e operação

- `.env` e bancos/backups ficam fora do Git via `.gitignore`; por segurança, o ZIP de entrega não contém `.env`, token nem credenciais. Use `.env.example` como modelo.
- O controle atual é de **um administrador local**, não de múltiplos usuários. A sessão é HMAC assinada e expira em 8 h; cinco falhas de senha em 60 s bloqueiam tentativas por 60 s.
- InfinitePay é acessada por proxy FastAPI autenticado; `INFINITEPAY_API_KEY` e `INFINITEPAY_HANDLE` nunca são enviados ao browser nem incluídos no ZIP. Respostas e erros do provedor são filtrados; timeouts/HTTP 5xx ficam como resultado incerto para evitar duplicar cobranças.
- RLS no schema Supabase restringe linhas por `organization_id`; membro lê/escreve em sua organização e remoção exige papel admin/owner. A validação de RLS contra contas e organizações reais está pendente.
- A integração SuperFrete usa proxy FastAPI autenticado; o bearer é mantido no `.env` do servidor, não é devolvido ao browser e não é incluído em exportações. O checkout em Produção exige confirmação por valor e ID. O token usado no teste deve ser rotacionado pelo usuário depois da validação e nunca publicado no ZIP.
- O front-end ainda é um HTML extenso que renderiza templates via `innerHTML`; valores dinâmicos de texto e atributos são escapados por `sfHtml`, e o smoke test confirmou que um nome de produto com marcação persistida é exibido como texto (sem criar elemento HTML). Isso reduz o risco de XSS armazenado, mas não substitui um pentest completo; se forem introduzidos campos HTML ricos ou novas fontes de dados externos, sanitize com uma política de allowlist/DOMPurify antes de renderizar.
- Nodes e PHP não foram encontrados no ZIP completo; nenhuma dessas tecnologias foi adicionada.

Consulte `reports/auditoria.md`, `reports/matriz-funcionalidades.md` e `reports/testes.md` para evidências e itens não concluídos.
#   c r m j s a  
 "# crmjsa" 
"# crmjsa" 
