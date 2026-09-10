from rest_framework import serializers

from orders.models import CustomerOrder

from .models import Address, User, UserProfile
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth import authenticate


class UserSerializer(serializers.ModelSerializer):
    """Serializer completo do utilizador para respostas da API."""

    phone_number = serializers.CharField(
        source="profile.phone_number", allow_blank=True, allow_null=True, required=False
    )
    cpf = serializers.CharField(
        source="profile.cpf", allow_blank=True, allow_null=True, required=False
    )

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
            "created_at", 
            "updated_at"
        ]

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
            user = authenticate(request=self.context.get("request"), email=email, password=password)
            if not user:
                raise serializers.ValidationError("Credenciais inválidas.", code="authorization")
        else:
            raise serializers.ValidationError("Email e password são obrigatórios.", code="authorization")

        attrs["user"] = user
        return attrs


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    name = serializers.CharField(required=True)

    class Meta:
        model = User
        fields = ["email", "name", "password"]

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data["email"],
            name=validated_data["name"],
            password=validated_data["password"],
            is_new_user=True
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

    class Meta:
        model = CustomerOrder
        fields = ["id", "status", "total_amount", "created_at", "tracking_code"]


class CustomerCRMSerializer(serializers.ModelSerializer):
    """Serializer da listagem principal de clientes com métricas de CRM."""

    total_orders = serializers.IntegerField(read_only=True)
    total_spent = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    last_purchase_date = serializers.DateTimeField(read_only=True)

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
        ]


class CustomerCRMDetailSerializer(CustomerCRMSerializer):
    """Serializer dos detalhes do cliente, incluindo histórico completo."""

    order_history = serializers.SerializerMethodField()

    class Meta(CustomerCRMSerializer.Meta):
        fields = CustomerCRMSerializer.Meta.fields + ["order_history"]

    def get_order_history(self, obj) -> list:
        orders = obj.orders.all().order_by("-created_at")
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
