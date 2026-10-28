# Integração InfinitePay Checkout

A integração usa o checkout hospedado da InfinitePay. O backend cria links e mantém um histórico local; os dados do cartão são tratados no checkout do provedor, não pelo JSA CRM.

## Contrato e fluxo

A documentação da conta descreve `POST https://api.checkout.infinitepay.io/links`. O corpo usa `items` (ao menos um item) com `quantity`, `price` inteiro em centavos e `description`; R$ 10,00 é enviado como `1000`. `order_nsu` associa o checkout ao registro local. O objeto `options` inclui `single_payment` e `valid_until`, e pode habilitar Pix. A solicitação é autenticada no servidor pelo header `X-Api-Key`.

Quando a API Key é enviada, a InfiniteTag/`handle` não precisa ser enviada ao endpoint `/links`; o CRM agora a omite e permite gerar links com API Key mesmo sem `INFINITEPAY_HANDLE`. O handle continua necessário no corpo de `POST /payment_check`, junto de `order_nsu`, `transaction_nsu` e `slug`, e também para a conciliação automática pelo retorno público. [1] [2]

A InfinitePay mantém cartão de crédito disponível no Checkout Integrado. Ao selecionar Pix, o CRM envia `payment_methods_enabled: ["credit_card", "pix"]`, conforme o exemplo da documentação da conta, habilitando Pix adicionalmente; isso não garante uma cobrança exclusiva por Pix. Ao selecionar cartão, o campo é omitido, pois cartão já fica disponível. A interface agora informa essa limitação e não promete “somente Pix”. [1] [2]

Ao converter um orçamento, o JSA CRM grava primeiro venda, orçamento e baixa de estoque atomicamente. Depois o backend calcula o total a partir da venda persistida e solicita o link conforme a opção indicada no CRM. Preços são convertidos para centavos no servidor; frete entra apenas quando está marcado como pago pelo cliente. Uma cobrança avulsa não cria venda nem movimenta estoque. Reenvios de uma venda com link ativo reutilizam o link; falhas de rede/timeout deixam a tentativa como incerta e nunca são repetidas automaticamente. Se a etiqueta SuperFrete for vinculada após a cobrança, o CRM mantém o valor do link já ativo e anota o custo real separadamente; não gera automaticamente um segundo link.

A consulta `POST /payment_check` recebe `handle`, `order_nsu`, `transaction_nsu` e `slug`. O CRM só marca como Pago quando a InfinitePay confirma o pagamento e o valor em centavos coincide com o valor local. Valor divergente vai para revisão. [1] [2]

## Diagnóstico da mensagem reportada

A mensagem antiga “Não foi possível confirmar a resposta da InfinitePay” era lançada no `except` de transporte do backend (`URLError`, timeout ou `OSError`). Portanto, esse texto não indicava uma rejeição dos campos `items`, `quantity`, `price` ou `options`; o código antigo ocultava se a falha era DNS, TLS, conexão ou tempo limite. A causa de rede específica daquela execução não pode ser recuperada sem os logs locais daquele momento.

O tratamento atualizado classifica DNS, TLS, conexão recusada e timeout, sem incluir conteúdo bruto da exceção, API Key, payload ou dados pessoais no log/resposta. Os logs identificam somente endpoint, tipo da causa e `errno`. Uma falha de transporte é mantida como resultado incerto, pois o provedor poderia ter recebido a solicitação antes de a conexão falhar. Antes de tentar novamente, confira se o `order_nsu` aparece no painel InfinitePay. A chamada do frontend aguarda 25 segundos, acima do timeout do proxy de 20 segundos, para que uma resposta diagnóstica do backend possa ser exibida.

## Retorno e operação local

Uma instalação publicada pode configurar `INFINITEPAY_REDIRECT_URL` com uma URL HTTPS pública terminada em `/api/infinitepay/return`. A página pública não abre o painel administrativo; aplica limite por IP, procura o `order_nsu` local e consulta novamente a API da InfinitePay antes de mostrar o resultado. O endpoint exige URL válida, API Key e handle. Em `localhost`, deixe a configuração vazia: o comprador não consegue acessar a máquina do lojista pela internet; o link hospedado ainda funciona e a consulta pode ser feita manualmente.

Webhooks não estão habilitados nesta versão: a referência consultada lista eventos e payload, mas não descreve verificação criptográfica de autenticidade. O CRM não trata webhook não autenticado como prova de pagamento. [2]

## Configuração

No `.env` do backend, configure `INFINITEPAY_API_KEY` para criar links com as opções avançadas deste projeto. Configure também `INFINITEPAY_HANDLE` para usar `payment_check` e retorno automático. Reinicie o servidor após editar. Não insira credenciais no navegador, banco de dados, relatórios ou ZIP. Para uso local em `localhost`, não use URL de retorno.

## Testes e limites

A suíte automatizada verifica request em centavos, métodos e opções de Checkout, validade, preço derivado da venda persistida, frete condicionado, proteção de segredos, criação sem handle, idempotência, erro DNS com estado incerto, tratamento de erros HTTP, recuperação confirmada de tentativa incerta e validação do valor na consulta. Os 33 testes unitários e o smoke Chromium usam mocks para chamadas externas.

Separadamente, em 28/10/2026 foram enviados quatro `POST /links` reais com a API Key fornecida: dois checkouts genéricos e dois com nome/endereço do comprador pré-preenchidos, todos por R$ 1,00, pagamento único e validade de 24 horas. Dois habilitaram Pix e dois foram criados sem a opção Pix no payload. Todos retornaram HTTP 200 e somente uma URL de Checkout; uma página confirmou o nome pré-preenchido. O checkout também solicita telefone e e-mail. O CPF não foi enviado porque não consta como campo documentado no endpoint. A API de criação de link não retorna QR/copia-e-cola; o checkout hospedado deve ser avançado pelo comprador. Depois, o usuário informou ter pago um desses links com sucesso; não há nesta atualização uma conciliação independente via `payment_check` desse pagamento. A chamada pelo terminal do sandbox expirou no handshake TLS, enquanto o navegador isolado funcionou; isso não valida a conexão TLS do backend na máquina local. Nenhuma chave, URL ou dado pessoal foi gravado nos arquivos do projeto, banco ou ZIP. Como a chave de produção foi compartilhada na conversa, rotacione-a após os testes.

## Referências oficiais

[1]: https://www.infinitepay.io/checkout-documentacao "Documentação do Checkout InfinitePay"
[2]: https://ajuda.infinitepay.io/pt-BR/articles/10766888-como-usar-o-checkout-integrado-da-infinitepay "Como usar o Checkout Integrado da InfinitePay"
[3]: https://www.infinitepay.io/desenvolvedores "Integração para desenvolvedores e InfiniteTap"
