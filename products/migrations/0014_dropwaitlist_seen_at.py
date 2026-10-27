from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0013_dropwaitlist"),
    ]

    operations = [
        migrations.AddField(
            model_name="dropwaitlist",
            name="seen_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Quando o usuário fechou o modal de aviso de abertura do drop.",
            ),
        ),
    ]
