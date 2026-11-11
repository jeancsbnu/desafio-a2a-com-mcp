#!/usr/bin/env python3
"""Teste do retry de MRTR depois de reiniciar o servidor MCP.

Reproduz os passos 7 a 12 do fluxo do avaliador no cenario mais duro: as
alternativas mudam entre a pergunta e o retry. Com o servidor recem-iniciado:

    python scripts/teste_restart.py preparar --mcp http://localhost:7301
      1. reserva sala-mirante 14:00-15:00 (ocupa uma das alternativas)
      2. pede sala-garagem 14:00-15:00: input_required oferecendo so sala-fusca
      3. guarda chave, requestState e arguments num arquivo temporario

    (reinicie o servidor MCP com o MESMO REQUEST_STATE_SECRET; as reservas em
     memoria somem, entao agora as alternativas seriam sala-fusca e sala-mirante)

    python scripts/teste_restart.py concluir --mcp http://localhost:7301
      4. faz o retry com escolha sala-fusca e confere a reserva concluida

Biblioteca padrao apenas, Python 3.10+. Sai com codigo 0 se passar.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOLO = "2026-07-28"
ARQUIVO = Path(tempfile.gettempdir()) / "desafio-a2a-teste-restart.json"
GARAGEM = {"sala": "sala-garagem", "inicio": "2026-11-03T14:00:00-03:00", "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty"}


def chamar(url: str, params: dict) -> tuple[int, dict]:
    corpo = {
        "jsonrpc": "2.0",
        "id": secrets.token_hex(6),
        "method": "tools/call",
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            },
        },
    }
    cabecalhos = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO,
        "Mcp-Method": "tools/call",
        "Mcp-Name": params["name"],
    }
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def falhar(motivo: str) -> int:
    print(f"FAIL {motivo}")
    return 1


def preparar(url: str) -> int:
    ocupante = {**GARAGEM, "sala": "sala-mirante", "responsavel": "Biff"}
    _, resposta = chamar(url, {"name": "reservar_sala", "arguments": ocupante})
    if not (resposta.get("result") or {}).get("structuredContent", {}).get("reservado"):
        return falhar(f"nao consegui ocupar sala-mirante (o servidor foi recem-iniciado?): {json.dumps(resposta)[:300]}")
    print("ok   sala-mirante 14:00-15:00 ocupada")

    _, resposta = chamar(url, {"name": "reservar_sala", "arguments": GARAGEM})
    resultado = resposta.get("result") or {}
    pedidos = resultado.get("inputRequests") or {}
    if resultado.get("resultType") != "input_required" or len(pedidos) != 1:
        return falhar(f"esperava input_required: {json.dumps(resposta)[:300]}")
    chave, pedido = next(iter(pedidos.items()))
    campo = pedido["params"]["requestedSchema"]["properties"]["sala"]
    oferecidas = campo.get("enum") or [campo.get("const")]
    if oferecidas != ["sala-fusca"]:
        return falhar(f"esperava so sala-fusca como alternativa, veio {oferecidas}")
    print(f"ok   input_required oferecendo {oferecidas}")

    ARQUIVO.write_text(json.dumps({"chave": chave, "requestState": resultado["requestState"]}), encoding="utf-8")
    print(f"\nestado guardado em {ARQUIVO}")
    print("agora reinicie o servidor MCP (mesmo REQUEST_STATE_SECRET) e rode: python scripts/teste_restart.py concluir")
    return 0


def concluir(url: str) -> int:
    if not ARQUIVO.exists():
        return falhar(f"{ARQUIVO} nao existe: rode 'preparar' antes")
    guardado = json.loads(ARQUIVO.read_text(encoding="utf-8"))
    params = {
        "name": "reservar_sala",
        "arguments": GARAGEM,
        "inputResponses": {guardado["chave"]: {"action": "accept", "content": {"sala": "sala-fusca"}}},
        "requestState": guardado["requestState"],
    }
    status, resposta = chamar(url, params)
    resultado = resposta.get("result") or {}
    dados = resultado.get("structuredContent") or {}
    if resultado.get("resultType") != "complete" or resultado.get("isError") or dados.get("sala") != "sala-fusca":
        return falhar(f"retry nao concluiu a reserva (http {status}): {json.dumps(resposta)[:400]}")
    ARQUIVO.unlink()
    print(f"PASS retry depois do restart concluiu {dados.get('reserva')} em {dados.get('sala')}, {dados.get('inicio')} a {dados.get('fim')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Teste do retry de MRTR depois de reiniciar o servidor MCP")
    p.add_argument("passo", choices=["preparar", "concluir"])
    p.add_argument("--mcp", default="http://localhost:7301")
    p.add_argument("--caminho-mcp", default="/mcp")
    args = p.parse_args()
    url = args.mcp.rstrip("/") + args.caminho_mcp
    return preparar(url) if args.passo == "preparar" else concluir(url)


if __name__ == "__main__":
    sys.exit(main())
