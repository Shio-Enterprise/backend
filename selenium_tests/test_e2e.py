import os
import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def slow_delay(seconds=1.5):
    """Adiciona um delay controlado para acompanhar a execução dos testes na tela."""
    factor = float(os.getenv("SHIO_TEST_DELAY", "2.5"))
    time.sleep(seconds * factor)


def wait_for_loading_to_disappear(driver, timeout=20):
    """Aguarda o sumiço de indicadores de carregamento ou salvamento."""
    translate_fn = "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁÂÃÉÊÍÓÔÕÚÇ', 'abcdefghijklmnopqrstuvwxyzàáâãéêíóôõúç')"
    xpath = f"//text()[contains({translate_fn}, 'carregando') or contains({translate_fn}, 'salvando')]/parent::*"

    end_time = time.time() + timeout
    time.sleep(0.4)
    while time.time() < end_time:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            visible_loading = any(el.is_displayed() for el in elements)
            if not visible_loading:
                return
        except Exception:
            pass
        time.sleep(0.3)


def wait_for_page(driver, page_name, timeout=20):
    """Aguarda até que o PageMarker correspondente à página esteja presente e o carregamento suma."""
    xpath = f"//span[text()='{page_name}']"
    el = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, xpath))
    )
    wait_for_loading_to_disappear(driver, timeout=timeout)
    slow_delay(1.5)
    return el


def find_visible_element(driver, xpath, timeout=10):
    """Aguarda e retorna o primeiro elemento correspondente ao XPath que esteja visível."""
    from selenium.common.exceptions import StaleElementReferenceException

    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    return el
        except StaleElementReferenceException:
            pass
        time.sleep(0.5)
    raise TimeoutError(
        f"Elemento visível para o XPath '{xpath}' não foi encontrado dentro de {timeout}s."
    )


def click_visible_element(driver, xpath, timeout=10):
    """Localiza o elemento visível, rola a página, destaca com borda vermelha e clica."""
    from selenium.common.exceptions import StaleElementReferenceException

    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            remaining_time = max(1.0, end_time - time.time())
            el = find_visible_element(driver, xpath, timeout=remaining_time)

            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                el,
            )
            time.sleep(0.2)

            original_style = el.get_attribute("style")
            driver.execute_script(
                "arguments[0].setAttribute('style', 'border: 3px solid #ff3333; background-color: rgba(255, 51, 51, 0.15); box-shadow: 0 0 10px #ff3333;');",
                el,
            )
            time.sleep(0.6)

            driver.execute_script(
                "arguments[0].setAttribute('style', arguments[1]);", el, original_style
            )
            WebDriverWait(driver, 5).until(EC.element_to_be_clickable(el))
            el.click()

            time.sleep(0.2)
            return el
        except StaleElementReferenceException:
            print(
                f"[Selenium] Elemento stale detectado para '{xpath}'. Tentando novamente..."
            )
            time.sleep(0.5)

    el = find_visible_element(driver, xpath, timeout=2)
    el.click()
    return el


def type_text_slowly(driver, element, text, delay=0.06):
    """Digita o texto caractere por caractere, destacando o campo de entrada com borda verde."""
    driver.execute_script(
        "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element
    )
    slow_delay(0.2)

    original_style = element.get_attribute("style")
    driver.execute_script(
        "arguments[0].setAttribute('style', 'border: 3px solid #10a545; background-color: rgba(16, 165, 69, 0.1); box-shadow: 0 0 10px #10a545;');",
        element,
    )

    element.clear()
    slow_delay(0.2)

    for char in text:
        element.send_keys(char)
        time.sleep(delay)

    slow_delay(0.4)
    driver.execute_script(
        "arguments[0].setAttribute('style', arguments[1]);", element, original_style
    )


def inject_auth_token(driver, base_url, token):
    """Injeta o token JWT no localStorage para simular login em sessões limpas."""
    driver.get(base_url)
    slow_delay(1)
    driver.execute_script("localStorage.setItem('accessToken', arguments[0]);", token)
    driver.execute_script("localStorage.setItem('access_token', arguments[0]);", token)
    driver.execute_script("localStorage.setItem('access', arguments[0]);", token)
    driver.refresh()
    slow_delay(1.5)


def check_logged_in(driver, base_url):
    """Verifica se a sessão do browser está autenticada acessando '/my-account'."""
    driver.get(f"{base_url}/my-account")
    try:
        wait_for_page(driver, "MyAccountPage", timeout=4)
        return True
    except Exception:
        return False


def navigate_via_click(driver, click_xpath, target_page_name, timeout=12):
    """Clica no elemento e aguarda a página correspondente carregar, retentando se necessário."""
    end_time = time.time() + timeout
    while time.time() < end_time:
        try:
            click_visible_element(driver, click_xpath, timeout=5)
            wait_for_page(driver, target_page_name, timeout=4)
            return
        except Exception:
            print(
                f"[Selenium] Navegação temporária para '{target_page_name}' falhou. Retentando..."
            )
            time.sleep(0.5)
    click_visible_element(driver, click_xpath, timeout=2)
    wait_for_page(driver, target_page_name, timeout=5)


def test_homepage_and_navigation(driver, base_url):
    """Cenário 1: Testa a página inicial, visibilidade do logo e links de navegação."""
    driver.get(base_url)
    wait_for_page(driver, "HomePage")

    logo = find_visible_element(driver, "//img[@alt='Shio Logo']")
    assert logo.is_displayed()
    slow_delay(1)

    navigate_via_click(driver, "//nav//a[contains(text(), 'Produtos')]", "CategoryPage")
    navigate_via_click(driver, "//img[@alt='Shio Logo']", "HomePage")
    print("✓ Cenário 1: Homepage e navegação básica executados com sucesso!")


def test_search_and_filters(driver, base_url):
    """Cenário 2: Testa a barra de busca e a aplicação de filtros de tamanho."""
    driver.get(base_url)
    wait_for_page(driver, "HomePage")

    search_input = find_visible_element(
        driver, "//form//input[@placeholder='O que você está buscando?']"
    )
    type_text_slowly(driver, search_input, "Moletom", delay=0.01)
    search_input.send_keys(Keys.ENTER)

    wait_for_page(driver, "CategoryPage")
    assert "q=Moletom" in driver.current_url

    size_buttons = driver.find_elements(By.XPATH, "//aside//button")
    if size_buttons:
        size_button = next((btn for btn in size_buttons if btn.is_displayed()), None)
        if size_button:
            size_text = size_button.text

            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                size_button,
            )
            slow_delay(0.5)
            driver.execute_script(
                "arguments[0].setAttribute('style', 'border: 3px solid #ff3333; box-shadow: 0 0 10px #ff3333;');",
                size_button,
            )
            slow_delay(1.5)
            size_button.click()

            click_visible_element(
                driver, "//button[contains(text(), 'Aplicar Filtros')]"
            )
            wait_for_loading_to_disappear(driver)
            slow_delay(2)
            print(f"✓ Cenário 2: Filtro de tamanho '{size_text}' aplicado!")
        else:
            print("⚠ Cenário 2: Encontrados botões de tamanho, mas nenhum visível.")
    else:
        print(
            "⚠ Cenário 2: Nenhum botão de filtro de tamanho foi localizado na página."
        )


def test_my_account_pages(driver, base_url, test_token):
    """Cenário 3: Navega por todas as sub-páginas internas do dashboard 'Minha Conta'."""
    if test_token:
        print("[Auth Setup] Injetando token de teste.")
        inject_auth_token(driver, base_url, test_token)

    is_auth = check_logged_in(driver, base_url)
    if not is_auth:
        pytest.skip("Cenário Minha Conta ignorado: não autenticado.")

    driver.get(base_url)
    wait_for_page(driver, "HomePage")

    navigate_via_click(driver, "//*[@data-testid='my-account-link']", "MyAccountPage")
    slow_delay(2)

    print("Navegando para Meus Pedidos...")
    navigate_via_click(
        driver, "//aside//a[contains(., 'Meus Pedidos')]", "MyOrdersPage"
    )
    slow_delay(2)

    print("Navegando para Endereços...")
    navigate_via_click(driver, "//aside//a[contains(., 'Endereços')]", "AddressesPage")
    slow_delay(2)

    print("Navegando para Novo Endereço...")
    navigate_via_click(
        driver,
        "//a[contains(., 'Novo Endereço') or contains(@href, 'new-address')]",
        "NewAddressPage",
    )
    slow_delay(2)

    print("Navegando de volta para Meus Dados...")
    navigate_via_click(driver, "//aside//a[contains(., 'Meus Dados')]", "MyAccountPage")
    slow_delay(2)

    print("✓ Cenário 3: Navegação por todas as abas da conta executada com sucesso!")


def test_admin_dashboard_navigation(driver, base_url, test_token):
    """Cenário 4: Acessa o dashboard do admin, adiciona drop e produto, e visualiza detalhes."""
    if test_token:
        inject_auth_token(driver, base_url, test_token)

    driver.get(f"{base_url}/admin/dashboard")
    try:
        wait_for_page(driver, "DashboardPage", timeout=5)
        slow_delay(1.0)
        if "admin/login" in driver.current_url:
            is_admin_auth = False
        else:
            is_admin_auth = True
    except Exception:
        is_admin_auth = False

    if not is_admin_auth:
        pytest.skip("Cenário Admin ignorado: não autenticado.")

    print("Navegando para Drops...")
    navigate_via_click(driver, "//aside//a[contains(., 'Drops')]", "DropsPage")
    slow_delay(1.5)

    print("Clicando no botão 'Novo drop'...")
    click_visible_element(driver, "//a[contains(., 'Novo drop')]")
    wait_for_page(driver, "NewDropPage")
    slow_delay(1.5)

    print("Preenchendo formulário do novo drop...")
    unique_suffix = str(int(time.time()))
    drop_name = f"Drop Teste E2E Auto {unique_suffix}"
    name_input = find_visible_element(
        driver, "//input[@placeholder='Ex: Drop Genesis']"
    )
    type_text_slowly(driver, name_input, drop_name, delay=0.01)

    date_input = find_visible_element(driver, "//input[@type='date']")
    type_text_slowly(driver, date_input, "31122026", delay=0.01)

    desc_input = find_visible_element(
        driver, "//textarea[@placeholder='Descrição do conceito do drop']"
    )
    type_text_slowly(
        driver,
        desc_input,
        "Drop conceitual criado pelo teste automatizado E2E Shio.",
        delay=0.01,
    )

    click_visible_element(
        driver, "//button[@type='submit' or contains(., 'Salvar drop')]"
    )

    print("Aguardando carregamento da tela de detalhes do drop criado...")
    wait_for_page(driver, "DropDetailsPage")
    slow_delay(2)

    print("Testando redirecionamento para o Dashboard clicando no logo Admin...")
    click_visible_element(driver, "//aside//a[contains(., 'Admin')]")
    wait_for_page(driver, "DashboardPage")
    slow_delay(1.5)

    print("Navegando para Produtos...")
    navigate_via_click(driver, "//aside//a[contains(., 'Produtos')]", "ProductsPage")
    slow_delay(1.5)

    print("Clicando no botão 'Novo produto'...")
    click_visible_element(driver, "//a[contains(., 'Novo produto')]")
    wait_for_page(driver, "NewProductPage")
    slow_delay(1.5)

    print("Preenchendo formulário do novo produto...")
    prod_name = f"Moletom Teste E2E Auto {unique_suffix}"
    prod_name_input = find_visible_element(
        driver, "//input[@placeholder='Ex: Moletom']"
    )
    type_text_slowly(driver, prod_name_input, prod_name, delay=0.01)

    price_input = find_visible_element(driver, "//input[@placeholder='0.00']")
    type_text_slowly(driver, price_input, "249.90", delay=0.01)

    stock_input = find_visible_element(driver, "//input[@placeholder='0']")
    type_text_slowly(driver, stock_input, "50", delay=0.01)

    prod_desc_input = find_visible_element(
        driver, "//textarea[@placeholder='Descrição do produto']"
    )
    type_text_slowly(
        driver,
        prod_desc_input,
        "Produto criado por script de automação E2E.",
        delay=0.01,
    )

    click_visible_element(driver, "//button[contains(., 'Salvar produto')]")

    print("Aguardando retorno para a listagem de produtos...")
    wait_for_page(driver, "ProductsPage")
    slow_delay(2)

    print("Abrindo detalhes do produto na listagem...")
    click_visible_element(driver, "(//button[contains(@aria-label, 'Ações para')])[1]")
    slow_delay(1)
    click_visible_element(driver, "//a[contains(text(), 'Ver detalhes')]")
    find_visible_element(driver, "//h2[contains(text(), 'Detalhes do Produto')]")
    slow_delay(2.5)
    click_visible_element(driver, "//aside//button[@aria-label='Fechar']")
    slow_delay(1.5)

    print("Navegando de volta ao Dashboard para testar redirecionamento...")
    click_visible_element(driver, "//aside//a[contains(., 'Dashboard')]")
    wait_for_page(driver, "DashboardPage")
    slow_delay(1.5)

    print("Navegando para Pedidos...")
    navigate_via_click(driver, "//aside//a[contains(., 'Pedidos')]", "OrdersPage")
    slow_delay(1.5)

    actions_btns = driver.find_elements(
        By.XPATH, "//button[contains(@aria-label, 'Ações para o pedido')]"
    )
    if actions_btns:
        print("Abrindo detalhes do primeiro pedido da lista...")
        click_visible_element(
            driver, "(//button[contains(@aria-label, 'Ações para o pedido')])[1]"
        )
        slow_delay(1)
        click_visible_element(driver, "//a[contains(text(), 'Ver detalhes')]")
        find_visible_element(driver, "//h2[contains(text(), 'Detalhes do Pedido')]")
        slow_delay(2.5)
        click_visible_element(driver, "//aside//button[@aria-label='Fechar']")
        slow_delay(1.5)
    else:
        print("Nenhum pedido encontrado para exibir detalhes.")

    print("Testando redirecionamento para o Dashboard clicando no logo Admin...")
    click_visible_element(driver, "//aside//a[contains(., 'Admin')]")
    wait_for_page(driver, "DashboardPage")
    slow_delay(1.5)

    print("Navegando para Clientes...")
    navigate_via_click(driver, "//aside//a[contains(., 'Clientes')]", "CustomersPage")
    slow_delay(1.5)

    print("Testando redirecionamento para o Dashboard clicando no menu lateral...")
    click_visible_element(driver, "//aside//a[contains(., 'Dashboard')]")
    wait_for_page(driver, "DashboardPage")
    slow_delay(1.5)

    metric_cards = driver.find_elements(
        By.XPATH,
        "//*[contains(text(), 'Receita') or contains(text(), 'Pedidos') or contains(text(), 'Clientes') or contains(text(), 'Estoque')]",
    )
    assert len(metric_cards) > 0, "Deveria exibir cartões de métricas no painel."
    print("✓ Métricas e fluxos de criação e detalhes do Admin validados com sucesso.")

    print("Navegando de volta para a loja...")
    navigate_via_click(
        driver, "//aside//a[contains(., 'Voltar para Loja')]", "HomePage"
    )
    slow_delay(2)

    print("✓ Cenário Admin completo executado com sucesso!")


def test_product_detail_and_add_to_cart(driver, base_url):
    """Cenário 5: Entra na página de detalhes, seleciona variação e adiciona ao carrinho."""
    driver.get(f"{base_url}/category/all")
    wait_for_page(driver, "CategoryPage")

    navigate_via_click(driver, "//article[h3]/a", "ProductDetailPage")

    size_options = WebDriverWait(driver, 5).until(
        EC.presence_of_all_elements_located(
            (
                By.XPATH,
                "//button[contains(@class, 'min-w-[80px]') and not(contains(text(), 'esgotado'))]",
            )
        )
    )
    assert len(size_options) > 0, "Sem tamanhos disponíveis para o produto."
    selected_size = size_options[0].text

    driver.execute_script(
        "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
        size_options[0],
    )
    slow_delay(0.5)
    driver.execute_script(
        "arguments[0].setAttribute('style', 'border: 3px solid #ff3333; box-shadow: 0 0 10px #ff3333;');",
        size_options[0],
    )
    slow_delay(1.5)
    size_options[0].click()

    color_selectors = driver.find_elements(
        By.XPATH, "//*[contains(text(), 'Cor') or contains(text(), 'Cores')]"
    )
    if color_selectors:
        color_buttons = driver.find_elements(
            By.XPATH,
            "//button[contains(@title, 'Cor') or contains(@class, 'color-btn')]",
        )
        if color_buttons:
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                color_buttons[0],
            )
            slow_delay(0.5)
            driver.execute_script(
                "arguments[0].setAttribute('style', 'border: 3px solid #ff3333; box-shadow: 0 0 10px #ff3333;');",
                color_buttons[0],
            )
            slow_delay(1.5)
            color_buttons[0].click()
            print("✓ Cor selecionada com sucesso.")
        else:
            print(
                "⚠ Encontrado título de cores, mas nenhum botão de seleção associado."
            )
    else:
        print("⚠ Seletor de cores não está presente na página de detalhes do produto.")

    click_visible_element(
        driver, "(//div[contains(@class, 'bg-[#f0f0f0]')]//button)[2]"
    )

    click_visible_element(driver, "//button[contains(text(), 'Adicionar ao Carrinho')]")

    try:
        success_msg = WebDriverWait(driver, 8).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(text(), 'Adicionado') or contains(text(), 'carrinho')]",
                )
            )
        )
        assert success_msg.is_displayed()
        print(
            f"✓ Cenário 5: Produto (Tamanho {selected_size}, Qtd: 2) adicionado ao carrinho!"
        )
    except Exception as e:
        print("\n--- DEBUG INFO NO CASO DE FALHA ---")
        print(
            "Texto visível na página:\n",
            driver.find_element(By.TAG_NAME, "body").text[:1000],
        )
        raise e

    navigate_via_click(driver, "//*[@data-testid='cart-link']", "CartPage")
    print("✓ Cenário 5: Entrou no carrinho clicando no ícone do header com sucesso!")


def test_cart_operations(driver, base_url, test_token):
    """Cenário 6: Valida as operações do carrinho (incremento de quantidade)."""
    if test_token:
        inject_auth_token(driver, base_url, test_token)

    is_auth = check_logged_in(driver, base_url)

    driver.get(f"{base_url}/cart")
    wait_for_page(driver, "CartPage")

    if not is_auth:
        empty_msg = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(text(), 'Seu carrinho está vazio') or contains(text(), 'entrar')]",
                )
            )
        )
        assert empty_msg.is_displayed()
        print("✓ Cenário 6: Verificado carrinho em modo deslogado.")
        return

    items = driver.find_elements(By.XPATH, "//article[contains(@class, 'grid')]")
    assert len(items) > 0, (
        "O carrinho deveria conter pelo menos um item no modo autenticado."
    )

    click_visible_element(
        driver, "(//div[contains(@class, 'bg-[#f0f0f0]')]//button)[2]"
    )
    slow_delay(2)

    print("✓ Cenário 6: Item no carrinho verificado e quantidade incrementada!")


def test_checkout_and_payment_redirect(driver, base_url, test_token):
    """Cenário 7: Executa o fluxo de finalização de compra (Checkout) e verifica redirecionamento para InfinitePay."""
    if test_token:
        print("[Auth Setup] Injetando token de teste.")
        inject_auth_token(driver, base_url, test_token)

    is_auth = check_logged_in(driver, base_url)
    if not is_auth:
        pytest.skip("Cenário 7 ignorado: não autenticado.")

    driver.get(f"{base_url}/cart")
    wait_for_page(driver, "CartPage")

    empty_msg = driver.find_elements(
        By.XPATH, "//*[contains(text(), 'Seu carrinho está vazio')]"
    )
    if empty_msg:
        print(
            "[Checkout Setup] Carrinho vazio inesperadamente. Adicionando um produto de teste..."
        )
        navigate_via_click(
            driver, "//nav//a[contains(text(), 'Produtos')]", "CategoryPage"
        )
        navigate_via_click(driver, "//article[h3]/a", "ProductDetailPage")

        size_options = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located(
                (
                    By.XPATH,
                    "//button[contains(@class, 'min-w-[80px]') and not(contains(text(), 'esgotado'))]",
                )
            )
        )
        if size_options:
            size_options[0].click()

        click_visible_element(
            driver, "//button[contains(text(), 'Adicionar ao Carrinho')]"
        )
        WebDriverWait(driver, 8).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//*[contains(text(), 'Adicionado') or contains(text(), 'carrinho')]",
                )
            )
        )
        navigate_via_click(driver, "//*[@data-testid='cart-link']", "CartPage")

    click_visible_element(driver, "//a[contains(text(), 'Finalizar compra')]")
    wait_for_page(driver, "PaymentPage")

    complete_profile_link = driver.find_elements(
        By.XPATH,
        "//a[contains(@href, '/my-account') and contains(., 'Complete seu perfil')]",
    )
    if complete_profile_link and complete_profile_link[0].is_displayed():
        print(
            "[Checkout] Perfil incompleto detectado. Indo preencher CPF e telefone..."
        )
        click_visible_element(
            driver,
            "//a[contains(@href, '/my-account') and contains(., 'Complete seu perfil')]",
        )
        wait_for_page(driver, "MyAccountPage")

        phone_input = find_visible_element(
            driver, "//input[@name='phone_number' or @placeholder='(XX) XXXXX-XXXX']"
        )
        if not phone_input.get_attribute("value"):
            type_text_slowly(driver, phone_input, "61999999999", delay=0.01)

        cpf_input = find_visible_element(
            driver, "//input[@name='cpf' or @placeholder='000.000.000-00']"
        )
        if not cpf_input.get_attribute("value"):
            type_text_slowly(driver, cpf_input, "00000000000", delay=0.01)

        click_visible_element(
            driver, "//button[contains(., 'Salvar Alterações') or @type='submit']"
        )
        wait_for_loading_to_disappear(driver)

        driver.get(f"{base_url}/cart")
        wait_for_page(driver, "CartPage")
        click_visible_element(driver, "//a[contains(text(), 'Finalizar compra')]")
        wait_for_page(driver, "PaymentPage")

    no_address_msg = driver.find_elements(
        By.XPATH, "//*[contains(text(), 'Você ainda não tem endereços cadastrados')]"
    )
    if no_address_msg and no_address_msg[0].is_displayed():
        print("[Checkout] Nenhum endereço cadastrado. Criando um novo...")
        click_visible_element(
            driver,
            "//a[contains(., 'Adicionar endereço') or contains(@href, 'new-address')]",
        )
        wait_for_page(driver, "NewAddressPage")

        title_input = find_visible_element(
            driver, "//input[@placeholder='Ex: Casa, Trabalho']"
        )
        type_text_slowly(driver, title_input, "Casa de Teste", delay=0.01)

        cep_input = find_visible_element(driver, "//input[@placeholder='00000-000']")
        type_text_slowly(driver, cep_input, "70040-010", delay=0.01)

        click_visible_element(
            driver, "//button[contains(., 'Buscar') or contains(., '...')]"
        )
        wait_for_loading_to_disappear(driver)

        num_input = find_visible_element(driver, "//input[@placeholder='123']")
        type_text_slowly(driver, num_input, "123", delay=0.01)

        street_input = find_visible_element(driver, "//input[@placeholder='Rua...']")
        if not street_input.get_attribute("value"):
            type_text_slowly(driver, street_input, "Rua de Teste E2E", delay=0.01)

        neighborhood_input = find_visible_element(
            driver, "//input[@placeholder='Bairro']"
        )
        if not neighborhood_input.get_attribute("value"):
            type_text_slowly(driver, neighborhood_input, "Asa Norte", delay=0.01)

        city_input = find_visible_element(driver, "//input[@placeholder='Cidade']")
        if not city_input.get_attribute("value"):
            type_text_slowly(driver, city_input, "Brasília", delay=0.01)

        state_input = find_visible_element(driver, "//input[@placeholder='SP']")
        if not state_input.get_attribute("value"):
            type_text_slowly(driver, state_input, "DF", delay=0.01)

        click_visible_element(
            driver, "//button[@type='submit' or contains(., 'Salvar Endereço')]"
        )
        wait_for_page(driver, "PaymentPage")
    else:
        print("[Checkout] Usando endereço cadastrado existente.")
        address_radios = driver.find_elements(
            By.XPATH, "//input[@type='radio' and @name='address']"
        )
        if address_radios:
            if not address_radios[0].is_selected():
                driver.execute_script("arguments[0].click();", address_radios[0])
                slow_delay(1.0)

    click_visible_element(
        driver,
        "//button[contains(., 'Continuar para Pagamento') or contains(., 'Confirmar Pagamento')]",
    )
    wait_for_loading_to_disappear(driver)

    click_visible_element(driver, "//button[.//span[text()='PIX']]")
    click_visible_element(driver, "//button[contains(text(), 'Revisar Pedido')]")
    wait_for_loading_to_disappear(driver)

    click_visible_element(
        driver,
        "//button[contains(., 'Finalizar Compra') or contains(., 'Processando')]",
    )

    print("Aguardando redirecionamento para o checkout da InfinitePay...")
    WebDriverWait(driver, 20).until(
        EC.url_contains("checkout.infinitepay.io/shiocompany")
    )
    assert "checkout.infinitepay.io/shiocompany" in driver.current_url
    print("✓ Sucesso: Compra finalizada e redirecionada para a InfinitePay!")
