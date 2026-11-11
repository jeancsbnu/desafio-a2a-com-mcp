"""O `requestState` que o agente guardou nunca aparece numa resposta A2A.

A verificacao #34 do validador procura o token que o proprio validador obteve
direto do servidor MCP, que o agente nunca viu: passaria com qualquer agente.
Este teste sobe o agente em processo, pausa Tasks, le o token que a ponte
guardou de verdade e varre todas as respostas A2A atras dele.

Com o servidor MCP recem-iniciado em http://localhost:7301:

    cd agente
    uv run python -m tests.teste_vazamento
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from agente.a2a import criar_app

URL_MCP = os.environ.get("MCP_URL", "http://localhost:7301/mcp")
DIA = "2026-11-10"
falhas = 0
respostas: list[str] = []


def verificar(descricao: str, condicao: bool, detalhe: object = "") -> None:
    global falhas
    print(f"{'PASS' if condicao else 'FAIL'} {descricao}" + ("" if condicao else f": {detalhe}"))
    falhas += not condicao


def subir():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    base = f"http://127.0.0.1:{porta}"
    app = criar_app(url_mcp=URL_MCP, url_publica=base)
    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=porta, log_level="warning"))
    threading.Thread(target=servidor.run, daemon=True).start()
    while not servidor.started:
        time.sleep(0.05)
    return base, app


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        bruto = r.read().decode()
    respostas.append(bruto)
    return json.loads(bruto)


def rpc(base: str, metodo: str, params: dict) -> dict:
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(4), "method": metodo, "params": params}
    req = urllib.request.Request(f"{base}/a2a", data=json.dumps(corpo).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        bruto = r.read().decode()
    respostas.append(bruto)
    return json.loads(bruto)


def enviar(base: str, texto: str, task_id: str | None = None) -> dict:
    mensagem = {"messageId": f"msg-{secrets.token_hex(4)}", "role": "ROLE_USER", "parts": [{"text": texto}]}
    if task_id:
        mensagem["taskId"] = task_id
    return rpc(base, "SendMessage", {"message": mensagem})["result"]["task"]


def main() -> int:
    base, app = subir()
    get(f"{base}/.well-known/agent-card.json")
    pedido = f"reservar sala=sala-garagem inicio={DIA}T10:00:00-03:00 fim={DIA}T11:00:00-03:00 responsavel=Ocupante"
    verificar("ocupa sala-garagem", enviar(base, pedido)["status"]["state"] == "TASK_STATE_COMPLETED")

    tokens = []
    for escolha in ("escolha=sala-fusca", "escolha=recusar"):
        tarefa = enviar(base, pedido.replace("Ocupante", "Teste"))
        verificar("conflito pausa a Task", tarefa["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", tarefa["status"])
        guardado = app.state.tarefas.buscar(tarefa["id"]).pendencia.pergunta.request_state
        tokens.append(guardado)
        rpc(base, "GetTask", {"id": tarefa["id"]})
        enviar(base, "escolha=sala-inexistente", tarefa["id"])
        enviar(base, "isso nao e uma escolha", tarefa["id"])
        final = enviar(base, escolha, tarefa["id"])
        rpc(base, "GetTask", {"id": tarefa["id"]})
        verificar(f"{escolha} termina a Task", final["status"]["state"] in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"), final["status"])

    verificar("o agente guardou requestStates de verdade", all(len(t) > 40 for t in tokens), [len(t) for t in tokens])
    for i, token in enumerate(tokens, 1):
        vazou = [r for r in respostas if token in r or token[:40] in r or token[-40:] in r]
        verificar(f"requestState {i} ausente das {len(respostas)} respostas A2A", not vazou, vazou[:1])
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
