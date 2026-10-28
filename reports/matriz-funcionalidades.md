# Matriz de funcionalidades preservadas e testadas

Legenda: **Preservado** = tela/funções continuam no código; **Testado** = operação foi executada neste trabalho; **Parcial** = parte testada ou limitação conhecida; **Bloqueado** = dependência/credencial/ambiente ausente. Uma tela visível não significa que todos os seus fluxos foram testados.

| Módulo/funcionalidade | Dados SQLite observados | Estado/evidência desta entrega | Limite conhecido |
|---|---|---|---|
| Painel e navegação | Indicadores derivados das demais chaves | **Testado** em Chromium; navegação/render das 17 abas | Indicadores reais limitados pela base, que contém zero vendas |
| Clientes, perfil, filtro e CSV | `jsa-clientes` (1.262) | **Parcial**: cadastro, persistência/reload e autofill em cópia temporária; navegação testada | Edição/exclusão e importação CSV real não percorridas ponta a ponta |
| Vendas e histórico | `jsa-vendas` (vazio) | **Parcial**: venda de teste criada no browser, autofill e número público `VEN-...` validados; API CRUD genérica testada | Nenhuma venda original; retorno/estorno e regras de estoque real ainda não certificados |
| Orçamentos e conversão | `jsa-orcamentos` (1 orçamento/1 item) | **Parcial**: orçamento de teste numerado `ORC-...`; conversão atômica em venda verificada no Chromium | Estoque original é `null`; não foi possível validar baixa/reversão de saldo comercial real |
| Produtos, custos, preços e estoque | `jsa-produtos` (5; estoque nulo) | **Parcial**: produto temporário criado para prova XSS; preços Yale/Tetra sincronizados; configurações e faixas persistidas após reload | Movimentação de estoque com quantidades reais não simulada |
| Catálogo e orçamento rápido | Derivado do cadastro de produtos | **Testado**: busca livre, clique de código e adição de item ao orçamento rápido sem sair do Catálogo | Não foi medido contra catálogo externo ou lista de milhares de itens |
| Conversor Yale/Tetra e códigos pendentes | `jsa-codigos-nao-encontrados` (1); tabelas no HTML | **Parcial**: mapeamento incluído no dry-run; navegação e integração de preço testadas | Não validado contra tabela física/ERP externo |
| Preços & Custos e meta financeira | `jsa-config` e `jsa-meta` ausentes da base recebida | **Parcial**: persistência/recarga, edição de faixas, Yale/Tetra, taxa de cartão, regra de frete grátis e margem de peso verificadas em Chromium | Regras financeiras precisam de conferência pelo responsável com dados contábeis reais |
| Logística SuperFrete | Configuração/embalagens/pesos ausentes da base original | **Testado parcialmente**: cotação real em Produção mostrou PAC, SEDEX, Jadlog e Loggi; quatro pedidos de teste ficaram pendentes, sem checkout/pagamento. UI de cotação, payload simulado e CRUD criar/editar/excluir de embalagens e pesos passaram no Chromium | Mini Envios não apareceu na combinação testada; disponibilidade depende de CEP/serviço. Impressão e rastreio reais não foram exercidos. Cancelar pedidos pendentes no painel do provedor se não forem necessários |
| Pagamentos InfinitePay | `jsa-pagamentos` ainda não existia na base recebida | **Implementado; testes mockados**: cobrança avulsa, link por venda após conversão, Pix/cartão, centavos, idempotência, consulta por valor e retorno público HTTPS verificado | Nenhuma API Key InfinitePay foi fornecida; não houve chamada real, criação de link nem pagamento. Em localhost, retorno automático HTTPS não funciona; configure `INFINITEPAY_REDIRECT_URL` somente em serviço publicamente acessível |
| Contas a pagar | Chave ausente do KV original | **Parcial**: tela renderizada e navegável | CRUD e cálculos com registros reais não testados |
| Agenda e retornos | Parte de `jsa-clientes.proximoRetorno`; sem chave própria original | **Parcial**: aba acessada | Criação/edição de eventos não percorrida ponta a ponta |
| Marketing e gastos | `jsa-marketing` ausente | **Parcial**: aba renderizada na navegação geral | CRUD e impacto no lucro não verificados com registros reais |
| Templates/WhatsApp | `jsa-templates` ausente | **Parcial**: tela preservada e renderizada | Nenhuma mensagem externa enviada |
| Atividades e follow-up | `jsa-activity-log` ausente; follow-up derivado | **Parcial**: navegação/render verificados | CRUD e notificações não testados integralmente |
| Importação/restauração JSON | Armazenamento KV | **Parcial**: API de gravação em lote transacional, precondições, backup e exportação de dados legados testados | Upload/preview/commit completo no browser não executado |
| Exportação e snapshot | SQLite e estado da SPA | **Testado parcialmente**: fixture localStorage temporária exportada sem merge; snapshot validado por SHA-256/integrity_check | Fixture do browser não foi mesclada; não inspeciona o perfil real da pessoa usuária |
| Autenticação/API | Tabela SQLite `kv_store` | **Testado**: sessão HMAC, falha fechada, rate limit, CORS, controle `If-Match`, backup e bloqueio de deleção em massa | Modelo é single-admin local; Supabase Auth runtime não implementado |
| Supabase e RLS | Migração do KV atual | **Preparado; bloqueado** para validação remota | Schema/importador ainda não executados em projeto real; backend continua usando SQLite |

## Correspondência de armazenamento observada

As chaves existentes no banco recebido são `jsa-clientes`, `jsa-codigos-nao-encontrados`, `jsa-orcamentos`, `jsa-produtos` e `jsa-vendas`. Os módulos sem chave no banco original começam vazios ou usam defaults do painel. O importador preserva as chaves encontradas; nenhum dado comercial ou saldo foi inventado para preencher lacunas.

A suíte atual teve 33 testes unitários/API/migração aprovados. O smoke Chromium final aprovou navegação dos 17 módulos, vínculo de cotação/etiqueta/rastreio ao pedido, proteção de link de pagamento existente, logomarca em desktop e mobile e terminou com zero erros de página e de console. As chamadas de APIs externas nos testes foram mockadas e não acessaram contas reais.
