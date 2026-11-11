"""Tasks A2A em memoria: identidade, estado, historico e produto.

A pendencia do MRTR (o `requestState` e companhia) mora na Task, mas fica fora
de `para_wire()`: a serializacao e por lista de campos, nunca um dump do objeto.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

from agente.host_mcp import Pergunta
from agente.trace import Trace

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"

TERMINAIS = frozenset({COMPLETED, FAILED, CANCELED})

# Transicoes permitidas. Estado terminal nao sai mais.
_TRANSICOES = {
    SUBMITTED: {WORKING, FAILED},
    WORKING: {INPUT_REQUIRED, COMPLETED, FAILED, CANCELED},
    INPUT_REQUIRED: {WORKING},
}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pendencia:
    """O que o agente precisa para o retry do MRTR, ligado a uma Task.

    `arguments` e o dict exato da 1a chamada: o `requestState` esta vinculado ao
    hash dele, entao o retry reenvia o mesmo objeto, sem reparsear a mensagem.
    """

    arguments: dict[str, str]
    pergunta: Pergunta
    rodadas: int = 1


@dataclass
class Tarefa:
    id: str
    context_id: str
    trace: Trace
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artefatos: list[dict[str, Any]] = field(default_factory=list)
    politica: str | None = None
    pendencia: Pendencia | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mudar(self, estado: str, texto: str | None = None) -> None:
        if estado not in _TRANSICOES.get(self.estado, set()):
            raise RuntimeError(f"transicao invalida {self.estado} -> {estado} na {self.id}")
        self.estado = estado
        self.mensagem_status = None
        if texto is not None:
            self.mensagem_status = self.mensagem_do_agente(texto)
            self.historico.append(self.mensagem_status)
        print(f"a2a: {self.id} {estado} trace={self.trace.trace_id}", file=sys.stderr, flush=True)

    def repetir_status(self, texto: str) -> None:
        """Responde sem mudar de estado (escolha invalida numa Task pausada)."""
        self.mensagem_status = self.mensagem_do_agente(texto)
        self.historico.append(self.mensagem_status)

    def mensagem_do_agente(self, texto: str) -> dict[str, Any]:
        return {
            "messageId": novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }

    def para_wire(self, tamanho_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        historico = self.historico if tamanho_historico is None else self.historico[-tamanho_historico:] if tamanho_historico > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": historico,
            "artifacts": self.artefatos,
        }


class Tarefas:
    def __init__(self) -> None:
        self._por_id: dict[str, Tarefa] = {}

    def criar(self, context_id: str | None, trace: Trace) -> Tarefa:
        tarefa = Tarefa(id=novo_id("task"), context_id=context_id or novo_id("ctx"), trace=trace)
        self._por_id[tarefa.id] = tarefa
        print(f"a2a: {tarefa.id} {SUBMITTED} trace={trace.trace_id}", file=sys.stderr, flush=True)
        return tarefa

    def buscar(self, tarefa_id: str) -> Tarefa | None:
        return self._por_id.get(tarefa_id)
