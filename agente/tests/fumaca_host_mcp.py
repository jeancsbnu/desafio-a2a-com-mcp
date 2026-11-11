"""Fumaca do host MCP, sem A2A: descobre, le a politica e fecha o ciclo de MRTR na mao.

Com o servidor MCP recem-iniciado:

    cd agente
    uv run python -m tests.fumaca_host_mcp
"""

from __future__ import annotations

import asyncio
import os
import sys

import mcp_types as types

from agente import trace
from agente.host_mcp import TOOL_RESERVA, Concluido, FalhaDaTool, HostMCP, Pergunta

URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
falhas = 0


def verificar(descricao: str, condicao: bool, detalhe: object = "") -> None:
    global falhas
    print(f"{'PASS' if condicao else 'FAIL'} {descricao}" + ("" if condicao else f": {detalhe}"))
    falhas += not condicao


def pedido(sala: str, inicio: str, fim: str, responsavel: str = "Doc") -> dict[str, str]:
    return {"sala": sala, "inicio": f"2026-11-03T{inicio}:00-03:00", "fim": f"2026-11-03T{fim}:00-03:00", "responsavel": responsavel}


async def main() -> int:
    t = trace.novo()
    print(f"trace-id: {t.trace_id}")
    async with HostMCP(URL) as host:
        tools = await host.listar_tools(t)
        verificar(f"tools/list descobre {TOOL_RESERVA}", TOOL_RESERVA in tools, sorted(tools))

        versao = await host.versao_politica(t)
        verificar("versao lida de politica://uso", versao == "2026-11-01", versao)

        r = await host.reservar(pedido("sala-aquario", "09:00", "10:00"), t)
        verificar("sala livre conclui", isinstance(r, Concluido) and r.dados.get("reservado") is True, r)

        garagem = pedido("sala-garagem", "14:00", "15:00", "Marty")
        r = await host.reservar(garagem, t)
        verificar("conflito vira Pergunta", isinstance(r, Pergunta) and r.alternativas == ["sala-fusca", "sala-mirante"], r)
        if isinstance(r, Pergunta):
            r = await host.retomar(garagem, r, types.ElicitResult(action="accept", content={"sala": "sala-fusca"}), t)
            verificar("retry com accept reserva na escolhida", isinstance(r, Concluido) and r.dados.get("sala") == "sala-fusca", r)

        r = await host.reservar(garagem, t)
        verificar("conflito de novo, agora com uma alternativa", isinstance(r, Pergunta) and r.alternativas == ["sala-mirante"], r)
        if isinstance(r, Pergunta):
            r = await host.retomar(garagem, r, types.ElicitResult(action="decline"), t)
            verificar("retry com decline conclui sem reservar", isinstance(r, Concluido) and r.dados.get("reservado") is False, r)

        r = await host.reservar(pedido("sala-delorean", "09:00", "10:00"), t)
        verificar("sala inexistente vira FalhaDaTool", isinstance(r, FalhaDaTool) and "Sala inexistente: sala-delorean" in r.mensagem, r)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
