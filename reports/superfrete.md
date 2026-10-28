# Integração SuperFrete — documentação e evidências

Data da validação: 27/10/2026. A chave nova de Produção foi fornecida para o teste e injetada apenas no ambiente do subprocesso de backend; não foi gravada no `.env`, banco, navegador, relatório nem ZIP.

## Contratos oficiais consultados

- [Primeiros passos e autenticação](https://superfrete.readme.io/reference/primeiros-passos): tokens são separados entre Sandbox e Produção; host de produção `https://api.superfrete.com/`; autenticação Bearer e header `User-Agent` identificando aplicação/contato. [1]
- [Cotação de frete](https://superfrete.readme.io/reference/cotacao-de-frete): `POST /api/v0/calculator`, CEPs, serviços e `package` (comprimento/largura/altura em cm, peso em kg), além de opções de seguro. O código não reutiliza o host Sandbox mostrado nos exemplos. [2]
- A referência mapeia `1` PAC, `2` SEDEX, `17` Mini Envios, `3` Jadlog e `31` Loggi. Informa que Loggi não precisa mais ser solicitada pelo código `31`: sua ativação é controlada nas configurações do token. Jadlog e Loggi dependem da existência de ponto de postagem próximo ao CEP de origem. [2]
- [Criar frete para a SuperFrete](https://superfrete.readme.io/reference/adicionar-frete-carrinho): `POST /api/v0/cart`; cria uma etiqueta com status `pending` (aguardando pagamento). A documentação separa esse passo do pagamento. Ela usa `volume` no texto descritivo, mas a tabela Body Params e o exemplo cURL indicam `volumes` como objeto; o payload também inclui remetente/destinatário, serviço, produtos/declaração e opções.
- [Finalizar pedido e gerar etiqueta](https://superfrete.readme.io/reference/apiintegrationv1checkout): `POST /api/v0/checkout`, com `orders`; debita o saldo e muda a etiqueta para `released`. Saldo é adicionado manualmente no painel SuperFrete; não foi identificado endpoint público para recarga.
- [Buscar informações do usuário](https://superfrete.readme.io/reference/user-buscar-informações-do-usuário): `GET /api/v0/user`, incluindo saldo/limites; o painel recebe somente saldo e contagens, não CPF, e-mail ou outros dados pessoais retornados.
- [Informações do pedido](https://superfrete.readme.io/reference/tag-informa%C3%A7%C3%B5es-do-pedido): `GET /api/v0/order/info/{id}`, status e rastreio.
- [Link para impressão](https://superfrete.readme.io/reference/tag-link): `POST /api/v0/tag/print` com IDs em `orders`; o backend aceita apenas URLs HTTPS nos domínios SuperFrete documentados.

## Fluxo implementado

1. A configuração é server-side (`SUPERFRETE_TOKEN`, `SUPERFRETE_ENV`, `SUPERFRETE_FROM_POSTAL_CODE`); no browser só aparece se existe token, o ambiente e o CEP. O cliente nunca recebe o bearer token.
2. O teste de conexão consulta `GET /user` por uma rota FastAPI autenticada e retorna apenas saldo/limites.
3. A cotação exige CEP de destino, largura, altura, comprimento e peso; a origem vem exclusivamente do `.env`. A margem de peso salva em Configurações é aplicada uma vez antes da chamada e o mesmo peso efetivo é reutilizado na etiqueta. O resultado retorna serviços/preços/prazos.
4. Criar etiqueta (`/cart`) é separado do checkout, exige endereço e documento do destinatário e cria só estado pendente. O ID e valor são registrados localmente para recuperação, sem guardar CPF/endereço no resumo.
5. Checkout exige preço conferido, pedido pendente criado pelo CRM, confirmação visual com valor e digitação `PAGAR <ID>`. Resultado incerto bloqueia repetição até consultar o status.
6. Consulta de status e impressão são rotas independentes.

## Associação da logística à venda

Na lista de vendas há um atalho para iniciar a cotação com venda selecionada; quando os dados existem, o CRM preenche CEP do cliente, dimensões da embalagem, peso pelos modelos e o valor declarado a partir dos itens. A etiqueta criada envia um `sale_id` opaco apenas ao backend do JSA, que confirma a existência da venda e mantém número e referência do cliente no registro local pendente; o `sale_id` não é repassado à SuperFrete. O front-end grava o estado logístico da venda e a etiqueta em uma escrita versionada conjunta. Se a tela recarregar após o pedido remoto ter sido criado, o endpoint pendente permite recuperar a associação.

O valor real da etiqueta fica em `freteLogistica.actualAmount`; `sale.frete` permanece como o valor comercial histórico. Se ainda não há cobrança ativa, o valor é atualizado ao preço confirmado do provedor. Se existe link InfinitePay ativo, em criação incerta, pendente ou pago para frete cobrado do cliente, a quantia desse link é mantida; o custo real é separado e o sistema não cria outro link automaticamente. O cálculo de lucro considera a diferença entre o frete cobrado e o custo real, quando a etiqueta já tem preço confirmado. A liberação/pagamento da etiqueta continua sendo uma ação separada, com confirmação explícita do operador.

## Correção do payload de criação (27/10/2026)

A tentativa anterior com formatos divergentes retornou HTTP 422. A referência oficial tem inconsistência interna: o texto corrido chama o pacote de `volume`, mas Body Params e o exemplo cURL especificam `volumes`. O `app.py` funcional enviado pelo usuário também usa `volumes`; o proxy mantém esse contrato. Na rodada atual, o endpoint local autenticado foi exercitado em Produção e aceitou uma criação pendente para cada serviço cotado (PAC, SEDEX, Jadlog e Loggi).

O tratamento de HTTP 400/422 agora lê o corpo do provedor apenas para extrair nomes de campos de validação (`field/path` ou chaves em `errors`). Mensagens e valores do corpo não são repassados; assim o painel pode indicar quais campos foram rejeitados sem expor CPF, CEP, endereço, token ou conteúdo livre. Se o provedor não devolver campos estruturados, a mensagem permanece genérica. [3]

A área de saldo inclui link para o painel da SuperFrete e instruções de recarga via Pix. O sistema não tenta automatizar uma recarga, pois a documentação direciona essa etapa ao painel.

## Testes reais de Produção

- `GET /api/v0/user`: autenticação aceita; saldo retornado **R$ 7,43** no momento do teste. Esse valor pode mudar depois.
- Cotação de exemplo: origem CEP `08280-340`; destino CEP `20020-050`; pacote `20 × 15 × 10 cm`, `1 kg`; serviço solicitado `1,2,17,3,31`; seguro R$ 0. O novo padrão do formulário solicita esses serviços.
- Respostas retornadas naquele momento: Correios PAC **R$ 21,68**, prazo de 5 dias úteis; Correios SEDEX **R$ 30,96**, prazo de 1 dia útil; Jadlog Econômico **R$ 14,30**, prazo de 2 dias úteis; Loggi **R$ 13,86**, prazo de 2 dias úteis. Mini Envios não foi retornado para essa combinação.
- O mesmo fluxo foi executado pela API local autenticada, usando banco temporário.
- Pedido pendente de teste criado em 27/10/2026: ID `F52wi5KmHTbA5FaVnWfy`, serviço PAC, pacote `15 × 10 × 20 cm`, `0,75 kg`, CEP de origem/destino `08280-340`, produto sintético de teste de valor declarado R$ 1,00, preço do frete R$ 21,15. A API retornou `pending` tanto no POST quanto na consulta subsequente `GET /order/info/{id}`.
- Para não inventar CPF, o campo obrigatório do destinatário usou em memória o documento associado à própria conta SuperFrete; nome/item eram de teste e o endereço foi o CEP de origem informado pelo usuário. Nenhum cliente do CRM foi utilizado, nenhum dado pessoal foi registrado neste relatório e nenhum `/checkout` foi chamado. O pedido continua pendente na conta SuperFrete; cancele-o pelo painel se não for mais necessário.
- Com a chave nova, a cotação real para `08280-340` (destino igual à origem, pacote `15 × 10 × 20 cm`, `0,75 kg`, seguro zero) retornou PAC R$ 21,15, SEDEX R$ 13,73, Jadlog Econômico R$ 22,84 e Loggi R$ 9,16. Mini Envios não retornou uma opção para essa combinação.
- A API local autenticada criou uma etiqueta de teste por opção e recebeu `status=pending` em todas: PAC `GjlVJrxwq0z3d99VxdyI`; SEDEX `qtvzFW1Ae0gy85Y4dDuM`; Jadlog `01M5ZV2BTJ04Q3WRN0R1X3FA49`; Loggi `01M5ZV2K01NZSVVKW384N740AN`. Uma consulta read-only subsequente confirmou os quatro status `pending`, assim como o pedido PAC anterior `F52wi5KmHTbA5FaVnWfy`. Nenhum `/checkout` foi chamado; não houve débito/pagamento. O envio real usou banco temporário e conteúdo/endereço sintéticos; o documento associado à conta foi mantido só em memória.
- Os pedidos estão na conta SuperFrete e não foram inseridos no banco local desta cópia. A partir do próximo pedido criado pela UI, o fluxo normal registra o ID pendente localmente. Os preços podem variar por CEP, pacote, cobertura, regras do serviço e horário.

## Diagnóstico da interface e Preços & Custos

O teste real acima prova que a nova chave autentica e que o proxy atual cria pedidos pendentes para os quatro serviços cotados com dados sintéticos válidos. O log antigo do usuário registra HTTP 422 mas não contém o corpo estruturado do provedor; portanto, não permite atribuir com certeza aquele 422 a um campo específico dos dados reais. O browser smoke valida a forma do payload produzido pela UI sem disparar POST real, enquanto os POSTs reais foram feitos pela rota autenticada do backend.

Durante a reprodução da tela de preços foi identificado o sintoma relatado: ao clicar “Adicionar faixa”, o re-render descartava campos de preço/frete/metas ainda não salvos. A tela agora preserva o rascunho. A gravação de Yale/Tetra é transacional entre `jsa-config` e `jsa-produtos`, as faixas por quantidade afetam o preço padrão das chaves, e a margem de peso é efetivamente aplicada uma vez à cotação e etiqueta. O Chromium confirmou gravação, leitura pela API e sobrevivência à recarga.

## Aplicação Supabase

O arquivo `supabase/superfrete.sql` acrescenta metadados mínimos de etiquetas e RLS por organização. Deve ser executado **depois** do schema base. CPF, endereço completo e token não são gravados nessa tabela. A extensão de schema foi validada com parser SQL, mas não aplicada num projeto Supabase real porque não havia acesso/credenciais do projeto. O runtime desta entrega continua usando SQLite local; o Supabase não passa a ser fonte ativa automaticamente.

## References

[1]: https://superfrete.readme.io/reference/primeiros-passos "Primeiros passos da API SuperFrete"
[2]: https://superfrete.readme.io/reference/cotacao-de-frete "Cotação de frete SuperFrete"
[3]: https://superfrete.readme.io/reference/adicionar-frete-carrinho "Criar frete para a SuperFrete"
