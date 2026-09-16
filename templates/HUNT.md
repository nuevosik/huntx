# HUNT — protocolo de sessão

## Regras de ouro

- Work order é claim, não pergunta: "confirme ou refute isto" — nunca "procure problemas".
- Toda saída de rede passa por `hx run`. Mutação fora de `allowed_mutations` é estagiada (exit 5) e executada só pelo dj.
- Refutação por auth-failure exige sessão viva (o `hx result` dispara o probe on-demand).
- Achado só entra em FINDINGS/ via `hx verify` (cenário PASS) ou `hx verify --attest` (TTY, dj).
- Evidência redigida na captura; segredos nunca em claro em disco.

## Início de sessão

1. `hx brief` (o plugin injeta automaticamente no system).
2. `hx next` — claima work orders.
3. Trabalha item a item via `hx run --save <dir-da-hipotese>`.
4. Fecha com `hx result <id> --verdict ... --note ... [--evidence ...]`.

## Durante

- Hipótese nova → `hx hypothesis add ...`.
- Sessão suspeita → `hx health check --session <tag>`.
- Probes contam no rate; nada de "tráfego de sistema".

## Cenários do verify

- **differential** — vítima/atacante/controle com sessões próprias; PASS automático com marcador longo; marcador curto (< 6) exige `--attest`.
- **echo** — param refletidor: `payload_template` sempre com `{CANARY}`; reflita cru via `{payload}` (percent-encoded) ou `{payload_raw}` na URL; **sempre exige `--attest`** (prova contextual).
- **callback** — BYO webhook.site: crie a URL no browser e cole `collaborator.url` + `collaborator.poll.url` no draft antes do verify. Timeout **não** refuta — feche com `blocked` ou `unverified`.

## Fim de sessão (/debrief)

- Escreve `hunt/sessions/AAAA-MM-DD-NN.md`: o que rodou, vereditos, hipóteses novas, deltas de coverage, dúvidas abertas.
- Atualiza `TARGET.md` se aprendeu algo do alvo.
- Só achado verificado vira `FINDINGS/`.

## Runner (opt-in)

- Comando: `python3 runner/runner.py --engagement . --slices N --wall SEGUNDOS` (a partir do checkout do huntx) — 1 worker sequencial; spawna `opencode run` headless com o agente `hunt-auto` injetado inline e adjudica drafts com `hx verify`.
- Cada fatia vira um record em `hunt/runs.jsonl`.
- Paradas: fila vazia, `--slices`/`--wall` esgotados, sessão morta (hipótese fecha `blocked`) ou `runner.stop` presente.
- Kill: `touch hunt/runner.stop`; SIGINT/SIGTERM no processo pedem a mesma parada.
