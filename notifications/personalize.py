"""Marcador {nome} nos textos que o admin escreve (newsletter e cupom).

O admin escreve, por exemplo, "Olá, {nome}!" e cada destinatário recebe com o
próprio primeiro nome. Sem nome cadastrado (ex.: inscrito na newsletter sem
conta), o marcador some sem deixar pontuação solta:

- no meio da frase, leva a vírgula e o espaço antes: "Olá, {nome}!" -> "Olá!";
- no começo da linha, leva a pontuação depois e a frase volta a começar com
  maiúscula: "{nome}, veja!" -> "Veja!";
- sozinho na linha, a linha some; se a linha de cima termina em vírgula, a
  pontuação dele toma o lugar dela: "Olá,\n{nome}!" -> "Olá!".
"""

import re

# Nome usado na pré-visualização no lugar de {nome}.
EXAMPLE_NAME = "Maria"

_PLACEHOLDER = r"\{\s*nome\s*\}"
_ANY = re.compile(_PLACEHOLDER, re.IGNORECASE)
_ALONE = re.compile(rf"^[ \t]*{_PLACEHOLDER}[ \t]*([,.!?]?)[ \t\r]*$", re.IGNORECASE)
_LINE_START = re.compile(rf"^[ \t]*{_PLACEHOLDER}[ \t]*[,.!?]?[ \t]*", re.IGNORECASE)
_INLINE = re.compile(rf"[ \t]*,?[ \t]*{_PLACEHOLDER}", re.IGNORECASE)


def first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


def _drop_placeholder(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        alone = _ALONE.match(line)
        if alone:
            punct = alone.group(1)
            prev = lines[-1].rstrip() if lines else ""
            if punct in (".", "!", "?") and prev.endswith(","):
                lines[-1] = prev[:-1] + punct
            continue
        rest = _LINE_START.sub("", line, count=1)
        if rest != line and rest:
            rest = rest[0].upper() + rest[1:]
        lines.append(_INLINE.sub("", rest))
    return "\n".join(lines)


def fill_name(text: str, name: str | None) -> str:
    """Troca {nome} pelo primeiro nome; sem nome, remove o marcador."""
    first = first_name(name)
    if first:
        return _ANY.sub(lambda _: first, text)
    return _drop_placeholder(text)
