from django.contrib.auth import authenticate
from django.contrib.auth.models import Permission
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from orders.models import CustomerOrder

from .admin_permissions import ADMIN_PERMISSION_CODES, ADMIN_PERMISSION_SET
from .models import Address, User, UserProfile


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

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", {})
        instance = super().update(instance, validated_data)

        if profile_data:
            profile, _ = UserProfile.objects.get_or_create(user=instance)
            if "phone_number" in profile_data:
                profile.phone_number = profile_data.get("phone_number")
            if "cpf" in profile_data:
                profile.cpf = profile_data.get("cpf")
            profile.save()

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
            raise serializers.ValidationError(
                "Selecione ao menos uma permissão para a conta administrativa."
            )

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

        managed_permissions = Permission.objects.filter(
            content_type__app_label="authentication",
            codename__in=ADMIN_PERMISSION_CODES,
        )
        instance.user_permissions.remove(*managed_permissions)
        selected_permissions = managed_permissions.filter(codename__in=selected_codes)
        instance.user_permissions.add(*selected_permissions)
        return instance


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
    password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )
    name = serializers.CharField(required=True)

    class Meta:
        model = User
        fields = ["email", "name", "password"]

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            name=validated_data["name"],
            password=validated_data["password"],
            is_new_user=True,
        )
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

    def validate_consent_lgpd(self, value):
        if not value:
            raise serializers.ValidationError(
                "É necessário aceitar o consentimento para se inscrever."
            )
        return value
