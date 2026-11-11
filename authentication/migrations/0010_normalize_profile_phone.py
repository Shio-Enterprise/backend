import re

from django.db import migrations


def phone_digits(value):
    # Regra congelada aqui para a migração não depender de código futuro.
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in (10, 11) or len(set(digits)) == 1:
        return None
    if "0" in digits[:2]:
        return None
    if len(digits) == 11 and digits[2] != "9":
        return None
    if len(digits) == 10 and digits[2] not in "2345":
        return None
    return digits


def normalize_profile_phones(apps, schema_editor):
    Profile = apps.get_model("authentication", "UserProfile")
    profiles = Profile.objects.using(schema_editor.connection.alias)
    for pk, email, value in profiles.exclude(phone_number=None).values_list(
        "pk", "user__email", "phone_number"
    ):
        if not value.strip():
            profiles.filter(pk=pk).update(phone_number=None)
            continue
        digits = phone_digits(value)
        if digits is None:
            print(f"[Telefone] Telefone inválido preservado: {email} (perfil {pk})")
            continue
        if digits != value:
            profiles.filter(pk=pk).update(phone_number=digits)


class Migration(migrations.Migration):
    dependencies = [
        ("authentication", "0009_merge_0006_normalize_profile_cpf_0008_manage_homepage")
    ]

    operations = [
        migrations.RunPython(normalize_profile_phones, migrations.RunPython.noop),
    ]
