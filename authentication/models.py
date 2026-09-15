import uuid

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("O email deve ser fornecido")

        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)

        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if not password:
            raise ValueError("Superuser must have a password.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Modelo de utilizador customizado que estende o AbstractUser do Django.
    Usa o email como identificador único em vez do username.
    Desenhado para autenticação via Google OAuth com JWT tokens.
    """

    objects = UserManager()

    username = None
    email = models.EmailField(unique=True, verbose_name="Email")
    name = models.CharField(max_length=255, verbose_name="Nome completo")
    google_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        verbose_name="Google ID",
    )
    avatar_url = models.URLField(
        max_length=500,
        null=True,
        blank=True,
        verbose_name="URL do avatar (Google)",
    )
    is_new_user = models.BooleanField(
        default=True,
        verbose_name="É um novo utilizador?",
        help_text="True no primeiro login, False nos logins subsequentes.",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Criado em")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Atualizado em")

    groups = models.ManyToManyField(
        "auth.Group",
        verbose_name=_("groups"),
        blank=True,
        help_text=_("The groups this user belongs to."),
        related_name="+",
    )
    user_permissions = models.ManyToManyField(
        "auth.Permission",
        verbose_name=_("user permissions"),
        blank=True,
        help_text=_("Specific permissions for this user."),
        related_name="+",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        verbose_name = "Utilizador"
        verbose_name_plural = "Utilizadores"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    @property
    def is_admin(self):
        """Indica se o utilizador tem um perfil administrativo no sistema."""
        if self.is_staff or self.is_superuser:
            return True

        profile = getattr(self, "profile", None)
        return profile is not None and profile.role == UserRole.ADMIN

    def get_full_name(self):
        return self.name

    def get_short_name(self):
        return self.name.split()[0] if self.name else self.email


class UserRole(models.TextChoices):
    CUSTOMER = "CUSTOMER", "Customer"
    ADMIN = "ADMIN", "Admin"
    INVENTORY_MANAGER = "INVENTORY_MANAGER", "Inventory Manager"


class UserProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    phone_number = models.CharField(max_length=20, null=True, blank=True)
    cpf = models.CharField(max_length=14, unique=True, null=True, blank=True)
    role = models.CharField(
        max_length=20, choices=UserRole.choices, default=UserRole.CUSTOMER
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Perfil de {self.user.email}"


class Address(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="addresses")
    title = models.CharField(max_length=50, default="Home")
    zip_code = models.CharField(max_length=9)
    street = models.CharField(max_length=255)
    address_number = models.CharField(max_length=20)
    complement = models.CharField(max_length=100, null=True, blank=True)
    neighborhood = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} - {self.user.email}"


class NewsletterSubscriber(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, verbose_name="Email")
    consent_lgpd = models.BooleanField(default=False, verbose_name="Consentimento LGPD")
    subscribed_at = models.DateTimeField(auto_now_add=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.email
