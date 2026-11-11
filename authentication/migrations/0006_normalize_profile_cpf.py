import re

from django.db import migrations


def valid_cpf(cpf):
    # Regra congelada aqui para a migração não depender de código futuro.
    if len(cpf) != 11 or len(set(cpf)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(cpf[i]) * (size + 1 - i) for i in range(size))
        if int(cpf[size]) != (total * 10 % 11) % 10:
            return False
    return True


def normalize_profile_cpfs(apps, schema_editor):
    Profile = apps.get_model("authentication", "UserProfile")
    profiles = Profile.objects.using(schema_editor.connection.alias)
    groups = {}
    for pk, email, value in profiles.exclude(cpf=None).values_list(
        "pk", "user__email", "cpf"
    ):
        if not value.strip():
            profiles.filter(pk=pk).update(cpf=None)
            continue
        cpf = re.sub(r"[^0-9]", "", value)
        if not valid_cpf(cpf):
            print(f"[CPF #91] CPF inválido preservado: {email} (perfil {pk})")
            continue
        groups.setdefault(cpf, []).append((pk, email))

    for cpf, accounts in groups.items():
        if len(accounts) > 1:
            print(
                "[CPF #91] CPF duplicado preservado: "
                + ", ".join(f"{email} (perfil {pk})" for pk, email in accounts)
            )
            continue
        profiles.filter(pk=accounts[0][0]).update(cpf=cpf)


class Migration(migrations.Migration):
    dependencies = [("authentication", "0005_newsletter_email_lowercase")]

    operations = [
        migrations.RunPython(normalize_profile_cpfs, migrations.RunPython.noop),
    ]
