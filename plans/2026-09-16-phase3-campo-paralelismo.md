# Hunt Harness — Phase 3 Plan (campo primeiro; workers + isolamento atrás do gate)

> **Status:** 3.1 mergeada em `main` (2026-09-17). 3.2 autorizada em 2026-09-17 com desenho revisado (netns rootless, sem sudo); o gate mecânico de §3.1 segue valendo em runtime e segura `--workers N>1` no alvo real até os critérios passarem. Vereditos travados pelo TypeSafe (jev-1.13.0, via MCP, 2026-09-16/17).
> **For agentic workers:** quando a 3.1 for autorizada, usar superpowers:subagent-driven-development com briefs completos (writing-plans) sobre este documento.

**Goal:** Destravar throughput (N workers) e enforcement de rede com o mínimo de especulação: primeiro o sistema vai a campo; o build paralelo nasce com dados reais e atrás de um gate mecânico.

**Decisões (TypeSafe jev-1.13.0, via MCP):**

| Pergunta | Veredito | Prob. |
|---|---|---|
| Primeiro entregável da fase 3 | **observação de campo primeiro** (nenhum código antes) | 0.74 |
| Prioridade de `--workers N` agora | **Não agora — esperar dados de campo** | 0.87 |
| Prioridade de isolamento de rede agora | Não agora (tende backlog) | 0.77 |
| Prioridade de scheduling agora | Não agora | 0.76 |
| Primitivas atuais (flock/atomic/TTL/stop) aguentam N=2–3? | Sim, com melhorias pontuais (confiança fraca) | 0.58 |
| Gate do operador vira precondição mecânica no código? | **Sim** | 0.77 |
| Um worker vivo = uma sessão distinta? | **Sim** | 0.86 |
| Mecanismo de isolamento (quando construir) | **netns + nftables do scope.json** | 0.99 |

---

## Phase 3.0 — Campo primeiro (entregável imediato)

**Objetivo:** wave manual no REI com o harness completo; produzir os dados que decidem o build paralelo.

- Instrumentos: TUI hunt (agente interativo) + `hx` (`next`/`run`/`verify`/`result`/`debrief`). O runner em 1 worker fica disponível, não obrigatório.
- Critérios de saída (destravam 3.1): ≥20 hipóteses abertas vivas; ≥2 hosts in-scope com trabalho pendente; ≥1 wave multi-sessão real (a+b); `hx debrief` de campo gravado.
- Sem código novo. Defeito achado em campo vira fix wave pelo processo normal (brief → implementer → review).
- Precisa de ok explícito do dj (alvo real).

## Phase 3.1 — Workers (`--workers N`) — design travado, build só pós-gate

- **Gate mecânico** (no runner): recusa `--workers N>1` a menos que — N ≤ nº de sessões em `scope.health.probes`; hyps abertas ≥ 20; hosts in-scope ≥ 2. A parte "observação de campo" do gate é decisão do dj, registrada no debrief.
- **Sessão por worker:** cada worker vivo preso a uma sessão distinta (a/b/c); nunca dois workers na mesma sessão. Health gate por sessão já existe.
- **Primitivas:** reuso de flock/atomic/TTL/`runner.stop`. Melhorias pontuais: `COVERAGE.md` sob lock; pid/stop por worker (`runner.stop` segue global, para todos); reconciliação por worker no fim da própria fatia.
- **Fila/rate/orçamento:** claim compartilhado via `.lock.hyps`; rate compartilhado (`.ratelimit.json`) — aceita a serialização: piso global 0.5s (~2 req/s) é teto do conjunto no mesmo host; orçamentos agregados (wall/slices/tokens/cost somados); stop file para todos.
- **Risco registrado (veredito fraco, 0.58):** a primeira task da 3.1 é um teste de concorrência — 2 workers sintéticos no fixture local (`smoke-plan`, 127.0.0.1) antes de qualquer coisa tocar o REI.
- **Spec:** atualizar `specs/2026-09-15-hunt-harness-design.md` para Rev 9 (§10 fase 3) quando a 3.1 for autorizada.

## Phase 3.2 — Isolamento netns rootless (autorizada 2026-09-17)

Decisões (TypeSafe jev-1.13.0, 2026-09-17 + spikes na box):

| Pergunta | Veredito | Prob. |
|---|---|---|
| Modelo de backend/privilegio | **Rootless** (`unshare -Urn` + slirp4netns/pasta), sem sudo | 0.99 (conf. 0.98) |
| Endpoint do proxy fora do allowlist dos workers | **Sim, remover** (rootless não alcança 127.0.0.1 do host; agente de fatia não tem browser) | 0.77 |
| Anti-tamper (bounding-set drop de cap_net_admin) | **Manter** | 0.77 |
| Allowlist por IP + refresh | sem sinal (0.53) — prossegue com o refinamento + teste-gate | — |
| Fork+handshake (preserva contratos da 3.1) | sem sinal (0.45) — prossegue com teste de composição como gate | — |

- **Mecanismo:** supervisor re-executa uma vez em `unshare -Ur` (fica no netns do host, ganha CAP_SYS_ADMIN) → cada worker cria só `CLONE_NEWNET` → supervisor sobe um `slirp4netns`/`pasta` por worker com handshake de prontidão → worker aplica nft default-drop e dropa `cap_net_admin` → `worker_loop` (fork+queue+orçamento da 3.1 intactos). Userns por worker não serve (o backend precisa de caps no userns do worker).
- **Allowlist fail-closed:** in_scope + host da hipótese + TARGET.md + provedor/MCPs derivados + extras `netns.allow_hosts`; todos os A records; refresh por fatia e em falha de transporte; falha persistente → `blocked (netns)`.
- **Sem proxy no allowlist** (desvio registrado do texto original desta seção).
- **Gate:** teste de composição 2 workers `--netns` no fixture local antes de qualquer uso real; plano B documentado: netns único compartilhado via re-exec do supervisor.
- **Spec:** §9.3 (Rev 10).

## Phase 3.3 — Scheduling (backlog, último)

- Prioridade/cadência: qual hipótese/endpoint roda a seguir, ondas vs contínuo.

## Execution sequence

3.0 (campo) → gate → briefs completos da 3.1 → build → 3.2 → 3.3.
