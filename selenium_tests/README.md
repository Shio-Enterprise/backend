# Guia de Configuração e Execução de Testes E2E com Selenium

Esta suíte de testes ponta a ponta (E2E) utiliza **Python**, **Pytest** e **Selenium WebDriver** para validar os fluxos críticos da aplicação em produção no endereço `https://shiocompany.com.br`.

Os testes cobrem:
1. Homepage e links de navegação.
2. Busca e aplicação de filtros na página de categorias.
3. Seleção de variação de tamanho, controle de quantidade e inclusão no carrinho.
4. Operações de adição/subtração e recálculo no Carrinho.
5. Checkout (inserção de CEP, busca nos Correios, seleção de PIX e geração do código Pix).
6. Navegação completa por todas as sub-páginas de "Minha Conta" (Meus Dados, Meus Pedidos, Endereços).

---

## Pré-requisitos Gerais

Você precisará de:
1. **Python 3.9+** instalado na máquina.
2. **Google Chrome** instalado.
3. Não é necessário baixar o `chromedriver` manualmente; o pacote `webdriver-manager` instalado pelas dependências do Python cuida disso de forma automática.

---

## Passo 1: Instalação e Preparação

Navegue até a pasta `selenium_tests` no seu terminal e execute os comandos abaixo de acordo com seu Sistema Operacional para configurar o ambiente virtual (`venv`) e instalar as bibliotecas necessárias:

### No macOS / 🐧 No Linux
No terminal, execute:
```bash
# 1. Navegue até a pasta do projeto de testes
cd "backend/selenium_tests"

# 2. Crie o ambiente virtual (venv)
python3 -m venv venv

# 3. Ative o ambiente virtual
source venv/bin/activate

# 4. Atualize o gerenciador de pacotes e instale as dependências
pip install --upgrade pip
pip install -r requirements.txt
```

### No Windows
No PowerShell ou Command Prompt (como administrador se necessário), execute:
```powershell
# 1. Navegue até a pasta do projeto de testes (ajuste o caminho se necessário)
cd "C:\Caminho\Para\backend\selenium_tests"

# 2. Crie o ambiente virtual
python -m venv venv

# 3. Ative o ambiente virtual
# No PowerShell:
.\venv\Scripts\Activate.ps1
# No Prompt de Comando clássico (cmd):
.\venv\Scripts\activate.bat

# 4. Atualize o pip e instale as dependências
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Passo 2: Configurando a Autenticação (Para o Checkout)

Como a aplicação usa Google OAuth, automatizar a digitação do login do Google é impedido pelas diretivas de segurança da Google (que detectam o Selenium e exigem 2FA ou bloqueiam a tela). Para realizar o checkout com sucesso, você tem **duas formas de autenticação**:

### Opção A: Usar seu perfil real do Chrome já logado (Mais Fácil)
Você pode dizer ao Selenium para usar o seu navegador diário do Chrome. Assim, os cookies e o login do Google estarão ativos e o Selenium iniciará a sessão já autenticado!

Crie um arquivo `.env` na pasta `selenium_tests/` e configure o caminho do seu perfil local do Chrome:

* **No macOS:**
  ```env
  SHIO_CHROME_USER_DATA_DIR=/Users/[SEU-USER]/Library/Application Support/Google/Chrome
  SHIO_CHROME_PROFILE=Default
  ```
  *(Nota: Se você usa outro perfil além do "Default", mude o valor de `SHIO_CHROME_PROFILE` para "Profile 1", "Profile 2", etc.)*

* **No Windows:**
  ```env
  SHIO_CHROME_USER_DATA_DIR=%LOCALAPPDATA%\Google\Chrome\User Data
  SHIO_CHROME_PROFILE=Default
  ```

* **No Linux:**
  ```env
  SHIO_CHROME_USER_DATA_DIR=~/.config/google-chrome
  SHIO_CHROME_PROFILE=Default
  ```

> [!CAUTION]
> **Importante:** Se você escolher usar o seu perfil pessoal diário, **feche completamente todas as janelas do seu Chrome pessoal** antes de rodar os testes. Caso contrário, o Chrome bloqueará o acesso do Selenium por segurança, gerando o erro `SessionNotCreatedException`.
>
> **Dica (Para rodar os testes sem fechar o seu Chrome pessoal):** Você pode apontar o perfil para uma pasta nova e dedicada aos testes dentro do próprio projeto! Exemplo no seu arquivo `.env`:
> ```env
> SHIO_CHROME_USER_DATA_DIR=backend/selenium_tests/chrome_profile_teste
> SHIO_CHROME_PROFILE=Default
> ```
> Na primeira vez que você rodar os testes com essa configuração, o Chrome abrirá "limpo". Você só precisa fazer o login manualmente uma única vez na loja. Nas execuções seguintes, a sessão estará gravada de forma isolada nesta pasta e você poderá rodar os testes à vontade com a conta logada, sem nunca precisar fechar o seu navegador de uso diário!

### Opção B: Injetar o Token JWT diretamente (Ideal para CI/CD)
Se não quiser fechar seu navegador Chrome ou preferir rodar em modo silencioso (`headless`), faça login na loja no seu navegador normal, abra o Console do Desenvolvedor (F12) -> aba **Application/Armazenamento** -> **Local Storage** -> copie o valor correspondente a chave `accessToken`.

Insira este token no arquivo `.env` localizado na pasta `selenium_tests/`:
```env
SHIO_TEST_ACCESS_TOKEN=cole_seu_token_jwt_aqui
```

## 🏃 Passo 3: Executando os Testes

> [!WARNING]
> **Atenção:** Certifique-se de que o seu ambiente virtual (`venv`) está **ativo** no terminal antes de rodar os comandos. O prompt do seu terminal deve começar com `(venv)`.
>
> Se receber o erro `command not found: pytest`, reative o ambiente com o comando apropriado para a sua pasta:
> - **macOS/Linux:** `source venv/bin/activate`
> - **Windows (PowerShell):** `.\venv\Scripts\Activate.ps1`

Com o ambiente virtual ativo, execute os testes com os seguintes comandos no terminal:

### Execução padrão (Abre a tela do Chrome)
```bash
pytest test_e2e.py -v
```


### Executar em Segundo Plano (Headless - Sem abrir janela do navegador e fechando ao final)
Adicione a variável `SHIO_HEADLESS=true` antes do comando ou no arquivo `.env`:
* **No macOS/Linux:**
  ```bash
  SHIO_HEADLESS=true pytest test_e2e.py -v
  ```
* **No Windows (PowerShell):**
  ```powershell
  $env:SHIO_HEADLESS="true"
  pytest test_e2e.py -v
  $env:SHIO_HEADLESS="false" # Limpar após execução
  ```

### Gerar um Relatório HTML Visual dos resultados
Você pode gerar um arquivo HTML interativo com detalhes dos testes executados:
```bash
pytest test_e2e.py -v --html=report.html
```
Um arquivo `report.html` será criado no diretório. Basta abri-lo em qualquer navegador para visualizar o relatório gráfico.

---

## Avise-me (local)

`test_e2e_stock_alert.py` cobre o fluxo "Avise-me quando chegar" (issue #66) e **roda somente contra o front local** (qualquer outra `SHIO_TEST_URL`, como produção, faz o teste pular). Ele cria um produto novo via API a cada teste e o desativa no final.

Com back (`http://localhost:8000`) e front (`http://localhost:5173`) rodando, e tokens JWT de curta duração (administrador e cliente) gerados por você, **apenas em variáveis de ambiente**:

```bash
SHIO_HEADLESS=true SHIO_KEEP_OPEN=false \
SHIO_TEST_URL=http://localhost:5173 \
SHIO_TEST_API_URL=http://localhost:8000 \
SHIO_TEST_ADMIN_TOKEN=<jwt-admin> \
SHIO_TEST_ACCESS_TOKEN=<jwt-cliente> \
pytest test_e2e_stock_alert.py -v
```

O cliente recebe um e-mail real de reposição no endereço da conta de teste.
