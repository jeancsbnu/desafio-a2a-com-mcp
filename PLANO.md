# Plano do desafio — A Ponte: um agente A2A com MCP por dentro

## 1. Decisões de stack

| Item | Escolha | Motivo |
|---|---|---|
| Linguagem | Python 3.12 (com `uv`) | Os wires em `exemplos/wire/` foram capturados com o SDK Python (`__main__:escolha_de_sala`, schemas com `title` do pydantic, `requestState` `v1.…`) |
| SDK MCP | `mcp==2.3.0` (v2, revisão `2026-07-28`) | Tem MRTR de primeira classe e utilitário de integridade do `requestState` |
| Servidor HTTP | Starlette + uvicorn (já vêm com o `mcp`; travar versão explicitamente) | Mesmo stack nos dois processos |
| A2A | JSON-RPC 2.0 escrito à mão em Starlette (só `SendMessage`, `GetTask` e o card) | Controle total da máquina de estados, das mensagens exatas e do formato dos ids (`task-…`, `ctx-…`, `msg-…`, `art-…`, como no wire). Alternativa: `a2a-sdk==1.3.0` |
| Dependências | Um `pyproject.toml` + `uv.lock` por processo, versões com `==` | Exigência de versões travadas; reforça que são dois processos |

## 2. Arquitetura

```
validador / cliente A2A
        │  HTTP JSON-RPC (A2A v1.0)  + header traceparent
        ▼
agente  :7300   GET /.well-known/agent-card.json   POST /a2a
  ├─ a2a.py      → SendMessage / GetTask, respostas no formato {"result": {"task": …}}
  ├─ tarefas.py  → TaskStore em memória (Task + pendência MRTR por Task)
  ├─ ponte.py    → input_required ⇄ TASK_STATE_INPUT_REQUIRED   ◄── a ponte
  └─ host_mcp.py → cliente MCP (tools/list, resources/read, tools/call)
        │  HTTP Streamable (MCP 2026-07-28) + _meta.traceparent
        ▼
servidor-mcp :7301  POST /mcp  (stateless)
  ├─ dominio.py  → salas, reservas em memória, política, conflitos, alternativas
  ├─ app.py      → MCPServer, 3 tools, resource politica://uso, RequestStateSecurity
  └─ log.py      → middleware ASGI: método, id e traceparent no stderr
```

Estrutura de arquivos proposta (pastas obrigatórias do starter mantidas):

```
servidor-mcp/
  pyproject.toml, uv.lock
  servidor_mcp/__init__.py, __main__.py, app.py, dominio.py, log.py
agente/
  pyproject.toml, uv.lock
  agente/__init__.py, __main__.py, card.py, a2a.py, tarefas.py, ponte.py, host_mcp.py, pedido.py, trace.py
scripts/
  teste_restart.py  (passos 7 a 12 do avaliador, com as alternativas mudando entre a pergunta e o retry)
.env.example        (REQUEST_STATE_SECRET= vazio, nunca o valor real; só documentação)
README.md           (substituído no fim)
```

## 3. O que a leitura do SDK `mcp` 2.3.0 já mostrou

Confirmado no código-fonte do pacote. Os caminhos são relativos a `servidor-mcp/.venv/Lib/site-packages/mcp/`; as referências da revisão de 04/11/2026 foram conferidas.

**Servidor**

- **Integridade do `requestState`:** `MCPServer(name=..., request_state_security=RequestStateSecurity(keys=[secret], ttl=...))`.
  - O codec padrão é AES-256-GCM com chave derivada por HKDF e recusa chave com menos de 32 bytes.
  - O `RequestStateBoundary` verifica expiração, audiência e o vínculo com o request: método, nome da tool e hash dos `arguments` (`server/request_state.py:271-282, 390-392`). Qualquer divergência dá `-32602 "Invalid or expired requestState"`.
  - O TTL padrão é 600 s. O SDK **não** lê variável de ambiente: quem lê `REQUEST_STATE_SECRET` é `servidor_mcp/app.py`.
- **O handler vê o estado em texto puro:** o boundary troca o token selado pelo plaintext antes de chamar a tool (`request_state.py:357-360`) e sela o que a tool devolver.
- **`Resolve`/`Elicit` não serve para `reservar_sala`.** No retry, o resolver recalcula a pergunta e compara o digest com o da pergunta original. Se as alternativas mudaram, por exemplo depois de um restart que zera as reservas em memória, o SDK descarta a resposta e pergunta de novo (`server/mcpserver/resolve.py:604-611`). Isso quebra o critério do restart, e o estado do SDK não tem onde guardar os argumentos nem o `enum`.
- **MRTR escrito à mão:** a tool devolve `ReservaOut | InputRequiredResult`. O braço `InputRequiredResult` sai do `outputSchema` (`server/mcpserver/utilities/func_metadata.py:390-396`). No retry, a tool lê `ctx.input_responses` e `ctx.request_state`. O SDK proíbe misturar isso com `Resolve` na mesma tool (`server/mcpserver/tools/base.py:91-97`).
- **`-32021` à mão:** sem `Resolve`, a checagem de capability é nossa. O formato a copiar está em `resolve.py:664-708`: `MCPError` com `MISSING_REQUIRED_CLIENT_CAPABILITY` e `data.requiredCapabilities`. Ela só roda quando há pergunta a fazer, e a de "sem alternativas" vem antes.
- **Transporte:** `streamable_http_app(stateless_http=True, json_response=True)`. Sem `json_response`, a resposta vem em SSE e o validador não consegue ler.
- **Erros de envelope (testado na Fase 1):** o próprio SDK já responde:
  - `-32602` + HTTP 400 para `_meta` incompleto;
  - `-32602` para URI de resource inexistente;
  - `-32020` + HTTP 400 para `Mcp-Method`/`Mcp-Name` divergente ou ausente.
- **Chave curta:** o SDK recusa um `REQUEST_STATE_SECRET` com menos de 32 bytes já na subida.

**Cliente (agente)**

- **MRTR no cliente:** `session.call_tool(..., allow_input_required=True)` devolve o `InputRequiredResult` cru, sem fechar o ciclo sozinho. O retry é `call_tool(..., input_responses=..., request_state=..., meta=...)`.
- **Retry reenvia exatamente o mesmo `arguments`:** por causa do vínculo por hash, guarde o dict da 1ª chamada e reenvie-o sem reparsear a mensagem.
- **Id do retry:** o dispatcher aloca ids por contador e `call_tool` não fixa id, então o retry já sai com id novo.
- **Headers e `_meta`:**
  - O SDK estampa `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` sozinho.
  - O stamp **sobrescreve** `protocolVersion`, `clientInfo` e `clientCapabilities` no `_meta` (`client/session.py:161-166`). O `traceparent` passado em `meta=` é mantido.
- **Capability de elicitation:** o SDK declara `form` **e** `url` quando existe `elicitation_callback` (`client/session.py:634-638`). Para declarar só `{"elicitation": {"form": {}}}`, sobrescrever `ClientSession._build_capabilities` (`session.py:614`).
  - Feito na Fase 5 sem callback nenhum: o `_build_capabilities` sobrescrito acrescenta a capability. Se o servidor mandasse um `elicitation/create` avulso, o callback padrão do SDK recusaria.
- **Subida do `Client`:** no modo `auto`, `Client.__aenter__` faz um probe `server/discover` (`client/client.py:465`); se o MCP estiver fora do ar, o agente cai.
  - O agente usa `ClientSession` direto, sem `Client`, e chama `adopt()` com a versão `2026-07-28` fixada. Abrir a sessão não toca no servidor.
- **O transporte prende a sessão à task que a abriu:** `streamable_http_client` abre um task group de anyio. Abrir num request e fechar em outro dá "Attempted to exit cancel scope in a different task". O agente abre e fecha no lifespan do Starlette.
- **Limitação do SDK, contornada (documentar no README):** uma exceção de rede no POST (`httpx2.ConnectError`, timeout) derruba o task group do `streamable_http_client` (`post_writer` → `_run_request_post`). Quem chamou recebe `CancelledError` e a sessão morre para sempre.
  - Já uma resposta ≥ 400 com erro JSON-RPC no corpo vira `MCPError` só daquela chamada (`_handle_post_request`).
  - O agente passa um `httpx2.AsyncClient` próprio com o transporte `FalhaDeConexaoComoErro`, que troca a falha de rede por um 503 com erro JSON-RPC.
  - Testado em 07/11/2026: com o servidor fora, duas chamadas dão `MCPError` em ~2,3 s; o servidor sobe e a mesma sessão volta a funcionar.
- **`tools/list` escondido:** `call_tool` revalida o resultado contra o `outputSchema`. Se a tool não estiver em cache, ele dispara um `tools/list` sem `traceparent` (`session.py:1120-1122`). Por isso o agente faz o `tools/list` dele antes, em cada Task.

## 4. Fases

Cada fase termina com o validador rodando contra os processos recém-iniciados. Os números `#N` são as verificações do `validador/validar.py`.

### Fase 0 — Preparação
- Fazer o fork no GitHub e apontar o `origin` para o fork. Hoje o `origin` aponta para `devfullcycle/desafio-a2a-com-mcp`.
- `uv init` em `servidor-mcp/` e em `agente/`, travar `mcp==2.3.0`, `starlette`, `uvicorn` e o que mais for usado.
- Gerar o segredo: `python -c "import secrets; print(secrets.token_hex(32))"`, exportar `REQUEST_STATE_SECRET` e garantir que `.env` esteja no `.gitignore` (já está).

### Fase 1 — Esqueleto do servidor MCP (`#1–#8`)
- `MCPServer("central-de-salas")` em Streamable HTTP stateless na porta 7301, em `/mcp`, com capabilities `tools` e `resources`.
- `listar_salas` com `outputSchema` (`ListaDeSalas { salas: SalaOut[] }`), `structuredContent` e o mesmo JSON em bloco de texto.
- Resource `politica://uso` com o conteúdo de `dados/politica-de-uso.md`, `mimeType` `text/markdown`.
- Middleware ASGI de log no stderr: `method`, `id` e `params._meta.traceparent` de cada request (ler o corpo e repassá-lo intacto).
- Conferir `_meta` incompleto (`#4`, `#5`), tool inexistente (`#6`) e URI inexistente (`#8`). Abrir o MCP Inspector para ver `_meta` e `resultType`.

### Fase 2 — Regras de domínio e `consultar_disponibilidade` (`#10–#12`)
- `dominio.py`: carrega `salas.json` e `reservas.json`, mantém as reservas em memória e gera ids sequenciais (`res-0003`, …).
- Ordem de validação (escolha documentada; o validador aceita qualquer ordem): sala existe → datas legíveis → `fim > inicio` → janela 08:00–20:00 (início e fim, convertidos para `-03:00`) → duração ≤ 2 h.
- Data ilegível ou sem fuso horário → `ToolError` com mensagem própria. Sem isso, comparar datetime com e sem fuso estoura `TypeError` e vira um "Error executing tool" genérico.
- Sobreposição com intervalo semiaberto `[inicio, fim)`: 14:00–15:00 não conflita com 15:00–16:00.
- Erros via `ToolError` com o texto exato do enunciado. `consultar_disponibilidade` devolve `{sala, livre, conflitos[]}`.
- Tools como `async def`: o SDK roda função síncrona em thread, e isso abriria corrida em `RESERVAS`.

### Fase 3 — `reservar_sala` com a assinatura final, caminho feliz (`#1`, `#9`)
- Já nasce como `async def reservar_sala(sala, inicio, fim, responsavel, ctx: Context) -> ReservaOut | InputRequiredResult`, para a Fase 4 não reescrever a assinatura.
- Conferir que o `inputSchema` e o `outputSchema` saem iguais aos do wire 01, sem `ctx` e sem o braço `InputRequiredResult`.
- Intervalo livre: cria a reserva e devolve `structuredContent` `{reserva, reservado, sala, inicio, fim, responsavel, politica, motivo}` (ver `02-tools-call-livre.json`).
- Com as três tools, o `#1` passa. O `#9` chama `reservar_sala`, então só passa aqui.

### Fase 4 — MRTR escrito à mão (`#13–#20`)
- **1ª rodada** (sem `requestState`): validar → detectar conflito → calcular as alternativas (salas livres no intervalo, capacidade ≥ a da pedida, ordenadas por `(capacidade, id)`, no máximo 3).
  - Sem conflito: reservar direto, sem checar capability. Cliente sem elicitation continua funcionando no caminho livre.
  - Conflito sem alternativa: `ToolError("Sem alternativas disponiveis no intervalo")` (`#20`). Esta checagem vem **antes** da de capability.
  - Conflito com alternativas, sem capability de elicitation em form mode: `-32021` com `data.requiredCapabilities: {"elicitation": {"form": {}}}` e HTTP 400 (`#15`). Considerar `elicitation: {}` como form, igual ao SDK (`resolve.py:676`).
  - Conflito com alternativas e com capability: `InputRequiredResult` (`#13`, `#14`):
    - `input_requests={"escolha_de_sala": ElicitRequest(form, requestedSchema plano {sala: {type: string, enum: alts}})}`. Sempre `enum`, mesmo com uma alternativa só.
    - `request_state` = JSON com `{v, sala, inicio, fim, responsavel, alternativas}`. O boundary do SDK sela.
- **Retry** (com `requestState`): o boundary já rejeitou adulteração e expiração (`#17`) e argumentos divergentes (`#18`). Ler o pedido **do estado**, nunca dos `arguments`.
  - `accept`: a sala escolhida precisa estar em `alternativas` do estado. Rechecar se ela está livre e reservar com os valores selados (`#16`).
  - `decline` ou `cancel`: `complete`, sem `isError`, `reservado: false`, `motivo: "recusado"` (`#19`).
  - Escolha fora das alternativas seladas, ou resposta sem a chave esperada: erro `-32602`.
  - Sala escolhida ocupada nesse meio-tempo: novo `InputRequiredResult` com as alternativas recalculadas, ou `ToolError` "Sem alternativas…" se não sobrar nenhuma. O agente trata as duas saídas (Fase 7).
- **Script de teste do restart** (`scripts/teste_restart.py`, só biblioteca padrão), reproduzindo os passos 7 a 12 do avaliador contra processos reais. Roda em dois passos, porque quem reinicia o servidor é você:
  1. `python scripts/teste_restart.py preparar`: ocupa mirante 14–15 e pede garagem 14–15, que agora oferece só `[fusca]`.
  2. Reiniciar o servidor MCP com o mesmo `REQUEST_STATE_SECRET`.
  3. `python scripts/teste_restart.py concluir`: retry com `fusca` e confere a reserva concluída.
  - É esse cenário, com as alternativas mudando entre pergunta e retry, que derrubaria a abordagem com `Resolve`. O teste com estado limpo dá falso verde.
  - Resultado em 07/11/2026: PASS. A reserva saiu como `res-0003`, o que confirma que a memória foi zerada.
- **Teste de expiração** (`servidor-mcp/tests/teste_expiracao.py`; `cd servidor-mcp && uv run python -m tests.teste_expiracao`): sobe o app num thread com o `time.time()` do `RequestStateBoundary` adiantado. O venv não tem `httpx`, então o `TestClient` do Starlette não serve. Não há variável de ambiente que permita TTL fora de 5–30 min.
  - Resultado em 07/11/2026: PASS. O retry 5 s antes de expirar conclui, e o retry depois de expirar dá `-32602`.
- **Casos extras conferidos à mão** (07/11/2026):
  - `cancel` conclui como recusa.
  - Escolha fora das alternativas seladas ou chave errada dá `-32602`.
  - Caminho livre sem capability de elicitation completa normalmente.
  - Sala escolhida ocupada entre a pergunta e o retry gera um novo `input_required` com o que sobrou.
  - Data ilegível ou sem fuso dá `isError` com mensagem própria.
  - 18:00–20:00 é aceito na borda da janela, e 11:00–12:00 encostado em 10:00–11:00 não conflita.

### Fase 5 — Agente como cliente MCP puro, já com `traceparent` (sem A2A ainda)
- ✅ Feita em 07/11/2026: `agente/agente/host_mcp.py` e `agente/agente/trace.py`.
- `HostMCP`: um único `ClientSession` (`SessaoSoFormulario`) aberto como `async with`, com a versão fixada via `adopt()` e sem probe na subida. A subida não depende da ordem dos processos.
- `_build_capabilities` sobrescrito para declarar só `{"elicitation": {"form": {}}}`. `client_info` igual ao wire: `agente-central-de-salas` `1.0.0`.
- O stderr do agente registra as capabilities declaradas na subida.
- **Toda** chamada do host recebe o `traceparent` da Task: `tools/list`, `resources/read`, `tools/call` e retry. O formato é `00-<trace-id>-<span-id novo>-<flags>`; sem header A2A, gera-se um trace-id por Task (`trace.novo()`).
- Por Task: `listar_tools` (conferir `reservar_sala`) → `versao_politica` → `reservar`, que devolve `Concluido`, `FalhaDaTool` ou `Pergunta(chave, alternativas, request_state)`. `retomar` faz o retry com o mesmo dict `arguments`.
- Fumaça: `cd agente && uv run python -m tests.fumaca_host_mcp`, com o servidor recém-iniciado. 8/8 PASS.
  - No log do servidor: todos os requests com o mesmo trace-id e span-id novo, retries com id novo (4→5, 6→7) e nenhum `tools/list` escondido.
- Conferido com um servidor HTTP falso que captura o fio: headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` (em `tools/call` e `resources/read`) e `_meta` idêntico em forma ao dos wires.

### Fase 6 — Agente como servidor A2A (`#21–#26`, `#31`, `#35`)
- ✅ Feita em 11/11/2026: `agente/agente/{a2a,tarefas,card,pedido}.py` e `agente/agente/__main__.py`. O host MCP abre e fecha no lifespan do Starlette, e o agente escuta em IPv4 e IPv6, como o servidor.
- `GET /.well-known/agent-card.json` igual em forma a `07-a2a-agent-card.json`: `supportedInterfaces[{url, protocolBinding: "JSONRPC", protocolVersion: "1.0"}]`, `capabilities`, skill `reservar-sala`.
  - Difere do wire só na URL (`localhost`; `AGENTE_URL_PUBLICA` muda) e na descrição da skill, que inclui o formato do pedido.
- `POST /a2a` com dispatch JSON-RPC: `SendMessage`, `GetTask`, ambos no formato `{"result": {"task": …}}` do wire (o validador aceita os dois formatos).
  - Códigos de erro, conferidos no `a2a-sdk` 1.3.0 (`a2a/utils/errors.py:146-154` e a docstring de `SendMessage`):
    - método desconhecido: `-32601`;
    - Task inexistente: `-32001` (`TaskNotFoundError`);
    - Task terminal: `-32004` (`UnsupportedOperationError`).
- Task: `id`, `contextId`, `status{state, message}`, `history`, `artifacts`.
  - Transições reais `SUBMITTED → WORKING → {COMPLETED | FAILED | CANCELED | INPUT_REQUIRED}`, registradas no stderr do agente, porque o `SendMessage` síncrono não deixa o cliente observar `SUBMITTED`/`WORKING`.
- Serialização da Task por whitelist (`to_wire()`), nunca um dump do objeto: a pendência MRTR não pode vazar.
- Parser fixo:
  - `reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<resto da linha>`. O `responsavel` é o último campo e pode ter espaços.
  - `escolha=<valor>`.
  - O agente não valida regra de sala.
- Pedido inicial malformado → `TASK_STATE_FAILED` com mensagem fixa mostrando o formato esperado.
- `isError` da tool → `TASK_STATE_FAILED` com o texto da tool em `status.message` e no histórico (`#35`).
- Erro de protocolo do MCP (ex.: `requestState` expirado) → `TASK_STATE_FAILED` com texto fixo. Nunca repassar o texto cru do erro.
- Sucesso → artifact `name: "reserva"` com o JSON `{reserva, sala, inicio, fim, responsavel, politica}`, sendo `politica` a versão lida do resource (`#25`).
- `SendMessage` com `taskId` de Task terminal → erro JSON-RPC (`#31`).

### Fase 7 — A ponte (`#27–#30`, `#32–#34`, `#36`)
- ✅ Feita em 11/11/2026: `agente/agente/ponte.py`.
  - A ida (`input_required` → `TASK_STATE_INPUT_REQUIRED`) está em `Ponte._aplicar`.
  - A volta (o `requestState` de volta ao servidor) está em `Ponte._retomar` → `HostMCP.retomar`.
- `input_required` → guardar na Task uma pendência `{request_state, chave, enum, arguments, trace}` → estado `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: a, b`.
  - A ordem é a do `enum`; ler também `const`, por defesa.
  - Nos `#32` e `#33` a pausa tem uma alternativa só.
- Continuação:
  - `escolha=<id>` com `id` no `enum` → retry com `{"action":"accept","content":{"sala":id}}`, a mesma chave, o `requestState` intacto e o **mesmo dict `arguments`** → `COMPLETED`.
  - `escolha=recusar` → `{"action":"decline"}` → `CANCELED`.
  - Escolha fora do `enum`, ou texto que nem é `escolha=` → continua `INPUT_REQUIRED` e repete a mesma linha (`#29`).
- **Retry que volta `input_required` de novo** (a sala escolhida foi ocupada nesse meio-tempo): trocar chave, estado e `enum` da pendência e continuar em `INPUT_REQUIRED` com a nova linha. Teto de rodadas por Task.
- Um `asyncio.Lock` por Task: duas Tasks pausadas não se misturam (`#33`).
- O `requestState` nunca sai do agente: nem em mensagem, nem em artifact, nem em texto de erro.
  - O `#34` é fraco: procura um token que o agente nunca viu. Garantir com teste próprio: pausar uma Task e varrer todas as respostas A2A atrás do `requestState` que o **agente** guardou.

### Fase 8 — Conferência do `traceparent` e dos logs
- A propagação já nasce na Fase 5. Aqui só conferimos.
- No stderr do servidor: o trace-id que o validador imprime na 1ª linha, o `tools/list` antes do 1º `tools/call` e o id do retry diferente do id inicial.
- Conferir também que nenhum request MCP do agente sai sem `traceparent`. Se aparecer um, é o `tools/list` escondido do `validate_tool_result` ou o probe de `server/discover`.

**Resultados de 11/11/2026, com os dois processos recém-iniciados**

- Validador: **36/36 PASS**, código de saída 0, em duas rodadas.
- No stderr do MCP:
  - o 1º request do agente é `tools/list` (id 1), antes do 1º `tools/call` (id 3);
  - o trace-id do validador aparece em 8 linhas;
  - nenhum request do agente sai sem `traceparent`;
  - o retry sai com id novo (6→7).
- Fluxo do avaliador com os corpos de `exemplos/wire/`, passos 3 e 7 a 13: todos OK.
  - O passo 7 lista `sala-fusca, sala-mirante`.
  - O artifact do passo 8 traz `sala-mirante` e `politica`.
  - O passo 10 mostra `Sala inexistente: sala-inexistente` no histórico.
  - O passo 12 conclui depois do restart.
  - O passo 13 dá `-32021`/400.
- Uma Task pausada no agente sobrevive ao restart do servidor MCP: a continuação conclui a reserva.
- `agente/tests/teste_vazamento.py`: os `requestState` guardados pela ponte não aparecem em nenhuma das 14 respostas A2A, nem inteiros nem pelos primeiros ou últimos 40 caracteres.
- Casos extras:
  - pedido malformado → `FAILED` com o formato esperado;
  - sala escolhida ocupada no meio → nova pausa `alternativas: sala-mirante` e conclusão;
  - servidor reiniciado com **outro** segredo → retry `-32602` → `FAILED` "Falha na comunicacao com o servidor MCP (codigo -32602)", com o texto cru só no stderr do agente;
  - `responsavel=Doc Brown` (com espaço) funciona.

### Fase 9 — Entrega
- README com as 4 seções (ver checklist).
  - Comandos para bash **e** PowerShell, já que `export` não existe no PowerShell.
  - Caminho com `uv` e caminho sem `uv`: `python -m venv` + `pip install -r requirements.txt`, com o `requirements.txt` gerado por `uv export`.
  - O `.env.example` só documenta: nada carrega `.env`, então o README manda exportar a variável.
- Clonar o próprio fork numa pasta limpa, seguir só o README, rodar o validador com os processos recém-iniciados e colar a saída completa no README.
- Rodar o fluxo do avaliador (passos 3 a 13) na mão, com `curl`, sobre processos recém-iniciados, e também `scripts/teste_restart.py`.

## 5. Pontos de atenção

- **Sempre reiniciar os dois processos antes do validador.** As reservas de uma execução mudam a seguinte. O fluxo manual do avaliador também depende de estado limpo: o passo 7 espera `sala-fusca, sala-mirante`.
- **Nada de callback de elicitation que responda sozinho** no cliente MCP do agente. Se a Task nunca pausa, o problema é esse.
- **Nada de domínio no agente.** Ele não calcula alternativa nem valida horário. Só confere se a escolha está no `enum` que o servidor mandou, o que é validação de protocolo.
- **O `requestState` é opaco para o agente:** guardar e ecoar, nunca decodificar.
- **O segredo nunca vai para o repositório**, nem no README, nem em teste, nem em script.
- **`localhost` no Windows:** o cliente tenta `::1` primeiro e, com a porta fechada, espera ~2 s antes de cair para `127.0.0.1`. Os dois processos escutam em todos os endereços de `localhost` (IPv4 e IPv6). Ver `abrir_sockets` em `servidor-mcp/servidor_mcp/__main__.py`.
- **Proteção contra DNS rebinding:** com `host="localhost"`, o SDK só aceita os headers `Host` `127.0.0.1:*`, `localhost:*` e `[::1]:*`; outro valor recebe `421`. Documentar no README.
- **Segredo com `.strip()`:** um `\r` ou `\n` sobrando ao exportar no shell muda a chave, e o retry depois de um restart passa a dar `-32602`.
- **A revisão simulou as 36 verificações em sequência** com as regras desta versão do plano, e todas dão o resultado esperado. Em `#32`, `#33a` e `#33b` a pausa tem uma alternativa só. O `#18` depende de `garagem` 9–10 estar livre quando o validador reserva para "Ocupante".

---

## 6. Checklist

Legenda: `#N` = verificação do validador · `(manual)` = conferida no fluxo do avaliador.

### Preparação
- [ ] Fork criado, `origin` apontando para o fork, trabalho na `main`
- [x] `servidor-mcp/pyproject.toml` e `agente/pyproject.toml` com versões travadas (`==`) e `uv.lock`
- [x] Nenhuma dependência de SDK de LLM em nenhum `pyproject.toml`
- [x] `REQUEST_STATE_SECRET` lido do ambiente, sem fallback hardcoded. O processo falha ao subir sem ele ou com menos de 32 bytes
- [x] `dados/`, `validador/` e `exemplos/` intocados (`git diff --stat origin/main -- dados validador exemplos` vazio)

### Servidor MCP
- [x] Sobe na 7301, Streamable HTTP, endpoint único `/mcp`, stateless
- [x] Declara as capabilities `tools` e `resources`
- [x] `tools/list` traz `listar_salas`, `consultar_disponibilidade` e `reservar_sala` — `#1`
- [x] Toda tool tem `inputSchema` `type: object` — `#2`
- [x] `listar_salas`: `structuredContent` conforme `outputSchema` + mesmo JSON em texto — `#3`
- [x] Sem `io.modelcontextprotocol/protocolVersion` → `-32602` + HTTP 400 — `#4`
- [x] Sem `io.modelcontextprotocol/clientCapabilities` → `-32602` + HTTP 400 — `#5`
- [x] Tool inexistente recusada (`-32602` ou `isError`) — `#6`
- [x] `resources/read politica://uso` devolve a política com `text/markdown` — `#7`
- [x] `resources/read` de URI inexistente → `-32602` (nunca `contents` vazio) — `#8`
- [x] `Sala inexistente: <id>` — `#9`
- [x] `Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00` — `#10`
- [x] `Duracao acima do limite: a politica permite no maximo 2 horas` — `#11`
- [x] `Intervalo invalido: fim deve ser posterior a inicio` — `#12`
- [x] `consultar_disponibilidade` devolve `livre` e as reservas em conflito
- [x] Reserva concluída traz `reserva, reservado, sala, inicio, fim, responsavel, politica` no `structuredContent`
- [x] Header divergente do corpo → `-32020`
- [x] stderr registra método, id e `traceparent` de cada request (manual)

### MRTR
- [x] Conflito → `resultType: input_required`, 1 entrada em `inputRequests`, `requestState` presente — `#13`
- [x] Elicitation em `mode: form`, schema plano, `sala` com `enum` (ou `const`) na ordem `(capacidade, id)`, no máximo 3 — `#14`
- [x] Conflito sem capability de elicitation em form mode → `-32021` + `data.requiredCapabilities` + HTTP 400 — `#15`
- [x] Retry com `inputResponses` + `requestState` → `complete` com a reserva na sala escolhida — `#16`
- [x] `requestState` adulterado → `-32602` — `#17`
- [x] Argumentos adulterados no retry não tomam efeito — `#18`
- [x] `decline` → `complete`, sem `isError`, `reservado: false` + `motivo` — `#19`
- [x] `cancel` tratado igual a `decline`
- [x] No retry, o pedido é reconstruído do `requestState` (valores selados), não dos `arguments`
- [x] Caminho livre funciona para cliente sem capability de elicitation
- [x] Conflito sem alternativa → `isError` `Sem alternativas disponiveis no intervalo` — `#20`
- [x] `requestState` com expiração entre 5 e 30 min; expirado → `-32602`
- [x] Retry funciona depois de reiniciar o servidor MCP, sem nada em memória entre as rodadas (manual)
- [x] Retry funciona depois do restart **mesmo que as alternativas mudem** entre a pergunta e o retry (`scripts/teste_restart.py`)

### Agente como host MCP
- [x] `tools/list` antes do primeiro `tools/call`, sem lista fixa de tools no código (manual)
- [x] Lê `politica://uso` e extrai a versão da 1ª linha
- [x] Todo request leva `_meta` obrigatório + exatamente `{"elicitation": {"form": {}}}`, sem `url`
- [x] O agente sobe mesmo com o servidor MCP fora do ar, e a sessão sobrevive à queda e à volta do servidor
- [x] Todo request leva os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` (em `tools/call` e `resources/read`)
- [x] `traceparent` com o mesmo trace-id do header A2A em todos os requests MCP da Task, inclusive na continuação (manual)
- [x] Fala com o servidor só por HTTP, sem importar código do `servidor-mcp/`

### Agente como servidor A2A
- [x] `GET /.well-known/agent-card.json` → 200 com JSON — `#21`
- [x] `supportedInterfaces` com `protocolBinding: JSONRPC`, `url` e `protocolVersion: 1.0` — `#22`
- [x] Skill `id: reservar-sala` — `#23`
- [x] Sala livre → `TASK_STATE_COMPLETED` — `#24`
- [x] Artifact `name: reserva` com a reserva e `politica` lida do resource — `#25`
- [x] `GetTask` → `id`, `contextId`, estado corrente e artifact — `#26`
- [x] `SendMessage` sem `taskId` abre Task nova com `id` e `contextId` próprios
- [x] Passa por `SUBMITTED` e `WORKING` antes de terminar
- [x] `SendMessage` em Task terminal → erro JSON-RPC — `#31`
- [x] Sala inexistente → `TASK_STATE_FAILED` com `Sala inexistente: …` na mensagem e no histórico — `#35`

### A ponte
- [x] Sala ocupada → `TASK_STATE_INPUT_REQUIRED` — `#27`
- [x] Mensagem exatamente `alternativas: <ids, na ordem do enum>` — `#28`
- [x] Escolha fora do `enum` mantém `INPUT_REQUIRED` e repete a linha — `#29`
- [x] `escolha=<id>` → `COMPLETED` na sala escolhida — `#30`
- [x] `escolha=recusar` → `decline` → `TASK_STATE_CANCELED` — `#32`
- [x] Duas Tasks pausadas concluem cada uma com a sua reserva — `#33`
- [x] `requestState` não aparece em nenhuma resposta A2A — `#34`, mais o teste próprio com o token que o agente guardou
- [x] Retry que volta `input_required` de novo mantém a Task pausada, com a nova lista
- [x] Erro de protocolo no retry (ex.: estado expirado) → `TASK_STATE_FAILED` com texto fixo
- [x] Pedido malformado → `TASK_STATE_FAILED` com mensagem do formato esperado
- [x] Mesmo pedido → mesma pausa, byte a byte — `#36`
- [x] Retry MCP com id JSON-RPC diferente do inicial, a mesma chave do `inputRequests` e o `requestState` intacto (manual)

### README e entrega
- [ ] Seção **Como rodar**: comandos exatos a partir de um clone limpo (instalar, gerar e exportar o segredo, subir os dois processos, rodar o validador), com e sem `uv`, em bash e PowerShell
- [ ] Seção **Onde a ponte acontece**: arquivo e função em que `input_required` vira `TASK_STATE_INPUT_REQUIRED` e em que o `requestState` volta ao servidor
- [ ] Seção **Decisões técnicas**: AES-GCM/HMAC do SDK, TTL, onde fica o estado das Tasks
- [ ] Seção **Saída do validador**: saída completa da última execução, em bloco de código
- [ ] Limitações reais do SDK documentadas com evidência, se houver
- [x] `python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301` → 36 PASS, código de saída 0, processos recém-iniciados
- [ ] Fluxo do avaliador (passos 1 a 13) percorrido na mão a partir de um clone limpo
- [ ] Push na `main` do fork e link enviado
