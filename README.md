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
