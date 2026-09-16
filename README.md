# huntx

A hunt harness for LLM-driven security work: **the hunt state lives outside the model.**

LLM sessions are amnesiac, over-eager, and careless with open network access. huntx is the bench around them: durable memory, work orders, scope guardrails, mechanically verified findings.

## Components

- **`bin/hx`** — single-file Python CLI (curl_cffi): hypothesis queue (claim/TTL), endpoint×class coverage, byte-bounded `brief`, adaptive rate (step-up on 429/WAF, decay between runs), scope guard with mutation staging (exit 5; humans execute in a TTY), `run` with capture-time redaction and `raw_sha256` evidence, session health (strikes, death window, retroactive reopen), differential `verify` plus manual attestation, allowlist CONNECT proxy sharing the rate state.
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

## Tests

```bash
python3 tests/test_hx.py                 # 64 tests, stdlib
node --test tests/plugin_guard.test.mjs  # 8 tripwire tests
```

## Scope

Not a sandbox. The tripwire blocks convenient paths, the guard is the only network exit for scripted traffic, and the proxy covers clients that honor `HTTP(S)_PROXY` — none of it replaces authorization and program rules. Authorized scope only.
