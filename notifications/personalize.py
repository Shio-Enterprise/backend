"""Marcador {nome} nos textos que o admin escreve (newsletter e cupom).

O admin escreve, por exemplo, "Olá, {nome}!" e cada destinatário recebe com o
próprio primeiro nome. Sem nome cadastrado, o marcador some junto com a
vírgula ou o espaço antes dele: "Olá, {nome}!" vira "Olá!".
"""

import re

# Nome usado na pré-visualização no lugar de {nome}.
EXAMPLE_NAME = "Maria"

_PLACEHOLDER = r"\{\s*nome\s*\}"
_ANY = re.compile(_PLACEHOLDER, re.IGNORECASE)
# No começo da linha, leva junto a pontuação e o espaço depois: "{nome}, oi" -> "oi".
_LINE_START = re.compile(
    rf"^[ \t]*{_PLACEHOLDER}[ \t]*[,.!]?[ \t]*", re.IGNORECASE | re.MULTILINE
)
# No meio da frase, leva junto a vírgula e o espaço antes: "Olá, {nome}!" -> "Olá!".
_INLINE = re.compile(rf"[ \t]*,?[ \t]*{_PLACEHOLDER}", re.IGNORECASE)


def first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


def fill_name(text: str, name: str | None) -> str:
    """Troca {nome} pelo primeiro nome; sem nome, remove o marcador."""
    first = first_name(name)
    if first:
        return _ANY.sub(lambda _: first, text)
    return _INLINE.sub("", _LINE_START.sub("", text))
