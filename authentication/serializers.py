import re

from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from notifications.marketing import opt_in
from orders.models import CustomerOrder

from .admin_accounts import find_user_by_email, normalize_email, set_admin_permissions
from .admin_permissions import ADMIN_PERMISSION_CODES, ADMIN_PERMISSION_SET
from .cpf import normalize_cpf
from .models import Address, NewsletterSend, User, UserProfile
from .phone import format_phone, normalize_phone

CPF_IN_USE_MESSAGE = "Este CPF já está cadastrado em outra conta."
EMAIL_IN_USE_MESSAGE = "Este e-mail já está em uso."
EMPTY_ADMIN_PERMISSIONS_MESSAGE = (
    "Selecione ao menos uma permissão para a conta administrativa."
)
ALREADY_ADMIN_MESSAGE = "Este e-mail já pertence a um administrador."
SUPERUSER_LOCKED_MESSAGE = "Superusuários não podem ser alterados pelo painel."
INACTIVE_ACCOUNT_MESSAGE = "Esta conta está desativada e não pode virar administradora."

EMAIL_ERROR_MESSAGES = {
    "required": "Informe o e-mail.",
    "blank": "Informe o e-mail.",
    "invalid": "Informe um e-mail válido, como nome@exemplo.com.",
    "max_length": "O e-mail pode ter no máximo {max_length} caracteres.",
}
PASSWORD_REQUIRED_MESSAGES = {
    "required": "Informe a senha.",
    "blank": "Informe a senha.",
}
# Mensagens dos validadores de senha do Django, pelo código do erro (#96).
PASSWORD_VALIDATOR_MESSAGES = {
    "password_too_short": "A senha precisa ter pelo menos {min_length} caracteres.",
    "password_too_common": "Essa senha é muito comum.",
    "password_entirely_numeric": "A senha não pode ter só números.",
    "password_too_similar": "A senha está muito parecida com seus dados.",
}


def validate_password_pt(value):
    """`validate_password` com as mensagens em português, uma por motivo."""
    try:
        validate_password(value)
    except DjangoValidationError as exc:
        messages = []
        for error in exc.error_list:
            template = PASSWORD_VALIDATOR_MESSAGES.get(error.code)
            if template is None:
                messages.extend(error.messages)
            else:
                messages.append(template.format(**(error.params or {})))
        raise serializers.ValidationError(messages) from None


class UserSerializer(serializers.ModelSerializer):
    """Serializer completo do utilizador para respostas da API."""

    phone_number = serializers.CharField(
        source="profile.phone_number", allow_blank=True, allow_null=True, required=False
    )
    cpf = serializers.CharField(
        source="profile.cpf", allow_blank=True, allow_null=True, required=False
    )
    admin_permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "avatar_url",
            "is_new_user",
            "is_staff",
            "is_superuser",
            "is_admin",
            "admin_permissions",
            "phone_number",
            "cpf",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "email",
            "is_staff",
            "is_superuser",
            "is_admin",
            "admin_permissions",
            "created_at",
            "updated_at",
        ]

    def get_admin_permissions(self, obj):
        if not obj.is_admin:
            return []
        if obj.is_superuser:
            return list(ADMIN_PERMISSION_CODES)
        return sorted(
            obj.user_permissions.filter(
                content_type__app_label="authentication",
                codename__in=ADMIN_PERMISSION_CODES,
            ).values_list("codename", flat=True)
        )

    def validate_phone_number(self, value):
        return normalize_phone(value)

    def validate_cpf(self, value):
        value = normalize_cpf(value)
        if value is None:
            return None
        # Duplicados legados mantêm a pontuação até a equipe resolver o conflito.
        pattern = r"^[^0-9]*" + r"[^0-9]*".join(value) + r"[^0-9]*$"
        in_use = UserProfile.objects.filter(cpf__regex=pattern)
        if self.instance is not None:
            in_use = in_use.exclude(user=self.instance)
        if in_use.exists():
            raise serializers.ValidationError(CPF_IN_USE_MESSAGE)
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        cpf = data.get("cpf")
        if cpf and re.fullmatch(r"[0-9]{11}", cpf):
            data["cpf"] = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
        data["phone_number"] = format_phone(data.get("phone_number"))
        return data

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        # Usuário e perfil juntos: se o perfil falhar, o nome não fica salvo.
        with transaction.atomic():
            instance = super().update(instance, validated_data)

            if profile_data:
                profile, _ = UserProfile.objects.get_or_create(user=instance)
                if "phone_number" in profile_data:
                    profile.phone_number = profile_data.get("phone_number")
                if "cpf" in profile_data:
                    profile.cpf = profile_data.get("cpf")
                profile.save()
                instance.profile = profile

        return instance


class AdminAccountSerializer(serializers.ModelSerializer):
    admin_permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=ADMIN_PERMISSION_CODES),
        required=False,
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "is_active",
            "is_staff",
            "is_superuser",
            "is_admin",
            "admin_permissions",
        ]
        read_only_fields = [
            "id",
            "email",
            "name",
            "is_active",
            "is_staff",
            "is_superuser",
            "is_admin",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.is_superuser:
            data["admin_permissions"] = list(ADMIN_PERMISSION_CODES)
        else:
            data["admin_permissions"] = sorted(
                instance.user_permissions.filter(
                    content_type__app_label="authentication",
                    codename__in=ADMIN_PERMISSION_CODES,
                ).values_list("codename", flat=True)
            )
        return data

    def validate_admin_permissions(self, value):
        if not value:
            raise serializers.ValidationError(EMPTY_ADMIN_PERMISSIONS_MESSAGE)

        unknown = set(value) - ADMIN_PERMISSION_SET
        if unknown:
            raise serializers.ValidationError("Permissões administrativas inválidas.")

        request = self.context.get("request")
        if self.instance and self.instance.is_superuser:
            raise serializers.ValidationError(
                "Permissões de superusuários são implícitas e não podem ser alteradas por este painel."
            )

        if (
            request
            and self.instance
            and request.user.pk == self.instance.pk
            and "manage_admin_permissions" not in value
        ):
            raise serializers.ValidationError(
                "Você não pode remover de si mesmo a permissão de gerenciar administradores."
            )
        return value

    def update(self, instance, validated_data):
        selected_codes = validated_data.pop("admin_permissions", None)
        instance = super().update(instance, validated_data)
        if selected_codes is None:
            return instance

        set_admin_permissions(instance, selected_codes)
        return instance


class AdminAccountCreateSerializer(serializers.Serializer):
    """Promove um cliente (e-mail já cadastrado) ou cadastra uma conta nova."""

    email = serializers.EmailField()
    name = serializers.CharField(
        required=False, allow_blank=True, max_length=255, trim_whitespace=True
    )
    admin_permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=ADMIN_PERMISSION_CODES),
        allow_empty=True,
    )

    def validate_email(self, value):
        return normalize_email(value)

    def validate_admin_permissions(self, value):
        if not value:
            raise serializers.ValidationError(EMPTY_ADMIN_PERMISSIONS_MESSAGE)
        return sorted(set(value))

    def validate(self, attrs):
        user = find_user_by_email(attrs["email"])
        if user is not None:
            if user.is_superuser:
                raise serializers.ValidationError({"email": SUPERUSER_LOCKED_MESSAGE})
            if user.is_admin:
                raise serializers.ValidationError({"email": ALREADY_ADMIN_MESSAGE})
            if not user.is_active:
                raise serializers.ValidationError({"email": INACTIVE_ACCOUNT_MESSAGE})
        elif not attrs.get("name"):
            raise serializers.ValidationError(
                {"name": "Informe o nome para cadastrar uma conta nova."}
            )
        attrs["existing_user"] = user
        return attrs


class GoogleAuthSerializer(serializers.Serializer):
    """Valida o payload de login com Google."""

    id_token = serializers.CharField(
        required=True,
        help_text="Token de ID retornado pelo Google Sign-In (credencial JWT).",
    )


class AuthResponseSerializer(serializers.Serializer):
    """Contrato da resposta do endpoint de autenticação (documentação)."""

    access = serializers.CharField(read_only=True)
    refresh = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)
    is_new_user = serializers.BooleanField(read_only=True)


class PasswordLoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        if email and password:
            user = authenticate(
                request=self.context.get("request"), email=email, password=password
            )
            if not user:
                raise serializers.ValidationError(
                    "Credenciais inválidas.", code="authorization"
                )
        else:
            raise serializers.ValidationError(
                "Email e password são obrigatórios.", code="authorization"
            )

        attrs["user"] = user
        return attrs


class RegisterSerializer(serializers.ModelSerializer):
    # Declarado à mão para trocar o UniqueValidator do modelo, que compara o
    # e-mail exatamente igual, por uma busca que ignora maiúsculas (#97).
    email = serializers.EmailField(max_length=254, error_messages=EMAIL_ERROR_MESSAGES)
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password_pt],
        error_messages=PASSWORD_REQUIRED_MESSAGES,
    )
    name = serializers.CharField(
        required=True,
        max_length=255,
        error_messages={
            "required": "Informe o nome.",
            "blank": "Informe o nome.",
            "max_length": "O nome pode ter no máximo {max_length} caracteres.",
        },
    )
    marketing_opt_in = serializers.BooleanField(
        required=False, default=False, write_only=True
    )

    class Meta:
        model = User
        fields = ["email", "name", "password", "marketing_opt_in"]

    def validate_email(self, value):
        email = normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(EMAIL_IN_USE_MESSAGE, code="unique")
        return email

    def create(self, validated_data):
        marketing_opt_in = validated_data.pop("marketing_opt_in", False)
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=validated_data["email"],
                    name=validated_data["name"],
                    password=validated_data["password"],
                    is_new_user=True,
                )
        except IntegrityError:
            # Outra requisição criou a mesma conta depois da validação.
            raise serializers.ValidationError(
                {"email": [EMAIL_IN_USE_MESSAGE]}, code="unique"
            ) from None
        if marketing_opt_in:
            opt_in(user.email)
        return user


class TokenRefreshInputSerializer(serializers.Serializer):
    refresh = serializers.CharField(
        required=True,
        help_text=(
            "Refresh token JWT obtido no login. "
            "Válido por 7 dias. "
            "Após uso, o token antigo é invalidado e um novo é emitido (rotation)."
        ),
    )


class LogoutInputSerializer(serializers.Serializer):
    refresh = serializers.CharField(
        required=True,
        help_text=(
            "Refresh token JWT a invalidar. "
            "Após este pedido, o token fica na blacklist e não pode mais ser usado."
        ),
    )


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = [
            "id",
            "title",
            "zip_code",
            "street",
            "address_number",
            "complement",
            "neighborhood",
            "city",
            "state",
            "is_default",
        ]
        read_only_fields = ["id"]


class CustomerOrderHistorySerializer(serializers.ModelSerializer):
    """Serializer do histórico de compras para o CRM."""

    payment_status = serializers.CharField(source="payment.status", read_only=True)
    paid_at = serializers.DateTimeField(source="payment.paid_at", read_only=True)
    commercial_status = serializers.SerializerMethodField()

    class Meta:
        model = CustomerOrder
        fields = [
            "id",
            "status",
            "payment_status",
            "commercial_status",
            "total_amount",
            "paid_at",
            "created_at",
            "tracking_code",
        ]

    def get_commercial_status(self, obj) -> str:
        from orders.models import OrderStatus, PaymentStatus

        if (
            getattr(obj, "payment", None)
            and obj.payment.status == PaymentStatus.REFUNDED
        ):
            return "REFUNDED"
        if (
            obj.status == OrderStatus.DELIVERED
            and getattr(obj, "payment", None)
            and obj.payment.status == PaymentStatus.PAID
        ):
            return "VALID_SALE"
        return "NOT_REVENUE"


class CustomerCRMSerializer(serializers.ModelSerializer):
    """Serializer da listagem principal de clientes com métricas de CRM."""

    total_orders = serializers.IntegerField(read_only=True)
    total_spent = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    last_purchase_date = serializers.DateTimeField(read_only=True)
    is_recurring = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "created_at",
            "total_orders",
            "total_spent",
            "last_purchase_date",
            "is_recurring",
        ]

    def get_is_recurring(self, obj) -> bool:
        from orders.metrics import is_recurring_customer

        return is_recurring_customer(obj)


class CustomerCRMDetailSerializer(CustomerCRMSerializer):
    """Serializer dos detalhes do cliente, incluindo histórico completo."""

    order_history = serializers.SerializerMethodField()

    class Meta(CustomerCRMSerializer.Meta):
        fields = CustomerCRMSerializer.Meta.fields + ["order_history"]

    def get_order_history(self, obj) -> list:
        orders = obj.orders.select_related("payment").all().order_by("-created_at")
        return CustomerOrderHistorySerializer(orders, many=True).data


class NewsletterSubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    consent_lgpd = serializers.BooleanField()

    def validate_email(self, value):
        return value.strip().lower()

    def validate_consent_lgpd(self, value):
        if not value:
            raise serializers.ValidationError(
                "É necessário aceitar o consentimento para se inscrever."
            )
        return value


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)

    def validate_email(self, value):
        return normalize_email(value)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uidb64 = serializers.CharField(required=True)
    token = serializers.CharField(required=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password_pt],
        error_messages=PASSWORD_REQUIRED_MESSAGES,
    )


class NewsletterEmailMessageSerializer(serializers.Serializer):
    subject = serializers.CharField(
        max_length=255,
        required=True,
        allow_blank=False,
        error_messages={"blank": "O assunto não pode estar em branco."},
    )
    message = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=10000,
        error_messages={"blank": "A mensagem não pode estar em branco."},
    )


class NewsletterEmailPreviewSerializer(serializers.Serializer):
    subject = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="Newsletter — Shio",
    )
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=10000,
    )


class NewsletterSendHistorySerializer(serializers.ModelSerializer):
    sent_by_name = serializers.SerializerMethodField()

    class Meta:
        model = NewsletterSend
        fields = [
            "id",
            "subject",
            "message",
            "sent_at",
            "sent_by_name",
            "sent_count",
            "failed_count",
            "total_count",
            "status",
            "finished_at",
        ]

    def get_sent_by_name(self, obj) -> str | None:
        if not obj.sent_by:
            return None
        return obj.sent_by.name or obj.sent_by.email
