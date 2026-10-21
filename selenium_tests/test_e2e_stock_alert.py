"""E2E local do "Avise-me quando chegar" (issue #66).

Roda somente contra back+front locais. O setup usa a API (admin) e cria um produto
novo a cada teste; o teardown desativa o produto (nao apaga, pois ha historico).

Variaveis de ambiente: SHIO_TEST_URL (front local), SHIO_TEST_API_URL,
SHIO_TEST_ADMIN_TOKEN e SHIO_TEST_ACCESS_TOKEN (cliente).
"""

import os
import time
import uuid
from urllib.parse import urlparse

import pytest
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from test_e2e import click_visible_element, inject_auth_token, wait_for_page

ALERT_BUTTON = "//button[contains(normalize-space(.), 'Avise-me quando chegar')]"
SOLD_OUT_SIZE = "//button[contains(normalize-space(.), 'G (esgotado)')]"
NOTIFY_TIMEOUT = 30


@pytest.fixture(scope="session", autouse=True)
def e2e_env(base_url):
    """Guarda: so roda em localhost e com as variaveis necessarias (antes do driver)."""
    if urlparse(base_url).hostname not in ("localhost", "127.0.0.1"):
        pytest.skip("E2E do Avise-me roda apenas contra o front local.")
    env = {
        "api": os.getenv("SHIO_TEST_API_URL", "").rstrip("/"),
        "admin": os.getenv("SHIO_TEST_ADMIN_TOKEN"),
        "customer": os.getenv("SHIO_TEST_ACCESS_TOKEN"),
    }
    missing = [key for key, value in env.items() if not value]
    if missing:
        pytest.skip(
            "Defina SHIO_TEST_API_URL, SHIO_TEST_ADMIN_TOKEN e SHIO_TEST_ACCESS_TOKEN."
        )
    return env


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class Catalog:
    def __init__(self, env):
        self.api = f"{env['api']}/api/catalog"
        self.admin = _auth(env["admin"])
        self.customer = _auth(env["customer"])

    def create_product(self):
        response = requests.post(
            f"{self.api}/products/",
            headers=self.admin,
            json={
                "name": f"E2E Avise-me {int(time.time())}-{uuid.uuid4().hex[:6]}",
                "base_price": "50.00",
                "cost_price": "20.00",
                "is_active": True,
                "variations": [
                    {"size": "P", "stock_quantity": 2},
                    {"size": "G", "stock_quantity": 0},
                ],
            },
            timeout=15,
        )
        assert response.status_code == 201, response.text
        product = response.json()
        sizes = {v["size"]: v["id"] for v in product["variations"]}
        return product, sizes["G"]

    def deactivate(self, product):
        requests.patch(
            f"{self.api}/products/{product['id']}/",
            headers=self.admin,
            json={"is_active": False, "cost_price": product["cost_price"]},
            timeout=15,
        )

    def stock_in(self, variation_id, quantity):
        response = requests.post(
            f"{self.api}/variations/{variation_id}/stock-movements/",
            headers=self.admin,
            json={
                "kind": "ENTRADA",
                "reason": "COMPRA",
                "quantity": quantity,
                "note": "E2E Avise-me",
                "idempotency_key": f"e2e-avise-me-{uuid.uuid4()}",
            },
            timeout=15,
        )
        assert response.status_code in (200, 201), response.text

    def customer_alert(self, product_id):
        response = requests.get(
            f"{self.api}/stock-alerts/", headers=self.customer, timeout=15
        )
        assert response.status_code == 200, response.text
        for item in response.json()["results"]:
            if item["product"]["id"] == product_id:
                return item
        return None


@pytest.fixture
def catalog(e2e_env):
    return Catalog(e2e_env)


@pytest.fixture
def sold_out_product(catalog):
    product, variation_id = catalog.create_product()
    yield product, variation_id
    catalog.deactivate(product)


@pytest.fixture
def clean_browser(driver, base_url):
    """Sessao sem login antes e depois de cada teste."""
    driver.get(base_url)
    driver.execute_script("localStorage.clear();")
    yield driver
    driver.get(base_url)
    driver.execute_script("localStorage.clear();")


def open_product_and_pick_sold_out(driver, base_url, product):
    driver.get(f"{base_url}/product/{product['id']}")
    wait_for_page(driver, "ProductDetailPage")
    click_visible_element(driver, SOLD_OUT_SIZE)


def test_visitor_goes_to_login_and_back(clean_browser, base_url, sold_out_product):
    driver = clean_browser
    product, _ = sold_out_product
    open_product_and_pick_sold_out(driver, base_url, product)
    click_visible_element(driver, ALERT_BUTTON)
    WebDriverWait(driver, 10).until(EC.url_contains("/login"))
    assert "/login" in driver.current_url


def test_customer_requests_alert_and_is_notified(
    clean_browser, base_url, e2e_env, catalog, sold_out_product
):
    driver = clean_browser
    product, variation_id = sold_out_product
    inject_auth_token(driver, base_url, e2e_env["customer"])

    open_product_and_pick_sold_out(driver, base_url, product)
    click_visible_element(driver, ALERT_BUTTON)
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (By.XPATH, "//*[contains(., 'Você já será avisado')]")
        )
    )

    driver.get(f"{base_url}/my-stock-alerts")
    wait_for_page(driver, "MyStockAlertsPage")
    row = f"//li[contains(., '{product['name']}')]"
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (By.XPATH, f"{row}//*[normalize-space(text())='Aguardando']")
        )
    )

    catalog.stock_in(variation_id, 3)

    deadline = time.time() + NOTIFY_TIMEOUT
    alert = None
    while time.time() < deadline:
        alert = catalog.customer_alert(product["id"])
        if alert and alert["notified_at"]:
            break
        time.sleep(1)
    assert alert and alert["notified_at"], "Aviso nao foi marcado como enviado a tempo."

    driver.get(f"{base_url}/my-stock-alerts")
    wait_for_page(driver, "MyStockAlertsPage")
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located(
            (By.XPATH, f"{row}//*[contains(normalize-space(.), 'Avisado em')]")
        )
    )


def test_mobile_layout_shows_alert_button(clean_browser, base_url, sold_out_product):
    driver = clean_browser
    product, _ = sold_out_product
    original = driver.get_window_size()
    driver.set_window_size(390, 844)
    try:
        open_product_and_pick_sold_out(driver, base_url, product)
        button = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.XPATH, ALERT_BUTTON))
        )
        assert button.is_displayed()
        right, left, width = driver.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return [r.right, r.left, window.innerWidth];",
            button,
        )
        assert left >= 0
        assert right <= width
    finally:
        driver.set_window_size(original["width"], original["height"])
