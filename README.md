# Backend

## Visão Geral

Este repositório contém o backend Django da aplicação, com autenticação JWT e suporte a login via Google OAuth. A arquitetura segue a estrutura por apps Django, mantendo a separação entre `orders`, `products` e `authentication`.

## Pré-requisitos

- Python 3.9+
- Docker e Docker Compose

## Decisões funcionais

- [DF-001: preços, variações, duplicação e estoque](docs/decisions/DF-001-produtos-precos-estoque.md)
- [Issue técnica derivada da DF-001 (texto preparado)](docs/issues/IT-001-produtos-precos-estoque.md)

- [Implementação da DF-001: uso, migração e validação](docs/IMPLEMENTACAO-DF-001.md)

## Dependências

As dependências do projeto estão em `requirements.txt`.

## Configuração do ambiente

1. Clone o repositório:

```bash
git clone https://github.com/Shio-Company/Backend.git && cd Backend
```

2. Copie o arquivo de exemplo de ambiente:

```bash
cp .env.example .env
```

3. Atualize os valores do `.env` conforme sua máquina.

4. Garanta permissões no `entrypoint.sh`:

```bash
chmod +x entrypoint.sh
```

## Comandos do dia a dia (Makefile)

Todos os targets rodam dentro do container `backend`:

| Comando | O que faz |
|---------|-----------|
| `make help` | Lista todos os targets disponíveis |
| `make run` | Sobe os containers (`docker compose up`) |
| `make migrate` | Aplica migrations pendentes |
| `make makemigrations` | Gera novas migrations |
| `make shell` | Abre o `python manage.py shell` |
| `make check` | Roda `python manage.py check` |
| `make test` | Roda a suite de testes com `pytest -v` |
| `make test-cov` | Roda testes e gera relatório HTML de coverage |
| `make lint` | Verifica `ruff check` + `ruff format` (sem alterar arquivos) |
| `make format` | Corrige com `ruff --fix` e formata com `ruff format` |
| `make schema-validate` | Valida o schema OpenAPI (`spectacular --validate --fail-on-warn`) |
| `make expire` | Libera reservas de estoque vencidas (`make expire ARGS=--dry-run` só lista) |
| `make ci` | **Gate de PR:** `lint` + `test` + `schema-validate` |
| `make install` | Reinstala `requirements.txt` no container |


## Executando com Docker

O modo recomendado é rodar a aplicação via Docker Compose.

```bash
docker compose up --build
```

O container já executa as migrações e cria o administrador automaticamente.


## Variáveis de ambiente importantes

- `SETTINGS_FILE_PATH`: caminho do settings, por exemplo `core.settings.dev`.
- `DJANGO_SECRET_KEY`: chave secreta do Django.
- `GOOGLE_CLIENT_ID`: client ID do OAuth do Google usado para validar `id_token`.
- `ADMIN_NAME`, `ADMIN_PASS`, `ADMIN_EMAIL`: credenciais para o superusuário inicial.
- `RESEND_API_KEY`, `DEFAULT_FROM_EMAIL`: envio de e-mails (veja [E-mails](#e-mails)).
- `STOCK_RESERVATION_TTL_MINUTES`, `STOCK_RESERVATION_GRACE_SECONDS`, `RESERVATION_SWEEP_INTERVAL_SECONDS`: prazos da reserva de estoque (veja [Reserva de estoque](#reserva-de-estoque)).


## Reserva de estoque

Ao finalizar a compra, o pedido nasce `AWAITING_PAYMENT` e o estoque já é baixado, com prazo de `STOCK_RESERVATION_TTL_MINUTES` (padrão 30) em `reservation_expires_at`. Se o pagamento não for confirmado no prazo, o pedido é cancelado e o estoque devolvido.

O projeto não tem fila de tarefas, então a liberação roda nas próprias requisições:

- Ao abrir o detalhe do pedido (cliente ou admin).
- Antes de validar o estoque no carrinho, na cotação e no checkout, só para as variações envolvidas.
- Numa varredura em lote na listagem pública de produtos e no carrinho, no máximo uma vez a cada `RESERVATION_SWEEP_INTERVAL_SECONDS` (padrão 60) por processo. É ela que devolve ao catálogo um produto esgotado por reserva abandonada.

A liberação automática espera `STOCK_RESERVATION_GRACE_SECONDS` (padrão 120) além do prazo, para não pegar quem está pagando no último instante. Pagamento confirmado depois da liberação mantém o pedido `CANCELED`, com o pagamento `PAID`.

Para liberar sem depender de tráfego (por exemplo, num cron):

```bash
make expire                  # libera até 100 reservas vencidas
make expire ARGS=--dry-run   # só lista o que seria liberado
python manage.py expire_stale_orders --batch-size 200
```

## Integração com os Correios

As funcionalidades de frete (preço/prazo), consulta de CEP, busca de agências,
pré-postagem (despacho) e rastreio usam a API dos Correios, que exige um contrato
comercial com cartão de postagem.

Variáveis de ambiente:

- `CORREIOS_MOCK_ENABLED`: quando `True`, todas as chamadas aos Correios retornam
  dados simulados, sem acesso à rede. Use enquanto o contrato/credenciais não
  estiverem disponíveis. O valor padrão é `False`.
- `CORREIOS_API_BASE_URL`: host base da API. Produção: `https://api.correios.com.br`.
  Homologação: `https://apihom.correios.com.br`.
- `CORREIOS_USERNAME`: login de acesso ao Meu Correios / CWS.
- `CORREIOS_PASSWORD`: código de acesso da API gerado no CWS (não é a senha de login).
- `CORREIOS_CARTAO_POSTAGEM`: número do cartão de postagem do contrato.

### Modo mock

Com `CORREIOS_MOCK_ENABLED=True`, o fluxo de checkout, pagamento e despacho funciona
de ponta a ponta sem credenciais. Os códigos de rastreio gerados nesse modo são
fictícios (ex.: `AA123456785BR`) e não existem nos Correios.

### Deploy em produção (Railway)

As variáveis de ambiente são definidas no painel do Railway, em **Variables**
(o arquivo `.env` é apenas local e não vai para produção).

- Mantenha `CORREIOS_MOCK_ENABLED` como `False` (ou não defina a variável) para não
  subir dados simulados em produção.
- Quando o contrato estiver disponível, defina no painel:

  ```
  CORREIOS_MOCK_ENABLED=False
  CORREIOS_API_BASE_URL=https://api.correios.com.br
  CORREIOS_USERNAME=<login do CWS produção>
  CORREIOS_PASSWORD=<código de acesso da API>
  CORREIOS_CARTAO_POSTAGEM=<número do cartão>
  ```

  Após salvar, o Railway faz o redeploy automaticamente.

## E-mails

O backend envia e-mails pelo [Resend](https://resend.com) usando a API HTTPS
(via `django-anymail`). Não usamos SMTP porque o Railway bloqueia SMTP de saída
nos planos Free/Trial/Hobby.

### Desenvolvimento e testes

- Sem `RESEND_API_KEY`, nenhum e-mail é enviado: o conteúdo aparece no console
  (logs do `docker compose`).
- Os testes usam o backend em memória do Django; nada sai da máquina.

### Configuração em produção (Railway)

1. Crie uma conta no Resend e adicione o domínio da loja em **Domains**.
2. Cadastre no DNS do domínio os registros SPF e DKIM indicados pelo Resend e
   aguarde a verificação.
3. Gere uma API key com permissão **Sending access**, restrita ao domínio.
4. No Railway, em **Variables**, defina:
   - `RESEND_API_KEY`: a chave gerada.
   - `DEFAULT_FROM_EMAIL`: remetente, ex.: `Shio <nao-responda@seudominio.com.br>`
     (precisa ser do domínio verificado).
5. Após o deploy, valide o envio:

```bash
python manage.py enviar_email_teste seu-email@exemplo.com
```

Se a chave não estiver definida em produção, o log do boot mostra
`RESEND_API_KEY ausente: e-mails não serão enviados, apenas exibidos no log.`

### Enviando e-mail em uma nova funcionalidade

Use sempre `send_email`; ele nunca lança exceção (falhas vão para o log e a
função retorna `False`), então não interrompe a ação do usuário.

```python
from notifications.email import send_email

send_email(
    user.email, "Seu pedido foi confirmado", "pedido_confirmado", {"pedido": order}
)
```

Crie o par de templates em `notifications/templates/emails/`
(`pedido_confirmado.html` e `pedido_confirmado.txt`), estendendo
`emails/base.html` e `emails/base.txt` e preenchendo `{% block content %}`.

## Google Auth

A autenticação Google é feita em `POST /api/auth/google/`.

O frontend deve enviar um payload JSON com o campo `id_token` retornado pelo Google Sign-In.

Exemplo:

```json
{
  "id_token": "<google-id-token>"
}
```

A resposta inclui `access`, `refresh`, `user` e `is_new_user`.

## Testes

Execute os testes com:

```bash
make test
```

Para gerar um relatório HTML de coverage em `htmlcov/`:

```bash
make test-cov
```

Os testes rodam via `pytest-django` apontando para `core.settings.test`, que usa SQLite em memória — sem necessidade do PostgreSQL ativo.

## Observações de arquitetura

- `authentication` contém o custom user model e a regra de negócio de login Google.
- `core/settings` é dividido em `base`, `dev`, `prod` e `test` (SQLite em memória, usado pelo pytest).
- `entrypoint.sh` aplica migrações e cria o administrador antes de iniciar o serviço.
