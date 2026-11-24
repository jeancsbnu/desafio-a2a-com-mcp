"""Falhas de rede com o servidor MCP: o erro e so daquela chamada, e a sessao sobrevive.

Usa um servidor HTTP falso, local, que corta a resposta no meio. Nao precisa do
servidor MCP de verdade.

    cd agente
    uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import asyncio
import json
import socket
import unittest

from mcp.shared.exceptions import MCPError

from agente import trace
from agente.host_mcp import HostMCP, resposta_perdida, servidor_indisponivel

TOOLS = {
    "tools": [{"name": "reservar_sala", "inputSchema": {"type": "object"}}],
    "resultType": "complete",
    "cacheScope": "private",
    "ttlMs": 0,
}


class ServidorFalso:
    """Responde `tools/list`; nas conexoes marcadas, manda os cabecalhos e so parte do corpo."""

    def __init__(self, cortar: int) -> None:
        self.cortar = cortar
        self.atendidas = 0

    async def atender(self, leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        cabecalho = await leitor.readuntil(b"\r\n\r\n")
        tamanho = next(
            int(linha.split(b":", 1)[1]) for linha in cabecalho.split(b"\r\n") if linha.lower().startswith(b"content-length:")
        )
        pedido = json.loads(await leitor.readexactly(tamanho))
        corpo = json.dumps({"jsonrpc": "2.0", "id": pedido["id"], "result": TOOLS}).encode()
        self.atendidas += 1
        resposta = (
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(corpo)}\r\nConnection: close\r\n\r\n"
        ).encode()
        if self.atendidas <= self.cortar:
            escritor.write(resposta + corpo[:10])  # corta no meio do corpo
        else:
            escritor.write(resposta + corpo)
        await escritor.drain()
        escritor.close()


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestFalhasDeRede(unittest.IsolatedAsyncioTestCase):
    async def test_corte_no_meio_da_resposta_vira_resposta_perdida_e_a_sessao_segue(self) -> None:
        falso = ServidorFalso(cortar=1)
        servidor = await asyncio.start_server(falso.atender, "127.0.0.1", 0)
        porta = servidor.sockets[0].getsockname()[1]
        try:
            async with HostMCP(f"http://127.0.0.1:{porta}/mcp") as host:
                with self.assertRaises(MCPError) as erro:
                    await host.listar_tools(trace.novo())
                self.assertTrue(resposta_perdida(erro.exception))
                self.assertFalse(servidor_indisponivel(erro.exception))
                # A mesma sessao continua funcionando depois do corte.
                self.assertEqual(await host.listar_tools(trace.novo()), {"reservar_sala"})
        finally:
            servidor.close()
            await servidor.wait_closed()
        self.assertEqual(falso.atendidas, 2)

    async def test_servidor_fora_do_ar_vira_indisponivel(self) -> None:
        async with HostMCP(f"http://127.0.0.1:{porta_livre()}/mcp") as host:
            with self.assertRaises(MCPError) as erro:
                await host.listar_tools(trace.novo())
            self.assertTrue(servidor_indisponivel(erro.exception))
            self.assertFalse(resposta_perdida(erro.exception))


if __name__ == "__main__":
    unittest.main()
