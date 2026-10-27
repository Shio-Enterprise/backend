from django.db import migrations

TOPS = {
    "name": "Parte de cima (camisetas, moletons, jaquetas)",
    "columns": [
        {"key": "chest", "label": "Tórax"},
        {"key": "waist", "label": "Cintura"},
        {"key": "hip", "label": "Quadril"},
    ],
    "rows": [
        {
            "size": "PP",
            "measures": {"chest": [82, 88], "waist": [68, 74], "hip": [86, 92]},
        },
        {
            "size": "P",
            "measures": {"chest": [88, 94], "waist": [74, 80], "hip": [92, 98]},
        },
        {
            "size": "M",
            "measures": {"chest": [94, 100], "waist": [80, 86], "hip": [98, 104]},
        },
        {
            "size": "G",
            "measures": {"chest": [100, 106], "waist": [86, 92], "hip": [104, 110]},
        },
        {
            "size": "GG",
            "measures": {"chest": [106, 112], "waist": [92, 98], "hip": [110, 116]},
        },
        {
            "size": "XG",
            "measures": {"chest": [112, 118], "waist": [98, 104], "hip": [116, 122]},
        },
    ],
}

BOTTOMS = {
    "name": "Parte de baixo (calças, shorts, bermudas)",
    "columns": [
        {"key": "waist", "label": "Cintura"},
        {"key": "hip", "label": "Quadril"},
    ],
    "rows": [
        {"size": "PP", "measures": {"waist": [68, 74], "hip": [86, 92]}},
        {"size": "P", "measures": {"waist": [74, 80], "hip": [92, 98]}},
        {"size": "M", "measures": {"waist": [80, 86], "hip": [98, 104]}},
        {"size": "G", "measures": {"waist": [86, 92], "hip": [104, 110]}},
        {"size": "GG", "measures": {"waist": [92, 98], "hip": [110, 116]}},
        {"size": "XG", "measures": {"waist": [98, 104], "hip": [116, 122]}},
    ],
}

# Categorias existentes são criadas à mão no admin; ligamos só as óbvias.
TOPS_KEYWORDS = ("camis", "moleton", "moletom", "jaqueta", "blusa", "regata", "polo")
BOTTOMS_KEYWORDS = ("calca", "calça", "short", "bermuda")


def seed(apps, schema_editor):
    SizeGuide = apps.get_model("products", "SizeGuide")
    Category = apps.get_model("products", "Category")
    tops, _ = SizeGuide.objects.get_or_create(
        name=TOPS["name"],
        defaults={"columns": TOPS["columns"], "rows": TOPS["rows"]},
    )
    bottoms, _ = SizeGuide.objects.get_or_create(
        name=BOTTOMS["name"],
        defaults={"columns": BOTTOMS["columns"], "rows": BOTTOMS["rows"]},
    )
    for category in Category.objects.filter(size_guide__isnull=True):
        name = category.name.lower()
        if any(k in name for k in BOTTOMS_KEYWORDS):
            category.size_guide = bottoms
        elif any(k in name for k in TOPS_KEYWORDS):
            category.size_guide = tops
        else:
            continue
        category.save(update_fields=["size_guide"])


def unseed(apps, schema_editor):
    SizeGuide = apps.get_model("products", "SizeGuide")
    SizeGuide.objects.filter(name__in=[TOPS["name"], BOTTOMS["name"]]).delete()


class Migration(migrations.Migration):
    dependencies = [("products", "0010_size_guide")]

    operations = [migrations.RunPython(seed, unseed)]
