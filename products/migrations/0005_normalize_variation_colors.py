from django.db import migrations


COLOR_NAMES = {
    "verde": "Verde",
    "vermelho": "Vermelho",
    "amarelo": "Amarelo",
    "laranja": "Laranja",
    "ciano": "Ciano",
    "azul": "Azul",
    "roxo": "Roxo",
    "rosa": "Rosa",
    "branco": "Branco",
    "preto": "Preto",
}


def normalize_variation_colors(apps, schema_editor):
    ProductVariation = apps.get_model("products", "ProductVariation")

    for variation in ProductVariation.objects.all().iterator():
        color = (variation.color or "").strip()
        normalized_color = COLOR_NAMES.get(color.casefold())
        if normalized_color and normalized_color != variation.color:
            variation.color = normalized_color
            variation.save(update_fields=["color"])


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0004_productvariation_color"),
    ]

    operations = [
        migrations.RunPython(normalize_variation_colors, migrations.RunPython.noop),
    ]
