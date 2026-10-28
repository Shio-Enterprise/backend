from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from products import drop_waitlist
from products.drop_waitlist import (
    notify_open_drops,
    send_drop_waitlist_emails,
    sweep_drop_openings,
)
from products.models import DropCampaign, DropWaitlist
from products.tests import make_user


def make_drop(slug="drop-verao", **kwargs):
    defaults = {
        "name": "Drop Verão",
        "is_public": True,
        "is_active": True,
        "launch_date": timezone.now() - timedelta(minutes=1),
    }
    defaults.update(kwargs)
    return DropCampaign.objects.create(slug=slug, **defaults)


class DropWaitlistSendTests(TestCase):
    def setUp(self):
        self.drop = make_drop()
        self.u1 = make_user("a@x.com", name="Ana")
        self.u2 = make_user("b@x.com", name="Beto")

    def join(self, user, drop=None, **kwargs):
        return DropWaitlist.objects.create(user=user, drop=drop or self.drop, **kwargs)

    def test_envia_para_a_lista_quando_o_drop_abre(self):
        self.join(self.u1)
        self.join(self.u2)
        self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 2)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["a@x.com", "b@x.com"])
        message = mail.outbox[0]
        self.assertIn("Drop Verão", message.subject)
        self.assertIn("/drop/drop-verao", message.body)
        self.assertFalse(DropWaitlist.objects.filter(notified_at__isnull=True).exists())

    def test_nao_envia_antes_do_launch_date(self):
        self.drop.launch_date = timezone.now() + timedelta(hours=1)
        self.drop.save()
        self.join(self.u1)
        self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 0)
        self.assertEqual(mail.outbox, [])

    def test_nao_envia_para_drop_privado_inativo_ou_encerrado(self):
        casos = [
            {"is_public": False},
            {"is_active": False},
            {"end_date": timezone.now() - timedelta(minutes=1)},
        ]
        for i, campos in enumerate(casos):
            with self.subTest(campos=campos):
                drop = make_drop(slug=f"drop-{i}", **campos)
                self.join(self.u1, drop=drop)
                self.assertEqual(send_drop_waitlist_emails(drop.pk), 0)
        self.assertEqual(mail.outbox, [])

    def test_cada_pessoa_recebe_uma_vez_so(self):
        self.join(self.u1)
        send_drop_waitlist_emails(self.drop.pk)
        self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_falha_num_envio_nao_interrompe_e_volta_para_a_fila(self):
        falha = self.join(self.u1)
        self.join(self.u2)
        with patch(
            "products.drop_waitlist.send_email",
            side_effect=lambda to, *a, **k: to != "a@x.com",
        ):
            self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 1)
        falha.refresh_from_db()
        self.assertIsNone(falha.notified_at)
        self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 1)

    def test_usuario_inativo_nao_recebe(self):
        self.u1.is_active = False
        self.u1.save()
        self.join(self.u1)
        self.assertEqual(send_drop_waitlist_emails(self.drop.pk), 0)

    def test_mesmo_usuario_nao_entra_duas_vezes_no_mesmo_drop(self):
        self.join(self.u1)
        with self.assertRaises(IntegrityError):
            self.join(self.u1)

    def test_notify_open_drops_cobre_so_os_drops_abertos(self):
        fechado = make_drop(
            slug="drop-futuro", launch_date=timezone.now() + timedelta(days=1)
        )
        self.join(self.u1)
        self.join(self.u2, drop=fechado)
        self.assertEqual(notify_open_drops(), 1)
        self.assertEqual([m.to[0] for m in mail.outbox], ["a@x.com"])


@override_settings(DROP_WAITLIST_RUN_INLINE=True)
class DropWaitlistSweepTests(TestCase):
    def setUp(self):
        drop_waitlist._last_sweep = None
        self.drop = make_drop()
        DropWaitlist.objects.create(user=make_user("a@x.com"), drop=self.drop)

    def tearDown(self):
        drop_waitlist._last_sweep = None

    def test_pagina_publica_do_drop_dispara_o_envio(self):
        response = self.client.get(f"/api/catalog/drops/slug/{self.drop.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(DROP_WAITLIST_SWEEP_INTERVAL_SECONDS=60)
    def test_varredura_respeita_o_intervalo(self):
        with patch("products.drop_waitlist.notify_open_drops") as notify:
            sweep_drop_openings()
            sweep_drop_openings()
        self.assertEqual(notify.call_count, 1)

    def test_erro_na_varredura_nao_quebra_a_requisicao(self):
        with patch(
            "products.drop_waitlist.notify_open_drops", side_effect=RuntimeError
        ):
            sweep_drop_openings()


class NotifyDropOpeningsCommandTests(TestCase):
    def setUp(self):
        self.drop = make_drop()
        DropWaitlist.objects.create(user=make_user("a@x.com"), drop=self.drop)

    def test_dry_run_so_lista(self):
        out = StringIO()
        call_command("notify_drop_openings", "--dry-run", stdout=out)
        self.assertIn("drop-verao  1 pendente(s)", out.getvalue())
        self.assertEqual(mail.outbox, [])

    def test_envia(self):
        out = StringIO()
        call_command("notify_drop_openings", stdout=out)
        self.assertIn("1 e-mail(s) enviados", out.getvalue())
        self.assertEqual(len(mail.outbox), 1)
