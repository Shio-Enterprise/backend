import os

import pytest
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()


@pytest.fixture(scope="session")
def base_url():
    """Retorna a URL base do site a ser testado."""
    return os.getenv("SHIO_TEST_URL", "https://shiocompany.com.br")


@pytest.fixture(scope="session")
def test_token():
    """Retorna o token JWT para injetar, se disponível."""
    return os.getenv("SHIO_TEST_ACCESS_TOKEN", None)


@pytest.fixture(scope="session")
def driver():
    """
    Inicializa e configura o WebDriver do Chrome.
    Mantém escopo de sessão para que os testes rodem na mesma janela sequencialmente.
    Suporta headless mode, keep-open e carregamento de perfis existentes.
    """
    options = webdriver.ChromeOptions()

    headless = os.getenv("SHIO_HEADLESS", "false").lower() == "true"
    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1470,950")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    keep_open = os.getenv("SHIO_KEEP_OPEN", "true").lower() == "true"
    if keep_open and not headless:
        options.add_experimental_option("detach", True)

    user_data_dir = os.getenv("SHIO_CHROME_USER_DATA_DIR", None)
    profile_dir = os.getenv("SHIO_CHROME_PROFILE", "Default")

    if user_data_dir:
        expanded_path = os.path.expanduser(user_data_dir)
        options.add_argument(f"--user-data-dir={expanded_path}")
        options.add_argument(f"--profile-directory={profile_dir}")
        print(
            f"\n[Selenium Setup] Carregando perfil do Chrome de: {expanded_path} (Perfil: {profile_dir})"
        )

    service = Service(ChromeDriverManager().install())
    chrome_driver = webdriver.Chrome(service=service, options=options)
    chrome_driver.implicitly_wait(3)

    yield chrome_driver

    if keep_open and not headless:
        print("\n" + "=" * 70)
        print("[SELENIUM] Testes finalizados!")
        print("[SELENIUM] O Chrome foi mantido aberto.")
        print("[SELENIUM] Pressione ENTER neste terminal para fechar o navegador...")
        print("=" * 70)
        try:
            import time

            input()
        except OSError:
            print(
                "\n[SELENIUM] Stdin capturado pelo pytest. Mantendo o navegador aberto."
            )
            print("[SELENIUM] Pressione Ctrl+C neste terminal para fechar o Chrome...")
            try:
                while True:
                    time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                pass
        except (KeyboardInterrupt, SystemExit, EOFError):
            pass
        chrome_driver.quit()
    else:
        chrome_driver.quit()
