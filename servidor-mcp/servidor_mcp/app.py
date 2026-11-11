"""Servidor MCP da Central de Salas: tools, resource e protecao do requestState."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

from servidor_mcp import dominio

# Validade do requestState: o enunciado exige entre 5 e 30 minutos.
VALIDADE_REQUEST_STATE = 10 * 60

# Chave do unico pedido em inputRequests. O cliente devolve a mesma no inputResponses.
CHAVE_ESCOLHA = "escolha_de_sala"
VERSAO_ESTADO = 1


def _segredo() -> str:
    # strip: um \r ou \n sobrando no export muda a chave e invalida os estados ja emitidos.
    segredo = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not segredo:
        sys.exit(
            "REQUEST_STATE_SECRET nao definido. Gere um com:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return segredo


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=VALIDADE_REQUEST_STATE),
)


@contextmanager
def _regras() -> Iterator[None]:
    """Traduz uma regra violada no dominio em erro de execucao da tool (isError)."""
    try:
        yield
    except dominio.RegraViolada as e:
        raise ToolError(str(e)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
async def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    with _regras():
        _, ini, fi = dominio.validar(sala, inicio, fim)
    conflitos = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in dominio.conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not conflitos, conflitos=conflitos)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
async def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is not None:
        return _retomar(ctx)

    with _regras():
        sala_pedida, ini, fi = dominio.validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return _reservar(sala, inicio, fim, responsavel)
    pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    return _perguntar(ctx, pedido, dominio.alternativas(sala_pedida, ini, fi))


def _reservar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = dominio.criar_reserva(sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=dominio.politica_versao(),
    )


def _perguntar(ctx: Context, pedido: dict, alternativas: list[str]) -> InputRequiredResult:
    """Termina a resposta pedindo a escolha. O pedido inteiro viaja selado no requestState."""
    # Sem alternativa nao ha o que perguntar, entao a capability nem entra em jogo.
    if not alternativas:
        raise ToolError(dominio.SEM_ALTERNATIVAS)
    _exigir_elicitation_form(ctx)
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": alternativas,
                    }
                },
                "required": ["sala"],
            },
        )
    )
    # O RequestStateBoundary do SDK sela este JSON (AES-GCM, com expiracao e vinculo ao request).
    estado = json.dumps({"v": VERSAO_ESTADO, **pedido, "alternativas": alternativas})
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: pergunta}, request_state=estado)


def _exigir_elicitation_form(ctx: Context) -> None:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # `elicitation: {}`, sem modos, e a forma anterior aos modos e vale como form.
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by tool 'reservar_sala'",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Retry do MRTR: o pedido vem do requestState, ja verificado pelo SDK, e nao dos arguments."""
    try:
        estado = json.loads(ctx.request_state)
        if estado.get("v") != VERSAO_ESTADO:
            raise ValueError
        pedido = {k: estado[k] for k in ("sala", "inicio", "fim", "responsavel")}
        oferecidas = list(estado["alternativas"])
    except (ValueError, KeyError, TypeError):
        raise MCPError(code=INVALID_PARAMS, message="requestState com formato desconhecido") from None

    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None:
        raise MCPError(code=INVALID_PARAMS, message=f"inputResponses sem a chave {CHAVE_ESCOLHA!r}")
    acao = getattr(resposta, "action", None)
    if acao in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado")
    escolha = (getattr(resposta, "content", None) or {}).get("sala")
    if acao != "accept" or escolha not in oferecidas:
        raise MCPError(code=INVALID_PARAMS, message="Escolha fora das alternativas oferecidas")

    with _regras():
        sala_pedida, ini, fi = dominio.validar(pedido["sala"], pedido["inicio"], pedido["fim"])
    if dominio.conflitos(escolha, ini, fi):
        # A escolhida foi ocupada entre a pergunta e o retry: pergunta de novo com o que sobrou.
        return _perguntar(ctx, pedido, dominio.alternativas(sala_pedida, ini, fi))
    return _reservar(escolha, pedido["inicio"], pedido["fim"], pedido["responsavel"])


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas. A primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return dominio.politica_texto()
