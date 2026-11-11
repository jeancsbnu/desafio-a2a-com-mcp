"""Sobe o servidor MCP em Streamable HTTP: `python -m servidor_mcp`."""

from __future__ import annotations

import os
import socket

import uvicorn

from servidor_mcp.app import mcp
from servidor_mcp.log import RegistrarRequests


def abrir_sockets(host: str, porta: int) -> list[socket.socket]:
    """Escuta em todos os enderecos do host, IPv4 e IPv6.

    Com `localhost`, isso cobre 127.0.0.1 e ::1. No Windows, um cliente que tenta
    ::1 primeiro e encontra a porta fechada espera cerca de 2 s antes de cair para
    o IPv4, e isso se repetiria em cada request.
    """
    sockets = []
    vistos = set()
    for familia, tipo, proto, _, endereco in socket.getaddrinfo(host, porta, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE):
        if endereco[:2] in vistos:
            continue
        vistos.add(endereco[:2])
        sock = socket.socket(familia, tipo, proto)
        if familia == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(endereco)
        sock.set_inheritable(True)
        sockets.append(sock)
    return sockets


def main() -> None:
    host = os.environ.get("MCP_HOST", "localhost")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host)
    servidor = uvicorn.Server(uvicorn.Config(RegistrarRequests(app), log_level="warning"))
    servidor.run(sockets=abrir_sockets(host, porta))


if __name__ == "__main__":
    main()
