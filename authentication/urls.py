from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AddressDetailView,
    AddressListCreateView,
    CustomerCRMViewSet,
    GoogleLoginView,
    LogoutView,
    MeView,
    TokenRefreshView,
    RegisterView,
    PasswordLoginView,
)

app_name = "authentication"

router = DefaultRouter()
router.register(r"crm/customers", CustomerCRMViewSet, basename="crm-customers")

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
]

urlpatterns += router.urls
