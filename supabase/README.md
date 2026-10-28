# Supabase do JSA CRM

## Arquivos

- `schema.sql`: schema inicial PostgreSQL, autorizado a ser aplicado uma única vez em projeto novo ou reexecutado com políticas recriadas dentro de transação.
- `migrations/20261026_001_initial_schema.sql`: cópia versionada do schema inicial. Os próximos deltas devem ser novos arquivos com timestamp/versão; não reescreva uma migração já aplicada em produção.
- `superfrete.sql`: extensão aditiva e repetível com tabela `shipping_labels`, índices, trigger e RLS; execute depois do schema base. A mesma extensão está em `migrations/20261026_002_superfrete_logistics.sql`.

## Modelo de segurança

O banco separa clientes, produtos, vendas/itens, orçamentos/itens, contas a pagar, gastos de marketing, atividades e códigos pendentes. `app_state` armazena apenas configurações/módulos com estrutura não observável ou não estável; não substitui as entidades comerciais normalizadas. `legacy_kv_store` conserva payloads crus para auditoria/reversão e tem RLS.

`shipping_labels` guarda somente metadados da etiqueta, status, serviço, preço e CEPs; não deve receber bearer token SuperFrete, CPF nem endereço completo. A tabela vincula a organização e, opcionalmente, a uma venda; a aplicação runtime desta entrega ainda guarda o estado de operação no SQLite, não grava diretamente nessa tabela.

Todas as linhas têm `organization_id`. As políticas permitem leitura e escrita autenticadas apenas para membros da organização; delete é reservado a `owner`/`admin`. A role `anon` não tem acesso às tabelas. O script de migração usa conexão direta PostgreSQL privilegiada **no servidor**, contorna RLS para carregar; não reutilize a credencial do script no cliente/browser. O runtime atual não está ligado ao Supabase nem valida tokens Supabase: integração de autenticação e persistência Supabase permanece pendente.

## Onboarding da organização

Crie primeiro usuário em Supabase Auth. A partir do SQL Editor (role administrativa), crie a organização vinculada ao UUID Auth real e a associação owner. Exemplo no `README.md` da raiz; substitua todos os placeholders. O UUID da organização será usado em `SUPABASE_ORG_ID`.

## Migração de SQLite

`python scripts/migrate_sqlite_to_supabase.py` é simulação: valida SQLite, cria backup verificável, inspeciona as chaves e monta a projeção normalizada. `--apply` é exigido para qualquer conexão/gravação no Supabase e também requer `SUPABASE_DATABASE_URL` e organização já existente. O script aborta se o dry-run encontrar erros e desfaz a transação ao detectar divergência de contagem/IDs.

A organização de destino deve estar vazia para as entidades comerciais, exceto quando se está repetindo exatamente a mesma carga idempotente. Não use o script para sincronização contínua de alterações; ele é um importador inicial, não CDC.

## Estado da validação

O parser PostgreSQL reconheceu 94 instruções do schema base atualizado e 19 do addon SuperFrete. Isso verifica sintaxe, não execução no serviço. Não havia Supabase configurado; SQL, grants e RLS não foram executados contra um Postgres real e estão pendentes de teste em duas organizações distintas antes do uso produtivo.
