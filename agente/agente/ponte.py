"""A ponte entre as duas camadas de estado: o MRTR do MCP e a Task do A2A.

Ida (`_aplicar`): um `input_required` do servidor MCP vira uma pausa da Task em
`TASK_STATE_INPUT_REQUIRED`, com o `requestState` guardado na propria Task.

Volta (`continuar`): a resposta do cliente A2A vira um retry do `tools/call`
original, com id de JSON-RPC novo, a mesma chave do `inputRequests`, o mesmo
`arguments` e o `requestState` ecoado sem modificacao.

O agente traduz protocolo, nao dominio: conflito, politica e alternativas sao
decisao do servidor MCP. A unica checagem daqui e se a escolha esta no `enum`
que o proprio servidor mandou.
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from collections.abc import Awaitable

import mcp_types as types
from mcp.shared.exceptions import MCPError

from agente import pedido
from agente.host_mcp import TOOL_RESERVA, Concluido, FalhaDaTool, HostMCP, Pergunta, RespostaInesperada, Resultado
from agente.tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pendencia, Tarefa, novo_id
from agente.trace import Trace

# Teto de pausas por Task: o servidor pode perguntar de novo se a sala escolhida
# for ocupada entre a pergunta e o retry.
MAXIMO_DE_RODADAS = 5

# O SDK do servidor prefixa a mensagem de erro da tool; o historico mostra a da tool.
_PREFIXO_DO_SDK = re.compile(r"^Error executing tool \S+: ")


def linha_de_alternativas(pergunta: Pergunta) -> str:
    return "alternativas: " + ", ".join(pergunta.alternativas)


class Ponte:
    def __init__(self, host: HostMCP) -> None:
        self.host = host

    async def iniciar(self, tarefa: Tarefa, texto: str, trace: Trace) -> None:
        """Primeiro `SendMessage` da Task: descobre, le a politica e pede a reserva."""
        tarefa.mudar(WORKING)
        arguments = pedido.reserva(texto)
        if arguments is None:
            tarefa.mudar(FAILED, f"Pedido invalido. Use: {pedido.FORMATO_RESERVA}")
            return
        await self._protegido(tarefa, self._reservar(tarefa, arguments, trace))

    async def _reservar(self, tarefa: Tarefa, arguments: dict[str, str], trace: Trace) -> None:
        # Descoberta em runtime, antes da primeira chamada, em vez de lista fixa.
        if TOOL_RESERVA not in await self.host.listar_tools(trace):
            tarefa.mudar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
            return
        tarefa.politica = await self.host.versao_politica(trace)
        resultado = await self.host.reservar(arguments, trace)
        self._aplicar(tarefa, arguments, resultado, rodada=1)

    async def continuar(self, tarefa: Tarefa, texto: str, trace: Trace) -> None:
        """`SendMessage` com `taskId` de uma Task pausada: a volta da ponte."""
        pendencia = tarefa.pendencia
        assert pendencia is not None and tarefa.estado == INPUT_REQUIRED
        valor = pedido.escolha(texto)
        if valor is None or (valor != pedido.RECUSAR and valor not in pendencia.pergunta.alternativas):
            # Escolha ilegivel ou fora do enum: a Task continua pausada e repete a pergunta.
            tarefa.repetir_status(linha_de_alternativas(pendencia.pergunta))
            return

        tarefa.mudar(WORKING)
        tarefa.pendencia = None
        if valor == pedido.RECUSAR:
            resposta = types.ElicitResult(action="decline")
        else:
            resposta = types.ElicitResult(action="accept", content={"sala": valor})
        await self._protegido(tarefa, self._retomar(tarefa, pendencia, resposta, trace))

    async def _retomar(self, tarefa: Tarefa, pendencia: Pendencia, resposta: types.ElicitResult, trace: Trace) -> None:
        # <- aqui o requestState volta para o servidor MCP, no retry com id novo.
        resultado = await self.host.retomar(pendencia.arguments, pendencia.pergunta, resposta, trace)
        self._aplicar(tarefa, pendencia.arguments, resultado, rodada=pendencia.rodadas + 1)

    def _aplicar(self, tarefa: Tarefa, arguments: dict[str, str], resultado: Resultado, rodada: int) -> None:
        if isinstance(resultado, Pergunta):
            if rodada > MAXIMO_DE_RODADAS:
                tarefa.mudar(FAILED, f"O servidor MCP pediu escolha mais de {MAXIMO_DE_RODADAS} vezes")
                return
            # <- aqui o input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
            tarefa.pendencia = Pendencia(arguments=arguments, pergunta=resultado, rodadas=rodada)
            tarefa.mudar(INPUT_REQUIRED, linha_de_alternativas(resultado))
        elif isinstance(resultado, FalhaDaTool):
            tarefa.mudar(FAILED, _PREFIXO_DO_SDK.sub("", resultado.mensagem, count=1))
        elif resultado.dados.get("reservado") is False:
            tarefa.mudar(CANCELED, "Reserva recusada: nenhuma sala foi reservada.")
        else:
            self._concluir(tarefa, resultado)

    def _concluir(self, tarefa: Tarefa, resultado: Concluido) -> None:
        dados = resultado.dados
        reserva = {k: dados.get(k) for k in ("reserva", "sala", "inicio", "fim", "responsavel")}
        reserva["politica"] = tarefa.politica
        tarefa.artefatos.append({"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva)}]})
        tarefa.mudar(COMPLETED, f"Reserva {dados.get('reserva')} confirmada na {dados.get('sala')}.")

    async def _protegido(self, tarefa: Tarefa, passo: Awaitable[None]) -> None:
        """Falha de protocolo ou de rede com o MCP termina a Task em FAILED, com texto fixo.

        O texto cru do erro fica so no stderr do agente: nunca vai para o cliente A2A.
        """
        try:
            await passo
        except MCPError as e:
            print(f"a2a: {tarefa.id} erro MCP {e.error.code}: {e.error.message}", file=sys.stderr, flush=True)
            tarefa.mudar(FAILED, f"Falha na comunicacao com o servidor MCP (codigo {e.error.code})")
        except RespostaInesperada as e:
            print(f"a2a: {tarefa.id} resposta inesperada do MCP: {e}", file=sys.stderr, flush=True)
            tarefa.mudar(FAILED, "O servidor MCP respondeu fora do contrato da reserva")
        except Exception:
            # Nenhuma Task fica presa em WORKING por causa de um bug.
            traceback.print_exc()
            tarefa.mudar(FAILED, "Erro interno do agente")
