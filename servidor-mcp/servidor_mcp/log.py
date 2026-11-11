"""Middleware ASGI que registra cada request MCP no stderr.

Fica por fora do app do SDK para registrar tambem os requests que o transporte
recusa antes de chegar a um handler (por exemplo, `_meta` incompleto).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime


def registrar(corpo: bytes) -> None:
    try:
        mensagem = json.loads(corpo)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(f"{_agora()} mcp <- corpo invalido ({len(corpo)} bytes)", file=sys.stderr, flush=True)
        return
    for m in mensagem if isinstance(mensagem, list) else [mensagem]:
        if not isinstance(m, dict):
            continue
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        alvo = params.get("name") or params.get("uri")
        partes = [f"method={m.get('method')}", f"id={m.get('id')}"]
        if alvo:
            partes.append(f"name={alvo}")
        if "requestState" in params:
            partes.append("retry=sim")
        partes.append(f"traceparent={meta.get('traceparent', '-')}")
        print(f"{_agora()} mcp <- {' '.join(partes)}", file=sys.stderr, flush=True)


def _agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RegistrarRequests:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        pedacos = []
        while True:
            msg = await receive()
            pedacos.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        corpo = b"".join(pedacos)
        registrar(corpo)

        entregue = False

        async def reenviar():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, reenviar, send)
