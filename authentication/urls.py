from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AddressDetailView,
    AddressListCreateView,
    AdminAccountViewSet,
    CustomerCRMViewSet,
    GoogleLoginView,
    LogoutView,
    MeView,
    NewsletterSubscribeView,
    PasswordLoginView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegisterView,
    TokenRefreshView,
)

app_name = "authentication"

router = DefaultRouter()
router.register(r"crm/customers", CustomerCRMViewSet, basename="crm-customers")
router.register(r"admins", AdminAccountViewSet, basename="admins")

urlpatterns = [
    # POST - Registo de novo utilizador
    path("register/", RegisterView.as_view(), name="register"),
    # POST - Login com email e senha
    path("login/", PasswordLoginView.as_view(), name="login"),
    # POST - Recebe id_token do Google e retorna JWT + dados do user
    path("google/", GoogleLoginView.as_view(), name="google-login"),
    # POST - Renova o access token com o refresh token
    path("token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    # POST - Invalida o refresh token (logout)
    path("logout/", LogoutView.as_view(), name="logout"),
    # GET - Retorna dados do utilizador autenticado
    path("me/", MeView.as_view(), name="me"),
    # GET/POST - Lista e cria endereços do utilizador
    path("addresses/", AddressListCreateView.as_view(), name="addresses"),
    # PATCH/DELETE - Atualiza ou remove um endereço específico
    path("addresses/<uuid:pk>/", AddressDetailView.as_view(), name="address-detail"),
    # POST - Inscrição pública na newsletter (consentimento LGPD obrigatório)
    path(
        "newsletter/subscribe/",
        NewsletterSubscribeView.as_view(),
        name="newsletter-subscribe",
    ),
    # POST - Solicitação de redefinição de senha
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    # POST - Confirmação da nova senha
    path(
        "password-reset-confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
]

urlpatterns += router.urls
