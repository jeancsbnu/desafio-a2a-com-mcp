"""Os dois textos de formato fixo que o agente entende. Sem LLM: so regex.

    reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
    escolha=<id da sala> | escolha=recusar

`responsavel` e o ultimo campo e pode ter espacos. O agente nao valida sala nem
horario: isso e regra de negocio, decidida pelo servidor MCP.
"""

from __future__ import annotations

import re

FORMATO_RESERVA = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
RECUSAR = "recusar"

_RESERVA = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(\S.*?)\s*$")
_ESCOLHA = re.compile(r"^escolha=(\S+)\s*$")


def reserva(texto: str) -> dict[str, str] | None:
    """Os `arguments` de `reservar_sala`, ou None se o texto nao segue o formato."""
    casamento = _RESERVA.match(texto.strip())
    if casamento is None:
        return None
    sala, inicio, fim, responsavel = casamento.groups()
    return {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}


def escolha(texto: str) -> str | None:
    """O valor de `escolha=`, ou None se o texto nao segue o formato."""
    casamento = _ESCOLHA.match(texto.strip())
    return casamento.group(1) if casamento else None
