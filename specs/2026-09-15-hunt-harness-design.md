# Hunt Harness — Design Spec

**Data:** 2026-09-15 · **Rev 11** (2026-09-17) · **Primeiro engagement:** acme-bbp
**Base:** Rev 10 + fase 4 autorizada (Jev/System One no harness; ver §9.4)

---

## 1. Problema

Caça a vuln com LLM sofre cinco falhas confirmadas:

1. **Conclusão fácil** — o modelo fecha "sem vuln" cedo demais, em vez de exaurir hipóteses.
2. **Amnésia** — o estado do hunt não sobrevive à sessão. Reteste, re-onboarding, thread perdida.
3. **Afogamento** — o contexto enche de recon bruto e a atenção dilui quando a caça fica sutil.
4. **Confiança errada** — achado confabulado, ou verificação manual como gargalo.
5. **Sem freio** — nada mecânico impede scope slip, rate alto contra WAF, ou request destrutivo.

**Meta:** cada sessão de caça compõe. A sessão N+1 começa mais forte que a N — sempre.

---

## 2. Arquitetura

Sem fork do opencode.

- **L1 — `hx`**: CLI Python + estado em arquivo. Agnóstico de LLM.
- **L2 — opencode**: agentes `hunt` / `hunt-auto` + plugin (tripwire/audit) + commands.
- **L3 — runner** (fase 2): supervisor headless que transforma work orders em sessões curtas e adjudica resultados.

### 2.1 O que cada mecanismo realmente cobre

Honestidade obrigatória — nada aqui é sandbox:

| mecanismo | cobre | NÃO cobre |
|---|---|---|
| `hx run` | tudo que passa por ele: scope, rate, staging, redação, evidência | o que não passa por ele |
| permissions do opencode | chamadas de tool dentro do opencode (`curl *: deny` etc.) | bash via outros meios, subagentes com outras perms |
| plugin (`tool.execute.before`) | binários conhecidos + hosts em URL nos args de `bash`/`webfetch` | `python -c`, script salvo, binário fora da lista |
| `hx proxy` (novo, fase 1) | todo cliente que honra `HTTP(S)_PROXY` — curl, `python requests` (honra por default), httpx, a maioria dos scanners — **com rate de conexão compartilhado** | sockets diretos, clientes com `trust_env=False`, Chrome sem flag; rate mais grosseiro que o do `hx run` (CONNECT esconde requests) |
| v2 (netns/nftables por uid) | processo não-cooperativo | box single-user com sudo: resíduo permanece |

**Threat model explícito: isso defende contra drift e erro, não contra adversário.** Um agente decidido a furar consegue — o objetivo é que o caminho convencional seja o caminho guardado, e que qualquer desvio deixe rastro no `audit.log`.

```
hx brief ──► sessão (LLM) ──► hx next ──► hx run ──► evidência
    ▲                                                    │
    └── hx result ◄── /debrief ◄── hx verify ◄───────────┘
                                      │
                                 FINDINGS/ (só PASS)
```

---

## 3. Workspace

Projeto: `/home/ngix/huntbench/`

```
huntbench/
  bin/hx                    # arquivo único Python (stdlib + curl_cffi)
  opencode/
    agents/hunt.md          # agente interativo
    agents/hunt-auto.md     # agente headless (fase 2, nasce junto)
    plugin/hunt.ts          # brief + tripwire + audit + shell.env (proxy)
    commands/brief.md       # /brief
    commands/next.md        # /next
    commands/debrief.md     # /debrief
  templates/scope.json      # starter de escopo
  templates/HUNT.md         # protocolo de sessão (vai pro engagement)
  tests/test_hx.py          # stdlib, sem framework
  install.sh                # symlinks + instrução de config
  specs/                    # este documento
  runner/runner.py          # fase 2
```

Engagement (ex.: `acme-bbp/`) ganha:

```
<eng>/hunt/
  scope.json
  TARGET.md
  COVERAGE.md
  HYPOTHESES.json
  FINDINGS/
    drafts/
  pending/                  # mutações estagiadas esperando o dj
  sessions/
  audit.log
  .ratelimit.json
  .health.json
  runner.pid                # fase 2
  .gitignore                # ignora evidence/, sessions/, scope creds
```

`recon/`, `scans/`, `notes/` continuam onde estão. Evidência nova vai pra `scans/evidence/<hyp-id>/`.

---

## 4. Estado (schemas)

### 4.1 `scope.json`

```json
{
  "engagement": "acme-bbp",
  "in_scope": ["acme.com", "login.acme.com", "collaboration.acme.com"],
  "out_of_scope_hosts": [
    "wpvip.acme.com", "vpn.acme.com", "test-login.acme.com", "desktop.acme.com",
    "partners2.acme.com", "greenvestrentals.acme.com", "*.rentals.acme.com",
    "destinations.acme.com", "engineering.acme.com", "test-vpn.acme.com",
    "acmefund.org", "acmecasting.com", "acme.jobs", "acme.gladly.com", "foryourbenefit-acme.com"
  ],
  "out_of_scope_paths": ["/used", "/rentals", "/garage", "/lists", "/blog"],
  "allow_extra_hosts": ["api.mail.tm", "hackerone.com"],
  "rate": { "per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30, "adaptive": true, "step_up_max_s": 30, "decay_clean_min": 60 },
  "health": {
    "probes": [
      { "session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/mobile-gateway/rest/cart/V3", "headers": { "X-Requested-With": "ACME-Android", "User-Agent": "ACME-Android/1.0 (Android 14)" } },
      { "session": "a", "file": "recon/session_a.json", "method": "GET", "url": "https://www.acme.com/mobile-gateway/rest/cart/V3", "headers": { "X-Requested-With": "ACME-Android", "User-Agent": "ACME-Android/1.0 (Android 14)" } }
    ],
    "fresh_max_s": 600,
    "session_invalid_if": ["status:401", "status:403", "redirect_host:login.acme.com", "body_contains:logonId"],
    "waf_block_if": ["body_contains:_px", "body_contains:px-captcha"],
    "rate_limited_if": ["status:429", "status:503"]
  },
  "allowed_mutations": [
    "POST /rest/user",
    "POST /rest/user/login",
    "POST /mobile-gateway/rest/user/guest/V1",
    "POST /mobile-gateway/rest/user/V1",
    "POST /mobile-gateway/rest/cart/items/V1"
  ],
  "evidence_dir": "scans/evidence"
}
```

Semântica de host: `in_scope` casa o domínio e subdomínios (exceto os banidos). `out_of_scope_hosts` ganha de `in_scope`. Path banido casa exato ou por segmento (`/used` pega `/used` e `/used/x` — não `/usedcars`).

`allowed_mutations`: lista curada pelo dj de mutações benignas de provisionamento (login, signup próprio, próprio carrinho). **Não existe detecção semântica de "destrutivo" — o default é staging (ver 5.1).**

`rate`: os números são **piso conservador, não medição calibrada** (ver §9.1). Dentro do run o intervalo só sobe (step-up ×2 em sinal de health, teto `step_up_max_s`); **entre runs ele decai** — a cada `decay_clean_min` de tráfego limpo desce um passo em direção ao piso — e há reset manual `hx rate reset` (TTY). Sondar o limite do WAF de propósito é proibido — calibração é por observação.

`health`: define os **probes — um por sessão usada** (attacker, vítima, guest) — e os sinais concretos que disparam pausa (ver §9.1). `hx init` exige o bloco — sem ele o runner não spawna. Probes aceitam `headers` opcional (alvos que bloqueiam por header — ex.: o gateway do ACME exige `X-Requested-With: ACME-Android`; sem ele o Akamai responde 403 "Access Denied" e o probe classifica waf).

### 4.2 `HYPOTHESES.json`

```json
[
  {
    "id": "h012",
    "status": "open",
    "claim": "BOLA read: sessão B lê giftcard da conta A via gateway mobile",
    "endpoint": "GET /mobile-gateway/rest/user/giftcard/{cardId}",
    "class": "BOLA",
    "confirm": "200 contendo dados do cartão de A (last4/marca)",
    "refute": "401/403/404, ou 200 sem dados de A",
    "source": null,
    "evidence": "scans/evidence/h012",
    "owner": null,
    "created": "2026-09-15T12:00:00Z",
    "closed": null,
    "result": {"verdict": null, "note": null, "evidence": null}
  }
]
```

Status: `open → claimed → running → result`, mais `dropped` (poda manual — fora da fila; `result` só vira `dropped` com `--force`). Vereditos: `confirmed | refuted | blocked | unverified`. `source` registra a origem (`planner` marca propostas do planner; `hx next` e o brief imprimem a marca `[planner]`).
`owner` = tag da sessão (`HX_SESSION` ou `pid-N`). Claim pega lock; TTL de 30 min devolve pra `open` com nota. `hx next` **claima** (ver 5); `--peek` só mostra. `result` carrega `session_tag` + `closed_ts` — insumo da invalidação retroativa (§9.1). `refuted` sobre auth-failure pode ser **reaberto automaticamente** quando a sessão usada é declarada morta em janela que o contém (`reopened_reason: session_death_window` — vale como motivo novo). `session_tag` deve casar com `health.probes[].session` — `hypothesis add` e `run` validam contra os probes e recusam mismatch (fail-loud, exit 2). Escrita atômica: temp + rename.

### 4.3 `COVERAGE.md`

Tabela, uma linha por célula endpoint × classe:

```
| endpoint | classe | status | evidência | nota |
|---|---|---|---|---|
| GET /mobile-gateway/rest/user/giftcard/{cardId} | BOLA | refuted | scans/evidence/h012 | 401 "user does not own this list" |
```

Status: `untested | confirmed | refuted | blocked`. Negativo entra com motivo (não retestar). `hx result` atualiza linha quando `endpoint`+`class` casam; senão o debrief edita à mão.

### 4.4 `TARGET.md`

Modelo denso do alvo, 100–150 linhas, seções: assets, auth, objetos/IDs, stack/WAF, quirks, intel de sessões. Só debrief atualiza. Feito pra LLM ler, não pra ler bonito.

### 4.5 Demais

- `sessions/AAAA-MM-DD-NN.md` — debrief por sessão (manual via `/debrief` ou determinístico via `hx debrief`).
- `audit.log` — JSONL: `{ts, actor, tool, args_digest, verdict, reason}` (plugin, proxy, hx).
- `.ratelimit.json` — `{host: last_ts, global: last_ts, effective: {host: interval}, events: [{ts, cause}]}`, com lock, compartilhado entre processos (hx run + proxy).
- `.health.json` — estado por sessão: `{tag: {strikes, dead_since, suspect_since, probes: []}}`.
- `pending/<id>.json` — mutação estagiada: request completo + motivo + timestamp. Só o dj executa (5.2).

---

## 5. hx CLI

Arquivo único `bin/hx`, Python ≥3.12, stdlib + `curl_cffi` (TLS impersonate `chrome131`, já validado contra o Akamai do alvo). `fetch()` isolado numa função substituível (testes usam stub).

Localização do engagement: env `HX_ENGAGEMENT` ou walk-up do cwd até achar `hunt/scope.json`.

| comando | contrato |
|---|---|
| `hx init [--dir .]` | cria `hunt/` + scope.json do template + HUNT.md + .gitignore; aborta se já existe |
| `hx brief [--max-bytes 4000]` | digest: alvo (1 linha), armadilhas de escopo, contadores de coverage, top hipóteses abertas, rabo do último debrief |
| `hx next [n] [--peek]` | **claima** e imprime as próximas n (default 3) como work orders: claim, confirm, refute, endpoint, dir de evidência. `--peek` não claima |
| `hx release <id>` | devolve hipótese ao estado `open` (dono ou `--force`) |
| `hx hypothesis add ... [--source planner]` | enfileira com id `hNNN`; `--source` registra a origem |
| `hx hypothesis drop <id> [--force]` | poda: status vira `dropped` (fora da fila); `result` exige `--force` |
| `hx debrief` | passe determinístico pós-wave: lê `runs.jsonl` + verdicts + coverage desde o último debrief e escreve `sessions/AAAA-MM-DD-NN.md` |
| `hx result <id> --verdict V --note N [--evidence PATH]` | fecha hipótese; atualiza COVERAGE; recusa `confirmed` sem evidência; refute por auth-failure **dispara o probe on-demand** da sessão usada (§9.1) e só fecha com sessão viva; exige ser o dono (ou `--force`) |
| `hx run <METHOD> <url> ...` | ver 5.1 |
| `hx pending list \| show <id> \| run <id>` | ver 5.2 |
| `hx proxy [--port 8899]` | proxy CONNECT com allowlist; loga tudo; ver 5.3 |
| `hx health check [--session TAG \| all]` | roda os probes, atualiza strikes/janelas, dispara varredura de reabertura quando declara morte; ver 9.1 |
| `hx rate show \| reset` | estado do rate adaptativo; reset exige TTY (exit 7 sem) |
| `hx verify <draft.json> [--attest]` | ver seção 8; `--attest` exige TTY (exit 7 sem) |

### 5.1 `hx run` — a única saída de rede

Ordem de checagem:

1. host/path contra scope.json — violação = exit 2 com motivo;
2. classificação do método:
   - `GET`/`HEAD` → segue;
   - não-GET que casa `allowed_mutations` (match `METHOD /path`, prefixo) → segue;
   - **qualquer outro não-GET → staging: escreve `pending/<id>.json` e sai com exit 5.** Nunca executa. (Padrão sintático não decide "destrutivo" — o default é humano.)
3. rate: espera até o intervalo corrente (`--max-wait`, default 30s), estourou = exit 3. O intervalo é piso: sinais de health fazem step-up (×2, teto do config) e nunca step-down no mesmo run;
4. sessão: estado `dead` → exit 6; `suspect` → registra, sobe a cadência de probe e segue;
5. executa.

**Pipeline de captura (ordem explícita):** fetch → resposta crua em memória → (a) marcadores **declarados** (`--marker SPEC` no run; no verify, os do cenário) são extraídos **agora, do cru** — nunca inferidos; (b) comparação/verify roda em memória; (c) só então redige e persiste.

- headers `Cookie`, `Set-Cookie`, `Authorization`, `*token*` → `sha256:<12>` — igualdade sem segredo; só vale pra valor de alta entropia. Segredo curto/baixa entropia **é dropado, não hasheado** (hash de baixa entropia é bruteforçável e protege nada);
- body de request: campos `password|passwd|secret|token|csrf|sessionid` → mesmo tratamento;
- body de response: persistido redigido (emails, telefones, PAN, campos sensíveis). **Marcadores declarados são isentos** — vivem num bloco `proof` à parte, porque são a prova mínima deliberada;
- `integrity.raw_sha256` do original = **checagem de integridade/tamper, não confidencialidade**;
- arquivo: `{HHMMSS}_{method}_{path}.json` com request/response redigidos + `proof` + `integrity` + meta (session alias, hipótese, exit). Store `--session FILE` no formato cookie-list já usado em `recon/session_*.json`.

**Regra dura:** extração de marcador nunca roda sobre cópia persistida — o verify refaz os fetches e extrai do cru em memória. A cópia redigida é registro, não insumo de verificação. Redação é heurística (regex), documentado. Sem flag de captura crua no v1.

Exit codes: `0` executou (mesmo 4xx/5xx); `2` guard/scope; `3` rate excedeu espera; `4` falha de transporte; `5` staged (aguarda o dj); `6` sessão morta/suspeita (recusa); `7` precisa TTY (pending run, attest, rate reset).

`--json` emite um objeto por execução (status, headers-chave, snippet, evidence path).

### 5.2 `hx pending` — mutações são do dj

- `hx pending list` — mostra estagiadas.
- `hx pending run <id>` — **exige TTY interativo** (`stdin.isatty()`), imprime o request na íntegra e pede confirmação digitada (o host do alvo). Sem TTY → recusa com exit 7. O mesmo gate vale para `hx verify --attest` e `hx rate reset`. O bash do opencode não é TTY — o agente não consegue confirmar por esse caminho.

Resíduo honesto: um agente que emule pty consegue furar isso — fora do threat model (drift, não adversário); o audit registraria a emulação. Em box single-user com mesmo uid, **não existe separação estrutural completa** entre "o dj aprovou" e "o agente aprovou"; por isso a arquitetura é: o agente *nunca executa* mutação fora de `allowed_mutations` — ele só prepara.

### 5.3 `hx proxy` — enforcement para clientes cooperativos

Proxy CONNECT em `127.0.0.1` (default `:8899`) com allowlist do scope.json. Host fora → 403 + registro no `audit.log`. Cobre exatamente o buraco do review: `python -c "import requests"` honra `HTTPS_PROXY` por default, curl também, a maioria dos scanners também.

O plugin injeta as variáveis (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY=localhost,127.0.0.1`) via hook `shell.env` quando a sessão roda num engagement. Chrome/agent-browser não honra env: precisa de `--proxy-server` (documentado no HUNT.md). Sockets diretos furam — v2 (netns/nftables).

**Rate no proxy:** ele **compartilha o `.ratelimit.json`** com o `hx run` e aplica rate de **granularidade de conexão** — intervalo mínimo entre novas conexões por host (mesmo piso do scope.json, com burst pra navegação) — porque CONNECT esconde os requests dentro do TLS. É mais grosseiro que o rate por request do `hx run`; a divisão fica: scriptado → `hx run` (request-granular), navegação → proxy (conexão-granular + audit). Resíduo anotado: cliente cooperativo com keep-alive abre poucas conexões e muitos requests — fora do alcance dessa camada; tripwire + audit cobrem o resto. O audit conta conexões e requests por host pra inspeção.

---

## 6. Camada opencode

### 6.1 Agente `hunt` (interativo)

`opencode/agents/hunt.md`, `mode: primary`. Corpo = disciplina de work order: lê brief, trabalha `hx next`, evidência via `hx run`, fecha via `hx result`, encerra com debrief. Nunca conclui "não tem vuln" — conclui hipóteses.

```yaml
permission:
  edit: allow
  bash:
    "*": ask
    "curl *": deny
    "wget *": deny
    "hx *": allow
```

### 6.2 Agente `hunt-auto` (headless, fase 2)

Gêmeo sem `ask` (em headless `ask` trava): bash só `hx *` allow, resto deny; edit allow no engagement. **Instalação padrão mantém só o agente `hunt`** (experiência de agente único); o runner **não** instala symlink — ele injeta a definição do `hunt-auto` **inline** no spawn: `OPENCODE_CONFIG_CONTENT` leva o agente (prompt lido de `opencode/agents/hunt-auto.md`, permissions inline no runner) + `default_agent: hunt-auto`. A TUI do dj nunca vê o gêmeo.

### 6.3 Plugin `hunt.ts` — tripwire e ritual (não é sandbox)

Três hooks:

- `experimental.chat.system.transform` — se o diretório da sessão tem `hunt/HYPOTHESES.json`, roda `hx brief --max-bytes 4000` e injeta no system. Sessão nova nasce com o mundo.
- `shell.env` — injeta as variáveis de proxy do `hx proxy` para shells em engagement.
- `tool.execute.before` — **tripwire** de `bash`/`webfetch`/`websearch`: extrai hosts (URL + heurística) e bloqueia host fora do escopo e binários de scan conhecidos (`curl|wget|nuclei|ffuf|naabu|dnsx|httpx|katana|subfinder|s3scanner|nmap|masscan`) apontados pra fora. Loopback passa. Falha-ao-fechar: `hunt/` presente com scope.json ilegível → bloqueia rede. Toda decisão vai pro `audit.log`.

Limite declarado (review do dj): `python -c`, script salvo e binário fora da lista passam — para esses, a cobertura vem do proxy (se honrarem env) e do audit. O plugin **não é** a camada que "impede"; é a que *denuncia*.

### 6.4 Commands

`/brief` (digest na hora), `/next` (claima work orders), `/debrief` (template: session log, hipóteses/coverage/TARGET, novas).

### 6.5 Install

`install.sh`: symlinka `agents/*.md` → `~/.config/opencode/agents/`, `commands/*.md` → `~/.config/opencode/commands/`, `bin/hx` → `~/.local/bin/hx`; imprime a linha de plugin pro `opencode.jsonc`:

```json
"plugin": ["@dietrichgebert/ponytail", "superpowers@git+https://github.com/obra/superpowers.git", "file:///home/ngix/huntbench/opencode/plugin/hunt.ts"]
```

Edição do config é manual (uma linha; jsonc não se edita por script sem risco). **opencode não recarrega a quente — reiniciar após instalar.**

---

## 7. Protocolo de sessão

Vive no `HUNT.md` do engagement + prompt do agente.

- **Início:** brief injetado → ler hipóteses abertas → `hx next` (claima) → trabalhar item a item.
- **Durante:** toda saída de rede via `hx run`; hipótese nova → `hx hypothesis add`. **Mutação fora de `allowed_mutations` nunca é executada pelo agente: `hx run` estagia (exit 5) e o dj decide.** O agente nunca usa a palavra "destrutivo" como passe — a arquitetura não tem passe.
- **Refutação por auth-failure exige sessão viva:** `result` recusa fechar `refuted` sobre 401/403/redirect-login sem health fresco da sessão usada (§9.1) — e resultado fechado dentro de uma janela de morte de sessão é reaberto automaticamente (`session_death_window`).
- **Fim:** `/debrief` → `sessions/AAAA-MM-DD-NN.md` (o que rodou, vereditos, hipóteses novas, deltas de coverage, dúvidas abertas) + TARGET.md se aprendeu algo.

Regra de ouro do protocolo: **work order é claim, não pergunta.** "Confirme ou refute isto" — nunca "procure problemas".

---

## 8. Verificação (`hx verify`) — registry de cenários

Draft em `hunt/FINDINGS/drafts/<hid>.json` (id do draft == id da hipótese) com `"scenario"` explícito:

```json
{
  "id": "h001",
  "title": "BOLA read em giftcard via gateway mobile",
  "scenario": "differential",
  "attacker_session": "recon/session_b.json",
  "victim_session": "recon/session_a.json",
  "request": { "method": "GET", "url": "https://.../user/giftcard/{cardId}", "victim_object": {"cardId": "..."} },
  "marker": { "extract": "regex/campo do marcador (ex.: last4)", "expect_in_attacker_response": true },
  "control": { "attacker_own_object": {"cardId": "..."} }
}
```

Cenários:

| cenário | classe | prova mecânica |
|---|---|---|
| `differential` (v1) | BOLA, IDOR, ATO | 3 fetches: vítima extrai marcador M → atacante com objeto da vítima deve conter M → controle (objeto próprio do atacante) não pode conter M |
| `echo` (v1) | XSS refletido | payload canônico `<hx{CANARY}>"'` com canário único por run (`secrets.token_hex(6)`; match é contra o canário **atual**, nunca um `<hx` genérico — mata confirm por cache/CDN de run anterior); contrato de substituição: `{payload}` → URL-encoded, `{payload_raw}` → cru; PASS mecânico = 2xx **e** payload cru presente **e** formas escapadas (`&lt;hx{canary}`, `&#60;hx{canary}`, `&#x3c;hx{canary}`) ausentes; **sempre `pass_requires_attest`** — "não-escapado" não prova contexto executável; evidência guarda snippet em volta do match + content-type |
| `callback` (v1) | SSRF, XXE, blind | colaborador **BYO webhook.site** (dj cria URL/token no browser e cola `collaborator.url` + `collaborator.poll.url` no draft **antes** do verify — pré-condição humana, como a sessão no differential; criação via API exige key, futura `HX_WEBHOOKSITE_KEY`); snapshot do inbox antes (controle) → request ao alvo com `{collaborator}` via `guarded_fetch` → poll com sleep (default 3s, min 1s, teto no `interaction_timeout_s`) até interação; PASS = interação **nova** pós-snapshot; polls são infra nossa — fora do `check_scope`/`rate_acquire`, mas com sleep educado, auditados e salvos |
| `manual-attested` | race, lógica de negócio, TUDO que não tem cenário | checklist estruturado + artefatos nomeados + **assinatura do dj com TTY obrigatório**; veredito próprio `verified-manual` — nunca promovido automaticamente |

Regra de cobertura: **classe sem cenário cai em `manual-attested` por default.** `confirmed` sem cenário PASS **e** sem assinatura = rejeitado pelo `hx result` e pelo verify. Isso define "evidência suficiente" por construção: ou é cenário que passou, ou é atestação humana com artefatos.

**Gates do callback (v1):** timeout **nunca** autoriza `refuted` — sem interação pode significar "não é SSRF" tanto quanto "egress do alvo bloqueia domínio externo", "DNS falhou" ou "colaborador fora do ar" (mesma classe de ambiguidade da sessão morta no differential). `no_interaction_timeout` só justifica `blocked` ou `unverified` (reteste com outro colaborador/variante). Mecanicamente: `hx result --verdict refuted` é **recusado** (exit 2) quando o draft da hipótese tem `fail_reason: no_interaction_timeout`, salvo `--force` com prova positiva de ausência.

**`guarded_fetch` único:** os fetches de **todos** os cenários do verify (differential, echo, callback) passam pelo mesmo helper `guarded_fetch` — `check_scope` → método estagiado → `rate_acquire` → `fetch` → `apply_health_signals`. A classe de bug "verify não reage a WAF/rate" não pode voltar; cenário novo que fizer fetch fora do helper é defeito de implementação.

**Proof fresco ou checklist:** um FAIL invalida qualquer PASS-proof pendente — `proof` e `pass_requires_attest` são descartados no caminho de falha, então o `--attest` reflete sempre a execução mecânica **mais recente**; sem prova fresca, a assinatura vira checklist manual (`sem prova mecanica`). E o snapshot do colaborador (callback) é **estrito**: status 2xx + JSON válido, senão `EXIT_TRANSPORT` — baseline não pode degradar para "inbox vazio" em silêncio.

**Gates de promoção (v1):** os fetches do `differential` exigem status 2xx pra contar — resposta de erro nunca vira PASS. Marker com menos de 6 caracteres = **PASS fraco**: não promove sozinho, exige `--attest` (evita last4 genérico bater por acaso em página de erro). E o draft id == id da hipótese, que é o que liga `hx result --verdict confirmed` ao finding verificado em `FINDINGS/<hid>.json`. Os fetches do `differential` passam pelo mesmo `apply_health_signals` do `run` — 429/WAF sobem o rate como qualquer tráfego.

`manual-attested` executa: imprime checklist (escalada de contexto? segundo teste independente? payload reprodutível?) e exige `hx verify <draft> --attest` **em TTY interativo** — sem TTY, exit 7; com TTY, pede o **id do draft digitado** e grava `uid`, tty e ts no artefato. O agente não origina `verified-manual`: é o mesmo gate do `pending run` — e o mesmo resíduo documentado (pty emulado; threat model: drift, não adversário).

**Marcadores × redação (ordem fixa):** marcadores são sempre **declarados**, nunca inferidos; o verify os extrai dos **fetches vivos**, em memória, antes de persistir qualquer coisa. A cópia redigida nunca alimenta o cenário — se o arquivo persistido não contém o marcador, nada quebra: o verify extrai de novo do cru. No bloco `proof` da evidência, o marcador declarado é isento de redação (é a prova mínima).

---

## 9. Runner L3 — fase 2

Loop por worker (**1 worker = 1 sessão viva por vez**; cada fatia é uma sessão curta que morre ao reportar — sem contexto acumulado; uma wave = até `slices_max` sessões **sequenciais**):

```
CLAIM → BUILD_PROMPT → INVOKE → PARSE → (RETRY | RESULT → ROTATE)
```

- **CLAIM** — próxima `open` com lock atômico (mesmo mecanismo do `hx next`); marca `claimed` com `owner`. Sem fatia livre: work order = hipótese (claim + confirm/refute + dir de evidência) + extrato do brief.
- **BUILD_PROMPT** — template: alvo + hipótese + extrato do brief + protocolo + budget da fatia.
- **INVOKE** — `opencode run`, cwd = engagement, `hunt-auto` via env, não-interativo.
- **PARSE** — `opencode export <session>` → tokens, custo, status, texto. Resultado entra como **candidato** (`runs.jsonl` + draft em `drafts/`) — nunca direto em FINDINGS.
- **RETRY** — falha de infra: backoff 5s→120s, até 3x, resume com feedback. Rate-limit/WAF: 30s→300s. Give_up por fatia: teto 15 min → `blocked` + rotaciona.
- **ROTATE** — grava, atualiza budget, volta pro CLAIM.

Paradas: fila vazia · budget (wall-clock 2h, 20 slices, tokens/custo via export) · health auto-pause (critério em §9.1) · kill switch (`runner.pid`, SIGTERM; estado sobrevive).

Segurança: rate global com lock — **1 worker default, ACME fica em 1**. Mutações: `hunt-auto` só tem `hx *`; `hx run` de mutação não-listada = exit 5 e o worker fecha a fatia como `blocked (staged)` — destrutivo é estruturalmente inalcançável pelo runner. Concorrência >1 só com gate manual.

- **Adjudicação (v1):** drafts criados pelo worker são adjudicados pelo **próprio `hx verify`**, como passe separado (subprocesso) — nenhum gate novo: `differential`/`callback` promovem mecanicamente quando passam; `echo` e `manual-attested` viram pendência pro dj (`--attest`).
- **Planner (Rev 8):** `runner.py --plan [--plan-n 5] [--model X]` — uma sessão headless esporádica (**entre** waves; **não** roda slices nem requests de alvo) com digest bounded (coverage/buracos + TARGET + rabo do último debrief + fila aberta + refutadas). O agente **adiciona** hipóteses via `hx hypothesis add --source planner` (cap no prompt; dedup instruída contra aberta/refutada equivalente). Propostas entram `open` e são podáveis com `hx hypothesis drop`. O planner **sugere**; `hx verify` e o dj decidem. Cada execução é registrada em `runs.jsonl` com `mode: plan`.

### 9.1 Health — critério concreto

Bloco `health` no scope.json (exemplo em 4.1) define **um probe por sessão usada** e os sinais. Estado em `hunt/.health.json`, por tag: `probes[]`, `strikes`, `dead_since`, `suspect_since`.

- **Strikes e morte:** resposta de **probe** que casa `session_invalid_if` incrementa o strike do tag; **2 strikes consecutivos** declaram a sessão morta com `dead_since` = ts do **strike 1** — a janela de morte começa no primeiro sinal, porque é isso que a morte retroativa significa. Resposta **não-probe** que casa os sinais não mata: vira evento de suspeita e a cadência de probe sobe (probe imediato).
- **Invalidação retroativa:** ao declarar morte, o `hx` varre resultados fechados com `closed_ts >= dead_since` que usaram aquele `session_tag` e **reabre** os afetados: status volta a `open`, `reopened_reason: session_death_window` — que vale como "motivo novo" para a regra de não-reteste. Nenhum falso-refutado por sessão morta fica preso.
- **Gate no fechamento (autossuficiente, sem beco):** `hx result --verdict refuted` cujo critério casou via auth-failure (401/403/redirect-login) exige **health fresco** da sessão usada — e é o próprio `result` que **dispara o probe on-demand** se o último check estiver velho (respeitando rate). Probe vivo → fecha com o check registrado. Probe morto → **recusa o refute (exit 6)**, registra strikes, sugere `blocked (session)` e, se a morte for declarada, roda a varredura de reabertura na hora. O agente nunca trava: a saída está no próprio retorno. Refutação por 200-sem-marcador não passa por esse gate.
- **Runner:** antes de cada fatia, roda os probes de **todas as sessões referenciadas pelas hipóteses** (união attacker/vítima/guest); tags com último check dentro de `fresh_max_s` são puladas (cache de validade). Sessão morta → fatias que dependem dela fecham `blocked (session)`, o resto segue. Ao declarar morte, roda a varredura de reabertura e registra no log.
- **Probes contam no rate:** são requisições reais ao alvo — mesma janela `.ratelimit.json`, mesmo audit, sem exceção. Não existe "tráfego de sistema" fora da contagem; o custo é amortizado pelo cache (`fresh_max_s`), não por contabilidade especial.
- **WAF:** 403 com `_px`/`px-captcha` → cooldown exponencial (30→60→120 min) + step-up de rate; retomada manual.
- **Rate-limit:** 429/503 → backoff 30s→300s; step-up (×2) **com decaimento entre runs** (`decay_clean_min`: um passo por hora limpa) e reset manual `hx rate reset` (TTY).

Sem bloco `health` configurado o runner **não spawna** — `hx init` exige.

### 9.2 Workers (fase 3.1) — N sessões vivas

`runner.py --workers N` (default 1): supervisor + N processos worker. Gate mecânico no start, recusa com exit 2 enquanto não passar: `N ≤ len(scope.health.probes)`, hipóteses `open` ≥ 20, `len(in_scope)` ≥ 2. A observação de campo (wave real antes de N>1) é decisão do dj registrada no debrief — não é código.

- **Sessão por worker:** worker i ↔ `probes[i].session`; `HX_SESSION` do worker é a tag, e o claim só pega `open` com `session_tag in (None, tag)` — dois workers nunca compartilham a mesma sessão. Com N=1 não há bind (comportamento Rev 8), senão fila tagged de outra sessão ficaria inalcançável.
- **Fila e rate:** claim atômico compartilhado (`.lock.hyps`); rate compartilhado (`.ratelimit.json`) — o piso global 0.5s vira o teto do conjunto no mesmo host.
- **Orçamento agregado:** `hunt/.runnerstate.json` sob `.lock.runner` (`{started_ts, slices, tokens, cost}`), incrementado ao fim de cada fatia; `check_stop` lê dali. `runner.stop` é global (para todos os workers); `runner.pid` é o supervisor; SIGTERM/SIGINT pedem stop.
- **Concorrência em arquivo:** `update_coverage` do hx passa a escrever `COVERAGE.md` sob `.lock.coverage` (dois `result` simultâneos não perdem linha).
- **Reconciliação por worker:** cada worker fecha a própria fatia órfã (`blocked`) e o registro em `runs.jsonl` ganha o campo `worker` (tag da sessão/slot).

### 9.3 Isolamento de rede dos workers (fase 3.2) — netns rootless

`runner.py --netns` (opt-in, default-off): cada worker de fatia roda em um netns próprio; fork+queue+orçamento da 3.1 ficam inalterados.

- **Arquitetura:** o supervisor re-executa uma vez em `unshare -Ur` (user namespace; permanece no netns do host e ganha CAP_SYS_ADMIN no userns) antes de qualquer spawn; cada worker de fatia cria só o netns (`os.unshare(CLONE_NEWNET)`) e o supervisor sobe um backend de NAT userspace (`slirp4netns`; `pasta` fica como alternativa futura no `NETNS_BACKENDS`) por worker, com handshake de prontidão; o worker aplica o nft do seu netns e só então roda a fatia. Um userns por worker não serve: o backend precisa de CAP_SYS_ADMIN no userns do worker para entrar no netns dele.
- **Pré-flight fail-closed:** `--netns` exige `unshare -Ur` funcional, backend instalado e `nft` presente; faltando qualquer um, o runner recusa (exit 2) — nunca cai para rede aberta.
- **Regras (por worker):** loopback accept; set IPv4 `allowed` accept; policy drop; IPv6 desligado; DNS sem regra (NSS via socket do systemd-resolved) e regra 53 só se o nameserver não for loopback; portas livres para IPs allowlistados (o limite é o host, como no scope).
- **Allowlist (fail-closed):** `in_scope` (apex) + host do endpoint da hipótese da fatia + hosts do TARGET.md + provedor LLM derivado de `~/.cache/opencode/models.json` + MCPs remotos do `opencode.jsonc` + extras `netns.allow_hosts` do scope.json; resolve **todos** os A records (`getaddrinfo` AF_INET); re-resolução no início de cada fatia. Falha de rede persistente (regras vazias) derruba o worker com erro — o runner retorna 1 e o host novo entra no allowlist no próximo start; sem provedor derivável e sem extra, o worker não roda.
- **Anti-tamper:** o agente é spawnado via `setpriv --bounding-set=-net_admin` (só a subárvore do agente perde `cap_net_admin`; o worker mantém a cap no userns para reaplicar o allowlist por fatia). Residual: o processo do worker é nosso código — threat model segue drift, não adversário.
- **Proxy:** o endpoint do proxy **não** entra no allowlist dos workers (netns rootless não alcança `127.0.0.1:8899` do host e o agente de fatia não tem browser/curl); o proxy permanece host-side para a TUI do dj. Desvio registrado do texto original de §3.2 do plano.
- **Escopo:** `--plan` segue no host (permissões não permitem tráfego ao alvo); com `--netns` até `--workers 1` roda como worker processado.
- **Gate de implementação:** um teste de composição com 2 workers `--netns` no fixture local (sem alvo) precede qualquer uso real; plano B documentado — netns único compartilhado por todos os workers — se a composição por worker falhar de forma estrutural.

### 9.4 Jev (System One) no harness — fase 4

Jev é **juiz tipado** (noul/choice/score), não gerador: probabilidades com thresholds no código. Opt-in por engagement; egress só de texto já redigido/derivado.

- **Núcleo:** `scope.json → "jev": {"enabled": false, "model": "jev-latest", "max_calls_per_run": 50, "thresholds": {"health": 0.8, "dedup": 0.85, "verify_high": 0.8, "verify_low": 0.5}}`; key em `TYPESAFE_API_KEY` (env). Toda chamada passa por `jev_ask` (urllib stdlib, POST `https://api.typesafe.ai/v1/systemone`, Bearer, timeout 10s) com: re-redação defensiva do payload (serializa → `redact_body` → reparse; falha = recusa), audit (`actor:"jev"`: caso, modelo, probs, threshold, ação, motivo), contador/teto em `hunt/.jev.json` (bucket = `HX_RUN_ID` quando setado pelo runner; senão hora corrente). `hx jev status` mostra config/budget/últimas decisões.
- **A. Health semântico:** status suspeito (401/403/redirect/429/503) → `choice {block|session_invalid|normal}` sobre `{status, url, excerpt ≤160 redigido}`; `block` → cooldown WAF + step-up; `session_invalid` → strike; `normal`/p < θ → nada. A substring mecânica segue como pré-filtro (401/403/429/503 fecham mecânico no probe; o Jev cobre 3xx e casos que o prefilter não pegar).
- **B. Dedup:** `hypothesis add` → candidatas (abertas + refutadas, cap 20 por classe/endpoint) → `choice {"new", "<id>"...}`; p(equivalente) ≥ θ_dedup → recusa (exit 2) salvo `--force`, que grava `duplicate_of`.
- **C. Priorização:** passe do planner → `score ["baixo","medio","alto"]` por hipótese aberta (cap pelo budget), gravando `priority`; `claim_next`/`hx next` ordenam operador-primeiro → `priority` desc → FIFO. Sem scores, ordem atual.
- **D. Segunda opinião (fail-closed):** `hx verify` com PASS mecânico → `noul` "a evidência sustenta a claim?" sobre resumo derivado (cenário, statuses, marcador, claim/endpoint, excerpt); p ≥ θ_alto promove; θ_baixo ≤ p < θ_alto promove com `jev: weak`; p < θ_baixo → draft retém com `pass_requires_attest` + `jev_fail`; **ligado e indisponível → não promove automático** (mesmo caminho de attest humano).
- **Fail modes:** A/B/C fail-open com `jev: unavailable` registrado; D fail-closed (acima).
- **Egress:** apenas excerpt ≤160 (já redigido), claim/endpoint/classe, contadores e resumo de draft — nunca corpo de evidência cru; sem `jev.enabled` nada sai.

---

## 10. Migração ACME

1. `hx init` no `acme-bbp`; SCOPE.md → scope.json (hosts, paths banidos, extras: mail.tm; `allowed_mutations` curadas: signup/login próprios, guest session, cart add).
2. TARGET.md ← "Observações técnicas" do `notes/findings.md` + "Ambiente do alvo" do SCOPE.
3. COVERAGE.md ← 14 linhas da matriz do findings.md (todas refuted/neutras, com motivo).
4. HYPOTHESES.json ← tasks do PLAN.md + gaps: semântica v=2/v=3 em user/orders e wishlist; login.acme.com (0 reports, intocado); transição guest→user (mutação de claims); collaboration.acme.com (0 reports); giftcards cross-account.
5. Instalar L2, reiniciar opencode, primeira sessão roda na bancada.

---

## 11. Testes e critérios de aceite

`tests/test_hx.py` (stdlib): (a) guard bloqueia host/path banido; (b) rate espaça por host (stub de tempo); (c) `next` claima e dois `next` não entregam a mesma hipótese; (d) `result` exige `confirmed` com evidência (cenário ou assinatura); (e) verify com transporte stub: PASS, FAIL-presente, FAIL-controle; (f) staging: non-GET fora de `allowed_mutations` → exit 5 + arquivo em `pending/`; `pending run` sem TTY → recusa; (g) redação: cookie/authorization nunca aparecem em claro no evidence salvo; (h) marcador declarado sobrevive à redação no bloco `proof`; email não-declarado some; (i) health: probe com sessão inválida em fixture → 1 strike segue, 2 strikes = morte; (j) refute sobre auth-failure com sessão morta → `result` recusa (exit 6); (k) morte declarada → resultado fechado na janela [dead_since, agora) é reaberto com `session_death_window`; (l) `--attest` e `rate reset` sem TTY → exit 7; (m) probes multi-sessão: fixture com vítima morta → fatia dependente bloqueada, fatia com a viva segue; (n) `result` refute com check velho → dispara probe on-demand (stub): vivo fecha, morto recusa e sugere `blocked`; (o) probe e run compartilham a mesma janela de rate (stub de tempo).

Smoke manual (ACME):
- `hx brief` ≤ 4KB, todas as seções.
- `hx run` GET guest 200; GET em `/blog` → exit 2; POST não-listado → exit 5.
- `hx proxy` no ar; `python3 -c "import requests; ..."` fora do escopo → negado; in-scope → passa; duas conexões rápidas ao mesmo host → a segunda espera (rate de conexão).
- Health: fixture com sessão adulterada → 2 strikes = morte com `dead_since` no strike 1; resultados fechados na janela reabertos; 1 strike isolado não pausa.
- Plugin: `curl` fora → bloqueado; `hx run` passa.
- Sessão 2 do engagement: brief nasce injetado; nenhuma célula `refuted` retestada sem motivo novo.

Aceite do L3: 1 worker respeitando caps; fila vazia = para; kill funciona; candidato nunca pula o verify; audit sem violação de rate.

---

## 12. Não-objetivos

Sem fork do opencode · sem MCP no v1 · sem custom tools no v1 · sem DB · sem GUI · **sem venda de sandbox: em box single-user com mesmo uid não existe isolamento total — o que existe é hx + proxy + tripwire + audit, contra drift, não contra adversário** · sandbox real (netns/nftables por uid) = v2 · nada específico de provider externo.

---

## 13. Fases

- **Fase 1:** L1 (`hx` + estado + `hx proxy`) + L2 (agentes, plugin, commands, install) + migração ACME + smoke.
- **Fase 1.5:** cenários `echo` e `callback` no verify (Rev 6 — implementação em `plans/2026-09-16-phase15-verify-scenarios.md`).
- **Fase 2:** L3 runner v1 — executa a fila (claim→prompt→invoke→parse→retry/rotate), adjudicação via passe de `hx verify`, budgets/stop/kill/health; smoke sintético antes de alvo vivo. **Fase 2.5 (Rev 8):** planner (`runner.py --plan`) + debrief determinístico (`hx debrief`) + `source`/`drop` na fila.
- **Fase 3 (3.1 mergeada em 2026-09-17, Rev 10):** 3.1 workers `--workers N` com gate mecânico e sessão por worker (§9.2); 3.2 isolamento netns rootless por worker (§9.3); 3.3 scheduling. A 3.0 (wave de campo no alvo real) continua pré-requisito declarado para N>1 em alvo vivo — decisão do dj no debrief; o gate em código já recusa sem os critérios. Detalhamento em `plans/2026-09-16-phase3-campo-paralelismo.md`.

---

## 14. Riscos e mitigações

| risco | mitigação |
|---|---|
| Ban por volume (Akamai/PerimeterX) | piso conservador; step-up só sobe dentro do run, decai entre runs (`decay_clean_min`) e tem reset humano (TTY) + rate compartilhado no proxy (conexão) + 1 worker + health com critério (§9.1) |
| Flood de falso-positivo | candidato ≠ achado; `hx verify` (cenário ou atestação) promove |
| Queima de orçamento | caps de wall-clock/slices/tokens/custo; give_up por fatia |
| Drift de escopo | hx run (scriptado) + proxy (clientes cooperativos) + tripwire (denúncia) + audit — **cobertura declarada na tabela 2.1** |
| Mutação acidental | default-staging + execução só pelo dj com TTY; agente não tem caminho mecânico |
| PII/credenciais em evidência | redação na captura; alta entropia → hash de igualdade; baixa entropia sensível → dropada (hash não protege — sem falsa sensação); marcador declarado = prova mínima isenta; `raw_sha256` = integridade, não sigilo; sessions/evidence no .gitignore |
| Falso-refutado por sessão morta | gate de health fresco no `result` (refute por auth-failure) + invalidação retroativa pela janela de morte; `session_death_window` conta como motivo novo |
| Probe inflando o ritmo real | probes contam na mesma janela de rate, com cache `fresh_max_s` — o piso declarado é o piso real, sem "tráfego de sistema" |
| Estado corrompido | escrita atômica + locks + TTL de claim |
| Bypass deliberado (agente hostil) | fora do threat model — documentado; audit registra o rastro; gate TTY vale para `pending run`, `--attest` e `rate reset` (pty emulado = resíduo documentado) |
