# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do módulo **Desenvolvimento de Agentes com IA**. São dois processos:

- **`servidor-mcp/`**: servidor MCP em Streamable HTTP, porta `7301`, endpoint `/mcp`. Expõe as salas da Hill Valley Tech com três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`) e um resource (`politica://uso`). A reserva faz o ciclo completo de MRTR.
- **`agente/`**: agente que é **host MCP por dentro** e **servidor A2A v1.0 por fora**, porta `7300`, com `/.well-known/agent-card.json` e JSON-RPC em `/a2a`. Não usa LLM: entende um pedido de formato fixo e decide por regra.

```
cliente A2A ──SendMessage/GetTask + traceparent──▶ agente :7300 ──tools/call + _meta.traceparent──▶ servidor-mcp :7301
            ◀── Task (INPUT_REQUIRED / COMPLETED) ──           ◀── input_required + requestState ──
```

Stack: Python 3.10+, SDK oficial `mcp==2.3.0` (revisão `2026-07-28` da spec), Starlette `1.7.0` e uvicorn `0.54.0`. As versões estão travadas em `pyproject.toml` (`==`), `uv.lock` e `requirements.txt` de cada processo.

---

## Como rodar

Pré-requisitos:

- [uv](https://docs.astral.sh/uv/) (recomendado), que baixa um Python 3.10+ se faltar, **ou** Python 3.10+ com `pip`. O `uv.lock` foi gerado com o uv 0.13; um uv muito antigo pode recusá-lo. Nesse caso, atualize com `uv self update` ou instale com `curl -LsSf https://astral.sh/uv/install.sh | sh`;
- Python 3.10+ para o validador.

Os três processos rodam em três terminais, a partir da raiz de um clone limpo:

```bash
git clone https://github.com/jeancsbnu/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

### 1. Servidor MCP (terminal 1)

O servidor exige a chave de integridade do `requestState` na variável `REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Gere a sua e exporte **uma vez**, **no mesmo terminal** em que o servidor vai rodar. O valor nunca vai para o repositório. Sem a variável, o processo para na subida e mostra o comando abaixo.

Gerar e exportar o segredo (uma vez por terminal):

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```powershell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Subir o servidor (bash e PowerShell):

```bash
cd servidor-mcp
uv run --frozen python -m servidor_mcp
```

O stderr deste terminal é o log de requests: método, id, nome da tool ou URI e `traceparent` de cada request.

**Para reiniciar o servidor** (passo 12 do fluxo do avaliador): Ctrl+C e rode de novo só o `uv run --frozen python -m servidor_mcp`, **no mesmo terminal e sem gerar outro segredo**. Um `requestState` emitido antes do restart continua válido porque a chave é a mesma. Com um segredo novo, ele é recusado com `-32602`, de propósito.

### 2. Agente (terminal 2)

O agente não precisa do segredo. Ele pode subir antes ou depois do servidor MCP.

```bash
cd agente
uv run --frozen python -m agente
```

O stderr mostra as capabilities que o agente declara ao MCP e cada transição de estado das Tasks, com o trace-id. É ali que aparecem `TASK_STATE_SUBMITTED` e `TASK_STATE_WORKING`. O `SendMessage` é bloqueante, como em `exemplos/wire/08` e `10`, e devolve a Task já pausada ou terminada. Um `GetTask` feito durante a chamada ao MCP vê `TASK_STATE_WORKING`.

### 3. Validador (terminal 3)

Com os dois processos **recém-iniciados**, porque as reservas de uma execução mudam o resultado da seguinte:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

No Windows, use `python` se não houver `python3`.

### Sem `uv`

Use o mesmo segredo e os mesmos terminais. Em cada processo, crie um venv e instale o `requirements.txt` travado:

```bash
cd servidor-mcp                       # ou: cd agente
python3 -m venv .venv
source .venv/bin/activate             # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m servidor_mcp                # ou: python -m agente
```

### Variáveis de ambiente

| Variável | Processo | Padrão | Para quê |
|---|---|---|---|
| `REQUEST_STATE_SECRET` | servidor-mcp | **obrigatória** | Chave do `requestState` (≥ 32 bytes). Veja `.env.example`: nada carrega `.env` sozinho |
| `MCP_HOST` / `MCP_PORT` | servidor-mcp | `localhost` / `7301` | Onde o servidor escuta (IPv4 e IPv6 de `localhost`) |
| `AGENTE_HOST` / `AGENTE_PORT` | agente | `localhost` / `7300` | Onde o agente escuta |
| `MCP_URL` | agente | `http://localhost:7301/mcp` | Endpoint MCP que o agente consome |
| `AGENTE_URL_PUBLICA` | agente | `http://localhost:7300` | Base da URL publicada no Agent Card |

A proteção contra DNS rebinding do SDK só aceita os headers `Host` `localhost:*`, `127.0.0.1:*` e `[::1]:*`.

### Testes além do validador

| Comando | O que prova |
|---|---|
| `cd servidor-mcp && uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v` | Regras de domínio nas bordas: janela em 08:00 e 20:00 (convertida para `-03:00`), duração de 2 h exatas, intervalo semiaberto, alternativas por `(capacidade, id)` e no máximo 3 |
| `cd agente && uv run --frozen python -m unittest discover -s tests -p "test_*.py" -v` | Parser dos textos fixos (`reservar …`, `escolha=…`), leitura do `traceparent` e falhas de rede no transporte MCP: um servidor falso corta a resposta no meio e a sessão sobrevive |
| `python3 scripts/teste_restart.py preparar` → reiniciar o servidor MCP **com o mesmo segredo** → `python3 scripts/teste_restart.py concluir` | Retry do MRTR depois de um restart, no pior caso: as alternativas mudam entre a pergunta e o retry |
| `cd servidor-mcp && uv run --frozen python -m tests.teste_expiracao` | `requestState` expirado vira `-32602`; 5 s antes de expirar, ainda conclui |
| `cd agente && uv run --frozen python -m tests.fumaca_host_mcp` (servidor MCP recém-iniciado) | Host MCP sozinho: descoberta, política, pausa, retry, recusa, erro da tool |
| `cd agente && uv run --frozen python -m tests.teste_vazamento` (servidor MCP no ar) | O `requestState` que o agente guardou não aparece em nenhuma resposta A2A |

O teste de vazamento existe porque a verificação 34 do validador procura um token que o validador obteve direto do MCP e que o agente nunca viu.

---

## Onde a ponte acontece

A ponte está em [`agente/agente/ponte.py`](agente/agente/ponte.py).

**Ida.** Quando o `tools/call` de `reservar_sala` volta com `resultType: input_required`, `HostMCP.reservar` ([`host_mcp.py:223`](agente/agente/host_mcp.py)) chama o SDK com `allow_input_required=True`, para receber o resultado cru, sem callback de elicitation. Em seguida, `_pergunta` traduz a resposta num `Pergunta(chave, alternativas, request_state)`. Em `Ponte._aplicar` ([`ponte.py:120`](agente/agente/ponte.py), comentário `<- aqui o input_required do MCP vira TASK_STATE_INPUT_REQUIRED`), essa pergunta vira a pausa:

- a Task ganha uma `Pendencia` com a chave, as alternativas, o `requestState` opaco e o **mesmo dict `arguments`** da primeira chamada;
- a Task muda para `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: <ids na ordem do enum>`.

**Volta.** O `SendMessage` de continuação traz `taskId` e `escolha=<id>`, e chega em `Ponte.continuar` ([`ponte.py:89`](agente/agente/ponte.py)), que confere a escolha contra o `enum` recebido. Daí, `Ponte._retomar` ([`ponte.py:115`](agente/agente/ponte.py), comentário `<- aqui o requestState volta para o servidor MCP`) chama `HostMCP.retomar` ([`host_mcp.py:229`](agente/agente/host_mcp.py)). Esse método repete o `tools/call` com um id de JSON-RPC novo, alocado pelo contador do SDK. O request leva `inputResponses` com a mesma chave, o `requestState` ecoado sem modificação e os mesmos `arguments`.

**Do lado do servidor**, `reservar_sala` ([`servidor_mcp/app.py:110`](servidor-mcp/servidor_mcp/app.py)) tem duas metades:

- `_perguntar` (`app.py:136`) monta o `input_required`.
- `_retomar` (`app.py:177`) atende o retry. Ele reconstrói o pedido **a partir do `requestState`**, e não dos `arguments` reenviados.

---

## Decisões técnicas

### O `requestState`

- **Proteção:** usa o utilitário do próprio SDK, `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` ([`app.py:83`](servidor-mcp/servidor_mcp/app.py)), instalado como `RequestStateBoundary` no pipeline do servidor.
  - O token é **AES-256-GCM** (AEAD, então tem integridade e confidencialidade) com chave derivada por HKDF do segredo.
  - Ele sela também a expiração, a audiência (nome do servidor) e o vínculo com o request: método, nome da tool e hash dos `arguments`.
  - Trocar um caractere do token, apresentar um token expirado ou mudar os `arguments` no retry dá `-32602` antes de a tool rodar.
- **Validade:** **10 minutos** (`VALIDADE_REQUEST_STATE`, [`app.py:27`](servidor-mcp/servidor_mcp/app.py)), dentro da faixa de 5 a 30 exigida.
- **Conteúdo:** `{v, sala, inicio, fim, responsavel, alternativas}`, ou seja, tudo de que o servidor precisa para reconstruir o pedido.
  - O servidor não guarda nada entre o `input_required` e o retry. Por isso, um retry depois de reiniciar o processo funciona, desde que o segredo seja o mesmo.
  - No retry, a sala escolhida precisa estar nas `alternativas` seladas e ainda estar livre. Se foi ocupada nesse meio-tempo, o servidor pergunta de novo com o que sobrou.
- **Segredo:** vem só de `REQUEST_STATE_SECRET`, sem fallback no código, com `.strip()` para um `\n` acidental não trocar a chave. O SDK recusa chaves com menos de 32 bytes.

### O MRTR do servidor é escrito à mão

O SDK tem um atalho de primeira classe, `Resolve(...)` com `Elicit`. Ele recalcula a pergunta no retry e descarta a resposta se o digest mudou (`mcp/server/mcpserver/resolve.py:604-611`, no pacote instalado). Com as reservas em memória, um restart muda as alternativas e o retry vira uma nova pergunta em vez de concluir. Isso quebraria o critério do restart.

Por isso `reservar_sala` devolve `ReservaOut | InputRequiredResult` e lê `ctx.input_responses` e `ctx.request_state` diretamente. A checagem de capability é feita à mão no mesmo formato do SDK:

- `-32021` com `data.requiredCapabilities: {"elicitation": {"form": {}}}` e HTTP 400;
- só quando há pergunta a fazer, e depois da checagem de "sem alternativas".

### Onde fica o estado das Tasks

Em memória, no processo do agente: [`agente/agente/tarefas.py`](agente/agente/tarefas.py), classe `Tarefas`, um dicionário por `id`.

- Cada `Tarefa` tem um `asyncio.Lock` próprio. Duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`, porque cada uma carrega a sua `Pendencia`.
- A pendência nunca é serializada. `Tarefa.para_wire()` monta a resposta por lista de campos (`id`, `contextId`, `status`, `history`, `artifacts`), nunca por dump do objeto.
- Estado terminal é definitivo, porque as transições permitidas são validadas. Um `SendMessage` para Task terminal recebe `-32004` (`UnsupportedOperationError`). Task inexistente recebe `-32001`.

As reservas também ficam em memória, no servidor MCP, e somem num restart, como o enunciado permite.

### Outras decisões

- **A2A escrito à mão** sobre Starlette ([`a2a.py`](agente/agente/a2a.py)): só `SendMessage` e `GetTask`, no binding JSON-RPC 2.0, com respostas no formato `{"result": {"task": …}}` dos exemplos de wire.
- **O agente não decide domínio.** Conflito, política e alternativas vêm do servidor MCP. O agente só confere se a escolha está no `enum` que o servidor mandou, o que é validação de protocolo.
- **Mensagens de status:**
  - Erro de execução da tool (`isError`) vira `TASK_STATE_FAILED` com a mensagem da tool, sem o prefixo `Error executing tool …:` que o SDK acrescenta.
  - Erro de protocolo do MCP vira `FAILED` com texto fixo. O texto cru fica só no stderr do agente. Um `-32602` no retry diz que o estado da reserva pode ter expirado.
  - Servidor MCP fora do ar (conexão recusada ou sem conectar a tempo, ou seja, o request nem saiu):
    - no pedido inicial, a Task termina em `FAILED` com "Servidor MCP indisponivel no momento";
    - na continuação de uma Task pausada, o `SendMessage` recebe `-32603` e a Task **continua em `TASK_STATE_INPUT_REQUIRED`, intacta** (`Ponte.continuar` e `Tarefa.restaurar_pausa`). O `requestState` segue válido, e a mesma escolha conclui quando o servidor volta.
  - Conexão que cai depois de o request sair (timeout de leitura, resposta cortada no meio): a Task termina em `FAILED` dizendo que a reserva pode ou não ter sido criada. Ela não volta à pausa, porque o servidor pode ter executado o retry.
  - Pedido fora do formato vira `FAILED` com o formato esperado.
  - Escolha fora do `enum` mantém a pausa e repete a linha.
- **Descoberta em runtime:** em cada Task, o agente faz `tools/list` e confere `reservar_sala`, depois `resources/read politica://uso` (versão da 1ª linha vai para o campo `politica` do artifact), e só então `tools/call`.
- **`traceparent`:** o trace-id do header A2A é guardado na Task. Cada request MCP leva `00-<mesmo trace-id>-<span-id novo>-<flags>` no `_meta`, inclusive o retry. Sem header, o agente gera um trace-id por Task.
- **Capabilities do host:** o agente declara exatamente `{"elicitation": {"form": {}}}`, e o SDK estampa `_meta` e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`.
- **Versão fixada:** o agente usa a versão `2026-07-28` sem probe de `server/discover`. Por isso sobe mesmo com o servidor MCP fora do ar.

### Limitações do SDK `mcp` 2.3.0 encontradas, com evidência

1. **Falha de rede derruba a sessão do cliente para sempre.**
   - **Onde:** em `mcp/client/streamable_http.py`, uma exceção de rede no POST escapa do SDK em dois pontos:
     - antes da resposta (`httpx2.ConnectError`, timeout), em `_run_request_post`, que roda no task group do `post_writer`;
     - no meio do corpo (`httpx2.RemoteProtocolError`), em `_handle_json_response`, que só trata `StreamError` e `ValidationError`.
   - **Efeito:** o task group do `streamable_http_client` cai, quem chamou recebe `CancelledError`, e todas as chamadas seguintes falham.
   - **O que funciona:** uma resposta HTTP ≥ 400 com erro JSON-RPC no corpo vira `MCPError` só daquela chamada (`_handle_post_request`).
   - **Contorno:** o transporte `FalhaDeConexaoComoErro` ([`host_mcp.py:55`](agente/agente/host_mcp.py)) lê o corpo JSON inteiro antes de entregar a resposta ao SDK e troca qualquer falha de rede por um 503 com erro JSON-RPC. O `data.motivo` separa "indisponível" (o request nem saiu) de "resposta perdida".
   - **Testado:**
     - com o servidor fora do ar, as chamadas dão `MCPError` e a mesma sessão volta a funcionar quando ele sobe;
     - em `agente/tests/test_transporte.py`, um servidor falso corta a resposta no meio, a chamada dá `MCPError` e a seguinte funciona. Com o transporte antigo, esse teste falha porque a sessão cai.
2. **O cliente declara elicitation `form` e `url`** sempre que há `elicitation_callback` (`mcp/client/session.py:634-638`). O agente sobrescreve `ClientSession._build_capabilities` em `SessaoSoFormulario` ([`host_mcp.py:108`](agente/agente/host_mcp.py)) para declarar só `form`, sem callback.
3. **O transporte prende a sessão à task do asyncio que a abriu.** Ela é aberta e fechada no lifespan do Starlette ([`a2a.py:129`](agente/agente/a2a.py)), e não no primeiro request.
4. **`Resolve`/`Elicit` não sobrevive à mudança das alternativas** (`resolve.py:604-611`); ver "O MRTR do servidor é escrito à mão".

---

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de um clone limpo:

```
trace-id desta execucao: 056d217482d9bd356d9e21c9918363cc
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
