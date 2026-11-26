from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .models import AnalyticsConsent, AnalyticsConsentChoice, SiteEvent, SiteEventType

User = get_user_model()

CONSENT_URL = "/api/analytics/consent/"
EVENTS_URL = "/api/analytics/events/"
LINK_URL = "/api/analytics/link/"


def _consent(choice, user=None, anonymous_id=None, version="1"):
    return AnalyticsConsent.objects.create(
        user=user, anonymous_id=anonymous_id, choice=choice, version=version
    )


@override_settings(ANALYTICS_CONSENT_VERSION="1")
class ConsentRegistrationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="consentimento@shio.com",
            name="Consentimento",
            password="senha_forte_123",
        )

    def test_visitante_registra_aceite_com_versao_vigente(self):
        response = self.client.post(
            CONSENT_URL,
            {"choice": "granted", "anonymous_id": "visitante-abc"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        consent = AnalyticsConsent.objects.get()
        self.assertIsNone(consent.user)
        self.assertEqual(consent.anonymous_id, "visitante-abc")
        self.assertEqual(consent.choice, AnalyticsConsentChoice.GRANTED)
        self.assertEqual(consent.version, "1")
        self.assertEqual(response.json()["choice"], "granted")
        self.assertEqual(response.json()["current_version"], "1")

    def test_usuario_logado_tem_escolha_associada_a_conta(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(CONSENT_URL, {"choice": "denied"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(AnalyticsConsent.objects.get().user, self.user)

    def test_visitante_sem_identificador_anonimo_recebe_400(self):
        response = self.client.post(CONSENT_URL, {"choice": "granted"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("anonymous_id", response.json())
        self.assertFalse(AnalyticsConsent.objects.exists())

    def test_escolha_invalida_recebe_400(self):
        response = self.client.post(
            CONSENT_URL,
            {"choice": "talvez", "anonymous_id": "visitante-abc"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("choice", response.json())

    def test_mudanca_de_escolha_mantem_historico(self):
        for choice in ("granted", "denied", "granted"):
            self.client.post(
                CONSENT_URL,
                {"choice": choice, "anonymous_id": "visitante-abc"},
                format="json",
            )

        self.assertEqual(AnalyticsConsent.objects.count(), 3)
        response = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-abc"})
        self.assertEqual(response.json()["choice"], "granted")

    def test_registro_exige_conta_ou_identificador_anonimo(self):
        with self.assertRaises(IntegrityError):
            _consent(AnalyticsConsentChoice.GRANTED)


@override_settings(ANALYTICS_CONSENT_VERSION="2")
class ConsentQueryTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="consulta@shio.com", name="Consulta", password="senha_forte_123"
        )

    def test_sem_escolha_registrada_retorna_choice_nulo(self):
        response = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-novo"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.json(),
            {
                "choice": None,
                "version": None,
                "current_version": "2",
                "created_at": None,
            },
        )

    def test_visitante_sem_identificador_anonimo_recebe_400(self):
        response = self.client.get(CONSENT_URL)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_retorna_versao_aceita_para_reexibir_banner(self):
        _consent(
            AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc", version="1"
        )

        body = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-abc"}).json()

        self.assertEqual(body["choice"], "granted")
        self.assertEqual(body["version"], "1")
        self.assertEqual(body["current_version"], "2")

    def test_visitante_nao_ve_escolha_de_outro_navegador(self):
        _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="outro-navegador")

        body = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-abc"}).json()

        self.assertIsNone(body["choice"])

    def test_visitante_nao_ve_escolha_vinculada_a_uma_conta(self):
        _consent(
            AnalyticsConsentChoice.GRANTED, user=self.user, anonymous_id="visitante-abc"
        )

        body = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-abc"}).json()

        self.assertIsNone(body["choice"])

    def test_usuario_logado_ve_escolha_da_conta(self):
        _consent(AnalyticsConsentChoice.DENIED, user=self.user)
        self.client.force_authenticate(user=self.user)

        body = self.client.get(CONSENT_URL).json()

        self.assertEqual(body["choice"], "denied")

    def test_usuario_logado_ve_escolha_feita_neste_navegador_antes_do_login(self):
        _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc")
        self.client.force_authenticate(user=self.user)

        body = self.client.get(CONSENT_URL, {"anonymous_id": "visitante-abc"}).json()

        self.assertEqual(body["choice"], "granted")


class ConsentRevocationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="revogacao@shio.com", name="Revogação", password="senha_forte_123"
        )
        self.other_user = User.objects.create_user(
            email="outro@shio.com", name="Outro", password="senha_forte_123"
        )

    def _event(self, user=None, anonymous_id=None):
        return SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW,
            user=user,
            anonymous_id=anonymous_id,
            path="/",
        )

    def test_visitante_recusa_e_eventos_do_navegador_sao_anonimizados(self):
        event = self._event(anonymous_id="visitante-abc")

        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "visitante-abc"},
            format="json",
        )

        event.refresh_from_db()
        self.assertIsNone(event.user)
        self.assertIsNotNone(event.anonymous_id)
        self.assertNotEqual(event.anonymous_id, "visitante-abc")

    def test_usuario_recusa_e_eventos_da_conta_e_do_navegador_sao_anonimizados(self):
        account_event = self._event(user=self.user, anonymous_id="celular")
        browser_event = self._event(anonymous_id="notebook")
        self.client.force_authenticate(user=self.user)

        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "notebook"},
            format="json",
        )

        account_event.refresh_from_db()
        browser_event.refresh_from_db()
        self.assertIsNone(account_event.user)
        self.assertIsNone(browser_event.user)
        self.assertNotEqual(account_event.anonymous_id, "celular")
        self.assertNotEqual(browser_event.anonymous_id, "notebook")
        self.assertEqual(account_event.anonymous_id, browser_event.anonymous_id)

    def test_eventos_anonimizados_continuam_contando_no_total(self):
        self._event(user=self.user)
        self._event(user=self.user)
        self.client.force_authenticate(user=self.user)

        self.client.post(CONSENT_URL, {"choice": "denied"}, format="json")

        self.assertEqual(SiteEvent.objects.count(), 2)
        self.assertFalse(SiteEvent.objects.filter(user=self.user).exists())

    def test_recusas_de_pessoas_diferentes_geram_identificadores_distintos(self):
        first = self._event(anonymous_id="visitante-a")
        second = self._event(anonymous_id="visitante-b")

        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "visitante-a"},
            format="json",
        )
        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "visitante-b"},
            format="json",
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.anonymous_id, second.anonymous_id)
        self.assertEqual(
            SiteEvent.objects.filter(user__isnull=True)
            .values("anonymous_id")
            .distinct()
            .count(),
            2,
        )

    def test_recusa_nao_altera_eventos_de_outras_pessoas(self):
        other_account = self._event(user=self.other_user, anonymous_id="visitante-abc")
        other_browser = self._event(anonymous_id="outro-navegador")

        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "visitante-abc"},
            format="json",
        )

        other_account.refresh_from_db()
        other_browser.refresh_from_db()
        self.assertEqual(other_account.user, self.other_user)
        self.assertEqual(other_account.anonymous_id, "visitante-abc")
        self.assertEqual(other_browser.anonymous_id, "outro-navegador")

    def test_aceite_nao_altera_eventos(self):
        event = self._event(user=self.user, anonymous_id="visitante-abc")
        self.client.force_authenticate(user=self.user)

        self.client.post(
            CONSENT_URL,
            {"choice": "granted", "anonymous_id": "visitante-abc"},
            format="json",
        )

        event.refresh_from_db()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.anonymous_id, "visitante-abc")


@override_settings(ANALYTICS_CONSENT_ENFORCED=False)
class ConsentNotEnforcedTests(APITestCase):
    """Verificação desligada: eventos e vínculo aceitos sem consentimento registrado."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="semflag@shio.com", name="Sem flag", password="senha_forte_123"
        )

    def test_evento_sem_consentimento_registrado_e_aceito(self):
        response = self.client.post(
            EVENTS_URL,
            {"event_type": SiteEventType.PAGE_VIEW, "anonymous_id": "visitante-abc"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SiteEvent.objects.count(), 1)

    def test_evento_apos_recusa_registrada_ainda_e_aceito(self):
        _consent(AnalyticsConsentChoice.DENIED, anonymous_id="visitante-abc")

        response = self.client.post(
            EVENTS_URL,
            {"event_type": SiteEventType.PAGE_VIEW, "anonymous_id": "visitante-abc"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_vinculo_sem_consentimento_registrado_funciona(self):
        SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW, anonymous_id="visitante-abc"
        )
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            LINK_URL, {"anonymous_id": "visitante-abc"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"linked": 1})


@override_settings(ANALYTICS_CONSENT_ENFORCED=True, ANALYTICS_CONSENT_VERSION="1")
class ConsentEnforcedEventTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="comflag@shio.com", name="Com flag", password="senha_forte_123"
        )

    def _post_event(self, anonymous_id="visitante-abc"):
        payload = {"event_type": SiteEventType.PAGE_VIEW, "path": "/"}
        if anonymous_id:
            payload["anonymous_id"] = anonymous_id
        return self.client.post(EVENTS_URL, payload, format="json")

    def test_evento_sem_consentimento_registrado_recebe_403(self):
        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SiteEvent.objects.exists())

    def test_evento_com_aceite_registrado_e_gravado(self):
        _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc")

        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SiteEvent.objects.count(), 1)

    def test_evento_apos_revogacao_recebe_403(self):
        self.client.post(
            CONSENT_URL,
            {"choice": "granted", "anonymous_id": "visitante-abc"},
            format="json",
        )
        self.client.post(
            CONSENT_URL,
            {"choice": "denied", "anonymous_id": "visitante-abc"},
            format="json",
        )

        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SiteEvent.objects.exists())

    @override_settings(ANALYTICS_CONSENT_VERSION="2")
    def test_aceite_de_versao_anterior_do_banner_recebe_403(self):
        _consent(
            AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc", version="1"
        )

        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_aceite_de_outro_navegador_nao_vale(self):
        _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="outro-navegador")

        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_usuario_logado_com_aceite_na_conta_tem_evento_gravado(self):
        _consent(AnalyticsConsentChoice.GRANTED, user=self.user)
        self.client.force_authenticate(user=self.user)

        response = self._post_event(anonymous_id=None)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SiteEvent.objects.get().user, self.user)

    def test_recusa_mais_recente_da_conta_prevalece_sobre_aceite_antigo(self):
        _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc")
        self.client.force_authenticate(user=self.user)
        self.client.post(CONSENT_URL, {"choice": "denied"}, format="json")

        response = self._post_event()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_settings(ANALYTICS_CONSENT_ENFORCED=True, ANALYTICS_CONSENT_VERSION="1")
class ConsentEnforcedLinkTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="vinculo@shio.com", name="Vínculo", password="senha_forte_123"
        )
        self.event = SiteEvent.objects.create(
            event_type=SiteEventType.PAGE_VIEW, anonymous_id="visitante-abc"
        )
        self.client.force_authenticate(user=self.user)

    def test_vinculo_sem_consentimento_recebe_403_e_nao_vincula(self):
        response = self.client.post(
            LINK_URL, {"anonymous_id": "visitante-abc"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.event.refresh_from_db()
        self.assertIsNone(self.event.user)

    def test_vinculo_com_aceite_do_navegador_vincula_eventos_sem_transferir_consentimento(
        self,
    ):
        consent = _consent(AnalyticsConsentChoice.GRANTED, anonymous_id="visitante-abc")

        response = self.client.post(
            LINK_URL, {"anonymous_id": "visitante-abc"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"linked": 1})
        self.event.refresh_from_db()
        consent.refresh_from_db()
        self.assertEqual(self.event.user, self.user)
        self.assertIsNone(consent.user)
        self.assertEqual(consent.anonymous_id, "visitante-abc")
