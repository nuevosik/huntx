# huntx

A hunt harness for LLM-driven security work: **the hunt state lives outside the model.**

LLM sessions are amnesiac, over-eager, and careless with open network access. huntx is the bench around them: durable memory, work orders, scope guardrails, mechanically verified findings.

## Components

- **`bin/hx`** — single-file Python CLI (curl_cffi): hypothesis queue (claim/TTL), endpoint×class coverage, byte-bounded `brief`, adaptive rate (step-up on 429/WAF, decay between runs), scope guard with mutation staging (exit 5; humans execute in a TTY), `run` with capture-time redaction and `raw_sha256` evidence, session health (strikes, death window, retroactive reopen), `verify`: differential + echo + callback (attest-gated where proof is contextual), manual attestation, allowlist CONNECT proxy sharing the rate state.
- **`opencode/`** — integration layer: `hunt` agent (work-order prompt + permissions), plugin (brief injection, proxy env, tripwire with audit log), `/brief` `/next` `/debrief` commands, `install.sh`.
- **`templates/`** — starter `scope.json` and session protocol.

## Design rules

- **Work orders, not open questions.** "Confirm or refute this claim" — never "find bugs".
- **Evidence is redacted at capture.** High-entropy secrets → `sha256:<12>`, low-entropy dropped, PII redacted; the proof marker lives in an exempt block.
- **Mutations are staged by default.** Anything outside `allowed_mutations` exits 5; only a human executes it, in a TTY, typing the host.
- **Refutations need a live session.** 401/403/redirect-login refutes fire an on-demand probe; a dead session reopens results closed inside the death window.
- **A finding exists only when verified.** `FINDINGS/<id>.json` with a scenario PASS or a signed attestation — `result --verdict confirmed` rejects everything else.

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

```bash
python3 runner/runner.py --engagement ~/hunts/target --slices 5 --wall 3600
```

One worker, one slice at a time: claims from the queue, spawns `opencode run` with a `hunt-auto` agent injected inline via `OPENCODE_CONFIG_CONTENT` (no symlink, nothing written to your opencode config), adjudicates the draft with `hx verify` when one exists, then appends the slice record to `hunt/runs.jsonl`. Budgets: `--slices`, `--wall`, and the optional `--max-tokens`/`--max-cost` caps; the loop also stops on an empty queue. `touch hunt/runner.stop` is honored between slices and between retries — an in-flight attempt runs to its timeout, then the runner stops (SIGINT/SIGTERM request the same stop). Resume with `rm hunt/runner.stop`; the latch is also cleared at the next runner start.

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

## Scope

Not a sandbox. The tripwire blocks convenient paths, the guard is the only network exit for scripted traffic, and the proxy covers clients that honor `HTTP(S)_PROXY` — none of it replaces authorization and program rules. Authorized scope only.
