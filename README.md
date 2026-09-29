# huntx

A hunt harness for LLM-driven security work: **the hunt state lives outside the model.**

LLM sessions are amnesiac, over-eager, and careless with open network access. huntx is the bench around them: durable memory, work orders, scope guardrails, mechanically verified findings.

## Components

- **`hx`** — single-file Python CLI (curl_cffi): hypothesis queue (claim/TTL), endpoint×class coverage, byte-bounded `brief`, adaptive rate (step-up on 429/WAF, decay between runs), scope guard with normalized path matching (exit 5 stages the mutation; humans execute in a TTY — `hx pending list/show/run/drop`, item parado há mais de 7 dias aparece `[stale]`), `run` with capture-time redaction and `raw_sha256` evidence, session health (strikes, death window, retroactive reopen), `verify`: differential + echo + callback (attest-gated where proof is contextual; the callback poll host must be registered in `in_scope`/`allow_extra_hosts` and is rate-limited like any egress), manual attestation, allowlist CONNECT proxy sharing the rate state (with a navigation burst for browsing). Claims are owned by the session tag — `HX_SESSION`, else the controlling TTY, else the PID.
- **`opencode/`** — integration layer: `hunt` agent (work-order prompt + permissions), plugin (brief injection, stable `HX_SESSION` per conversation, proxy env, tripwire with audit log, `scope.json` write protection), `/brief` `/next` `/debrief` commands, `install.sh`.
- **`templates/`** — starter `scope.json` and session protocol.

## Design rules

- **Work orders, not open questions.** "Confirm or refute this claim" — never "find bugs".
- **Evidence is redacted at capture.** High-entropy secrets → `sha256:<12>`, low-entropy dropped, PII redacted (phone only with separator shape, card numbers only when Luhn-valid — datas, versões e IDs públicos passam intactos); the proof marker lives in an exempt block.
- **Staged mutations keep the raw request.** `hunt/pending/<id>.json` holds the exact method/URL/headers/body so the operator can replay it verbatim; the directory is gitignored.
- **Mutations are staged by default.** Anything outside `allowed_mutations` exits 5; only a human executes it, in a TTY, typing the host.
- **`scope.json` is not writable by the agent.** The plugin refuses `edit`/`write` on it: scope changes are the operator's call, never the model's.
- **Refutations need a live session.** 401/403/redirect-login refutes fire an on-demand probe; a dead session reopens results closed inside the death window.
- **A finding exists only when verified.** `FINDINGS/<id>.json` with a scenario PASS or a signed attestation — `result --verdict confirmed` rejects everything else.
- **The tripwire is a fence, not a sandbox.** It blocks the convenient paths (scheme-less egress binaries, subshells/backticks with an out-of-scope host, interpreters without an in-scope literal, `/dev/tcp`, non-http schemes in `webfetch`) and audits every decision in `hunt/audit.log`; `hx run` remains the only sanctioned exit.

## Quickstart

```bash
bash install.sh   # symlinks into ~/.config/opencode + ~/.local/bin/hx
# add to the "plugin" array of ~/.config/opencode/opencode.jsonc:
#   "file:///path/to/huntx/opencode/plugin/hunt.ts"
# restart opencode

mkdir -p ~/hunts/target && cd ~/hunts/target
hx init --dir .
# edit hunt/scope.json: hosts, banned paths, rate, session probes
hx hypothesis add --claim "..." --endpoint "GET /api/x" --class BOLA --confirm "..." --refute "..."
hx brief
hx next
```

## Runner (opt-in)

The runner drives a **headless agent runtime**: `opencode` (default when both are installed) or `omp`. Pick explicitly with `--runtime opencode|omp`; without the flag it uses `opencode` if it is on `PATH`, else `omp`.

```bash
python3 runner/runner.py --engagement ~/hunts/target --slices 5 --wall 3600              # opencode (default)
python3 runner/runner.py --engagement ~/hunts/target --runtime omp --slices 5 --wall 3600
```

One worker, one slice at a time: claims from the queue, spawns `opencode run` with a `hunt-auto` agent injected inline via `OPENCODE_CONFIG_CONTENT` (no symlink, nothing written to your opencode config), adjudicates the draft with `hx verify` when one exists, then appends the slice record to `hunt/runs.jsonl`. Budgets: `--slices`, `--wall`, and the optional `--max-tokens`/`--max-cost` caps (usage is summed over every attempt, including retries); the loop also stops on an empty queue. `touch hunt/runner.stop` is honored between slices and between retries — a stop also kills the in-flight slice's process group (no orphaned egress; the slice is not retried), and SIGINT/SIGTERM request the same stop. Resume with `rm hunt/runner.stop`; the latch is also cleared at the next runner start. A second runner on the same engagement is refused by a lock file, not only by the pid file. Slices spent on hypotheses whose session is already dead are returned to the budget. `--plan` exits non-zero when the planner session fails.

Com `--workers N` (fase 3.1): supervisor + N processos worker, cada um preso a uma sessão distinta de `scope.health.probes` (`HX_SESSION` + claim filtrado por `session_tag`); orçamento agregado em `hunt/.runnerstate.json`; `runner.stop` para todos. Gate no start (exit 2): `N ≤ probes`, hipóteses `open` ≥ 20, hosts in-scope ≥ 2 — a wave de campo (3.0) é decisão do dj registrada no debrief.

`--netns` (fase 3.2): cada worker roda num network namespace próprio com nft default-drop e allowlist por IP (escopo + host da hipótese, filtrado por `check_scope`, + hosts do TARGET.md + provedor LLM/MCPs derivados + host do Jev quando ligado + `netns.allow_hosts` do scope.json), re-resolvida a cada fatia; agente spawnado com `setpriv --bounding-set=-net_admin,-net_raw` (não mexe nas regras do nft e não abre socket cru, que escaparia do hook `output` do netfilter). Requer `slirp4netns`, `nft` e `setpriv`; o runner se re-executa em `unshare -Ur` antes de checar o preflight, então `--netns-selfcheck` funciona sem privilégio. Opt-in; o endpoint do proxy não é alcançável de dentro do netns (o agente de fatia não tem browser). Com `--netns`, o cenário callback do `hx verify` exige o host do colaborador em `netns.allow_hosts` (decisão do dj; nunca derivado do draft).

## Planner (opt-in)

```bash
python3 runner/runner.py --engagement ~/hunts/target --plan --plan-n 3
```

One headless planner session instead of a slice: it reads a digest (coverage, `TARGET.md`, last debrief, open queue, already-refuted pairs) and proposes up to `--plan-n` hypotheses with `--source planner` — it never touches the target. Proposals land in the queue marked `[planner]` in `hx next`/`hx brief`; the operator decides, and `hx hypothesis drop <id>` removes a proposal (a closed hypothesis needs `--force`). The record in `hunt/runs.jsonl` carries `mode: plan` and `proposed`.

## Debrief

```bash
hx debrief   # CLI twin of /debrief (deterministic, no LLM)
```

Deterministic, no LLM: writes `hunt/sessions/YYYY-MM-DD-NN.md` from `runs.jsonl`, `HYPOTHESES.json`, `COVERAGE.md` and the existing session files (numbering + cutoff) — runs/tokens/cost per hypothesis, verdicts, coverage counts, queue, and observation notes (timeouts, reconciliations) since the last debrief.

## Jev (ligado por padrão)

Ligado por padrão em todo engagement; desligue com `scope.json → "jev": {"enabled": false}`. Requer `TYPESAFE_API_KEY` no ambiente (sem key/via API fora, A/B/C degradam e D retém para `--attest`). Quatro usos: health semântico (`choice`), dedup no `hypothesis add`, prioridade da fila no planner (`score`) e segunda opinião no `hx verify` (`noul`, fail-closed: sem veredito, promoção exige `--attest`). Egress só de texto redigido: health manda excerpt ≤160; dedup manda endpoint/classe/claim[:80]; prioridade manda excerpt do TARGET ≤600; verify manda claim/cenário/steps (statuses, marcadores e flags; nunca corpos). O teto `max_calls_per_run` é configurado no `scope.json`; `hunt/.jev.json` guarda bucket/uso; `hx jev status` mostra config/budget/últimas decisões.

## CLI (hx)

```
hx init --dir .                          # cria o engagement (scope.json, protocolo, .gitignore)
hx hypothesis add --claim C --endpoint "GET /x/{id}" --class BOLA --confirm ... --refute ...
                       [--evidence DIR] [--session-tag TAG] [--source planner] [--force]
hx hypothesis drop <id> [--force]        # remove da fila (fechada exige --force)
hx next [N] [--peek]                     # claima N work orders (--peek só mostra)
hx release <id> [--force]                # devolve o claim para open
hx run METHOD URL [--session ARQ] [--data BODY] [--header "K: v"] [--save DIR]
                   [--marker REGEX] [--hypothesis ID] [--json] [--body-limit BYTES]
hx links [--dir DIR] [--limit N]        # URLs in-scope (+ robots) dos corpos de evidência
hx result <id> --verdict confirmed|refuted|blocked|unverified --note N [--evidence DIR] [--force]
hx verify <id> [--attest]                # cenário do draft, ou assinatura manual do dj
hx pending list | show <id> | run <id> | drop <id>    # mutações estagiadas (TTY)
hx health check [--session TAG|all]      # probes de sessão (strikes/death window/reopen)
hx rate show | reset                     # estado adaptativo; reset exige TTY
hx brief [--max-bytes N] | hx debrief | hx jev status | hx proxy [--port 8899]
hx ui --serve [--port 8137] [--dir ENG] [--interval 15] [--show-findings]
                                         # painel local read-only em 127.0.0.1
```

Exit codes: `0` ok · `2` guard/config · `3` rate (cooldown ou espera acima do teto) · `4` transporte · `5` mutação estagiada · `6` sessão morta/suspeita · `7` exige TTY.

## Tests

```bash
bash tests/run_all.sh          # tudo: unittest + node --test
python3 -m unittest discover -s tests -v                 # hx + runner + premissa e2e
node --test tests/                                       # tripwire (guard.js) + hooks do plugin
```

`tests/test_premise_e2e.py` é a prova de premissa: nada mockado — servidor HTTP local, cada comando `hx` como processo separado (amnesia entre invocações) e um PTY real para os gates de TTY (attestação, `pending run`). Cobre claim/dono entre processos, verify differential/echo/callback, sessão morta + reabertura retroativa, staging de mutação, guard de escopo (inclusive path normalizado), redação na captura e rate/cooldown.

Os testes do runtime `omp` são de unidade/fake (`OmpRuntimeTest` em `tests/test_runner.py`, `tests/omp_hook.test.mjs`): argv headless, dispatch por runtime, hook nos três modos (hx-only/guard/plan), parsing de `id`/`model_usage` da sessão, egress do provedor e orçamento somado. Um slice real com LLM não roda em teste automatizado (custa tokens).

## Runtime: omp

Nada é escrito na sua config do `omp`: a fatia roda com flags de linha de comando apenas.

- **Invocação headless**: `omp --print --auto-approve --no-title --no-skills [--model M] [--resume ID] --session-dir <eng>/hunt/.omp-sessions --hook runner/omp/guard-hook.ts --append-system-prompt opencode/agents/hunt-auto.md --max-time <s> -- <work order>`.
- **Tripwire equivalente ao plugin**: `runner/omp/guard-hook.ts` é um hook `tool_call` que reusa as mesmas regras do `guard.js` (`HX_OMP_BASH_MODE=guard`) e, no modo fatia (`hx-only`), só permite comandos `hx *` — sem substituição (`$(…)`, crase) e sem encadear outros comandos (`;`, `&&`, `|`) —; no modo `plan`, só `hx hypothesis add` e nenhuma escrita. Toda decisão vai para `hunt/audit.log` com `actor: omp-hook`.
- **`scope.json` continua imutável**: o hook bloqueia `edit`/`write`/`patch` em `hunt/scope.json` igual ao plugin do opencode.
- **Sessão e uso**: as sessões ficam em `hunt/.omp-sessions/` (gitignored); o runner lê o `id` (para `--resume` nos retries) e soma `model_usage.totalTokens` + `usage.cost.total` de cada sessão para o orçamento — é o que alimenta `--max-tokens`/`--max-cost` e o `tokens/custo` do `runs.jsonl` e do debrief.
- **`--netns`**: o allowlist por IP passa a derivar o provedor do `omp` (URLs em `~/.omp/agent/models.yml`/`config.yml`). Se nada for derivável, o runner recusa (fail-closed) e você declara o host do provedor em `scope.json → netns.allow_hosts`.
- **Sessão interativa com omp** (opcional): rode com `HX_OMP_BASH_MODE=guard omp --hook <repo>/runner/omp/guard-hook.ts` dentro do engagement para ter o mesmo tripwire sem o runner.
- **O que não muda**: prompt/work order, `HX_SESSION`, staging de mutação, adjudicação com `hx verify`, kill do grupo de processos no stop/timeout, e o `--netns`.

## CLI (runner)

```
runner.py [--engagement DIR] [--slices N] [--wall S] [--runtime opencode|omp] [--model M]
          [--plan] [--plan-n N] [--workers N] [--netns] [--netns-selfcheck]
          [--max-tokens N] [--max-cost USD] [--no-adjudicate]
```

## Scope

Not a sandbox. The tripwire blocks convenient paths, the guard is the only network exit for scripted traffic, and the proxy covers clients that honor `HTTP(S)_PROXY` — none of it replaces authorization and program rules. Authorized scope only.
