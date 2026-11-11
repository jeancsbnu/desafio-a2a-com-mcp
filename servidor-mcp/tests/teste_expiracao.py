"""Teste da expiracao do requestState, com o relogio do SDK adiantado.

Sobe o servidor MCP num thread, numa porta livre, com `time.time()` do
`RequestStateBoundary` substituido por um relogio que este teste controla.
Nao precisa de REQUEST_STATE_SECRET real: gera um descartavel.

    cd servidor-mcp
    uv run python -m tests.teste_expiracao
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

os.environ["REQUEST_STATE_SECRET"] = secrets.token_hex(32)

import mcp.server.request_state as request_state  # noqa: E402
import uvicorn  # noqa: E402


class Relogio:
    """Substitui o modulo `time` dentro de `request_state`: so `time()` e usado la."""

    avanco = 0.0

    def time(self) -> float:
        return time.time() + self.avanco


RELOGIO = Relogio()
request_state.time = RELOGIO

from servidor_mcp.app import VALIDADE_REQUEST_STATE, mcp  # noqa: E402

GARAGEM = {"sala": "sala-garagem", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty"}
falhas = 0


def verificar(descricao: str, condicao: bool, detalhe: object = "") -> None:
    global falhas
    print(f"{'PASS' if condicao else 'FAIL'} {descricao}" + ("" if condicao else f": {detalhe}"))
    falhas += not condicao


def subir() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, host="127.0.0.1")
    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=porta, log_level="warning"))
    threading.Thread(target=servidor.run, daemon=True).start()
    while not servidor.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{porta}/mcp"


def reservar(url: str, extra: dict | None = None) -> dict:
    corpo = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(6),
        "method": "tools/call",
        "params": {
            "name": "reservar_sala",
            "arguments": GARAGEM,
            **(extra or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            },
        },
    }
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "reservar_sala",
    }
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def perguntar(url: str) -> tuple[str, str, list[str]]:
    resultado = reservar(url)["result"]
    chave, pedido = next(iter(resultado["inputRequests"].items()))
    return chave, resultado["requestState"], pedido["params"]["requestedSchema"]["properties"]["sala"]["enum"]


def retomar(url: str, chave: str, estado: str, sala: str) -> dict:
    return reservar(url, {"inputResponses": {chave: {"action": "accept", "content": {"sala": sala}}}, "requestState": estado})


def main() -> int:
    verificar(
        f"validade do requestState entre 5 e 30 minutos ({VALIDADE_REQUEST_STATE} s)",
        5 * 60 <= VALIDADE_REQUEST_STATE <= 30 * 60,
    )
    url = subir()

    chave, estado, alternativas = perguntar(url)
    RELOGIO.avanco += VALIDADE_REQUEST_STATE - 5
    resposta = retomar(url, chave, estado, alternativas[0])
    verificar(
        "retry 5 s antes de expirar conclui a reserva",
        (resposta.get("result") or {}).get("structuredContent", {}).get("reservado") is True,
        json.dumps(resposta)[:300],
    )

    chave, estado, alternativas = perguntar(url)
    RELOGIO.avanco += VALIDADE_REQUEST_STATE + 1
    resposta = retomar(url, chave, estado, alternativas[0])
    verificar(
        "retry depois de expirar e rejeitado com -32602",
        (resposta.get("error") or {}).get("code") == -32602,
        json.dumps(resposta)[:300],
    )
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
