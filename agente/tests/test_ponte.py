"""A ponte diante de falhas do servidor MCP, com um host falso (sem rede).

    cd agente
    uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v
"""

from __future__ import annotations

import unittest

import mcp_types as types
from mcp.shared.exceptions import MCPError

from agente import trace
from agente.host_mcp import MOTIVO_INDISPONIVEL, MOTIVO_RESPOSTA_PERDIDA, Concluido, Pergunta
from agente.ponte import Ponte, ServidorMCPIndisponivel
from agente.tarefas import COMPLETED, FAILED, INPUT_REQUIRED, Tarefas

PEDIDO = "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
PERGUNTA = Pergunta(chave="escolha_de_sala", alternativas=["sala-fusca", "sala-mirante"], request_state="opaco")


def falha(motivo: str) -> MCPError:
    return MCPError(code=types.INTERNAL_ERROR, message="falha de rede simulada", data={"motivo": motivo})


class HostFalso:
    """Responde como o servidor MCP, exceto no passo marcado para falhar."""

    def __init__(self, falhar_em: str | None = None, motivo: str = MOTIVO_RESPOSTA_PERDIDA) -> None:
        self.falhar_em = falhar_em
        self.motivo = motivo
        self.retries = 0

    def _talvez_falhar(self, passo: str) -> None:
        if passo == self.falhar_em:
            raise falha(self.motivo)

    async def listar_tools(self, t: trace.Trace) -> set[str]:
        self._talvez_falhar("listar_tools")
        return {"reservar_sala"}

    async def versao_politica(self, t: trace.Trace) -> str:
        self._talvez_falhar("versao_politica")
        return "2026-11-01"

    async def reservar(self, arguments: dict[str, str], t: trace.Trace):
        self._talvez_falhar("reservar")
        return PERGUNTA

    async def retomar(self, arguments, pergunta, resposta, t: trace.Trace):
        self.retries += 1
        if self.retries == 1:
            self._talvez_falhar("retomar")
        return Concluido({"reserva": "res-0003", "reservado": True, "sala": "sala-fusca", "inicio": "a", "fim": "b", "responsavel": "Marty"})


def texto_do_status(tarefa) -> str:
    return tarefa.mensagem_status["parts"][0]["text"]


class TestFalhasNaPonte(unittest.IsolatedAsyncioTestCase):
    async def iniciar(self, host: HostFalso):
        tarefa = Tarefas().criar(None, trace.novo())
        await Ponte(host).iniciar(tarefa, PEDIDO, tarefa.trace)
        return tarefa

    async def test_resposta_perdida_na_descoberta_diz_que_nada_foi_reservado(self) -> None:
        for passo in ("listar_tools", "versao_politica"):
            with self.subTest(passo=passo):
                tarefa = await self.iniciar(HostFalso(falhar_em=passo))
                self.assertEqual(tarefa.estado, FAILED)
                self.assertIn("nenhuma reserva foi feita", texto_do_status(tarefa))

    async def test_resposta_perdida_no_tools_call_diz_que_pode_ter_reservado(self) -> None:
        tarefa = await self.iniciar(HostFalso(falhar_em="reservar"))
        self.assertEqual(tarefa.estado, FAILED)
        self.assertIn("pode ou nao ter sido criada", texto_do_status(tarefa))

    async def test_servidor_indisponivel_na_descoberta_falha_com_texto_proprio(self) -> None:
        tarefa = await self.iniciar(HostFalso(falhar_em="listar_tools", motivo=MOTIVO_INDISPONIVEL))
        self.assertEqual(tarefa.estado, FAILED)
        self.assertIn("Servidor MCP indisponivel", texto_do_status(tarefa))

    async def test_indisponivel_no_retry_volta_a_pausa_intacta_e_depois_conclui(self) -> None:
        host = HostFalso(falhar_em="retomar", motivo=MOTIVO_INDISPONIVEL)
        tarefa = await self.iniciar(host)
        self.assertEqual((tarefa.estado, texto_do_status(tarefa)), (INPUT_REQUIRED, "alternativas: sala-fusca, sala-mirante"))
        historico = list(tarefa.historico)

        with self.assertRaises(ServidorMCPIndisponivel):
            await Ponte(host).continuar(tarefa, "escolha=sala-fusca", tarefa.trace)
        self.assertEqual(tarefa.estado, INPUT_REQUIRED)
        self.assertEqual(texto_do_status(tarefa), "alternativas: sala-fusca, sala-mirante")
        self.assertEqual(tarefa.historico, historico)
        self.assertIs(tarefa.pendencia.pergunta, PERGUNTA)

        await Ponte(host).continuar(tarefa, "escolha=sala-fusca", tarefa.trace)
        self.assertEqual(tarefa.estado, COMPLETED)
        self.assertEqual(len(tarefa.artefatos), 1)

    async def test_resposta_perdida_no_retry_termina_em_failed(self) -> None:
        host = HostFalso(falhar_em="retomar")
        tarefa = await self.iniciar(host)
        await Ponte(host).continuar(tarefa, "escolha=sala-fusca", tarefa.trace)
        self.assertEqual(tarefa.estado, FAILED)
        self.assertIn("pode ou nao ter sido criada", texto_do_status(tarefa))
        self.assertIsNone(tarefa.pendencia)


if __name__ == "__main__":
    unittest.main()
