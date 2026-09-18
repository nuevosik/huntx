# HUNT — protocolo de sessão

## Regras de ouro

- Work order é claim, não pergunta: "confirme ou refute isto" — nunca "procure problemas".
- Toda saída de rede passa por `hx run`. Mutação fora de `allowed_mutations` é estagiada (exit 5) e executada só pelo dj.
- Refutação por auth-failure exige sessão viva (o `hx result` dispara o probe on-demand).
- Achado só entra em FINDINGS/ via `hx verify` (cenário PASS) ou `hx verify --attest` (TTY, dj).
- Evidência redigida na captura; segredos nunca em claro em disco.
- Jev (opt-in): ... egress redigido; D é fail-closed (attest humano sem Jev).

## Início de sessão

1. `hx brief` (o plugin injeta automaticamente no system).
2. `hx next` — claima work orders.
3. Trabalha item a item via `hx run --save <dir-da-hipotese>`.
4. Fecha com `hx result <id> --verdict ... --note ... [--evidence ...]`.

## Durante

- Hipótese nova → `hx hypothesis add ...`.
- Sessão suspeita → `hx health check --session <tag>`.
- Probes contam no rate; nada de "tráfego de sistema".
- `waf_block_if` marcado → cooldown do host (30→60→120min, dobra a cada reincidência); retomada manual: `hx rate reset`.

## Cenários do verify

- **differential** — vítima/atacante/controle com sessões próprias; PASS automático com marcador longo; marcador curto (< 6) exige `--attest`.
- **echo** — param refletidor: `payload_template` sempre com `{CANARY}`; reflita cru via `{payload}` (percent-encoded) ou `{payload_raw}` na URL; **sempre exige `--attest`** (prova contextual).
- **callback** — BYO webhook.site: crie a URL no browser e cole `collaborator.url` + `collaborator.poll.url` no draft antes do verify. Timeout **não** refuta — feche com `blocked` ou `unverified`.

## Fim de sessão (/debrief)

- Escreve `hunt/sessions/AAAA-MM-DD-NN.md`: o que rodou, vereditos, hipóteses novas, deltas de coverage, dúvidas abertas.
- Atualiza `TARGET.md` se aprendeu algo do alvo.
- Só achado verificado vira `FINDINGS/`.

## Runner (opt-in)

- Comando: `python3 runner/runner.py --engagement . --slices N --wall SEGUNDOS` (a partir do checkout do huntx) — 1 worker sequencial; spawna `opencode run` headless com o agente `hunt-auto` injetado inline e adjudica o draft (se houver) com `hx verify`.
- Cada fatia vira um record em `hunt/runs.jsonl`.
- Paradas: fila vazia, `--slices`/`--wall`/caps (`--max-tokens`, `--max-cost`) esgotados ou `runner.stop` presente.
- Sessão morta (só com probes no scope e `session_tag` na hipótese): a hipótese fecha `blocked` e o runner segue.
- Com `--netns`, egress do worker é só o allowlist do netns (in_scope + host da hipótese + TARGET.md + provedor/MCPs + `netns.allow_hosts`); host novo fora disso → fatia fecha `blocked` — registre o host no scope.json e re-rode.
- --netns: host do colaborador do callback (`hx verify`) precisa estar em `netns.allow_hosts`; draft não autoriza egress.
- Kill: `touch hunt/runner.stop` (honrado entre fatias e entre retries; a tentativa em voo roda até o timeout); SIGINT/SIGTERM no processo pedem a mesma parada. Retome com `rm hunt/runner.stop` — o start do próximo runner também limpa o latch.

## Planner e debrief

- Planner: `python3 runner/runner.py --engagement . --plan --plan-n N` — uma sessão headless que só propõe (não fala com o alvo); cap de N propostas (default 5); o digest traz coverage, `TARGET.md`, último debrief, fila aberta e já refutadas.
- Proposta entra na fila com `source: planner` e aparece como `[planner]` no `hx next`/`hx brief`; o dj decide — `hx hypothesis drop <id>` (fechada exige `--force`).
- O planner vira record em `hunt/runs.jsonl` com `mode: plan` e `proposed`.
- Debrief: `/debrief` (na sessão, manual) ou `hx debrief` (determinístico, sem LLM/rede): escreve `hunt/sessions/AAAA-MM-DD-NN.md` — runs/tokens/custo por hipótese, vereditos, coverage, fila e observações desde o debrief anterior.
