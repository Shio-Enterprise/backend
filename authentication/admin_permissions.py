ADMIN_PERMISSION_DEFINITIONS = (
    (
        "access_admin_dashboard",
        "Acessar dashboard administrativo",
        "Dashboard",
        "Visualizar métricas, indicadores e relatórios do painel.",
    ),
    (
        "manage_catalog",
        "Gerenciar catálogo e estoque",
        "Produtos e estoque",
        "Criar, editar e remover produtos, categorias, variações, imagens e movimentações de estoque.",
    ),
    (
        "manage_drops",
        "Gerenciar drops",
        "Drops",
        "Criar, editar e remover drops e administrar os produtos vinculados a eles.",
    ),
    (
        "manage_orders",
        "Gerenciar pedidos",
        "Pedidos",
        "Consultar pedidos administrativos, alterar estados, despachar e acompanhar o fluxo operacional.",
    ),
    (
        "manage_customers",
        "Gerenciar clientes",
        "Clientes",
        "Consultar dados e histórico de clientes no CRM administrativo.",
    ),
    (
        "manage_admin_permissions",
        "Gerenciar permissões administrativas",
        "Permissões",
        "Consultar administradores e alterar as permissões administrativas de outras contas.",
    ),
)

ADMIN_PERMISSION_CODES = tuple(item[0] for item in ADMIN_PERMISSION_DEFINITIONS)
ADMIN_PERMISSION_SET = frozenset(ADMIN_PERMISSION_CODES)


def serialized_permission_definitions():
    return [
        {
            "code": code,
            "name": name,
            "label": label,
            "description": description,
        }
        for code, name, label, description in ADMIN_PERMISSION_DEFINITIONS
    ]


def assign_default_admin_permissions(user):
    """Atribui todas as permissões funcionais quando uma conta vira admin."""
    if not user or getattr(user, "is_superuser", False):
        return

    from django.contrib.auth.models import Permission

    permissions = Permission.objects.filter(
        content_type__app_label="authentication",
        codename__in=ADMIN_PERMISSION_CODES,
    )
    if permissions.exists():
        user.user_permissions.add(*permissions)
