# Hunt Harness — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `hx` (single-file Python state CLI) + the opencode hunt layer (2 agents, 1 plugin, 3 commands) so every hunt session compounds — briefs inject automatically, work orders come pre-claimed, requests are scope-guarded, findings adjudicated, sessions health-checked.

**Architecture:** All hunt logic lives in `bin/hx` (scope guard, rate, evidence, health, queue, staging, verify). The opencode layer is thin: agents carry the protocol prompt + permissions, the plugin injects the brief, exports proxy env vars and tripwires network tool calls, commands wrap the rituals.

**Tech Stack:** Python 3.14 (floor 3.12), stdlib + `curl_cffi` (TLS impersonate `chrome131`), Node 26 (`node --test`) for the plugin lib, opencode 1.18.x for agents/plugin/commands.

**Spec:** `specs/2026-09-15-hunt-harness-design.md` (Rev 5) — the plan argues from the spec; executors read both.

## Global Constraints

- `bin/hx` stays ONE file. No comments and no docstrings anywhere in code (AGENTS.md rule).
- Docs and `hx` user-facing messages in Portuguese; code identifiers in English.
- Exit codes: `0` ok · `2` guard/scope · `3` rate exceeded wait · `4` transport · `5` staged · `6` session dead/suspect · `7` needs TTY.
- Every state write is atomic (`tmp` + `os.replace`); every shared-state access is under `flock`.
- TTY gates (`pending run`, `verify --attest`, `rate reset`) check `stdin.isatty()`; tests patch `hx.stdin_is_tty`.
- Test clock: `HX_FAKE_NOW` env + monkeypatched `hx.sleep`; tests never really sleep and never touch the network.
- Never commit secrets: session files, evidence, `.health.json`, `.ratelimit.json`, `pending/` stay out of git.
- E2E smoke runs against ACME only with own test accounts (`acme-bbp/SCOPE.md` rules).
- Out of scope: phase 1.5 (`echo`/`callback` verify scenarios), phase 2 (`runner/runner.py`, L3 waves).

---

## File Structure

```
huntbench/
  bin/hx                          # everything: state, guard, rate, health, run, pending, proxy, verify
  templates/scope.json            # starter scope (copied by hx init)
  templates/HUNT.md               # session protocol doc (copied into engagement root)
  opencode/agents/hunt.md         # interactive hunt agent (prompt + permissions)
  opencode/agents/hunt-auto.md    # headless twin (no ask perms)
  opencode/plugin/hunt.ts         # hooks: brief injection, proxy env, tripwire + audit
  opencode/plugin/lib/guard.js    # pure decision functions (node-testable)
  opencode/commands/brief.md      # /brief
  opencode/commands/next.md       # /next
  opencode/commands/debrief.md    # /debrief
  tests/test_hx.py                # stdlib unittest suite for bin/hx
  tests/plugin_guard.test.mjs     # node --test for guard.js
  install.sh                      # symlinks + plugin config instructions
  plans/2026-09-15-hunt-harness-phase1.md
```

Every task below ends with a commit in this repo (`/home/ngix/huntbench`).

---

### Task 1: hx skeleton + engagement discovery + test harness

**Files:**
- Create: `bin/hx`
- Create: `tests/test_hx.py`

**Interfaces:**
- Produces: `HxError(message, code)`, `now()`, `sleep`, `stdin_is_tty()`, `read_line(prompt)`, `atomic_write(path, text)`, `flock(lock_path)`, `load_json(path, default)`, `dump_json(path, data)`, `find_engagement(start=None) -> Path`, `Repo`, `repo_from_cwd()`, exit constants `EXIT_OK/EXIT_GUARD/EXIT_RATE/EXIT_TRANSPORT/EXIT_STAGED/EXIT_SESSION/EXIT_TTY`, `main(argv=None)`.
- Test helpers: `load_hx()`, `make_engagement(root)`.

- [ ] **Step 1: Write the failing test**

```python
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HX_PATH = Path(__file__).resolve().parent.parent / "bin" / "hx"


def load_hx():
    spec = importlib.util.spec_from_file_location("hx", HX_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hx = load_hx()


def make_engagement(root):
    hunt = Path(root) / "hunt"
    hunt.mkdir(parents=True)
    (hunt / "scope.json").write_text(json.dumps({
        "engagement": "t",
        "in_scope": ["example.com"],
        "out_of_scope_hosts": [],
        "out_of_scope_paths": [],
        "allow_extra_hosts": [],
        "rate": {"per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30},
        "allowed_mutations": [],
        "evidence_dir": "scans/evidence",
    }))
    return Path(root)


class DiscoveryTest(unittest.TestCase):
    def test_find_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engagement(tmp)
            sub = eng / "scans" / "deep"
            sub.mkdir(parents=True)
            os.environ.pop("HX_ENGAGEMENT", None)
            self.assertEqual(hx.find_engagement(sub), eng)

    def test_missing_engagement_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("HX_ENGAGEMENT", None)
            with self.assertRaises(hx.HxError):
                hx.find_engagement(tmp)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py -v`
Expected: FAIL — `FileNotFoundError` on `bin/hx` (does not exist yet).

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

EXIT_OK = 0
EXIT_GUARD = 2
EXIT_RATE = 3
EXIT_TRANSPORT = 4
EXIT_STAGED = 5
EXIT_SESSION = 6
EXIT_TTY = 7


def now():
    fake = os.environ.get("HX_FAKE_NOW")
    return float(fake) if fake else time.time()


sleep = time.sleep


def stdin_is_tty():
    return sys.stdin.isatty()


def read_line(prompt):
    return input(prompt)


class HxError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def dump_json(path, data):
    atomic_write(Path(path), json.dumps(data, indent=1, ensure_ascii=False))


_LOCKS_HELD = set()


@contextmanager
def flock(lock_path):
    lock_path = Path(lock_path)
    key = str(lock_path.resolve())
    if key in _LOCKS_HELD:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _LOCKS_HELD.add(key)
        yield
    finally:
        _LOCKS_HELD.discard(key)
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def find_engagement(start=None):
    env = os.environ.get("HX_ENGAGEMENT")
    if env:
        eng = Path(env).resolve()
        if not (eng / "hunt" / "scope.json").exists():
            raise HxError(f"HX_ENGAGEMENT sem hunt/scope.json: {eng}", EXIT_GUARD)
        return eng
    cur = Path(start or os.getcwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "hunt" / "scope.json").exists():
            return parent
    raise HxError("engagement nao encontrado (hunt/scope.json)", EXIT_GUARD)


class Repo:
    def __init__(self, eng):
        self.eng = Path(eng)
        self.hunt = self.eng / "hunt"
        self.hyps = self.hunt / "HYPOTHESES.json"
        self.coverage = self.hunt / "COVERAGE.md"
        self.scope_path = self.hunt / "scope.json"
        self.health = self.hunt / ".health.json"
        self.rate = self.hunt / ".ratelimit.json"

    def scope(self):
        return load_json(self.scope_path, {})


def repo_from_cwd():
    return Repo(find_engagement())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hx")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            print("not implemented")
        return EXIT_OK
    except json.JSONDecodeError as err:
        print(f"hx: estado corrompido: {err}", file=sys.stderr)
        return EXIT_GUARD
    except HxError as err:
        print(f"hx: {err}", file=sys.stderr)
        return err.code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS — 2 tests ok.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): scaffold, engagement discovery, test harness"
```
---

### Task 2: `hx init` + templates

**Files:**
- Modify: `bin/hx` (add `cmd_init`, wire `init` subcommand)
- Create: `templates/scope.json`
- Create: `templates/HUNT.md`
- Modify: `tests/test_hx.py` (add `InitTest`)

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces: `cmd_init(args)`; created tree `hunt/{scope.json,TARGET.md,COVERAGE.md,HYPOTHESES.json,.gitignore}`, `hunt/FINDINGS/drafts/`, `hunt/pending/`, `hunt/sessions/`, plus `<eng>/HUNT.md`.

- [ ] **Step 1: Write the failing test**

```python
class InitTest(unittest.TestCase):
    def test_init_creates_tree_and_refuses_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = hx.main(["init", "--dir", tmp])
            self.assertEqual(rc, 0)
            hunt = Path(tmp) / "hunt"
            for name in ("scope.json", "TARGET.md", "COVERAGE.md", "HYPOTHESES.json", ".gitignore"):
                self.assertTrue((hunt / name).exists(), name)
            self.assertTrue((hunt / "FINDINGS" / "drafts").is_dir())
            self.assertTrue((hunt / "pending").is_dir())
            self.assertTrue((Path(tmp) / "HUNT.md").exists())
            scope = json.loads((hunt / "scope.json").read_text())
            self.assertEqual(scope["engagement"], Path(tmp).name)
            self.assertEqual(hx.main(["init", "--dir", tmp]), hx.EXIT_GUARD)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py InitTest -v`
Expected: FAIL — `init` prints "not implemented"; tree missing.

- [ ] **Step 3: Write minimal implementation**

`templates/scope.json`:

```json
{
  "engagement": "",
  "in_scope": [],
  "out_of_scope_hosts": [],
  "out_of_scope_paths": [],
  "allow_extra_hosts": [],
  "rate": { "per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30, "adaptive": true, "step_up_max_s": 30, "decay_clean_min": 60 },
  "health": {
    "probes": [],
    "fresh_max_s": 600,
    "session_invalid_if": ["status:401", "status:403", "redirect_host:login"],
    "waf_block_if": ["body_contains:_px", "body_contains:px-captcha"],
    "rate_limited_if": ["status:429", "status:503"]
  },
  "allowed_mutations": [],
  "evidence_dir": "scans/evidence"
}
```

`templates/HUNT.md`:

```markdown
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

## Fim de sessão (/debrief)

- Escreve `hunt/sessions/AAAA-MM-DD-NN.md`: o que rodou, vereditos, hipóteses novas, deltas de coverage, dúvidas abertas.
- Atualiza `TARGET.md` se aprendeu algo do alvo.
- Só achado verificado vira `FINDINGS/`.
```

`bin/hx` — add:

```python
def cmd_init(args):
    eng = Path(args.dir).resolve()
    hunt = eng / "hunt"
    if (hunt / "scope.json").exists():
        raise HxError("engagement ja inicializado", EXIT_GUARD)
    templates = Path(__file__).resolve().parent.parent / "templates"
    hunt.mkdir(parents=True, exist_ok=True)
    scope = load_json(templates / "scope.json", {})
    scope["engagement"] = eng.name
    dump_json(hunt / "scope.json", scope)
    for name in ("TARGET.md", "COVERAGE.md"):
        atomic_write(hunt / name, "")
    dump_json(hunt / "HYPOTHESES.json", [])
    for sub in ("FINDINGS/drafts", "pending", "sessions"):
        (hunt / sub).mkdir(parents=True, exist_ok=True)
    protocol = templates / "HUNT.md"
    if protocol.exists():
        atomic_write(eng / "HUNT.md", protocol.read_text())
    atomic_write(hunt / ".gitignore", "scans/\nsessions/\n.health.json\n.ratelimit.json\npending/\naudit.log\n")
    print(f"engagement inicializado: {hunt}")
```

Wire in `main`:

```python
    init_p = sub.add_parser("init")
    init_p.add_argument("--dir", default=".")
    ...
        if args.cmd == "init":
            cmd_init(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS — 3 tests ok.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py templates/
git commit -m "feat(hx): init + engagement templates"
```

---

### Task 3: hypothesis queue — add, next/claim, peek, release, TTL

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `QueueTest`)

**Interfaces:**
- Produces: `TTL_CLAIM_S`, `session_tag()`, `load_hyps(repo) -> (hyps, changed)`, `save_hyps(repo, hyps)`, `cmd_hypothesis_add(args)`, `cmd_next(args)`, `cmd_release(args)`; subcommands `hypothesis add`, `next [n] [--peek]`, `release <id> [--force]`.

- [ ] **Step 1: Write the failing test**

```python
class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def add(self, claim="BOLA em wishlist"):
        return hx.main(["hypothesis", "add", "--claim", claim, "--endpoint", "GET /wishlist/{id}", "--class", "BOLA", "--confirm", "200 com dado da vitima", "--refute", "403/404"])

    def test_add_next_claim_and_owner(self):
        self.add()
        self.add("v=2 em orders")
        repo = hx.repo_from_cwd()
        hyps, _ = hx.load_hyps(repo)
        self.assertEqual([h["id"] for h in hyps], ["h001", "h002"])
        self.assertEqual(hx.main(["next", "2"]), 0)
        hyps, _ = hx.load_hyps(repo)
        self.assertEqual([h["status"] for h in hyps], ["claimed", "claimed"])
        self.assertEqual(hyps[0]["owner"], "sess-a")

    def test_peek_does_not_claim(self):
        self.add()
        hx.main(["next", "--peek"])
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual(hyps[0]["status"], "open")

    def test_ttl_reclaim(self):
        self.add()
        hx.main(["next"])
        os.environ["HX_FAKE_NOW"] = "5000"
        hyps, changed = hx.load_hyps(hx.repo_from_cwd())
        self.assertTrue(changed)
        self.assertEqual(hyps[0]["status"], "open")

    def test_release_requires_owner(self):
        self.add()
        hx.main(["next"])
        os.environ["HX_SESSION"] = "sess-b"
        self.assertEqual(hx.main(["release", "h001"]), hx.EXIT_GUARD)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py QueueTest -v`
Expected: FAIL — `hypothesis` subcommand missing.

- [ ] **Step 3: Write minimal implementation**

```python
TTL_CLAIM_S = 1800


def session_tag():
    return os.environ.get("HX_SESSION") or f"pid-{os.getpid()}"


def load_hyps(repo):
    hyps = load_json(repo.hyps, [])
    t = now()
    changed = False
    for hyp in hyps:
        if hyp.get("status") in ("claimed", "running") and (hyp.get("claimed_ts") or 0) + TTL_CLAIM_S < t:
            hyp["status"] = "open"
            hyp["owner"] = None
            hyp["claimed_ts"] = None
            hyp.setdefault("notes", []).append({"ts": t, "note": "claim TTL expirado; devolvido a open"})
            changed = True
    return hyps, changed


def save_hyps(repo, hyps):
    dump_json(repo.hyps, hyps)


def cmd_hypothesis_add(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    probe_tags = [probe["session"] for probe in (scope.get("health") or {}).get("probes", [])]
    if args.session_tag and not probe_tags:
        raise HxError("--session-tag exige probes no scope (health.probes) para validar", EXIT_GUARD)
    if args.session_tag and args.session_tag not in probe_tags:
        raise HxError(f"session-tag '{args.session_tag}' nao bate com os probes ({', '.join(probe_tags)})", EXIT_GUARD)
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        hid = f"h{len(hyps) + 1:03d}"
        hyps.append({
            "id": hid,
            "status": "open",
            "claim": args.claim,
            "endpoint": args.endpoint,
            "class": args.cls,
            "confirm": args.confirm,
            "refute": args.refute,
            "evidence": args.evidence or f"scans/evidence/{hid}",
            "session_tag": args.session_tag,
            "owner": None,
            "created_ts": now(),
            "claimed_ts": None,
            "closed_ts": None,
            "result": {"verdict": None, "note": None, "evidence": None},
            "reopened_reason": None,
        })
        save_hyps(repo, hyps)
    print(hid)


def cmd_next(args):
    repo = repo_from_cwd()
    owner = session_tag()
    picked = []
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        for hyp in hyps:
            if len(picked) >= args.n:
                break
            if hyp["status"] != "open":
                continue
            if not args.peek:
                hyp["status"] = "claimed"
                hyp["owner"] = owner
                hyp["claimed_ts"] = now()
            picked.append(hyp)
        save_hyps(repo, hyps)
    for hyp in picked:
        print(f"[{hyp['id']}] {hyp['claim']}")
        print(f"  endpoint: {hyp['endpoint']} | classe: {hyp['class']}")
        print(f"  confirm: {hyp['confirm']}")
        print(f"  refute:  {hyp['refute']}")
        print(f"  evidencia: {hyp['evidence']}")
        print(f"  sessao: {hyp['session_tag'] or '-'}")
        print()


def cmd_release(args):
    repo = repo_from_cwd()
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        for hyp in hyps:
            if hyp["id"] != args.id:
                continue
            if hyp["owner"] not in (session_tag(), None) and not args.force:
                raise HxError("nao e o dono (use --force)", EXIT_GUARD)
            if hyp["status"] == "result":
                raise HxError("hipotese ja fechada (result) — release nao reabre terminal", EXIT_GUARD)
            hyp["status"] = "open"
            hyp["owner"] = None
            hyp["claimed_ts"] = None
            save_hyps(repo, hyps)
            print(f"liberada {hyp['id']}")
            return
    raise HxError(f"hipotese nao encontrada: {args.id}", EXIT_GUARD)
```

Wire parsers:

```python
    hyp_p = sub.add_parser("hypothesis")
    hyp_sub = hyp_p.add_subparsers(dest="hyp_cmd", required=True)
    add_p = hyp_sub.add_parser("add")
    add_p.add_argument("--claim", required=True)
    add_p.add_argument("--endpoint", required=True)
    add_p.add_argument("--class", dest="cls", required=True)
    add_p.add_argument("--confirm", required=True)
    add_p.add_argument("--refute", required=True)
    add_p.add_argument("--evidence")
    add_p.add_argument("--session-tag", dest="session_tag")
    next_p = sub.add_parser("next")
    next_p.add_argument("n", nargs="?", type=int, default=3)
    next_p.add_argument("--peek", action="store_true")
    rel_p = sub.add_parser("release")
    rel_p.add_argument("id")
    rel_p.add_argument("--force", action="store_true")
```

Dispatch in `main`: `elif args.cmd == "hypothesis": cmd_hypothesis_add(args)`, `elif args.cmd == "next": cmd_next(args)`, `elif args.cmd == "release": cmd_release(args)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): hypothesis queue with claim/peek/release/TTL"
```
---

### Task 4: `hx result` + COVERAGE update + `hx brief`

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `ResultTest`)

**Interfaces:**
- Consumes: Task 3 queue.
- Produces: `VERDICT_TO_COVERAGE`, `update_coverage(repo, endpoint, cls, status, evidence, note)`, `finding_verified(repo, hypothesis_id) -> (bool, reason)`, `cmd_result(args)`, `cmd_brief(args)`; subcommands `result <id> --verdict --note [--evidence] [--force]`, `brief [--max-bytes N]`. `confirmed` exige finding verificado (`FINDINGS/<id>.json` com cenário PASS **ou** atestação) — confirmar sem `hx verify` é rejeitado. Task 9 extends `cmd_result` with the refute gate.

- [ ] **Step 1: Write the failing test**

```python
class ResultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_confirmed_requires_verified_finding(self):
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), hx.EXIT_GUARD)
        hx.dump_json(self.eng / "hunt" / "FINDINGS" / "h001.json", {"id": "h001", "verified_at": 1, "proof": {"attacker": {"marker_present": True}}})
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), 0)
        cov = (self.eng / "hunt" / "COVERAGE.md").read_text()
        self.assertIn("GET /w", cov)
        self.assertIn("confirmed", cov)
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual(hyps[0]["status"], "result")
        self.assertEqual(hyps[0]["result"]["verdict"], "confirmed")

    def test_confirmed_rejects_unverified_finding_file(self):
        hx.dump_json(self.eng / "hunt" / "FINDINGS" / "h001.json", {"id": "h001"})
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), hx.EXIT_GUARD)

    def test_owner_check(self):
        hx.main(["next"])
        os.environ["HX_SESSION"] = "sess-b"
        self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "n"]), hx.EXIT_GUARD)

    def test_brief_bounded(self):
        rc = hx.main(["brief", "--max-bytes", "400"])
        self.assertEqual(rc, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py ResultTest -v`
Expected: FAIL — `result` subcommand missing.

- [ ] **Step 3: Write minimal implementation**

```python
VERDICT_TO_COVERAGE = {"confirmed": "confirmed", "refuted": "refuted", "blocked": "blocked", "unverified": "untested"}


def update_coverage(repo, endpoint, cls, status, evidence, note):
    path = repo.coverage
    path.touch(exist_ok=True)
    lines = path.read_text().splitlines()
    header = "| endpoint | classe | status | evidência | nota |"
    sep = "|---|---|---|---|---|"
    row = f"| {endpoint} | {cls} | {status} | {evidence} | {note} |"
    replaced = False
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == endpoint and cells[1] == cls:
            lines[i] = row
            replaced = True
            break
    if not replaced:
        if not lines:
            lines = [header, sep]
        elif header not in lines:
            lines += ["", header, sep]
        lines.append(row)
    atomic_write(path, "\n".join(lines) + "\n")


def finding_verified(repo, hypothesis_id):
    finding_path = repo.hunt / "FINDINGS" / f"{hypothesis_id}.json"
    if not finding_path.exists():
        return False, "sem FINDINGS/<id>.json"
    data = load_json(finding_path, {})
    if data.get("verdict") == "verified-manual":
        return True, "manual-attested"
    if data.get("verified_at") and data.get("proof"):
        return True, "cenario PASS"
    return False, "finding sem verificacao"


def cmd_result(args):
    repo = repo_from_cwd()
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        hyp = next((h for h in hyps if h["id"] == args.id), None)
        if not hyp:
            raise HxError(f"hipotese nao encontrada: {args.id}", EXIT_GUARD)
        if hyp["owner"] not in (session_tag(), None) and not args.force:
            raise HxError("nao e o dono (use --force)", EXIT_GUARD)
        if args.verdict == "confirmed":
            verified, reason = finding_verified(repo, hyp["id"])
            if not verified:
                raise HxError(f"confirmed exige finding verificado ({reason}) — rode hx verify primeiro", EXIT_GUARD)
        session_check = None
        if args.verdict == "refuted":
            session_check = refute_gate(repo, hyp)
        hyp["status"] = "result"
        hyp["closed_ts"] = now()
        hyp["result"] = {"verdict": args.verdict, "note": args.note, "evidence": args.evidence, "session_check": session_check}
        save_hyps(repo, hyps)
    update_coverage(repo, hyp["endpoint"], hyp["class"], VERDICT_TO_COVERAGE[args.verdict], args.evidence or hyp["evidence"], args.note or "")
    print(f"{hyp['id']} -> {args.verdict}")


def refute_gate(repo, hyp):
    return None
```

(`refute_gate` is filled in Task 9; for now it returns `None` so the flow works.)

```python
def cmd_brief(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    hyps, _ = load_hyps(repo)
    health = load_json(repo.health, {})
    parts = ["== HUNT BRIEF =="]
    target_path = repo.hunt / "TARGET.md"
    if target_path.exists():
        tlines = [line.strip() for line in target_path.read_text().splitlines() if line.strip()][:4]
        if tlines:
            parts.append("alvo: " + " | ".join(tlines))
    in_scope = len(scope.get("in_scope", []))
    banned_hosts = len(scope.get("out_of_scope_hosts", []))
    banned_paths = ", ".join(scope.get("out_of_scope_paths", [])) or "-"
    parts.append(f"escopo: {in_scope} hosts in | {banned_hosts} banidos | paths banidos: {banned_paths}")
    dead = [tag for tag, entry in health.items() if entry.get("dead_since")]
    if dead:
        parts.append("sessoes mortas: " + ", ".join(dead))
    counts = {}
    if repo.coverage.exists():
        for line in repo.coverage.read_text().splitlines():
            if line.startswith("|") and "---" not in line and "endpoint" not in line:
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                if len(cells) >= 3:
                    counts[cells[2]] = counts.get(cells[2], 0) + 1
    if counts:
        parts.append("coverage: " + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
    open_hyps = [h for h in hyps if h["status"] == "open"]
    if open_hyps:
        parts.append("hipoteses abertas:")
        for hyp in open_hyps[:5]:
            parts.append(f"  [{hyp['id']}] {hyp['claim'][:80]}")
    sessions_dir = repo.hunt / "sessions"
    sessions = sorted(sessions_dir.glob("*.md")) if sessions_dir.exists() else []
    if sessions:
        tail = sessions[-1].read_text().splitlines()[-8:]
        parts.append("ultimo debrief:")
        parts.extend("  " + line for line in tail)
    text = "\n".join(parts)
    limit = args.max_bytes
    if len(text.encode("utf-8")) > limit:
        text = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore") + "\n...[truncado]"
    print(text)
```

Wire parsers (`result`: `id`, `--verdict` choices `confirmed refuted blocked unverified`, `--note`, `--evidence`, `--force`; `brief`: `--max-bytes` int default 4000) and dispatch.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): result + coverage update + bounded brief"
```

---

### Task 5: rate module — acquire, step-up, decay, show, reset

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `RateTest`)

**Interfaces:**
- Produces: `rate_effective_interval(scope, state, host)`, `rate_acquire(scope, repo, host, max_wait) -> float` (raises `HxError(EXIT_RATE)`), `rate_step_up(scope, repo, host, cause) -> float`, `cmd_rate_show(args)`, `cmd_rate_reset(args)`; subcommands `rate show`, `rate reset` (reset requires TTY).

- [ ] **Step 1: Write the failing test**

```python
class RateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_tty = hx.stdin_is_tty
        hx.stdin_is_tty = lambda: False

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.stdin_is_tty = self.orig_tty
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_spacing_uses_scheduled_time(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_acquire(scope, repo, "example.com", 30)
        hx.rate_acquire(scope, repo, "example.com", 30)
        state = json.loads(repo.rate.read_text())
        self.assertEqual(state["hosts"]["example.com"]["last"], 1002.0)

    def test_max_wait_exceeded(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_acquire(scope, repo, "example.com", 30)
        state = json.loads(repo.rate.read_text())
        state["hosts"]["example.com"]["last"] = 1100
        repo.rate.write_text(json.dumps(state))
        with self.assertRaises(hx.HxError):
            hx.rate_acquire(scope, repo, "example.com", 1)

    def test_step_up_doubles_and_caps(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        self.assertEqual(hx.rate_step_up(scope, repo, "example.com", "429"), 4.0)
        self.assertEqual(hx.rate_step_up(scope, repo, "example.com", "429"), 8.0)

    def test_decay_after_clean_hour(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_step_up(scope, repo, "example.com", "429")
        state = json.loads(repo.rate.read_text())
        os.environ["HX_FAKE_NOW"] = str(1000 + 3600)
        self.assertEqual(hx.rate_effective_interval(scope, state, "example.com"), 2.0)

    def test_reset_requires_tty(self):
        self.assertEqual(hx.main(["rate", "reset"]), hx.EXIT_TTY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py RateTest -v`
Expected: FAIL — `rate_acquire` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def rate_effective_interval(scope, state, host):
    floor = float(scope["rate"]["per_host_interval_s"])
    effective = state.get("effective", {}).get(host)
    if not effective:
        return floor
    events = [event for event in state.get("events", []) if event.get("host") == host]
    if not events:
        return floor
    last_ts = max(event["ts"] for event in events)
    decay_min = float(scope["rate"].get("decay_clean_min", 60))
    decay = int((now() - last_ts) // (decay_min * 60))
    return max(floor, effective / (2 ** decay))


def rate_acquire(scope, repo, host, max_wait):
    with flock(repo.hunt / ".lock.rate"):
        state = load_json(repo.rate, {})
        interval = rate_effective_interval(scope, state, host)
        global_interval = float(scope["rate"].get("global_interval_s", 0.5))
        t = now()
        last = (state.get("hosts", {}).get(host, {}) or {}).get("last") or 0
        global_last = state.get("global") or 0
        scheduled = max(t, last + interval, global_last + global_interval)
        wait = scheduled - t
        if wait > max_wait:
            raise HxError(f"rate: esperaria {wait:.1f}s (max {max_wait}s)", EXIT_RATE)
        if wait > 0:
            sleep(wait)
        state.setdefault("hosts", {}).setdefault(host, {})["last"] = scheduled
        state["global"] = scheduled
        dump_json(repo.rate, state)
        return wait


def rate_step_up(scope, repo, host, cause):
    with flock(repo.hunt / ".lock.rate"):
        state = load_json(repo.rate, {})
        floor = float(scope["rate"]["per_host_interval_s"])
        cap = float(scope["rate"].get("step_up_max_s", 30))
        current = max(floor, float(state.get("effective", {}).get(host, floor)))
        new = min(cap, current * 2)
        state.setdefault("effective", {})[host] = new
        state.setdefault("events", []).append({"ts": now(), "host": host, "cause": cause, "interval": new})
        dump_json(repo.rate, state)
        return new


def cmd_rate_show(args):
    repo = repo_from_cwd()
    print(json.dumps(load_json(repo.rate, {}), indent=1))


def cmd_rate_reset(args):
    if not stdin_is_tty():
        raise HxError("rate reset exige TTY", EXIT_TTY)
    repo = repo_from_cwd()
    with flock(repo.hunt / ".lock.rate"):
        state = load_json(repo.rate, {})
        state["effective"] = {}
        state.setdefault("events", []).append({"ts": now(), "host": "*", "cause": "manual reset", "interval": None})
        dump_json(repo.rate, state)
    print("rate reiniciado — piso restaurado")
```

Wire `rate` subcommand with nested `show`/`reset`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): rate acquire/step-up/decay/reset"
```
---

### Task 6: scope guard + method classification + staging

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `ScopeTest`)

**Interfaces:**
- Produces: `LOOPBACK`, `host_matches(pattern, host)`, `check_scope(scope, url) -> (ok, reason)`, `classify_method(scope, method, url) -> "auto"|"staged"`, `stage_pending(repo, spec) -> pending_id`, `cmd_pending(args)` (`list`/`show`; `run` arrives in Task 7); subcommand `pending list|show <id>|run <id>`.

- [ ] **Step 1: Write the failing test**

```python
class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com", "login.acme.com"]
        scope["out_of_scope_hosts"] = ["vpn.acme.com", "*.rentals.acme.com"]
        scope["out_of_scope_paths"] = ["/blog", "/used"]
        scope["allow_extra_hosts"] = ["api.mail.tm"]
        scope["allowed_mutations"] = ["POST /rest/user", "POST /mobile-gateway/rest/user/guest/V1"]
        scope_path.write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        self.scope = json.loads(scope_path.read_text())

    def tearDown(self):
        os.environ.pop("HX_ENGAGEMENT", None)
        self.tmp.cleanup()

    def test_scope_rules(self):
        self.assertTrue(hx.check_scope(self.scope, "https://www.acme.com/x")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://vpn.acme.com/x")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://foo.rentals.acme.com/x")[0])
        self.assertTrue(hx.check_scope(self.scope, "https://api.mail.tm/messages")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://www.acme.com/blog/post")[0])
        self.assertTrue(hx.check_scope(self.scope, "https://www.acme.com/usedcars")[0])
        self.assertTrue(hx.check_scope(self.scope, "http://127.0.0.1:9999/x")[0])

    def test_classify(self):
        self.assertEqual(hx.classify_method(self.scope, "GET", "https://www.acme.com/x"), "auto")
        self.assertEqual(hx.classify_method(self.scope, "POST", "https://www.acme.com/rest/user"), "auto")
        self.assertEqual(hx.classify_method(self.scope, "POST", "https://www.acme.com/rest/orders/cancel-order/1"), "staged")
        self.assertEqual(hx.classify_method(self.scope, "DELETE", "https://www.acme.com/rest/user/giftcard/1"), "staged")

    def test_staging_writes_pending(self):
        repo = hx.Repo(self.eng)
        pid = hx.stage_pending(repo, {"method": "DELETE", "url": "https://www.acme.com/rest/user/giftcard/1", "body": None, "session": None})
        self.assertTrue((repo.hunt / "pending" / f"{pid}.json").exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py ScopeTest -v`
Expected: FAIL — `check_scope` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def host_matches(pattern, host):
    pattern = pattern.lower().strip()
    host = host.lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) or host == pattern[2:]
    return host == pattern or host.endswith("." + pattern)


def check_scope(scope, url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if host in LOOPBACK:
        return True, None
    for banned in scope.get("out_of_scope_paths", []):
        prefix = banned.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return False, f"path banido: {banned}"
    for pattern in scope.get("out_of_scope_hosts", []):
        if host_matches(pattern, host):
            return False, f"host banido: {pattern}"
    for pattern in scope.get("in_scope", []):
        if host_matches(pattern, host):
            return True, None
    for pattern in scope.get("allow_extra_hosts", []):
        if host_matches(pattern, host):
            return True, None
    return False, f"host fora do escopo: {host}"


def classify_method(scope, method, url):
    if method.upper() in ("GET", "HEAD"):
        return "auto"
    path = urlparse(url).path or "/"
    for entry in scope.get("allowed_mutations", []):
        entry_method, _, entry_path = entry.partition(" ")
        prefix = entry_path.rstrip("/")
        if not prefix:
            continue
        if entry_method.upper() == method.upper() and (path == prefix or path.startswith(prefix + "/")):
            return "auto"
    return "staged"


def stage_pending(repo, spec):
    (repo.hunt / "pending").mkdir(parents=True, exist_ok=True)
    pending_id = f"{int(now())}-{uuid4().hex[:6]}"
    record = {"id": pending_id, "staged_ts": now(), **spec}
    dump_json(repo.hunt / "pending" / f"{pending_id}.json", record)
    return pending_id


def cmd_pending(args):
    repo = repo_from_cwd()
    if args.pending_cmd == "list":
        for path in sorted((repo.hunt / "pending").glob("*.json")):
            record = load_json(path, {})
            print(f"{record.get('id')} {record.get('method')} {record.get('url')}")
    elif args.pending_cmd == "show":
        record = load_json(repo.hunt / "pending" / f"{args.id}.json", None)
        if record is None:
            raise HxError(f"pending nao encontrado: {args.id}", EXIT_GUARD)
        print(json.dumps(record, indent=1, ensure_ascii=False))
    elif args.pending_cmd == "run":
        cmd_pending_run(repo, args.id)
```

Wire `pending` subcommand (`list`, `show <id>`, `run <id>`); `cmd_pending_run` stub raising `HxError("pending run chega na Task 7", EXIT_GUARD)` until Task 7 replaces it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): scope guard, method classification, staging"
```

---

### Task 7: `hx run` execution + evidence + redaction + markers + `pending run`

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `RunTest`)

**Interfaces:**
- Consumes: Task 5 rate, Task 6 guard/staging.
- Produces: `fetch(method, url, headers, body, cookies) -> (status, headers_dict, text)` (tests stub it), `load_session(path)`, `sha12(text)`, `hash_or_drop(value)`, `redact_headers(headers)`, `redact_body(text)` (segredos em request **e** response), `extract_marker(pattern, text)`, `signal_matches(sig, status, headers, body)` (definido aqui — Task 8 consome), `parse_headers(pairs)`, `apply_health_signals(repo, scope, host, status, headers, body)` (step-up de rate em 429/503 e marcadores de WAF em **tráfego real**), `audit(repo, actor, tool, payload, verdict, reason)`, `save_evidence(repo, dir_path, meta, request, response, proof) -> Path`, `execute_request(repo, scope, method, url, session_path, body, marker, save_dir, hypothesis, headers=None) -> (status, resp_headers, text, path)`, `cmd_run(args)`, `cmd_pending_run(repo, pending_id)` (re-checa escopo antes de executar).

- [ ] **Step 1: Write the failing test**

```python
class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["out_of_scope_paths"] = ["/blog"]
        scope["allowed_mutations"] = ["POST /rest/user"]
        scope_path.write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_fetch = hx.fetch
        self.orig_tty = hx.stdin_is_tty
        self.orig_read = hx.read_line

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            return 200, {"Set-Cookie": "sess=supersecrettoken123456"}, '{"last4": "4242", "email": "a@b.com"}'

        hx.fetch = fake_fetch

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        hx.stdin_is_tty = self.orig_tty
        hx.read_line = self.orig_read
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_get_saves_redacted_evidence_with_marker(self):
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/h001", "--marker", r'"last4": "(\d+)"'])
        self.assertEqual(rc, 0)
        files = sorted((self.eng / "scans" / "evidence" / "h001").glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["response"]["status"], 200)
        blob = json.dumps(data)
        self.assertNotIn("supersecrettoken123456", blob)
        self.assertIn("sha256:", blob)
        self.assertEqual(data["proof"]["marker"], "4242")
        self.assertNotIn("a@b.com", data["response"]["body"])
        self.assertTrue(data["integrity"]["raw_sha256"])

    def test_banned_path_and_host_exit_2(self):
        self.assertEqual(hx.main(["run", "GET", "https://www.acme.com/blog/x"]), hx.EXIT_GUARD)
        self.assertEqual(hx.main(["run", "GET", "https://other.com/x"]), hx.EXIT_GUARD)

    def test_staged_method_exit_5_and_pending_flow(self):
        self.assertEqual(hx.main(["run", "DELETE", "https://www.acme.com/api/x"]), hx.EXIT_STAGED)
        pending = list((self.eng / "hunt" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        pending_id = pending[0].stem
        hx.stdin_is_tty = lambda: False
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_TTY)
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "www.acme.com"
        self.assertEqual(hx.main(["pending", "run", pending_id]), 0)
        self.assertEqual(len(list((self.eng / "hunt" / "pending").glob("*.json"))), 0)

    def test_pending_run_wrong_typed_host_aborts(self):
        hx.main(["run", "DELETE", "https://www.acme.com/api/y"])
        pending_id = list((self.eng / "hunt" / "pending").glob("*.json"))[0].stem
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "wrong.host"
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_GUARD)

    def test_json_output(self):
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/_misc", "--json"])
        self.assertEqual(rc, 0)

    def test_header_passed_and_429_steps_up(self):
        orig = hx.fetch
        try:
            hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "ok")
            hx.main(["run", "GET", "https://www.acme.com/api/x", "--header", "Accept: application/json;v=1", "--save", "scans/evidence/_misc"])
            hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (429, {}, "slow down")
            hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/_misc"])
        finally:
            hx.fetch = orig
        state = json.loads((self.eng / "hunt" / ".ratelimit.json").read_text())
        self.assertEqual(state["effective"]["www.acme.com"], 4.0)

    def test_headers_reach_fetch_and_request_body_is_redacted(self):
        seen = {}
        orig = hx.fetch
        try:
            def fake_fetch(method, url, headers=None, body=None, cookies=None):
                seen["headers"] = headers
                return 200, {}, "ok"
            hx.fetch = fake_fetch
            rc = hx.main(["run", "POST", "https://www.acme.com/rest/user", "--header", "Accept: application/json;v=1", "--data", "email=victim@x.com&password=hunter2secret", "--save", "scans/evidence/_misc"])
        finally:
            hx.fetch = orig
        self.assertEqual(rc, 0)
        self.assertEqual(seen["headers"]["Accept"], "application/json;v=1")
        blob = sorted((self.eng / "scans" / "evidence" / "_misc").glob("*.json"))[-1].read_text()
        self.assertNotIn("victim@x.com", blob)
        self.assertNotIn("hunter2secret", blob)

    def test_pending_reruns_scope_check(self):
        hx.main(["run", "DELETE", "https://www.acme.com/api/z"])
        pending_id = list((self.eng / "hunt" / "pending").glob("*.json"))[0].stem
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = []
        scope_path.write_text(json.dumps(scope))
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "www.acme.com"
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_GUARD)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py RunTest -v`
Expected: FAIL — `fetch` / `run` subcommand not defined.

- [ ] **Step 3: Write minimal implementation**

```python
HASH_HEADERS = re.compile(r"(?i)^(cookie|set-cookie|authorization)$|token")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
PAN_RE = re.compile(r"\b\d{13,19}\b")
SENSITIVE_PAIR_RE = re.compile(r"(?i)(password|passwd|secret|token|csrf|sessionid)=([^&\s\"']+)")
SENSITIVE_JSON_RE = re.compile(r'(?i)("(?:password|passwd|secret|token|csrf|sessionid)"\s*:\s*")([^"]*)(")')


def sha12(value):
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def hash_or_drop(value):
    if len(value) >= 16:
        return f"sha256:{sha12(value)}"
    return "<dropped>"


def redact_headers(headers):
    out = {}
    for name, value in (headers or {}).items():
        out[name] = hash_or_drop(str(value)) if HASH_HEADERS.search(name) else str(value)
    return out


def redact_body(text):
    if not text:
        return text
    redacted = EMAIL_RE.sub("[email-redacted]", text)
    redacted = PHONE_RE.sub("[phone-redacted]", redacted)
    redacted = PAN_RE.sub("[pan-redacted]", redacted)
    redacted = SENSITIVE_PAIR_RE.sub(lambda m: f"{m.group(1)}={hash_or_drop(m.group(2))}", redacted)
    return SENSITIVE_JSON_RE.sub(lambda m: m.group(1) + hash_or_drop(m.group(2)) + m.group(3), redacted)


def extract_marker(pattern, text):
    if not pattern or not text:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def load_session(path):
    if not path:
        return []
    return load_json(Path(path), [])


def http_request(session, method, url, headers, body):
    response = session.request(method, url, headers=headers or {}, data=body, timeout=30, allow_redirects=False)
    return response.status_code, dict(response.headers), response.text


def fetch(method, url, headers=None, body=None, cookies=None):
    from curl_cffi import requests as creq
    session = creq.Session(impersonate="chrome131", trust_env=False)
    for cookie in cookies or []:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
    try:
        return http_request(session, method, url, headers, body)
    except Exception as err:
        raise HxError(f"transporte: {err}", EXIT_TRANSPORT)


def audit(repo, actor, tool, payload, verdict, reason):
    entry = json.dumps({"ts": now(), "actor": actor, "tool": tool, "args_digest": (payload or "")[:200], "verdict": verdict, "reason": reason}, ensure_ascii=False)
    with flock(repo.hunt / ".lock.audit"):
        with open(repo.hunt / "audit.log", "a") as handle:
            handle.write(entry + "\n")


def save_evidence(repo, dir_path, meta, request, response, proof):
    dir_path = Path(dir_path)
    if not dir_path.is_absolute():
        dir_path = repo.eng / dir_path
    dir_path.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S", time.localtime(now()))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", request["url"])[-80:]
    path = dir_path / f"{stamp}_{request['method']}_{safe}_{uuid4().hex[:6]}.json"
    integrity = response.pop("integrity")
    record = {"meta": meta, "request": request, "response": response, "proof": proof, "integrity": integrity}
    atomic_write(path, json.dumps(record, indent=1, ensure_ascii=False))
    return path


def signal_matches(sig, status, headers, body):
    if sig.startswith("status:"):
        return status == int(sig.split(":", 1)[1])
    if sig.startswith("redirect_host:"):
        location = headers.get("location") or headers.get("Location") or ""
        return sig.split(":", 1)[1] in location
    if sig.startswith("body_contains:"):
        return sig.split(":", 1)[1] in (body or "")
    return False


def parse_headers(pairs):
    headers = {}
    for pair in pairs or []:
        name, _, value = pair.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    return headers or None


def apply_health_signals(repo, scope, host, status, headers, body):
    health_cfg = scope.get("health", {})
    for sig in health_cfg.get("rate_limited_if", []):
        if signal_matches(sig, status, headers, body):
            rate_step_up(scope, repo, host, f"http-{status}")
            audit(repo, "hx run", "health", host, "step-up", sig)
            return
    for sig in health_cfg.get("waf_block_if", []):
        if signal_matches(sig, status, headers, body):
            rate_step_up(scope, repo, host, "waf-marker")
            audit(repo, "hx run", "health", host, "step-up", sig)
            return


def execute_request(repo, scope, method, url, session_path, body, marker, save_dir, hypothesis, headers=None):
    host = urlparse(url).hostname or ""
    if session_path:
        tag = Path(session_path).stem.replace("session_", "")
        probe_tags = [probe["session"] for probe in (scope.get("health") or {}).get("probes", [])]
        if probe_tags and tag not in probe_tags:
            raise HxError(f"sessao '{tag}' (de {session_path}) nao bate com os probes ({', '.join(probe_tags)}) — registre o probe ou renomeie o arquivo", EXIT_GUARD)
        if health_get(repo, tag).get("dead_since"):
            raise HxError(f"sessao {tag} morta — reautentique antes de usar", EXIT_SESSION)
    max_wait = float(scope["rate"].get("max_wait_s", 30))
    rate_acquire(scope, repo, host, max_wait)
    cookies = load_session(repo.eng / session_path) if session_path else []
    status, resp_headers, text = fetch(method, url, headers, body, cookies)
    apply_health_signals(repo, scope, host, status, resp_headers, text)
    raw_hash = hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()
    proof = {"marker": extract_marker(marker, text)} if marker else {}
    request_record = {"method": method, "url": url, "headers": redact_headers(headers or {}), "body": redact_body(body) if body else None}
    response_record = {
        "status": status,
        "headers": redact_headers(resp_headers),
        "body": (redact_body(text) or "")[:8192],
        "integrity": {"raw_sha256": raw_hash, "body_len": len(text or "")},
    }
    meta = {"ts": now(), "session": session_path, "hypothesis": hypothesis, "status": status}
    path = save_evidence(repo, save_dir, meta, request_record, response_record, proof)
    return status, resp_headers, text, path


def cmd_run(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    headers = parse_headers(args.header)
    ok, reason = check_scope(scope, args.url)
    if not ok:
        raise HxError(f"guard: {reason}", EXIT_GUARD)
    if classify_method(scope, args.method, args.url) == "staged":
        pending_id = stage_pending(repo, {
            "method": args.method.upper(),
            "url": args.url,
            "body": args.data,
            "headers": headers,
            "session": args.session,
            "marker": args.marker,
        })
        print(f"staged: hunt/pending/{pending_id}.json — dj executa: hx pending run {pending_id}")
        return EXIT_STAGED
    save_dir = args.save or "scans/evidence/_misc"
    status, _, _, path = execute_request(repo, scope, args.method.upper(), args.url, args.session, args.data, args.marker, save_dir, args.hypothesis, headers)
    print(f"{status} {args.method.upper()} {args.url}")
    print(f"evidence: {path.relative_to(repo.eng)}")
    if args.json_out:
        print(json.dumps({"status": status, "evidence": str(path.relative_to(repo.eng))}, ensure_ascii=False))
    return EXIT_OK


def cmd_pending_run(repo, pending_id):
    if not stdin_is_tty():
        raise HxError("pending run exige TTY interativo", EXIT_TTY)
    path = repo.hunt / "pending" / f"{pending_id}.json"
    record = load_json(path, None)
    if record is None:
        raise HxError(f"pending nao encontrado: {pending_id}", EXIT_GUARD)
    scope = repo.scope()
    ok, reason = check_scope(scope, record["url"])
    if not ok:
        audit(repo, "dj", "pending run", record["url"], "refused", reason)
        raise HxError(f"pending agora fora do escopo: {reason}", EXIT_GUARD)
    host = urlparse(record["url"]).hostname or ""
    typed = read_line(f"digite o host para confirmar ({host}): ")
    if typed.strip() != host:
        raise HxError("confirmacao nao confere; abortado", EXIT_GUARD)
    execute_request(repo, scope, record["method"], record["url"], record.get("session"), record.get("body"), record.get("marker"), "scans/evidence/_pending", None, record.get("headers"))
    audit(repo, "dj", "pending run", record["url"], "executed", host)
    path.unlink()
    print("executado e removido da fila de pendentes")
    return EXIT_OK
```

Wire `run` parser: `method`, `url`, `--session`, `--data`, `--header` (append, repeatable, `Name: value`), `--save`, `--marker`, `--hypothesis`, `--json` (`dest="json_out"`). `pending` parser nested `show`/`run` take `id`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): run pipeline with redaction, markers, pending run"
```
---

### Task 8: health module — probes, strikes, death window, reopen sweep

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `HealthTest`)

**Interfaces:**
- Consumes: Task 5 rate, Task 7 fetch + `signal_matches` (definido lá).
- Produces: `health_load(repo)`, `health_get(repo, tag)`, `health_probe_result(repo, tag, ok, signal) -> declared`, `signal_matches(sig, status, headers, body)`, `probe_once(repo, scope, tag) -> (ok, signal)`, `health_fresh(repo, tag, fresh_s) -> bool`, `reopen_sweep(repo, tag, dead_since) -> list[str]`, `cmd_health_check(args)`; subcommand `health check [--session TAG|all]`.

- [ ] **Step 1: Write the failing test**

```python
class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_two_strikes_declare_death_with_first_strike_ts(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        hx.health_probe_result(self.repo, "a", False, "invalid")
        state = json.loads(self.repo.health.read_text())
        self.assertFalse(state["a"]["dead_since"])
        os.environ["HX_FAKE_NOW"] = "1100"
        declared = hx.health_probe_result(self.repo, "a", False, "invalid")
        state = json.loads(self.repo.health.read_text())
        self.assertTrue(declared)
        self.assertEqual(state["a"]["dead_since"], 1000)

    def test_valid_probe_resets_strikes(self):
        hx.health_probe_result(self.repo, "a", False, "invalid")
        hx.health_probe_result(self.repo, "a", True, None)
        state = json.loads(self.repo.health.read_text())
        self.assertEqual(state["a"]["strikes"], 0)

    def test_reopen_sweep_restores_refuted_in_window(self):
        os.environ["HX_SESSION"] = "sess-a"
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "a"])
        hx.main(["next"])
        os.environ["HX_FAKE_NOW"] = "1050"
        hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        reopened = hx.reopen_sweep(self.repo, "a", 1000)
        self.assertEqual(reopened, ["h001"])
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["status"], "open")
        self.assertEqual(hyps[0]["reopened_reason"], "session_death_window")

    def test_signal_matches(self):
        self.assertTrue(hx.signal_matches("status:401", 401, {}, ""))
        self.assertTrue(hx.signal_matches("redirect_host:login", 302, {"Location": "https://login.acme.com/x"}, ""))
        self.assertTrue(hx.signal_matches("body_contains:px-captcha", 403, {}, "blocked px-captcha here"))
        self.assertFalse(hx.signal_matches("status:401", 200, {}, ""))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py HealthTest -v`
Expected: FAIL — `health_probe_result` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def health_load(repo):
    return load_json(repo.health, {})


def health_get(repo, tag):
    default = {"strikes": 0, "first_strike": None, "dead_since": None, "suspect_since": None, "probes": []}
    return health_load(repo).get(tag, default)


def health_probe_result(repo, tag, ok, signal):
    with flock(repo.hunt / ".lock.health"):
        state = health_load(repo)
        entry = state.setdefault(tag, {"strikes": 0, "first_strike": None, "dead_since": None, "suspect_since": None, "probes": []})
        ts = now()
        entry["probes"].append({"ts": ts, "ok": ok, "signal": signal})
        declared = False
        if ok:
            entry["strikes"] = 0
            entry["first_strike"] = None
            entry["suspect_since"] = None
        elif signal == "invalid":
            if entry["strikes"] == 0:
                entry["first_strike"] = ts
            entry["strikes"] += 1
            if entry["strikes"] >= 2 and not entry["dead_since"]:
                entry["dead_since"] = entry["first_strike"]
                declared = True
        else:
            entry["suspect_since"] = ts
        dump_json(repo.health, state)
    return declared


def probe_once(repo, scope, tag):
    probe = next((p for p in scope["health"]["probes"] if p["session"] == tag), None)
    if probe is None:
        raise HxError(f"sem probe definido para sessao {tag}", EXIT_GUARD)
    cookies = load_session(repo.eng / probe["file"]) if probe.get("file") else []
    host = urlparse(probe["url"]).hostname or ""
    max_wait = float(scope["rate"].get("max_wait_s", 30))
    rate_acquire(scope, repo, host, max_wait)
    status, headers, body = fetch(probe["method"], probe["url"], probe.get("headers"), None, cookies)
    for sig in scope["health"]["rate_limited_if"]:
        if signal_matches(sig, status, headers, body):
            return False, "ratelimit"
    for sig in scope["health"]["waf_block_if"]:
        if signal_matches(sig, status, headers, body):
            return False, "waf"
    for sig in scope["health"]["session_invalid_if"]:
        if signal_matches(sig, status, headers, body):
            return False, "invalid"
    return True, None


def health_fresh(repo, tag, fresh_s):
    probes = health_get(repo, tag).get("probes") or []
    if not probes:
        return False
    last = probes[-1]
    return bool(last.get("ok")) and (now() - last["ts"]) <= fresh_s


def reopen_sweep(repo, tag, dead_since):
    reopened = []
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        for hyp in hyps:
            if hyp.get("status") != "result":
                continue
            result = hyp.get("result") or {}
            if result.get("verdict") != "refuted" or hyp.get("session_tag") != tag:
                continue
            if (hyp.get("closed_ts") or 0) >= dead_since:
                hyp["status"] = "open"
                hyp["owner"] = None
                hyp["reopened_reason"] = "session_death_window"
                reopened.append(hyp["id"])
        if reopened:
            save_hyps(repo, hyps)
    return reopened


def cmd_health_check(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    probes = scope["health"]["probes"]
    tags = [args.session] if args.session and args.session != "all" else [p["session"] for p in probes]
    for tag in tags:
        ok, signal = probe_once(repo, scope, tag)
        if signal == "ratelimit":
            probe = next(p for p in probes if p["session"] == tag)
            rate_step_up(scope, repo, urlparse(probe["url"]).hostname or "", "probe-429")
        declared = health_probe_result(repo, tag, ok, signal)
        print(f"{tag}: {'ok' if ok else signal}")
        if declared:
            entry = health_get(repo, tag)
            reopened = reopen_sweep(repo, tag, entry["dead_since"])
            print(f"{tag}: MORTA desde {entry['dead_since']} — reabertas: {', '.join(reopened) or 'nenhuma'}")
    return EXIT_OK
```

Wire `health check` parser (`--session`, default `all`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): health probes, strikes, death window, reopen sweep"
```

---

### Task 9: `hx result` refute gate — on-demand probe, exit 6, blocked suggestion

**Files:**
- Modify: `bin/hx` (`refute_gate` body, `AUTH_FAIL_STATUSES`)
- Modify: `tests/test_hx.py` (add `ResultGateTest`)

**Interfaces:**
- Consumes: Task 8 health; Task 4 `cmd_result` + `refute_gate` stub.
- Produces: `AUTH_FAIL_STATUSES`, `evidence_auth_failure(repo, hyp) -> bool`, filled `refute_gate(repo, hyp) -> dict | None`; refute behavior: dead → `EXIT_SESSION`; stale → on-demand probe; probe invalid → strike + `EXIT_SESSION`; probe dead (2 strikes) → reopen sweep + `EXIT_SESSION`; live → returns `{"session_tag": tag, "checked_ts": ts}` recorded in `result.session_check`.

- [ ] **Step 1: Write the failing test**

```python
class ResultGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["health"] = {
            "probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/probe"}],
            "fresh_max_s": 600,
            "session_invalid_if": ["status:401"],
            "waf_block_if": ["body_contains:px-captcha"],
            "rate_limited_if": ["status:429"],
        }
        scope_path.write_text(json.dumps(scope))
        evidence_dir = self.eng / "scans" / "evidence" / "h001"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "r.json").write_text(json.dumps({"response": {"status": 403}}))
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "b", "--evidence", "scans/evidence/h001"])
        hx.main(["next"])

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_refute_with_dead_probe_refused(self):
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (401, {}, "")
        try:
            rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, hx.EXIT_SESSION)

    def test_refute_with_live_probe_passes(self):
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "ok")
        try:
            rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["result"]["session_check"]["session_tag"], "b")

    def test_refute_with_dead_session_already_flagged(self):
        hx.health_probe_result(self.repo, "b", False, "invalid")
        hx.health_probe_result(self.repo, "b", False, "invalid")
        rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        self.assertEqual(rc, hx.EXIT_SESSION)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py ResultGateTest -v`
Expected: FAIL — refute closes without probing (`rc == 0` in the first test).

- [ ] **Step 3: Write minimal implementation**

```python
AUTH_FAIL_STATUSES = {401, 403}


def evidence_auth_failure(repo, hyp):
    if not hyp.get("evidence"):
        return False
    evidence_dir = Path(hyp["evidence"])
    if not evidence_dir.is_absolute():
        evidence_dir = repo.eng / evidence_dir
    if not evidence_dir.exists():
        return False
    files = sorted(evidence_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not files:
        return False
    response = (load_json(files[-1], {}).get("response") or {})
    status = response.get("status")
    if isinstance(status, int) and status in AUTH_FAIL_STATUSES:
        return True
    if isinstance(status, int) and 300 <= status < 400:
        location = (response.get("headers") or {}).get("location") or (response.get("headers") or {}).get("Location") or ""
        for sig in (repo.scope().get("health") or {}).get("session_invalid_if", []):
            if sig.startswith("redirect_host:") and sig.split(":", 1)[1] in location:
                return True
    return False


def refute_gate(repo, hyp):
    tag = hyp.get("session_tag")
    if not tag or not evidence_auth_failure(repo, hyp):
        return None
    scope = repo.scope()
    fresh_s = float(scope["health"].get("fresh_max_s", 600))
    entry = health_get(repo, tag)
    if entry.get("dead_since"):
        raise HxError(f"sessao {tag} morta — use --verdict blocked", EXIT_SESSION)
    if not health_fresh(repo, tag, fresh_s):
        ok, signal = probe_once(repo, scope, tag)
        declared = health_probe_result(repo, tag, ok, signal)
        if declared:
            reopened = reopen_sweep(repo, tag, health_get(repo, tag)["dead_since"])
            raise HxError(f"sessao {tag} morreu no probe (reabertas: {', '.join(reopened) or 'nenhuma'}) — use --verdict blocked", EXIT_SESSION)
        if not ok:
            raise HxError(f"sessao {tag} suspeita ({signal}) — resolva antes de refutar", EXIT_SESSION)
    return {"session_tag": tag, "checked_ts": now()}
```

The existing `cmd_result` from Task 4 already calls `refute_gate(repo, hyp)` and stores the return in `result.session_check`; with `refute_gate` filled, the behavior lands. Note the gate must run BEFORE the queue write (it does: it's called while building the record; probes hit the network, so restructuring for a shorter lock is acceptable here — the lock is held during the probe in this version; that is a deliberate simplification, single-operator box).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS — all suites.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): refute requires live session via on-demand probe"
```
---

### Task 10: `hx verify` — differential scenario

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `VerifyTest`)

**Interfaces:**
- Consumes: Task 7 fetch/evidence; Task 5 rate.
- Produces: `run_differential(repo, scope, draft) -> (passed, steps, requires_attest)`, `cmd_verify(args)`; draft schema (spec §8), **draft id == id da hipótese** (ex.: `h001`); evidence under `hunt/FINDINGS/drafts/<id>/`; PASS com marker ≥ 6 chars promove a `hunt/FINDINGS/<id>.json`; marker curto (PASS fraco) fica no draft com `pass_requires_attest: true` esperando `--attest`. Status gates: vítima/atacante/controle exigem 2xx pra contar — resposta de erro nunca vira PASS.

- [ ] **Step 1: Write the failing test**

```python
class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope_path.write_text(json.dumps(scope))
        recon = self.eng / "recon"
        recon.mkdir()
        (recon / "session_a.json").write_text(json.dumps([{"name": "s", "value": "victim", "domain": "www.acme.com"}]))
        (recon / "session_b.json").write_text(json.dumps([{"name": "s", "value": "attacker", "domain": "www.acme.com"}]))
        self.responses = {
            ("victim", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "B2"): (200, {}, '{"code": "ACME-111111"}'),
        }
        self.orig_fetch = hx.fetch

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            tag = (cookies or [{}])[0].get("value")
            object_id = url.rsplit("/", 1)[-1]
            return self.responses[(tag, object_id)]

        hx.fetch = fake_fetch
        draft = {
            "id": "h001",
            "title": "BOLA giftcard",
            "scenario": "differential",
            "attacker_session": "recon/session_b.json",
            "victim_session": "recon/session_a.json",
            "request": {"method": "GET", "url": "https://www.acme.com/giftcard/{cardId}", "victim_object": {"cardId": "A1"}},
            "marker": {"extract": r'"code": "([A-Z0-9-]+)"'},
            "control": {"attacker_own_object": {"cardId": "B2"}},
        }
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.fetch = self.orig_fetch
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_differential_pass_moves_to_findings(self):
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        self.assertFalse((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())
        proof = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(proof["proof"]["attacker"]["marker_present"], True)
        self.assertEqual(proof["proof"]["control"]["marker_absent"], True)
        steps = list((self.repo.hunt / "FINDINGS" / "drafts" / "h001").glob("*.json"))
        self.assertEqual(len(steps), 3)

    def test_differential_fail_keeps_draft_marked(self):
        self.responses[("attacker", "A1")] = (200, {}, '{"code": "ACME-111111"}')
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertIn("failed_at", draft)
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_short_marker_requires_attest(self):
        self.responses[("victim", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "B2")] = (200, {}, '{"last4": "1111"}')
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["marker"] = {"extract": r'"last4": "(\d+)"'}
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(stored["pass_requires_attest"])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_non_2xx_attacker_cannot_pass(self):
        self.responses[("attacker", "A1")] = (403, {}, '{"error": "forbidden"}')
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertIn("failed_at", stored)
```

**Plan erratum:** o teste de integração diferencial→attest (`test_short_marker_pass_can_be_attested`) foi movido para a Task 11 — aqui o `run_attest` ainda é stub e o teste não passaria. A Task 11 o adiciona à classe `VerifyTest`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py VerifyTest -v`
Expected: FAIL — `verify` subcommand missing.

- [ ] **Step 3: Write minimal implementation**

```python
def run_differential(repo, scope, draft):
    request = draft["request"]
    url_template = request["url"]
    victim_object = request.get("victim_object") or {}
    attacker_object = (draft.get("control") or {}).get("attacker_own_object") or {}
    pattern = (draft.get("marker") or {}).get("extract")

    def call(session_file, url):
        allowed, reason = check_scope(scope, url)
        if not allowed:
            raise HxError(f"guard: {reason}", EXIT_GUARD)
        if classify_method(scope, request["method"], url) == "staged":
            raise HxError(f"verify nao executa metodo estagiado: {request['method']} {url}", EXIT_STAGED)
        cookies = load_session(repo.eng / session_file)
        host = urlparse(url).hostname or ""
        rate_acquire(scope, repo, host, float(scope["rate"].get("max_wait_s", 30)))
        status, headers, body = fetch(request["method"], url, None, None, cookies)
        apply_health_signals(repo, scope, host, status, headers, body)
        return status, headers, body

    def substitute(url, mapping):
        for key, value in mapping.items():
            url = url.replace("{" + key + "}", str(value))
        return url

    victim_url = substitute(url_template, victim_object)
    attacker_url = victim_url
    control_url = substitute(url_template, attacker_object)

    output_dir = repo.hunt / "FINDINGS" / "drafts" / draft["id"]
    steps = {}
    victim_status, victim_headers, victim_body = call(draft["victim_session"], victim_url)
    marker = extract_marker(pattern, victim_body)
    steps["victim"] = {"status": victim_status, "marker": marker}
    attacker_status, attacker_headers, attacker_body = call(draft["attacker_session"], attacker_url)
    marker_present = 200 <= attacker_status < 300 and bool(marker) and marker in attacker_body
    steps["attacker"] = {"status": attacker_status, "marker_present": marker_present}
    control_status, control_headers, control_body = call(draft["attacker_session"], control_url)
    marker_absent = 200 <= control_status < 300 and not (bool(marker) and marker in control_body)
    steps["control"] = {"status": control_status, "marker_absent": marker_absent}

    def evidence(step, url, status, headers, body, proof):
        save_evidence(
            repo,
            output_dir,
            {"kind": "verify", "step": step, "draft": draft["id"]},
            {"method": request["method"], "url": url, "headers": {}, "body": None},
            {"status": status, "headers": redact_headers(headers), "body": (redact_body(body) or "")[:8192], "integrity": {"raw_sha256": hashlib.sha256((body or "").encode("utf-8", errors="ignore")).hexdigest(), "body_len": len(body or "")}},
            proof,
        )

    evidence("victim", victim_url, victim_status, victim_headers, victim_body, {"marker": marker})
    evidence("attacker", attacker_url, attacker_status, attacker_headers, attacker_body, {"marker": marker})
    evidence("control", control_url, control_status, control_headers, control_body, {})
    passed = 200 <= victim_status < 300 and bool(marker) and marker_present and marker_absent
    requires_attest = passed and len(str(marker)) < 6
    return passed, steps, requires_attest


def cmd_verify(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    draft_path = repo.hunt / "FINDINGS" / "drafts" / f"{args.id}.json"
    draft = load_json(draft_path, None)
    if draft is None:
        raise HxError(f"draft nao encontrado: {args.id}", EXIT_GUARD)
    if getattr(args, "attest", False):
        run_attest(repo, draft, draft_path)
        return EXIT_OK
    if not draft.get("request"):
        raise HxError(f"draft sem request: {args.id}", EXIT_GUARD)
    scenario = draft.get("scenario")
    if scenario == "differential":
        passed, steps, requires_attest = run_differential(repo, scope, draft)
    else:
        raise HxError(f"cenario nao suportado no v1: {scenario}", EXIT_GUARD)
    if passed and requires_attest:
        draft["pass_requires_attest"] = True
        draft["proof"] = steps
        dump_json(draft_path, draft)
        print(f"{draft['id']}: PASS fraco (marker curto '{steps['victim']['marker']}') — assine com: hx verify {draft['id']} --attest")
        return EXIT_OK
    if passed:
        draft["verified_at"] = now()
        draft["proof"] = steps
        dump_json(repo.hunt / "FINDINGS" / f"{draft['id']}.json", draft)
        draft_path.unlink()
        print(f"{draft['id']}: PASS — {json.dumps(steps, ensure_ascii=False)}")
        return EXIT_OK
    draft["failed_at"] = now()
    draft["fail_detail"] = steps
    dump_json(draft_path, draft)
    print(f"{draft['id']}: FAIL — {json.dumps(steps, ensure_ascii=False)}")
    return EXIT_OK
```

`run_attest` is defined in Task 11; for the Task 10 commit, add a temporary `def run_attest(repo, draft, draft_path): raise HxError("attest chega na Task 11", EXIT_GUARD)` so `cmd_verify` imports cleanly; Task 11 replaces it.

Wire `verify` parser: `id`, `--attest` flag.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): differential verify scenario"
```

---

### Task 11: `hx verify --attest` — manual-attested, TTY-gated

**Files:**
- Modify: `bin/hx` (replace `run_attest` stub)
- Modify: `tests/test_hx.py` (add `AttestTest`)

**Interfaces:**
- Produces: `run_attest(repo, draft, draft_path)` — requires TTY (`EXIT_TTY`), prints checklist, requires typed draft id, records `verified-manual` + `attest{uid, tty, ts}`; moves draft to `FINDINGS/`.

- [ ] **Step 1: Write the failing test**

```python
class AttestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_tty = hx.stdin_is_tty
        self.orig_read = hx.read_line
        draft = {"id": "h001", "title": "race condition", "scenario": "manual"}
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.stdin_is_tty = self.orig_tty
        hx.read_line = self.orig_read
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_attest_requires_tty(self):
        hx.stdin_is_tty = lambda: False
        self.assertEqual(hx.main(["verify", "h001", "--attest"]), hx.EXIT_TTY)

    def test_attest_wrong_id_aborts(self):
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h999"
        self.assertEqual(hx.main(["verify", "h001", "--attest"]), hx.EXIT_GUARD)

    def test_attest_happy_path(self):
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        rc = hx.main(["verify", "h001", "--attest"])
        self.assertEqual(rc, 0)
        attested = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(attested["verdict"], "verified-manual")
        self.assertEqual(attested["attest"]["draft_id"], "h001")
        self.assertIn("uid", attested["attest"])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())
```

**Adição (movida da Task 10):** anexe também à classe `VerifyTest` (as fixtures de sessão/fetch já existem lá) o teste de integração diferencial→attest:

```python
    def test_short_marker_pass_can_be_attested(self):
        self.responses[("victim", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "B2")] = (200, {}, '{"last4": "1111"}')
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["marker"] = {"extract": r'"last4": "(\d+)"'}
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        orig_tty, orig_read = hx.stdin_is_tty, hx.read_line
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        try:
            rc = hx.main(["verify", "h001", "--attest"])
        finally:
            hx.stdin_is_tty, hx.read_line = orig_tty, orig_read
        self.assertEqual(rc, 0)
        attested = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(attested["verdict"], "verified-manual")
        self.assertTrue(attested["proof"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py AttestTest -v`
Expected: FAIL — stub raises "attest chega na Task 11".

- [ ] **Step 3: Write minimal implementation**

```python
def run_attest(repo, draft, draft_path):
    if not stdin_is_tty():
        raise HxError("attest exige TTY interativo", EXIT_TTY)
    proof = draft.get("proof")
    if proof:
        victim = proof.get("victim", {})
        attacker = proof.get("attacker", {})
        control = proof.get("control", {})
        print("prova mecanica do draft:")
        print(f"  marcador: {victim.get('marker')}")
        print(f"  vitima: {victim.get('status')} | atacante: {attacker.get('status')} (marker presente: {attacker.get('marker_present')}) | controle: {control.get('status')} (marker ausente: {control.get('marker_absent')})")
    else:
        print("sem prova mecanica — checklist manual:")
    print("checklist: (1) contexto escalado p/ 2 contas proprias? (2) segundo teste independente? (3) payload reproduzivel?")
    typed = read_line(f"digite o id do draft para assinar ({draft['id']}): ")
    if typed.strip() != draft["id"]:
        raise HxError("assinatura nao confere; abortado", EXIT_GUARD)
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError:
        tty = None
    draft["verified_at"] = now()
    draft["verdict"] = "verified-manual"
    draft["attest"] = {"draft_id": draft["id"], "uid": os.getuid(), "tty": tty, "ts": now()}
    dump_json(repo.hunt / "FINDINGS" / f"{draft['id']}.json", draft)
    draft_path.unlink()
    print(f"{draft['id']}: verified-manual (uid {os.getuid()})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): manual-attested verify with TTY gate"
```

---

### Task 12: `hx proxy` — CONNECT allowlist proxy with connection rate

**Files:**
- Modify: `bin/hx`
- Modify: `tests/test_hx.py` (add `ProxyTest`)

**Interfaces:**
- Produces: `ProxyHandler`, `build_proxy(repo, scope, port) -> ThreadingTCPServer`, `cmd_proxy(args)`; shares `.ratelimit.json` (via `rate_acquire` on each new connection); denials logged with `audit()`.

- [ ] **Step 1: Write the failing test**

```python
class ProxyTest(unittest.TestCase):
    def setUp(self):
        import socketserver
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        scope = json.loads((self.repo.hunt / "scope.json").read_text())
        scope["in_scope"] = ["127.0.0.1"]
        scope["rate"] = {"per_host_interval_s": 0.0, "global_interval_s": 0.0, "max_wait_s": 5}
        (self.repo.hunt / "scope.json").write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None

        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(1024)
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.proxy = hx.build_proxy(self.repo, scope, 0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.shutdown()
        hx.sleep = self.orig_sleep
        os.environ.pop("HX_ENGAGEMENT", None)
        self.tmp.cleanup()

    def test_allowed_tunnels_and_denied_403(self):
        import socket
        proxy_port = self.proxy.server_address[1]
        upstream_port = self.upstream.server_address[1]
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        head = client.recv(1024)
        self.assertIn(b"200", head.split(b"\r\n")[0])
        client.sendall(b"ping")
        self.assertIn(b"ok", client.recv(1024))
        client.close()
        client2 = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client2.sendall(b"CONNECT evil.example.org:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        head2 = client2.recv(1024)
        self.assertIn(b"403", head2.split(b"\r\n")[0])
        client2.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_hx.py ProxyTest -v`
Expected: FAIL — `build_proxy` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
import select
import socketserver


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            first = self.request.recv(4096)
            if not first:
                return
            line = first.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            scope = self.server.scope
            repo = self.server.repo
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                audit(repo, "proxy", "CONNECT", line[:120], "denied", "nao-CONNECT")
                self.request.sendall(b"HTTP/1.1 501 Not Implemented\r\n\r\n")
                return
            host_port = parts[1]
            host = host_port.rsplit(":", 1)[0]
            port = int(host_port.rsplit(":", 1)[1]) if ":" in host_port else 443
            allowed, reason = check_scope(scope, f"https://{host}/")
            if not allowed:
                audit(repo, "proxy", "CONNECT", host_port, "denied", reason)
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            try:
                rate_acquire(scope, repo, host, float(scope["rate"].get("max_wait_s", 30)))
            except HxError as err:
                audit(repo, "proxy", "CONNECT", host_port, "rate", str(err))
                self.request.sendall(f"HTTP/1.1 429 Too Many Requests\r\nX-Hx: {err}\r\n\r\n".encode())
                return
            upstream = socket.create_connection((host, port), timeout=15)
            try:
                audit(repo, "proxy", "CONNECT", host_port, "allowed", "")
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                sockets = [self.request, upstream]
                while True:
                    readable, _, _ = select.select(sockets, [], [], 60)
                    if not readable:
                        break
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is self.request else self.request
                        target.sendall(data)
            finally:
                upstream.close()
        except Exception as err:
            audit(self.server.repo, "proxy", "CONNECT", "", "error", str(err))


def build_proxy(repo, scope, port):
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    server = Server(("127.0.0.1", port), ProxyHandler)
    server.repo = repo
    server.scope = scope
    return server


def cmd_proxy(args):
    repo = repo_from_cwd()
    scope = repo.scope()
    server = build_proxy(repo, scope, args.port)
    print(f"hx proxy em 127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
```

Add `import socket` and `import select` to the imports at the top of `bin/hx`. Wire `Proxy` parser (`--port` default 8899).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_hx.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(hx): CONNECT allowlist proxy with shared connection rate"
```
---

### Task 13: plugin guard lib (`guard.js`) + node tests

**Files:**
- Create: `opencode/plugin/lib/guard.js`
- Create: `tests/plugin_guard.test.mjs`

**Interfaces:**
- Produces (ESM): `SCAN_BINARIES`, `hostMatches(pattern, host)`, `hostAllowed(scope, host)`, `extractHosts(command)`, `decideBash(scope, command) -> {action: "allow"|"block", reason}`, `decideUrl(scope, url)`. Consumed by `hunt.ts` (Task 14).

- [ ] **Step 1: Write the failing test**

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { decideBash, decideUrl, hostAllowed } from "../opencode/plugin/lib/guard.js";

const scope = {
  in_scope: ["acme.com"],
  out_of_scope_hosts: ["vpn.acme.com", "*.rentals.acme.com"],
  allow_extra_hosts: ["api.mail.tm"],
};

test("allows hx commands without judging", () => {
  assert.equal(decideBash(scope, "hx run GET https://evil.example.org/x").action, "allow");
});

test("blocks out-of-scope URL inside python -c", () => {
  const cmd = `python3 -c "import requests; requests.get('https://evil.example.org/x')"`;
  assert.equal(decideBash(scope, cmd).action, "block");
});

test("blocks userinfo-spoofed URL", () => {
  const cmd = `python3 -c "import requests; requests.get('https://www.acme.com:x@evil.example.org/')"`;
  assert.equal(decideBash(scope, cmd).action, "block");
});

test("blocks scan binary with bare out-of-scope host", () => {
  assert.equal(decideBash(scope, "nmap -p443 evil.example.org").action, "block");
});

test("blocks scan binary with IP, CIDR and sudo wrapper", () => {
  assert.equal(decideBash(scope, "nmap 1.2.3.4").action, "block");
  assert.equal(decideBash(scope, "masscan 10.0.0.0/8").action, "block");
  assert.equal(decideBash(scope, "sudo nmap evil.example.org").action, "block");
  assert.equal(decideBash(scope, "sudo nmap -p443 www.acme.com").action, "allow");
});

test("allows in-scope and extra hosts", () => {
  assert.equal(decideBash(scope, "curl https://www.acme.com/x").action, "allow");
  assert.equal(decideBash(scope, "curl https://api.mail.tm/messages").action, "allow");
});

test("banned host is denied by rules", () => {
  assert.equal(hostAllowed(scope, "vpn.acme.com"), false);
  assert.equal(hostAllowed(scope, "x.rentals.acme.com"), false);
  assert.equal(decideUrl(scope, "https://vpn.acme.com/x").action, "block");
  assert.equal(decideUrl(scope, "https://www.acme.com/x").action, "allow");
});

test("ipv6 loopback parity", () => {
  assert.equal(decideUrl(scope, "http://[::1]:8080/x").action, "allow");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/plugin_guard.test.mjs`
Expected: FAIL — cannot find module `../opencode/plugin/lib/guard.js`.

- [ ] **Step 3: Write minimal implementation**

```js
export const SCAN_BINARIES = ["curl", "wget", "nuclei", "ffuf", "naabu", "dnsx", "httpx", "katana", "subfinder", "s3scanner", "nmap", "masscan"];

const WRAPPERS = new Set(["sudo", "env", "command", "time", "nice", "nohup"]);
const IPV4_RE = /^(?:\d{1,3}\.){3}\d{1,3}$/;

function stripBrackets(host) {
  const value = (host || "").toLowerCase();
  if (value.startsWith("[") && value.endsWith("]")) return value.slice(1, -1);
  return value;
}

function baseBinary(command) {
  const words = command.trim().split(/\s+/).filter((token) => !token.startsWith("-") && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(token));
  for (const word of words) {
    if (WRAPPERS.has(word)) continue;
    return word.split("/").pop();
  }
  return "";
}

function hostToken(token) {
  let candidate = token.replace(/:\d+$/, "").replace(/\/\d{1,2}$/, "");
  candidate = stripBrackets(candidate);
  if (IPV4_RE.test(candidate)) return candidate;
  if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(candidate)) return candidate;
  return null;
}

export function hostMatches(pattern, host) {
  const normalized = pattern.toLowerCase().trim();
  const target = stripBrackets(host);
  if (normalized.startsWith("*.")) {
    return target.endsWith(normalized.slice(1)) || target === normalized.slice(2);
  }
  return target === normalized || target.endsWith("." + normalized);
}

export function hostAllowed(scope, host) {
  const target = stripBrackets(host);
  if (!target || target === "127.0.0.1" || target === "localhost" || target === "::1") return true;
  for (const pattern of scope.out_of_scope_hosts || []) {
    if (hostMatches(pattern, target)) return false;
  }
  for (const pattern of scope.in_scope || []) {
    if (hostMatches(pattern, target)) return true;
  }
  for (const pattern of scope.allow_extra_hosts || []) {
    if (hostMatches(pattern, target)) return true;
  }
  return false;
}

export function extractHosts(command) {
  const hosts = new Set();
  const urlRe = /https?:\/\/[^\s"']+/gi;
  let match;
  while ((match = urlRe.exec(command)) !== null) {
    try {
      hosts.add(stripBrackets(new URL(match[0]).hostname));
    } catch {
      continue;
    }
  }
  return [...hosts];
}

export function decideBash(scope, command) {
  if (!command) return { action: "allow", reason: "" };
  const trimmed = command.trim();
  const segments = trimmed.split(/[;\n]|&&|\|\|?/).map((segment) => segment.trim()).filter(Boolean);
  if (segments.length > 0 && segments.every((segment) => segment === "hx" || segment.startsWith("hx "))) {
    return { action: "allow", reason: "hx path" };
  }
  const offenders = new Set();
  for (const host of extractHosts(trimmed)) {
    if (!hostAllowed(scope, host)) offenders.add(host);
  }
  if (SCAN_BINARIES.includes(baseBinary(trimmed))) {
    for (const token of trimmed.split(/\s+/)) {
      if (token.startsWith("-")) continue;
      const host = hostToken(token);
      if (host && !hostAllowed(scope, host)) offenders.add(host);
    }
  }
  if (offenders.size > 0) {
    return { action: "block", reason: `host fora do escopo: ${[...offenders].join(", ")} — use hx run` };
  }
  return { action: "allow", reason: "" };
}

export function decideUrl(scope, url) {
  if (!url) return { action: "allow", reason: "" };
  let host = "";
  try {
    host = stripBrackets(new URL(url).hostname);
  } catch {
    return { action: "allow", reason: "" };
  }
  if (hostAllowed(scope, host)) return { action: "allow", reason: "" };
  return { action: "block", reason: `host fora do escopo: ${host}` };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/plugin_guard.test.mjs`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add opencode/plugin/lib/guard.js tests/plugin_guard.test.mjs
git commit -m "feat(plugin): scope tripwire guard lib + node tests"
```
---

### Task 14: opencode layer — plugin `hunt.ts`, agents, commands, `install.sh`

**Files:**
- Create: `opencode/plugin/hunt.ts`
- Create: `opencode/agents/hunt.md`
- Create: `opencode/agents/hunt-auto.md`
- Create: `opencode/commands/brief.md`
- Create: `opencode/commands/next.md`
- Create: `opencode/commands/debrief.md`
- Create: `install.sh`

**Interfaces:**
- Consumes: `guard.js` (Task 13), `hx` (Tasks 1–12).
- Produces: plugin hooks (brief injection via `experimental.chat.system.transform`, proxy env via `shell.env`, tripwire via `tool.execute.before`); agent files; command templates; installer.

**Honest note:** exact hook payload shapes (`input.tool` vs `input.name`, `output.system` string vs array) can vary by opencode version — this task codes defensively (`try/catch`, both shapes handled). Task 16 confirms at runtime and corrects if needed.

- [ ] **Step 1: Write `opencode/plugin/hunt.ts`**

```ts
import type { Plugin } from "@opencode-ai/plugin"
import { decideBash, decideUrl, extractHosts, hostAllowed } from "./lib/guard.js"
import { appendFileSync, existsSync, readFileSync } from "node:fs"
import { join, resolve } from "node:path"

function engagementDir(): string | null {
  let dir = resolve(process.cwd())
  for (;;) {
    if (existsSync(join(dir, "hunt", "HYPOTHESES.json"))) return dir
    const parent = resolve(dir, "..")
    if (parent === dir) return null
    dir = parent
  }
}

function scopeOf(dir: string): any | null {
  try {
    return JSON.parse(readFileSync(join(dir, "hunt", "scope.json"), "utf8"))
  } catch {
    return null
  }
}

function audit(dir: string, tool: string, argsDigest: string, verdict: string, reason: string) {
  try {
    const entry = { ts: Date.now() / 1000, actor: "plugin", tool, args_digest: argsDigest.slice(0, 200), verdict, reason }
    appendFileSync(join(dir, "hunt", "audit.log"), JSON.stringify(entry) + "\n")
  } catch {}
}

export default (async ({ $ }) => {
  return {
    "experimental.chat.system.transform": async (_input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        const brief = await $`hx brief --max-bytes 4000`.cwd(dir).text()
        if (typeof output?.system === "string") {
          output.system = output.system + "\n\n" + brief
        } else if (Array.isArray(output?.system)) {
          output.system.push(brief)
        }
      } catch (err) {
        audit(dir, "brief", "hx brief", "error", String(err))
      }
    },
    "shell.env": async (_input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      try {
        output.env.HTTP_PROXY = "http://127.0.0.1:8899"
        output.env.HTTPS_PROXY = "http://127.0.0.1:8899"
        output.env.ALL_PROXY = "http://127.0.0.1:8899"
        output.env.NO_PROXY = "localhost,127.0.0.1"
      } catch (err) {
        audit(dir, "shell.env", "proxy vars", "error", String(err))
      }
    },
    "tool.execute.before": async (input: any, output: any) => {
      const dir = engagementDir()
      if (!dir) return
      const scope = scopeOf(dir)
      if (!scope) {
        audit(dir, "tripwire", "scope.json", "block", "scope ilegivel — rede bloqueada")
        throw new Error("hunt/ presente mas scope.json ilegivel — rede bloqueada")
      }
      const tool = input?.tool || input?.name
      if (tool === "bash") {
        const command = String(output?.args?.command || "")
        const decision = decideBash(scope, command)
        audit(dir, "bash", command, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "webfetch") {
        const url = String(output?.args?.url || "")
        const decision = decideUrl(scope, url)
        audit(dir, "webfetch", url, decision.action, decision.reason)
        if (decision.action === "block") throw new Error(decision.reason)
      }
      if (tool === "websearch") {
        const query = String(output?.args?.query || "")
        const offenders = extractHosts(query).filter((host: string) => !hostAllowed(scope, host))
        if (offenders.length > 0) {
          audit(dir, "websearch", query, "block", `host fora do escopo: ${offenders.join(", ")}`)
          throw new Error(`host fora do escopo: ${offenders.join(", ")} — use hx run`)
        }
        audit(dir, "websearch", query, "allow", "")
      }
    },
  }
}) satisfies Plugin
```

- [ ] **Step 2: Write `opencode/agents/hunt.md`**

```markdown
---
description: Caça a vuln num engagement hx — executa work orders, guarda evidência, fecha com verify.
mode: primary
permission:
  edit: allow
  bash:
    "*": ask
    "curl *": deny
    "wget *": deny
    "hx *": allow
---

Você executa caça com o harness `hx`. O brief do engagement já entra no seu contexto.

Disciplina de work order:
- `hx next` claima hipóteses; trabalhe cada uma como "confirme ou refute este claim" — nunca "procure problemas".
- Toda saída de rede: `hx run` (ele aplica escopo, rate, evidência e redação). Nunca contorne com curl/wget/python.
- Feche cada hipótese com `hx result <id> --verdict ... --note ...`; `confirmed` exige `--evidence`.
- Refute baseado em 401/403: o `hx result` dispara o probe de sessão sozinho; se a sessão estiver morta/suspeita ele recusa (exit 6) — feche como `blocked`, não force.
- Mutação fora de `allowed_mutations` estagia (exit 5): reporte o pending id ao dj, nunca insista.
- Hipótese nova descoberta no meio: `hx hypothesis add ...`.
- Achado candidato: monte o draft em `hunt/FINDINGS/drafts/<id>.json` e rode `hx verify <id>`. Só PASS vira finding. `manual-attested` é assinatura do dj — você não origina.
- Fim de sessão: siga `/debrief` — escreva `hunt/sessions/AAAA-MM-DD-NN.md`, atualize coverage e TARGET.

Nunca conclua "não tem vulnerabilidade". Conclua hipóteses — refutadas, bloqueadas ou confirmadas.
```

- [ ] **Step 3: Write `opencode/agents/hunt-auto.md`**

```markdown
---
description: Gêmeo headless do hunt — sem permissões interativas (runner L3).
mode: primary
permission:
  edit: allow
  bash:
    "*": deny
    "hx *": allow
---

Mesma disciplina do agente `hunt`, em modo headless:
- Sem `ask` em lugar nenhum — o que não tem permissão é negado e reportado no resultado.
- Toda saída de rede via `hx run`; mutações estagiam (exit 5) e a fatia fecha como `blocked (staged)`.
- Refute com sessão morta/suspeita fecha como `blocked (session)`.
- Encerre o trabalho com um resumo curto e estruturado da fatia (claim, veredito, evidência, o que falta).
```

- [ ] **Step 4: Write the three commands**

`opencode/commands/brief.md`:

```markdown
---
description: Mostra o brief atual do engagement.
---

Rode `hx brief --max-bytes 4000` e resuma os pontos que mudam o plano de agora (sessões mortas, coverage, hipóteses abertas).
```

`opencode/commands/next.md`:

```markdown
---
description: Claima as próximas work orders da fila.
---

Rode `hx next 3` e trabalhe as hipóteses na ordem, uma a uma, fechando com `hx result`.
```

`opencode/commands/debrief.md`:

```markdown
---
description: Encerra a sessão gravando o debrief no estado durável.
---

1. Escreva `hunt/sessions/AAAA-MM-DD-NN.md` com: o que rodou, vereditos, hipóteses novas, deltas de coverage, dúvidas abertas e o próximo passo sugerido.
2. Enfileire as hipóteses novas que surgiram (`hx hypothesis add`).
3. Atualize `TARGET.md` se aprendeu algo técnico do alvo (auth, IDs, quirks, gates).
4. Confirme que só findings verificados estão em `hunt/FINDINGS/`.
```

- [ ] **Step 5: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/opencode/agents" "$HOME/.config/opencode/commands" "$HOME/.local/bin"
chmod +x "$ROOT/bin/hx"
ln -sf "$ROOT/bin/hx" "$HOME/.local/bin/hx"
ln -sf "$ROOT/opencode/agents/hunt.md" "$HOME/.config/opencode/agents/hunt.md"
ln -sf "$ROOT/opencode/commands/brief.md" "$HOME/.config/opencode/commands/brief.md"
ln -sf "$ROOT/opencode/commands/next.md" "$HOME/.config/opencode/commands/next.md"
ln -sf "$ROOT/opencode/commands/debrief.md" "$HOME/.config/opencode/commands/debrief.md"
echo "symlinks ok"
echo "adicione ao array plugin do ~/.config/opencode/opencode.jsonc:"
echo "\"file://$ROOT/opencode/plugin/hunt.ts\""
echo "reinicie o opencode depois de editar o config (nao ha hot reload)"
```

- [ ] **Step 6: Verify**

Run: `bash -n install.sh && node --test tests/plugin_guard.test.mjs && python3 tests/test_hx.py`
Expected: syntax ok, node tests PASS, python tests PASS.

- [ ] **Step 7: Commit**

```bash
git add opencode/ install.sh
git commit -m "feat(opencode): hunt agents, plugin hooks, commands, installer"
```
---

### Task 15: ACME migration — init + scope + state seeds

**Files:**
- Modify (created by hx): `/home/ngix/acme-bbp/hunt/` (scope.json, TARGET.md, COVERAGE.md, HYPOTHESES.json)

**Interfaces:**
- Consumes: all `hx` commands (Tasks 1–12).
- Produces: the first real engagement workspace, ready for the Task 16 smoke.

- [ ] **Step 1: Init the engagement**

```bash
cd /home/ngix/acme-bbp
/home/ngix/huntbench/bin/hx init --dir .
```

Expected: `engagement inicializado: /home/ngix/acme-bbp/hunt`.

- [ ] **Step 2: Write `hunt/scope.json` (full content, replacing the template values)**

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
      { "session": "a", "file": "recon/session_a.json", "method": "GET", "url": "https://www.acme.com/mobile-gateway/rest/cart/V3" },
      { "session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/mobile-gateway/rest/cart/V3" }
    ],
    "fresh_max_s": 600,
    "session_invalid_if": ["status:401", "status:403", "redirect_host:login.acme.com", "body_contains:logonId"],
    "waf_block_if": ["body_contains:_px", "body_contains:px-captcha"],
    "rate_limited_if": ["status:429", "status:503"]
  },
  "allowed_mutations": [
    "POST /rest/user",
    "POST /mobile-gateway/rest/user/V1",
    "POST /mobile-gateway/rest/user/guest/V1",
    "POST /mobile-gateway/rest/cart/items/V1"
  ],
  "evidence_dir": "scans/evidence"
}
```

- [ ] **Step 3: Write `hunt/TARGET.md`**

```markdown
# TARGET — ACME (acme-bbp)

## Assets
- www.acme.com — Akamai edge + PerimeterX (px-cloud.net/pxchk.net) + ThreatMetrix. CSP grande com terceiros.
- Mobile gateway: https://www.acme.com/mobile-gateway/rest/ (app ACME-Android/26.7.0; requests assinados com APIGuard (com.apiguard3) no app real — forjar inviável sem device; rotas testáveis via browser/cliente com headers de app).
- login.acme.com — 0 reports resolvidos; superfície nova.
- collaboration.acme.com — 0 reports; não mapeado.

## Auth
- Guest: POST /mobile-gateway/rest/user/guest/V1 (Content-Length: 0) → 201 + JWT em cookie.
- Contas próprias A/B/C criadas via POST /rest/user (form). Login web pela UI do browser (API de login bloqueada por Akamai p/ cliente externo).
- Sessões em recon/session_{a,b,c}.json (cookie-list).

## Versionamento de API (crítico)
- Header `Accept: application/json;q=0.9;v=1` (site) / `application/json;v=1` + Content-Type json (app).
- VersionCheckUtil#acceptsVersion rejeita versão errada com 415. Semântica de v=2/v=3 não testada.

## Objetos e IDs
- Pedido/carrinho: orderId + externalOrderId (ex.: A407275194). Checkout valida orderId contra a sessão (BaseWebServiceImpl#validateOrderIdAgainstCheckoutInfoDtoOrderId).
- Wishlist: id + entry id. SFL: itemEntryId. Endereço: id uint64. Conta: accountId hex. Giftcard: cardId. Cards: sequenceId.

## Quirks observados
- Erros de wishlist vazam classes internas (WishListsWebServiceImpl, "user does not own this list").
- Gateway mobile tem gate Akamai mais estrito que o site.
- DELETE SFL com id alheio = 204 no-op (deleta da própria lista).
- POST address com id alheio ignora o id e cria endereço novo.
- /rest/user/account?accountId=X ignora o param (session-scoped).
- forgot-password: resposta idêntica existente/inexistente (200 {"expiresAfterMinutes":60}).
- orders/cancel-order/999999999 → 503 com classe Java (error disclosure — fora de escopo).
```

- [ ] **Step 4: Write `hunt/COVERAGE.md`**

```markdown
| endpoint | classe | status | evidência | nota |
|---|---|---|---|---|
| PUT /mobile-gateway/rest/wishlist | BOLA | refuted | scans/evidence/b_put_a_wishlist.json | 401 "user does not own this list" |
| DELETE /mobile-gateway/rest/wishlist/entry/{id} | BOLA | refuted | scans/evidence/b_delete_a_wl_entry.json | 401 mesma mensagem |
| POST /mobile-gateway/rest/checkout/orders/{externalOrderId}/audit-request/V3 | BOLA | refuted | scans/evidence/b_audit_a_order.json | 404 "Empty cart" — session-scoped |
| POST /rest/checkout/orders/{externalOrderId}/audit-request (web) | BOLA | refuted | scans/evidence/web_b_audit_a.json | 404 session-scoped |
| POST audit-request c/ carrinho próprio + orderId alheio | BOLA | refuted | (log de sessão) | 403 "session id invalid" — valida orderId vs sessão |
| DELETE /mobile-gateway/rest/saveforlater/{itemEntryId} | BOLA | refuted | scans/evidence/b_delete_a_sfl.json | 204 no-op (deleta da própria lista) |
| PUT /rest/user/address/{id} (id alheio em ambos campos) | BOLA | refuted | scans/evidence/b_update_a_address.json | 412 "does not exist or does not belong to the user" |
| POST /rest/user/address c/ id alheio | BOLA | refuted | scans/evidence/b_update_a_address.json | 200 cria endereço novo do B (ignora id estrangeiro) |
| GET /rest/user/account?accountId={A} | IDOR | refuted | (log de sessão) | 200 com dados do B (param ignorado) |
| POST /rest/user (mass assignment employeeId/memberNumber/memberType) | mass assignment | refuted | (log de sessão) | campos ignorados (C: employeeNumber=null) |
| POST /rest/user/forgot-password | user enum | refuted | (log de sessão) | respostas idênticas 200 em existente vs random |
| GET /mobile-gateway/rest/user/member/{numero}/type | IDOR | refuted | (log de sessão) | 404 session-bound |
| POST /mobile-gateway/rest/orders/cancel-order/{orderId} | BOLA | untested | (log de sessão) | só 503 error disclosure testado (fora de escopo); cross-account não |
| login.acme.com (superfície) | surface | untested | - | 0 reports resolvidos — prioridade |
| collaboration.acme.com (superfície) | surface | untested | - | 0 reports; não mapeado |
| GET /mobile-gateway/rest/user/orders (v=2/v=3) | API versioning | untested | - | só v=1 exercitado |
```

- [ ] **Step 5: Seed the hypothesis queue**

```bash
cd /home/ngix/acme-bbp
H=/home/ngix/huntbench/bin/hx
$H hypothesis add --claim "Semântica de versão: v=2/v=3 em user/orders retorna forma/dados diferentes e authz mais fraca que v=1" --endpoint "GET /mobile-gateway/rest/user/orders?year=2026&includeSkus=true" --class "API versioning" --confirm "200 com dado válido em v≠1; ou 415 seletivo (só algumas versões validadas)" --refute "415 em todas as versões ≠1" --session-tag b
$H hypothesis add --claim "login.acme.com (0 reports) tem fluxo com validação fraca: enumeração/OTP/reset fora do padrão do www" --endpoint "login.acme.com" --class "auth" --confirm "resposta divergente para existente vs inexistente; ou fluxo sem rate/validação" --refute "respostas idênticas e rate consistente em N=5"
$H hypothesis add --claim "Claims do JWT/sessão influenciam authz: mutar member id na claim é aceito (transição guest→user)" --endpoint "cookies de sessão (guest JWT)" --class "JWT/authz" --confirm "alteração de claim refletida em dados ou ausência de validação de assinatura" --refute "assinatura validada (401/403)"
$H hypothesis add --claim "collaboration.acme.com (0 reports) é superfície nova com authz fraca" --endpoint "collaboration.acme.com" --class "surface" --confirm "fluxo autenticado sem ownership check" --refute "authz consistente nos fluxos testáveis"
$H hypothesis add --claim "BOLA read: sessão B lê giftcard da conta A via gateway mobile" --endpoint "GET /mobile-gateway/rest/user/giftcard/{cardId}?includeBalance=true" --class "BOLA" --confirm "200 com last4/saldo do cartão de A" --refute "401/403/404; ou 200 sem dados de A" --session-tag b
$H hypothesis add --claim "Endpoints de checkout além do audit-request validam externalOrderId contra a sessão? (só o audit-request foi testado)" --endpoint "POST /mobile-gateway/rest/checkout/orders/{externalOrderId}/..." --class "BOLA" --confirm "endpoint que aceite orderId alheio retornando dado de A" --refute "403 'session id invalid'/404 em todos os testados" --session-tag b
```

- [ ] **Step 6: Verify the workspace**

```bash
cd /home/ngix/acme-bbp
/home/ngix/huntbench/bin/hx brief --max-bytes 4000
/home/ngix/huntbench/bin/hx next --peek
```

Expected: brief with alvo/escopo/coverage/hipóteses abertas; `next --peek` lists h001–h006 with claim/confirm/refute/evidência.

- [ ] **Step 7: Commit (huntbench side only)**

Workspace `hunt/` state is not committed anywhere by design (evidence/sessions gitignored; engagement folder is not a repo). No commit in this task.

---

### Task 16: E2E acceptance smoke (ACME, manual)

**Files:**
- Create: `/home/ngix/acme-bbp/hunt/sessions/2026-09-15-01.md` (acceptance notes)

**Interfaces:**
- Consumes: everything.
- Produces: the acceptance record — session 1 evidence that the bench works end-to-end.

- [ ] **Step 1: CLI checks (no browser needed)**

```bash
cd /home/ngix/acme-bbp
H=/home/ngix/huntbench/bin/hx
$H brief --max-bytes 4000 | wc -c
$H run GET https://www.acme.com/mobile-gateway/rest/geo/countries --save scans/evidence/_smoke
$H run GET https://www.acme.com/blog/x; echo "exit=$?"
$H run DELETE https://www.acme.com/mobile-gateway/rest/user/giftcard/1; echo "exit=$?"
```

Expected: brief ≤ 4000 bytes; geo 200 + evidence file; `/blog` exit 2; DELETE exit 5 + pending file.

- [ ] **Step 2: Proxy check (terminal A holds `hx proxy`, terminal B runs clients)**

```bash
# terminal A
cd /home/ngix/acme-bbp && /home/ngix/huntbench/bin/hx proxy
# terminal B
HTTPS_PROXY=http://127.0.0.1:8899 python3 -c "import requests; requests.get('https://example.org', timeout=5)"
# expected: requests.exceptions.ProxyError / 403 from proxy

HTTPS_PROXY=http://127.0.0.1:8899 python3 -c "import requests; r=requests.get('https://www.acme.com/mobile-gateway/rest/geo/countries', timeout=15); print(r.status_code)"
# expected: 200
```

- [ ] **Step 3: opencode layer check (restart needed after install + config edit)**

Add `"file:///home/ngix/huntbench/opencode/plugin/hunt.ts"` to the `plugin` array in `~/.config/opencode/opencode.jsonc`, run `bash install.sh`, restart opencode inside `/home/ngix/acme-bbp`, select the `hunt` agent.

Then: (a) confirm the brief appears in context at session start; (b) ask the agent to run `curl https://example.org` → blocked by tripwire; (c) run `hx run GET https://www.acme.com/mobile-gateway/rest/geo/countries` → allowed. Record any hook-shape corrections needed in `hunt/sessions/2026-09-15-01.md` (and fix `hunt.ts` in this repo if so).

- [ ] **Step 4: Session-2 compounding check**

Work one hypothesis (e.g., h001) to a verdict with `hx result`; se o veredito for `confirmed`, o caminho obrigatório é: draft em `hunt/FINDINGS/drafts/h001.json` → `hx verify h001` → `hx result h001 --verdict confirmed` (o `result` rejeita sem finding verificado); write the acceptance debrief to `hunt/sessions/2026-09-15-01.md` (o que rodou, vereditos, deltas de coverage, próximo passo); start a fresh conversation and confirm `hx brief` now shows: coverage counters with the new status, the debrief tail, and the work order queue — i.e., the session begins stronger than the first.

- [ ] **Step 5: Record acceptance**

Write `hunt/sessions/2026-09-15-01.md` with: the 7 check results, any deviations (hook shapes, proxy behavior), and the go/no-go for phase 1.5. No commit (external state; `hunt/sessions/` is gitignored on the bench side as a rule of the spec).

---

**Done = phase 1 complete:** `hx` green (`python3 tests/test_hx.py` + `node --test tests/plugin_guard.test.mjs`), ACME workspace seeded, acceptance recorded, and one real work order closed with durable state.
