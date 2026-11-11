"""Dados e regras do dominio: salas, reservas em memoria e a politica de uso.

Os arquivos de `dados/` sao lidos uma vez, na subida. As reservas criadas depois
ficam so em memoria e somem num restart, como o enunciado permite.

Este modulo nao conhece MCP: viola uma regra levantando `RegraViolada` com a
mensagem exata do enunciado, e a camada de tools traduz para `isError`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(os.environ.get("DADOS_DIR", Path(__file__).resolve().parents[2] / "dados"))

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_DE_ALTERNATIVAS = 3

SEM_ALTERNATIVAS = "Sem alternativas disponiveis no intervalo"


class RegraViolada(Exception):
    """Pedido que fere uma regra de negocio. A mensagem vai para o cliente como esta."""


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict] = _ler_json("salas.json")
RESERVAS: list[dict] = _ler_json("reservas.json")


def politica_texto() -> str:
    # Normaliza CRLF, que o git pode introduzir no checkout em Windows.
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def politica_versao() -> str:
    primeira = politica_texto().splitlines()[0]
    return primeira.split(":", 1)[1].strip()


def sala_por_id(sala_id: str) -> dict | None:
    return next((s for s in SALAS if s["id"] == sala_id), None)


def _data(texto: str) -> datetime:
    try:
        valor = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        raise RegraViolada(f"Data invalida: {texto}. Use ISO 8601 com fuso, ex. 2026-11-03T14:00:00-03:00") from None
    if valor.tzinfo is None:
        raise RegraViolada(f"Data sem fuso horario: {texto}. Use ISO 8601 com fuso, ex. 2026-11-03T14:00:00-03:00")
    return valor


def _na_janela(momento: datetime) -> bool:
    return JANELA_INICIO <= momento.astimezone(FUSO_SAO_PAULO).time() <= JANELA_FIM


def validar(sala_id: str, inicio: str, fim: str) -> tuple[dict, datetime, datetime]:
    """Aplica as regras da politica, na ordem: sala, datas, intervalo, janela, duracao."""
    sala = sala_por_id(sala_id)
    if sala is None:
        raise RegraViolada(f"Sala inexistente: {sala_id}")
    ini, fi = _data(inicio), _data(fim)
    if fi <= ini:
        raise RegraViolada("Intervalo invalido: fim deve ser posterior a inicio")
    if not (_na_janela(ini) and _na_janela(fi)):
        raise RegraViolada("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise RegraViolada("Duracao acima do limite: a politica permite no maximo 2 horas")
    return sala, ini, fi


def conflitos(sala_id: str, ini: datetime, fi: datetime) -> list[dict]:
    """Reservas da sala que se sobrepoem ao intervalo semiaberto [ini, fi)."""
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala_id and ini < datetime.fromisoformat(r["fim"]) and datetime.fromisoformat(r["inicio"]) < fi
    ]


def alternativas(sala: dict, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da pedida, por (capacidade, id), no maximo 3."""
    candidatas = [
        s
        for s in SALAS
        if s["id"] != sala["id"] and s["capacidade"] >= sala["capacidade"] and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAXIMO_DE_ALTERNATIVAS]]


def criar_reserva(sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
    numero = max((int(r["id"].removeprefix("res-")) for r in RESERVAS), default=0) + 1
    reserva = {"id": f"res-{numero:04d}", "sala": sala_id, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return reserva
