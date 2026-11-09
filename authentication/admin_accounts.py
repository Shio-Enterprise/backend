"""Criação, promoção e remoção de administradores pelo painel (issue #81)."""

from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from notifications.email import send_email

from .admin_permissions import ADMIN_PERMISSION_CODES
from .models import User, UserProfile, UserRole


def normalize_email(email):
    return (email or "").strip().lower()


def find_user_by_email(email):
    return User.objects.filter(email__iexact=normalize_email(email)).first()


def _managed_permissions():
    return Permission.objects.filter(
        content_type__app_label="authentication",
        codename__in=ADMIN_PERMISSION_CODES,
    )


def set_admin_permissions(user, codes):
    """Troca as permissões do painel da conta pelas escolhidas."""
    managed = _managed_permissions()
    user.user_permissions.remove(*managed)
    user.user_permissions.add(*managed.filter(codename__in=codes))


def _make_admin(user, codes):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.role != UserRole.ADMIN:
        profile.role = UserRole.ADMIN
        # save() do perfil dá todas as permissões; trocamos logo abaixo.
        profile.save()
    set_admin_permissions(user, codes)


@transaction.atomic
def promote_to_admin(user, codes):
    _make_admin(user, codes)
    return user


@transaction.atomic
def create_admin(email, name, codes):
    # Sem senha: a pessoa cria a própria pelo link do e-mail.
    user = User.objects.create_user(
        email=normalize_email(email), name=name.strip(), password=None
    )
    _make_admin(user, codes)
    return user


@transaction.atomic
def revoke_admin_access(user):
    """A conta volta a ser cliente comum."""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    if profile.role != UserRole.CUSTOMER:
        profile.role = UserRole.CUSTOMER
        profile.save()
    if user.is_staff:
        user.is_staff = False
        user.save(update_fields=["is_staff", "updated_at"])
    user.user_permissions.remove(*_managed_permissions())
    return user


def build_set_password_url(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}"


def send_admin_invite(user, invited_by):
    return send_email(
        to=user.email,
        subject="Você foi convidado para o painel — Shio",
        template="admin_invite",
        context={
            "nome": user.name,
            "convidado_por": invited_by.name or invited_by.email,
            "set_password_url": build_set_password_url(user),
        },
    )
