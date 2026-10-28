# Backend JSA CRM

O backend FastAPI deste projeto serve `static/index.html`/`static/Painel_JSA.html` e mantém o SQLite local como persistência ativa desta versão. Para instalação, configuração de `APP_API_TOKEN`, execução, testes, backup e status do Supabase, siga o [README da raiz](../README.md).

## Inicialização

Execute da raiz do projeto:

```bash
python backend/app.py
```

A API escuta em `127.0.0.1` por padrão. Sem `APP_API_TOKEN` válido (32+ caracteres), as rotas de dados falham fechadas e o painel não carrega registros. O backend não usa fallback de gravação no `localStorage`.

## API

- `GET /api/health`: status não sensível.
- `POST /api/auth/session`: valida a chave local e emite token HMAC de sessão (8 horas).
- `GET /api/auth/check`: valida sessão.
- `GET /api/data`, `GET /api/data/{key}`, `PUT /api/data/{key}`: leitura/gravação autenticadas.
- `PUT /api/data`: restauração em lote transacional após backup.
- `DELETE /api/data/{key}`: exclusão autenticada com backup prévio.
- `POST /api/admin/reset`: reset administrativo com frase exata e backup.
- `POST /api/backup/snapshot`: snapshot SQLite validado; não revela caminho.

`DELETE /api/data` é deliberadamente desativado. O schema Supabase e o importador são preparatórios; o runtime atual ainda usa SQLite. Veja `reports/testes.md` antes de usar em produção.

As gravações individuais exigem `If-None-Match: *` para chave nova ou `If-Match: <updated_at>` para atualização; versão ausente retorna `428` e conflito retorna `409` sem sobrescrever a versão mais nova. O painel envia esses cabeçalhos e aborta requisições que excedem 15 segundos. A restauração em lote é uma ação administrativa separada, transacional e precedida de backup.

## SuperFrete

O token fica exclusivamente em `SUPERFRETE_TOKEN` no `.env` do servidor; configure também `SUPERFRETE_ENV=production` (ou `sandbox`) e `SUPERFRETE_FROM_POSTAL_CODE`. Rotas exigem a sessão administrativa já autenticada: `GET /api/superfrete/config`, `GET /api/superfrete/account`, `POST /api/superfrete/quote`, `POST /api/superfrete/shipments` (cria somente pedido pendente), `POST /api/superfrete/checkout`, `GET /api/superfrete/pending`, `GET /api/superfrete/orders/{id}` e `POST /api/superfrete/orders/{id}/print`.

O host remoto é fixo por ambiente; dados brutos de conta/endereços não são devolvidos ao browser. A cotação padrão solicita serviços `1,2,17,3,31` (PAC, SEDEX, Mini Envios, Jadlog e Loggi). A disponibilidade de Jadlog/Loggi depende de pontos de postagem próximos ao CEP de origem; Loggi também é controlada pela configuração do token. `POST /api/superfrete/shipments` aceita `sale_id` opcional e valida contra vendas do CRM; a referência opaca fica no resumo pendente para reconciliação e não é enviada ao provedor. Checkout não é acionado pelo fluxo de criação: requer pedido pendente criado nesta instalação, preço confirmado pela API, o mesmo `expected_price` exibido e `confirmation: "PAGAR <ID>"`. Resultado de rede incerto mantém o estado bloqueado até consultar o pedido. Veja [`reports/superfrete.md`](../reports/superfrete.md) para contratos oficiais, associação com venda e teste sem cobrança.

## InfinitePay Checkout

Configure `INFINITEPAY_API_KEY` somente no `.env` do servidor para gerar links com as opções avançadas usadas pelo CRM. `INFINITEPAY_HANDLE` é necessário para `payment-check` e para a página de retorno público, não para o `POST /links` autenticado pela API Key. Rotas administrativas autenticadas: `GET /api/infinitepay/config`, `GET /api/infinitepay/payments`, `POST /api/infinitepay/links`, `POST /api/infinitepay/payment-check` e `POST /api/infinitepay/payments/{id}/resolve-uncertain`. A exceção `GET /api/infinitepay/return` é uma página pública mínima, disponível apenas se `INFINITEPAY_REDIRECT_URL` for configurada como URL HTTPS terminada nesse caminho e API Key/handle estiverem presentes; ela aplica limite por IP e confere o pagamento consultando a InfinitePay no servidor. O proxy usa host remoto fixo e não devolve credenciais. Para venda, o backend obtém os itens/preços do registro persistido e converte para centavos; para cobrança avulsa, aceita descrição e valor validados. Links pendentes de venda são reutilizados para evitar uma segunda cobrança; falhas de rede/timeout e HTTP 5xx ficam incertas, então não tente novamente sem antes conferir o painel do provedor. A recuperação exige frase com o `order_nsu` e só é aceita após dois minutos quando a tentativa ainda está em `creating`. A InfinitePay mantém cartão disponível; selecionar Pix habilita Pix também, sem garantia de exclusividade.

A conversão do orçamento é persistida primeiro (venda, orçamento e estoque de forma atômica) e só então é solicitada a criação do link Pix/cartão. A resposta não transforma a venda em paga. Para conciliar, `payment-check` recebe `payment_id`, `slug` e `transaction_nsu`, consulta `/payment_check` no servidor e verifica o valor registrado antes de definir status `paid`. Se uma etiqueta for anexada a uma venda com cobrança ativa, o frontend preserva o frete comercial já enviado ao checkout e registra o custo real em `freteLogistica`; quando o histórico InfinitePay não pode ser consultado, aplica a mesma proteção por segurança. Consulte [`reports/infinitepay.md`](../reports/infinitepay.md) para fontes oficiais, requisitos HTTPS e limites da validação.
