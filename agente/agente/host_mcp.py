"""O agente como host MCP: um cliente HTTP para o servidor da Central de Salas.

O loop de MRTR fica nas maos do agente (`allow_input_required=True`): o
`input_required` volta cru para quem chamou, que decide quando e com o que
fazer o retry. Nenhum callback responde a elicitation sozinho.
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx2
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher

from agente.trace import Trace

PROTOCOLO = "2026-07-28"
TOOL_RESERVA = "reservar_sala"
RESOURCE_POLITICA = "politica://uso"
TIMEOUT_HTTP = 30.0

# Marcam, no `data` do erro, as duas falhas de rede que `FalhaDeConexaoComoErro` converte.
MOTIVO_INDISPONIVEL = "servidor_mcp_indisponivel"
MOTIVO_RESPOSTA_PERDIDA = "resposta_do_servidor_mcp_perdida"

# Falhas em que o request com certeza nao chegou ao servidor.
_ANTES_DE_ENVIAR = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)

# O corpo ja vem decodificado depois de lido; esses cabecalhos deixam de valer.
_CABECALHOS_DO_CORPO_ORIGINAL = {"content-encoding", "content-length", "transfer-encoding"}


def _motivo(erro: MCPError) -> str | None:
    return erro.error.data.get("motivo") if isinstance(erro.error.data, dict) else None


def servidor_indisponivel(erro: MCPError) -> bool:
    """O request nem chegou ao servidor MCP: conexao recusada ou sem conectar a tempo."""
    return _motivo(erro) == MOTIVO_INDISPONIVEL


def resposta_perdida(erro: MCPError) -> bool:
    """O request saiu, mas a resposta nao chegou inteira: o servidor pode ter executado."""
    return _motivo(erro) == MOTIVO_RESPOSTA_PERDIDA


class FalhaDeConexaoComoErro(httpx2.AsyncBaseTransport):
    """Transforma falha de rede numa resposta 503 com erro JSON-RPC.

    Limitacao do SDK 2.3.0: uma excecao de rede no POST derruba o task group do
    `streamable_http_client`, cancela quem chamou e mata a sessao para sempre.
    Isso vale tanto antes da resposta (`post_writer` -> `_run_request_post`)
    quanto no meio do corpo, porque `_handle_json_response` so trata
    `StreamError`, e um corte de conexao e `RemoteProtocolError`. Ja uma resposta
    >= 400 com erro JSON-RPC no corpo vira `MCPError` so daquela chamada
    (`_handle_post_request`).

    Por isso o corpo JSON e lido inteiro aqui dentro, antes de chegar ao SDK. As
    respostas do servidor sao JSON pequeno, nunca streaming. Qualquer falha de rede
    vira 503, e o `data.motivo` separa "nem chegou" de "resposta perdida".
    """

    def __init__(self, interno: httpx2.AsyncBaseTransport) -> None:
        self._interno = interno

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            resposta = await self._interno.handle_async_request(request)
        except httpx2.TransportError as e:
            return _erro_de_rede(request, e)
        if not resposta.headers.get("content-type", "").lower().startswith("application/json"):
            return resposta
        try:
            corpo = await resposta.aread()
        except httpx2.TransportError as e:
            await resposta.aclose()
            return _erro_de_rede(request, e)
        cabecalhos = [(k, v) for k, v in resposta.headers.multi_items() if k.lower() not in _CABECALHOS_DO_CORPO_ORIGINAL]
        return httpx2.Response(
            resposta.status_code, headers=cabecalhos, content=corpo, request=request, extensions=resposta.extensions
        )

    async def aclose(self) -> None:
        await self._interno.aclose()


def _erro_de_rede(request: httpx2.Request, erro: httpx2.TransportError) -> httpx2.Response:
    if isinstance(erro, _ANTES_DE_ENVIAR):
        mensagem, motivo = "Servidor MCP indisponivel", MOTIVO_INDISPONIVEL
    else:
        mensagem, motivo = "Conexao com o servidor MCP caiu antes da resposta completa", MOTIVO_RESPOSTA_PERDIDA
    corpo = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": types.INTERNAL_ERROR, "message": f"{mensagem} ({type(erro).__name__})", "data": {"motivo": motivo}},
    }
    return httpx2.Response(503, json=corpo, request=request)


class SessaoSoFormulario(ClientSession):
    """Declara exatamente `{"elicitation": {"form": {}}}`.

    Sem `elicitation_callback` o SDK nao declara elicitation; com ele, declara
    form e url. O agente so sabe repassar form, e nunca responde sozinho.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        capabilities = super()._build_capabilities(version)
        return capabilities.model_copy(
            update={"elicitation": types.ElicitationCapability(form=types.FormElicitationCapability())}
        )


@dataclass(frozen=True)
class Concluido:
    """`tools/call` completo e sem erro: o `structuredContent` da tool."""

    dados: dict[str, Any]


@dataclass(frozen=True)
class FalhaDaTool:
    """`isError: true`: a mensagem da tool, como veio."""

    mensagem: str


@dataclass(frozen=True)
class Pergunta:
    """`input_required` com uma elicitation de escolha de sala.

    `request_state` e opaco: o agente guarda e devolve, nunca abre.
    """

    chave: str
    alternativas: list[str]
    request_state: str


Resultado = Concluido | FalhaDaTool | Pergunta


class RespostaInesperada(Exception):
    """O servidor respondeu algo fora do contrato da reserva."""


class HostMCP:
    """Um cliente MCP vivo durante toda a vida do agente.

    Use como `async with HostMCP(url) as host:`. Entrada e saida precisam acontecer
    na mesma task do asyncio, porque o transporte do SDK prende o seu task group a
    ela; por isso o agente abre e fecha no lifespan, e nao no primeiro request.
    Abrir nao toca no servidor: o agente sobe mesmo com o MCP fora do ar.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._sessao: ClientSession | None = None
        self._pilha = AsyncExitStack()

    async def __aenter__(self) -> HostMCP:
        http = await self._pilha.enter_async_context(
            httpx2.AsyncClient(
                timeout=httpx2.Timeout(TIMEOUT_HTTP),
                transport=FalhaDeConexaoComoErro(httpx2.AsyncHTTPTransport()),
            )
        )
        leitura, escrita = await self._pilha.enter_async_context(streamable_http_client(self.url, http_client=http))
        sessao = await self._pilha.enter_async_context(
            SessaoSoFormulario(
                dispatcher=JSONRPCDispatcher(leitura, escrita),
                client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            )
        )
        # Versao fixada: sem probe de server/discover na subida e sem estado de handshake.
        sessao.adopt(
            types.DiscoverResult(
                supported_versions=[PROTOCOLO],
                capabilities=types.ServerCapabilities(),
                result_type="complete",
                ttl_ms=0,
                cache_scope="public",
            )
        )
        declaradas = sessao._build_capabilities(PROTOCOLO).model_dump(by_alias=True, mode="json", exclude_none=True)
        print(f"host mcp: {self.url}, protocolo {PROTOCOLO}, capabilities {json.dumps(declaradas)}", file=sys.stderr, flush=True)
        self._sessao = sessao
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._sessao = None
        await self._pilha.aclose()

    @property
    def sessao(self) -> ClientSession:
        if self._sessao is None:
            raise RuntimeError("HostMCP fora do bloco async with")
        return self._sessao

    async def listar_tools(self, trace: Trace) -> set[str]:
        params = types.PaginatedRequestParams.model_validate({"_meta": {"traceparent": trace.novo_traceparent()}})
        resultado = await self.sessao.list_tools(params=params)
        return {t.name for t in resultado.tools}

    async def versao_politica(self, trace: Trace) -> str:
        """Le `politica://uso` e devolve a versao declarada na primeira linha (`versao: ...`)."""
        resultado = await self.sessao.read_resource(RESOURCE_POLITICA, meta={"traceparent": trace.novo_traceparent()})
        texto = next((c.text for c in resultado.contents if isinstance(c, types.TextResourceContents)), "")
        primeira = texto.replace("\r\n", "\n").split("\n", 1)[0]
        rotulo, _, versao = primeira.partition(":")
        if rotulo.strip() != "versao" or not versao.strip():
            raise RespostaInesperada(f"{RESOURCE_POLITICA} sem 'versao:' na primeira linha")
        return versao.strip()

    async def reservar(self, arguments: dict[str, str], trace: Trace) -> Resultado:
        resultado = await self.sessao.call_tool(
            TOOL_RESERVA, arguments, meta={"traceparent": trace.novo_traceparent()}, allow_input_required=True
        )
        return _traduzir(resultado)

    async def retomar(self, arguments: dict[str, str], pergunta: Pergunta, resposta: types.ElicitResult, trace: Trace) -> Resultado:
        """Retry do MRTR: mesmo `arguments` da 1a chamada, id novo, a mesma chave e o estado intacto."""
        resultado = await self.sessao.call_tool(
            TOOL_RESERVA,
            arguments,
            input_responses={pergunta.chave: resposta},
            request_state=pergunta.request_state,
            meta={"traceparent": trace.novo_traceparent()},
            allow_input_required=True,
        )
        return _traduzir(resultado)


def _traduzir(resultado: types.Result) -> Resultado:
    if isinstance(resultado, types.InputRequiredResult):
        return _pergunta(resultado)
    if not isinstance(resultado, types.CallToolResult):
        raise RespostaInesperada(f"resultado de tools/call de tipo {type(resultado).__name__}")
    if resultado.is_error:
        return FalhaDaTool(" ".join(c.text for c in resultado.content if isinstance(c, types.TextContent)))
    if not isinstance(resultado.structured_content, dict):
        raise RespostaInesperada("tools/call sem structuredContent")
    return Concluido(resultado.structured_content)


def _pergunta(resultado: types.InputRequiredResult) -> Pergunta:
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or resultado.request_state is None:
        raise RespostaInesperada(f"input_required com {len(pedidos)} pedidos")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, types.ElicitRequest) or not isinstance(pedido.params, types.ElicitRequestFormParams):
        raise RespostaInesperada("input_required que nao e elicitation em form mode")
    schema = pedido.params.requested_schema
    if not isinstance(schema, dict):
        schema = schema.model_dump(by_alias=True, exclude_none=True)
    campo = (schema.get("properties") or {}).get("sala") or {}
    # Com uma alternativa so, alguns geradores de schema emitem `const` em vez de `enum`.
    alternativas = list(campo["enum"]) if "enum" in campo else [campo["const"]] if "const" in campo else []
    if not alternativas:
        raise RespostaInesperada("elicitation sem as alternativas de sala")
    return Pergunta(chave=chave, alternativas=alternativas, request_state=resultado.request_state)
