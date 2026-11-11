"""O agente como servidor A2A v1.0: Agent Card e binding JSON-RPC 2.0 sobre HTTP.

So `SendMessage` e `GetTask`. Sem streaming, push notification ou cancelamento.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agente import trace as tracectx
from agente.card import agent_card
from agente.host_mcp import HostMCP
from agente.ponte import Ponte
from agente.tarefas import INPUT_REQUIRED, Tarefas
from agente.trace import Trace

# JSON-RPC 2.0
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
# A2A v1.0
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004


class ErroJsonRpc(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _texto(mensagem: dict[str, Any]) -> str:
    partes = mensagem.get("parts")
    if not isinstance(partes, list):
        raise ErroJsonRpc(INVALID_PARAMS, "message.parts precisa ser uma lista")
    texto = "\n".join(p["text"] for p in partes if isinstance(p, dict) and isinstance(p.get("text"), str))
    if not texto.strip():
        raise ErroJsonRpc(INVALID_PARAMS, "A mensagem precisa de uma parte de texto")
    return texto


def _tamanho_historico(params: dict[str, Any]) -> int | None:
    configuracao = params.get("configuration") if isinstance(params.get("configuration"), dict) else params
    valor = configuracao.get("historyLength")
    if valor is None:
        return None
    if not isinstance(valor, int) or isinstance(valor, bool) or valor < 0:
        raise ErroJsonRpc(INVALID_PARAMS, "historyLength precisa ser um inteiro >= 0")
    return valor


async def send_message(ponte: Ponte, tarefas: Tarefas, params: dict[str, Any], trace: Trace | None) -> dict[str, Any]:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict):
        raise ErroJsonRpc(INVALID_PARAMS, "params.message e obrigatorio")
    if mensagem.get("role") != "ROLE_USER":
        raise ErroJsonRpc(INVALID_PARAMS, "message.role precisa ser ROLE_USER")
    if not isinstance(mensagem.get("messageId"), str) or not mensagem["messageId"]:
        raise ErroJsonRpc(INVALID_PARAMS, "message.messageId e obrigatorio")
    texto = _texto(mensagem)
    tamanho = _tamanho_historico(params)

    tarefa_id = mensagem.get("taskId")
    if tarefa_id is None:
        # Task nova, com id proprio; o trace-id do cliente A2A vale para todos os requests MCP dela.
        tarefa = tarefas.criar(mensagem.get("contextId"), trace or tracectx.novo())
        async with tarefa.trava:
            tarefa.historico.append(dict(mensagem))
            await ponte.iniciar(tarefa, texto, tarefa.trace)
        return {"task": tarefa.para_wire(tamanho)}

    tarefa = tarefas.buscar(tarefa_id) if isinstance(tarefa_id, str) else None
    if tarefa is None:
        raise ErroJsonRpc(TASK_NOT_FOUND, f"Task nao encontrada: {tarefa_id}")
    async with tarefa.trava:
        if tarefa.terminal:
            raise ErroJsonRpc(UNSUPPORTED_OPERATION, f"A Task {tarefa.id} esta em estado terminal ({tarefa.estado}) e nao aceita mensagens")
        if tarefa.estado != INPUT_REQUIRED:
            raise ErroJsonRpc(UNSUPPORTED_OPERATION, f"A Task {tarefa.id} nao esta aguardando entrada ({tarefa.estado})")
        if mensagem.get("contextId") not in (None, tarefa.context_id):
            raise ErroJsonRpc(INVALID_PARAMS, "message.contextId nao corresponde ao da Task")
        tarefa.historico.append(dict(mensagem))
        await ponte.continuar(tarefa, texto, trace or tarefa.trace)
    return {"task": tarefa.para_wire(tamanho)}


async def get_task(ponte: Ponte, tarefas: Tarefas, params: dict[str, Any], trace: Trace | None) -> dict[str, Any]:
    tarefa_id = params.get("id")
    if not isinstance(tarefa_id, str):
        raise ErroJsonRpc(INVALID_PARAMS, "params.id e obrigatorio")
    tarefa = tarefas.buscar(tarefa_id)
    if tarefa is None:
        raise ErroJsonRpc(TASK_NOT_FOUND, f"Task nao encontrada: {tarefa_id}")
    return {"task": tarefa.para_wire(_tamanho_historico(params))}


Metodo = Callable[[Ponte, Tarefas, dict[str, Any], Trace | None], Awaitable[dict[str, Any]]]
METODOS: dict[str, Metodo] = {"SendMessage": send_message, "GetTask": get_task}


def _resposta_de_erro(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def criar_app(url_mcp: str, url_publica: str) -> Starlette:
    tarefas = Tarefas()
    card = agent_card(f"{url_publica}/a2a")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Aberto e fechado na mesma task: o transporte MCP do SDK exige isso.
        async with HostMCP(url_mcp) as host:
            app.state.ponte = Ponte(host)
            yield

    async def agent_card_json(request: Request) -> JSONResponse:
        return JSONResponse(card)

    async def jsonrpc(request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _resposta_de_erro(None, PARSE_ERROR, "JSON invalido")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str) or "id" not in corpo:
            return _resposta_de_erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC 2.0 invalido")
        id_ = corpo["id"]
        metodo = METODOS.get(corpo["method"])
        if metodo is None:
            return _resposta_de_erro(id_, METHOD_NOT_FOUND, f"Metodo nao suportado: {corpo['method']}")
        params = corpo.get("params", {})
        if not isinstance(params, dict):
            return _resposta_de_erro(id_, INVALID_PARAMS, "params precisa ser um objeto")
        trace = tracectx.do_header(request.headers.get("traceparent"))
        try:
            resultado = await metodo(request.app.state.ponte, tarefas, params, trace)
        except ErroJsonRpc as e:
            return _resposta_de_erro(id_, e.code, e.message)
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})

    app = Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card_json, methods=["GET"]),
            Route("/a2a", jsonrpc, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.tarefas = tarefas
    return app
