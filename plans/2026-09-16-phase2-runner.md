# Hunt Harness — Phase 2 Implementation Plan (L3 runner v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `runner/runner.py` — the L3 supervisor that executes the hypothesis queue in sequential short-lived headless sessions: `CLAIM → BUILD_PROMPT → INVOKE → PARSE → (RETRY | RESULT → ROTATE)`, with budgets, stop conditions, kill switch, health gate, and adjudication via the existing `hx verify`.

**Architecture:** The runner is a thin supervisor: it reuses `bin/hx` (loaded as a module via the same importlib trick the tests use) for queue/health/rate/time seams, spawns `opencode run` headless with `hunt-auto` injected inline via `OPENCODE_CONFIG_CONTENT`, parses `opencode export`, appends to `hunt/runs.jsonl`, and adjudicates drafts with `hx verify` as a subprocess. No new trust paths: candidates never bypass existing gates.

**Tech Stack:** Python 3.14 stdlib (subprocess, json, signal). No new deps. Suites: `python3 tests/test_runner.py` + existing.

**Spec:** `specs/2026-09-15-hunt-harness-design.md` (Rev 7 — §9 runner + §6.2 injection decision).

## Global Constraints

- NO comments and NO docstrings anywhere in code (strict). Portuguese prose in user messages.
- **1 worker = 1 sessão viva por vez**; wave = até `slices_max` sessões **sequenciais**. Concorrência >1 fora do v1.
- Worker result = **candidato**: adjudicação só via `hx verify` (subprocesso). Nenhum gate novo.
- `hunt-auto` **não** vira symlink: injeção inline via `OPENCODE_CONFIG_CONTENT` (prompt do `opencode/agents/hunt-auto.md`, permissions inline).
- Time/sleep seams via `hx.now()`/`hx.sleep` (HX_FAKE_NOW-friendly). Tests never spawn real agents.
- Budgets default: `wall_s 7200`, `slices_max 20`, `slice_timeout_s 900`, backoff `5s→120s` (3 tentativas), rate-backoff `30s→300s`.
- Smoke (T5) roda **engagement sintético local**; REI fica fora até o dj aprovar explicitamente.
- Commit por task: `git add runner/ tests/ README.md templates/ && git commit -m ...` (specs/plans ficam untracked).

---

### Task 1: runner skeleton — claim, prompt, invoke, parse, one slice

**Files:** Create `runner/runner.py`; Create `tests/test_runner.py`.

**Interfaces:**
- Produces: `load_hx(repo_root) -> module`; `DEFAULTS`; `claim_next(repo, owner) -> dict | None` (usa `hx.load_hyps/save_hyps/flock`; marca `claimed` + `owner` + `claimed_ts`); `build_prompt(hyp, brief_text, slice_timeout_s) -> str`; `invoke_opencode(prompt, cwd, config_content, timeout_s, model=None) -> (rc, stdout, stderr)`; `parse_session_id(stdout) -> str | None`; `export_usage(hx_mod, session_id) -> dict`; `run_slice(ctx) -> dict`; `record_run(repo, record)` (append `hunt/runs.jsonl`).
- Consumes: `bin/hx` (module), `opencode run --format json` (subprocess), `opencode export` (subprocess).

- [ ] **Step 1: Write the failing tests**

Fixture: engagement temporário (`hx init`), `scope` default; uma hipótese enfileirada. Patch seams: `runner.invoke_opencode`, `runner.export_usage`.

```python
import importlib.util, json, os, tempfile, unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "runner" / "runner.py"

def load_runner():
    spec = importlib.util.spec_from_file_location("runner_mod", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner = load_runner()

class SliceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "BOLA x", "--endpoint", "GET /a/{id}", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        self.orig_invoke = runner.invoke_opencode
        self.orig_export = runner.export_usage
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (0, '{"sessionID":"ses_test"}\n', "")
        runner.export_usage = lambda hx_mod, session_id: {"tokens": 123, "cost": 0.01}

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        runner.export_usage = self.orig_export
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_claim_next_marks_owner(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        self.assertEqual(hyp["id"], "h001")
        self.assertEqual(hyp["owner"], "runner-test")

    def test_build_prompt_contains_work_order(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        prompt = runner.build_prompt(hyp, "BRIEF-DE-TESTE", 900)
        for needle in ("h001", "BOLA x", "confirm", "refute", "hx run", "hx result", "BRIEF-DE-TESTE"):
            self.assertIn(needle, prompt)

    def test_run_slice_records_jsonl(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertEqual(record["session"], "ses_test")
        self.assertEqual(record["tokens"], 123)
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(lines[-1])["hyp"], "h001")
```

- [ ] **Step 2: RED** — `python3 tests/test_runner.py -v` → runner missing.

- [ ] **Step 3: Implement** — `runner/runner.py` começa com:

```python
#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "wall_s": 7200,
    "slices_max": 20,
    "slice_timeout_s": 900,
    "backoff_base_s": 5,
    "backoff_max_s": 120,
    "backoff_attempts": 3,
    "rate_backoff_s": 30,
    "rate_backoff_max_s": 300,
}


def load_hx(repo_root=REPO_ROOT):
    spec = importlib.util.spec_from_file_location("hx", Path(repo_root) / "bin" / "hx")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hx = load_hx()


def claim_next(repo, owner):
    with hx.flock(repo.hunt / ".lock.hyps"):
        hyps, _ = hx.load_hyps(repo)
        for hyp in hyps:
            if hyp["status"] != "open":
                continue
            hyp["status"] = "claimed"
            hyp["owner"] = owner
            hyp["claimed_ts"] = hx.now()
            hx.save_hyps(repo, hyps)
            return hyp
    return None
```

`build_prompt` (template PT — verbatim, com as variáveis interpoladas):

```python
PROMPT_TEMPLATE = """Você executa UMA fatia de caça (work order). Não explore além dela.

HIPÓTESE [{id}] — {claim}
endpoint: {endpoint} | classe: {cls}
confirm: {confirm}
refute: {refute}
evidência: {evidence} (salve pares req/resp aqui)

BRIEF:
{brief}

REGRAS:
- Toda saída de rede via `hx run`; mutação fora de allowed_mutations estagia (exit 5) — não insista.
- Feche com `hx result {id} --verdict ... --note ...` (confirmed exige finding verificado).
- Hipótese nova descoberta: `hx hypothesis add ...`.
- Sem conclusão em {minutes} min: `hx result {id} --verdict blocked --note "give_up"` e pare.
- Termine com um resumo curto: veredito, evidência, o que falta."""


def build_prompt(hyp, brief_text, slice_timeout_s):
    return PROMPT_TEMPLATE.format(
        id=hyp["id"],
        claim=hyp["claim"],
        endpoint=hyp["endpoint"],
        cls=hyp["class"],
        confirm=hyp["confirm"],
        refute=hyp["refute"],
        evidence=hyp["evidence"],
        brief=brief_text,
        minutes=int(slice_timeout_s / 60),
    )
```

`invoke_opencode` / `parse_session_id` / `export_usage` / `run_slice` / `record_run`:

```python
def invoke_opencode(prompt, cwd, config_content, timeout_s, model=None):
    env = dict(os.environ)
    env["OPENCODE_CONFIG_CONTENT"] = config_content
    argv = ["opencode", "run", "--format", "json"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return (proc.returncode, proc.stdout, proc.stderr)


def parse_session_id(stdout):
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        sid = data.get("sessionID") or data.get("session_id")
        if sid:
            return sid
    return None


def export_usage(hx_mod, session_id):
    if not session_id:
        return {}
    try:
        proc = subprocess.run(["opencode", "export", session_id], capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return {}
    info = data.get("info") or {}
    tokens = info.get("tokens") or {}
    total = tokens.get("total") or sum(tokens.get(key, 0) or 0 for key in ("input", "output", "reasoning"))
    return {"tokens": total or None, "cost": info.get("cost")}


def record_run(repo, record):
    path = repo.hunt / "runs.jsonl"
    with hx.flock(repo.hunt / ".lock.runs"):
        with open(path, "a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_slice(ctx):
    repo = ctx["repo"]
    hyp = ctx["hyp"]
    prompt = build_prompt(hyp, ctx.get("brief") or "", ctx.get("slice_timeout_s") or DEFAULTS["slice_timeout_s"])
    rc, stdout, stderr = invoke_opencode(prompt, repo.eng, ctx["config"], ctx.get("slice_timeout_s") or DEFAULTS["slice_timeout_s"], ctx.get("model"))
    session = parse_session_id(stdout)
    usage = export_usage(hx, session) if session else {}
    record = {
        "ts": hx.now(),
        "hyp": hyp["id"],
        "session": session,
        "rc": rc,
        "tokens": usage.get("tokens"),
        "cost": usage.get("cost"),
        "stderr": (stderr or "")[:400],
    }
    record_run(repo, record)
    return record
```

- [ ] **Step 4: GREEN** — `python3 tests/test_runner.py -v`.

- [ ] **Step 5: Commit** — `git add runner/ tests/test_runner.py && git commit -m "feat(runner): slice skeleton (claim/prompt/invoke/parse)"`

---

### Task 2: retry/backoff, budgets, stop conditions, kill, health gate, CLI

**Files:** Modify `runner/runner.py`; Modify `tests/test_runner.py`.

**Interfaces:**
- `invoke_opencode(..., model=None, resume_session=None)` — quinta/keyword novo adicionado; com `resume_session`, argv ganha `["--continue", "--session", sid]` antes do prompt.
- `is_rate_failure(text) -> bool` (marcadores `rate`/`429`/`waf`/`captcha` no texto somado de stdout+stderr).
- `retry_invoke(ctx, prompt) -> (rc, stdout, stderr, attempts)` — backoff `base*2^(n-1)` capado em `backoff_max_s`; rate-failure usa `rate_backoff_s*2^(n-1)` capado em `rate_backoff_max_s`; resume da sessão quando o parse já viu um `sessionID`; sleep via `hx.sleep` (patchável).
- `health_gate(hx_mod, repo, hyp) -> (alive: bool, tag)` — sem probes/no scope ou sem `session_tag` → `(True, None)`; `health_get(tag).dead_since` → `(False, tag)`.
- `check_stop(ctx) -> str | None` — razões: `"kill"` (`hunt/runner.stop` existe), `"slices"`, `"wall"` (via `hx.now()`), `"tokens"`/`"cost"` (caps opcionais), `"empty_queue"`.
- `request_stop(repo)`, `write_pid(repo)`, `install_signals(ctx)` (SIGTERM/SIGINT → `request_stop`), `claim_peek_empty(repo)`.
- `runner_main(argv=None) -> int` — CLI `--engagement --slices --wall --model --no-adjudicate`; loop: `check_stop` → `claim_next` → `health_gate` → `run_slice` → acumula tokens/custo/slices. Config de spawn: `RUNNER_CONFIG_CONTENT` env ou fallback `{"default_agent":"hunt-auto"}` (a T4 troca pelo builder real). `runner.pid` escrito no start, removido no fim.

- [ ] **Step 1: Write the failing tests** (adicionar a `tests/test_runner.py`)

```python
class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        self.sleeps = []
        self.orig_sleep = runner.hx.sleep
        runner.hx.sleep = lambda seconds: self.sleeps.append(seconds)
        self.orig_invoke = runner.invoke_opencode
        self.calls = []

    def tearDown(self):
        runner.hx.sleep = self.orig_sleep
        runner.invoke_opencode = self.orig_invoke
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def make_ctx(self):
        return {"repo": self.repo, "budget": dict(runner.DEFAULTS), "config": "{}", "model": None, "slice_timeout_s": 900}

    def test_backoff_doubles_then_succeeds(self):
        results = [(1, "", "boom"), (1, "", "boom"), (0, '{"sessionID":"ses_x"}', "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            self.calls.append(kwargs)
            return results.pop(0)
        runner.invoke_opencode = fake
        rc, stdout, stderr, attempts = runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual((rc, attempts), (0, 3))
        self.assertEqual(self.sleeps, [5, 10])

    def test_rate_failure_uses_rate_backoff(self):
        results = [(1, "", "HTTP 429 too many"), (0, "", "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            return results.pop(0)
        runner.invoke_opencode = fake
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.sleeps, [30])

    def test_resume_passes_session_after_first_failure(self):
        results = [(1, '{"sessionID":"ses_r"}', "boom"), (0, "", "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            self.calls.append(kwargs.get("resume_session"))
            return results.pop(0)
        runner.invoke_opencode = fake
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.calls, [None, "ses_r"])


class StopGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def ctx(self, **kw):
        base = {"repo": self.repo, "budget": dict(runner.DEFAULTS), "started_ts": 1000, "slices_done": 0, "tokens_used": 0, "cost_used": 0}
        base.update(kw)
        return base

    def test_stop_reasons(self):
        runner.request_stop(self.repo)
        self.assertEqual(runner.check_stop(self.ctx()), "kill")
        (self.repo.hunt / "runner.stop").unlink()
        self.assertEqual(runner.check_stop(self.ctx(slices_done=20)), "slices")
        self.assertEqual(runner.check_stop(self.ctx(**{"budget": {**runner.DEFAULTS, "wall_s": 0}})), "wall")

    def test_health_gate_dead_session(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        runner.hx.health_probe_result(self.repo, "b", False, "invalid")
        runner.hx.health_probe_result(self.repo, "b", False, "invalid")
        alive, tag = runner.health_gate(runner.hx, self.repo, {"session_tag": "b"})
        self.assertFalse(alive)
        self.assertEqual(tag, "b")
        self.assertEqual(runner.health_gate(runner.hx, self.repo, {})[0], True)
```

- [ ] **Step 2: RED** — `python3 tests/test_runner.py -v`.

- [ ] **Step 3: Implement** (código real do plano):

```python
def invoke_opencode(prompt, cwd, config_content, timeout_s, model=None, resume_session=None):
    env = dict(os.environ)
    env["OPENCODE_CONFIG_CONTENT"] = config_content
    argv = ["opencode", "run", "--format", "json"]
    if model:
        argv += ["--model", model]
    if resume_session:
        argv += ["--continue", "--session", resume_session]
    argv.append(prompt)
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    return (proc.returncode, proc.stdout, proc.stderr)


def is_rate_failure(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("rate", "429", "waf", "captcha"))


def retry_invoke(ctx, prompt):
    attempts = 0
    session = None
    budget = ctx["budget"]
    rc, stdout, stderr = 1, "", ""
    while attempts < budget["backoff_attempts"]:
        attempts += 1
        rc, stdout, stderr = invoke_opencode(
            prompt, ctx["repo"].eng, ctx["config"], ctx.get("slice_timeout_s") or DEFAULTS["slice_timeout_s"], ctx.get("model"), resume_session=session
        )
        if rc == 0:
            break
        text = f"{stdout}\n{stderr}"
        if is_rate_failure(text):
            wait = min(budget["rate_backoff_max_s"], budget["rate_backoff_s"] * (2 ** (attempts - 1)))
        else:
            wait = min(budget["backoff_max_s"], budget["backoff_base_s"] * (2 ** (attempts - 1)))
        session = session or parse_session_id(stdout)
        hx.sleep(wait)
    return rc, stdout, stderr, attempts


def health_gate(hx_mod, repo, hyp):
    scope = repo.scope()
    probes = (scope.get("health") or {}).get("probes") or []
    tag = hyp.get("session_tag")
    if not probes or not tag:
        return True, None
    if hx_mod.health_get(repo, tag).get("dead_since"):
        return False, tag
    return True, tag


def claim_peek_empty(repo):
    with hx.flock(repo.hunt / ".lock.hyps"):
        hyps, _ = hx.load_hyps(repo)
    return not any(hyp["status"] == "open" for hyp in hyps)


def request_stop(repo):
    (repo.hunt / "runner.stop").touch()


def write_pid(repo):
    (repo.hunt / "runner.pid").write_text(str(os.getpid()))


def install_signals(ctx):
    def handler(signum, frame):
        request_stop(ctx["repo"])
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def check_stop(ctx):
    repo = ctx["repo"]
    budget = ctx["budget"]
    if (repo.hunt / "runner.stop").exists():
        return "kill"
    if ctx["slices_done"] >= budget["slices_max"]:
        return "slices"
    if hx.now() - ctx["started_ts"] >= budget["wall_s"]:
        return "wall"
    if budget.get("token_cap") and ctx["tokens_used"] >= budget["token_cap"]:
        return "tokens"
    if budget.get("cost_cap") and ctx["cost_used"] >= budget["cost_cap"]:
        return "cost"
    if claim_peek_empty(repo):
        return "empty_queue"
    return None


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("--engagement", default=".")
    parser.add_argument("--slices", type=int, default=DEFAULTS["slices_max"])
    parser.add_argument("--wall", type=int, default=DEFAULTS["wall_s"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-adjudicate", action="store_true")
    args = parser.parse_args(argv)
    repo = hx.Repo(Path(args.engagement).resolve())
    os.environ["HX_ENGAGEMENT"] = str(repo.eng)
    owner = f"runner-{os.getpid()}"
    budget = {**DEFAULTS, "slices_max": args.slices, "wall_s": args.wall, "token_cap": None, "cost_cap": None}
    ctx = {
        "repo": repo, "hyp": None, "brief": "", "config": os.environ.get("RUNNER_CONFIG_CONTENT") or json.dumps({"default_agent": "hunt-auto"}),
        "model": args.model, "budget": budget, "started_ts": hx.now(), "slices_done": 0, "tokens_used": 0, "cost_used": 0,
    }
    write_pid(repo)
    install_signals(ctx)
    while True:
        reason = check_stop(ctx)
        if reason:
            print(f"runner: parada ({reason})")
            break
        hyp = claim_next(repo, owner)
        if hyp is None:
            print("runner: fila vazia")
            break
        alive, tag = health_gate(hx, repo, hyp)
        if not alive:
            hx.main(["result", hyp["id"], "--verdict", "blocked", "--note", f"sessao {tag} morta", "--force"])
            ctx["slices_done"] += 1
            continue
        ctx["hyp"] = hyp
        record = run_slice(ctx)
        ctx["slices_done"] += 1
        ctx["tokens_used"] += record.get("tokens") or 0
        ctx["cost_used"] += record.get("cost") or 0
    (repo.hunt / "runner.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(runner_main())
```

E `run_slice` passa a usar `retry_invoke` no lugar de `invoke_opencode` direto.

- [ ] **Step 4: GREEN** — `python3 tests/test_runner.py -v` + `python3 tests/test_hx.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(runner): retry/backoff, budgets, stop, kill, health gate, CLI"`

---

### Task 3: adjudication pass (`hx verify`)

**Files:** Modify `runner/runner.py`; Modify `tests/test_runner.py`.

**Interfaces:**
- `adjudicate(repo, hid) -> dict` — se `hunt/FINDINGS/drafts/<hid>.json` **não** existe → `{"ran": False}` (sem subprocesso). Se existe: roda `[REPO_ROOT/bin/hx, "verify", hid]` (subprocess, cwd=engagement, timeout 600); mapeia `{"ran": True, "rc": ..., "promoted": rc==0 and draft sumiu}`; **`pending_attest` = rc 0 + draft retido + `draft["pass_requires_attest"]`** (o verify sai 0 no PASS-fraco — rc 7 não acontece nesse caminho); exceção → `{"ran": True, "rc": None, "error": str(exc)[:200]}`.
- `run_slice` chama `adjudicate` após gravar o record, e inclui `record["adjudication"]`; `ctx.get("no_adjudicate")` desliga (flag do CLI ganha efeito aqui).

- [ ] **Step 1: Write the failing tests**

```python
class AdjudicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        self.orig_run = runner.subprocess.run

    def tearDown(self):
        runner.subprocess.run = self.orig_run
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def draft(self, hid="h001"):
        path = self.repo.hunt / "FINDINGS" / "drafts" / f"{hid}.json"
        runner.hx.dump_json(path, {"id": hid, "scenario": "differential"})
        return path

    def test_no_draft_skips_subprocess(self):
        called = []
        runner.subprocess.run = lambda *a, **k: called.append(a) or None
        self.assertEqual(runner.adjudicate(self.repo, "h001"), {"ran": False})
        self.assertEqual(called, [])

    def test_promoted_when_rc0_and_draft_moved(self):
        path = self.draft()
        def fake(argv, **kwargs):
            path.unlink()
            class P: returncode = 0; stdout = "PASS"; stderr = ""
            return P()
        runner.subprocess.run = fake
        result = runner.adjudicate(self.repo, "h001")
        self.assertTrue(result["promoted"])
        self.assertEqual(result["rc"], 0)

    def test_pending_attest_on_rc7(self):
        self.draft()
        def fake(argv, **kwargs):
            class P: returncode = 7; stdout = ""; stderr = ""
            return P()
        runner.subprocess.run = fake
        result = runner.adjudicate(self.repo, "h001")
        self.assertTrue(result["pending_attest"])
        self.assertFalse(result["promoted"])
```

E em `SliceTest`: `run_slice` inclui `record["adjudication"]` (patch `runner.adjudicate` para `{"ran": False}` no teste existente; um teste novo com draft presente + patch do subprocess cobre o include; `no_adjudicate: True` no ctx pula a chamada).

- [ ] **Step 2: RED** — `python3 tests/test_runner.py -v`.

- [ ] **Step 3: Implement**

```python
def adjudicate(repo, hid):
    draft_path = repo.hunt / "FINDINGS" / "drafts" / f"{hid}.json"
    if not draft_path.exists():
        return {"ran": False}
    hx_bin = REPO_ROOT / "bin" / "hx"
    try:
        proc = subprocess.run([str(hx_bin), "verify", hid], cwd=str(repo.eng), capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return {"ran": True, "rc": None, "error": str(exc)[:200]}
    result = {"ran": True, "rc": proc.returncode, "promoted": proc.returncode == 0 and not draft_path.exists()}
    if proc.returncode == 0 and draft_path.exists():
        draft = runner.hx.load_json(draft_path, {})
        if draft.get("pass_requires_attest"):
            result["pending_attest"] = True
    return result
```

Em `run_slice`, após `record_run`:

```python
    if not ctx.get("no_adjudicate"):
        record["adjudication"] = adjudicate(repo, hyp["id"])
        record_run(repo, record)
```

(ajustar: montar o record completo, incluir adjudication quando aplicável, e gravar uma vez; o caso `no_adjudicate` grava sem a chave).

- [ ] **Step 4: GREEN** — `python3 tests/test_runner.py -v` + `python3 tests/test_hx.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(runner): adjudication via hx verify subprocess"`

---

### Task 4: `hunt-auto` inline injection + runner_main hardening (scoped additions da review do T2)

**Files:** Modify `runner/runner.py`; Modify `tests/test_runner.py`.

**Interfaces:**
- `build_config_content(agent_md_path) -> str` — lê o md, descarta o frontmatter (`---` … `---`), usa o corpo como `prompt`; JSON `{"$schema": "https://opencode.ai/config.json", "agent": {"hunt-auto": {"description": "headless hunt worker (runner)", "mode": "primary", "permission": {"bash": {"*": "deny", "hx *": "allow"}, "edit": "allow"}, "prompt": body}}, "default_agent": "hunt-auto"}`. Nenhum symlink; TUI não vê o gêmeo.
- `runner_main` troca o fallback: `config = os.environ.get("RUNNER_CONFIG_CONTENT") or build_config_content(REPO_ROOT / "opencode" / "agents" / "hunt-auto.md")`.

**Scoped additions (ruled da review do T2):**
- `runner_main`: wrap do loop em `try/finally` — `runner.pid` **sempre** removido, mesmo com exceção no meio.
- `retry_invoke`: sleep **só entre tentativas** — `if attempts < budget["backoff_attempts"]: hx.sleep(wait)`.

- [ ] **Step 1: Write the failing tests**

```python
class ConfigContentTest(unittest.TestCase):
    def test_builds_inline_agent_from_md(self):
        config = json.loads(runner.build_config_content(runner.REPO_ROOT / "opencode" / "agents" / "hunt-auto.md"))
        self.assertEqual(config["default_agent"], "hunt-auto")
        agent = config["agent"]["hunt-auto"]
        self.assertEqual(agent["permission"]["bash"]["*"], "deny")
        self.assertEqual(agent["permission"]["bash"]["hx *"], "allow")
        self.assertIn("headless", agent["prompt"])
        self.assertNotIn("---", agent["prompt"][:3])


class RunnerMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def tearDown(self):
        runner.invoke_opencode = getattr(self, "orig_invoke", runner.invoke_opencode)
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_pid_removed_even_when_loop_raises(self):
        self.orig_invoke = runner.invoke_opencode
        def boom(*a, **kw):
            raise RuntimeError("boom")
        runner.invoke_opencode = boom
        with self.assertRaises(RuntimeError):
            runner.runner_main(["--engagement", str(self.eng), "--slices", "1"])
        self.assertFalse((self.repo.hunt / "runner.pid").exists())
```

E no `RetryTest`: caso de exaustão — três falhas seguidas → `sleeps == [5, 10]` (sem o 20 final).

- [ ] **Step 2: RED** — `python3 tests/test_runner.py -v`.

- [ ] **Step 3: Implement**

```python
def build_config_content(agent_md_path):
    text = Path(agent_md_path).read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text
    else:
        body = text.strip()
    config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            "hunt-auto": {
                "description": "headless hunt worker (runner)",
                "mode": "primary",
                "permission": {"bash": {"*": "deny", "hx *": "allow"}, "edit": "allow"},
                "prompt": body,
            }
        },
        "default_agent": "hunt-auto",
    }
    return json.dumps(config)
```

- [ ] **Step 4: GREEN** — ambos os suites.

- [ ] **Step 5: Commit** — `git commit -m "feat(runner): inline hunt-auto injection; pid safety; sleep only between attempts"`

---

### Task 5: synthetic smoke + docs + gitignore dos artefatos do runner (scoped addition da review do T2)

**Files:** Modify `README.md`, `templates/HUNT.md`, `bin/hx` (linha do .gitignore gerado), `tests/test_hx.py`; append em `/home/ngix/rei-bbp/hunt/.gitignore`; record outputs.

- [ ] **Step 1: gitignore** — em `cmd_init` (bin/hx), o `.gitignore` gerado ganha `runner.pid`, `runner.stop`, `runs.jsonl` (append na string existente). Teste em `InitTest`: após `hx init`, o `.gitignore` contém `runner.pid`. E append das mesmas linhas no arquivo vivo `/home/ngix/rei-bbp/hunt/.gitignore` (não é repo — edição direta).

- [ ] **Step 2: smoke sintético (local, real `opencode run` 1 fatia, tiny budget)**

```bash
# engagement sintético com fixture refletidor local (do smoke da 1.5)
mkdir -p /tmp/opencode/smoke-runner && cd /tmp/opencode/smoke-runner
python3 -c "
import http.server, urllib.parse
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('q', [''])[0]
        body = f'<div>{q}</div>'.encode()
        self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 8911), H).serve_forever()" &
SRV=$!
/home/ngix/huntbench/bin/hx init --dir . >/dev/null
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("hunt/scope.json"); s = json.loads(p.read_text()); s["in_scope"] = ["127.0.0.1"]; p.write_text(json.dumps(s))
EOF
/home/ngix/huntbench/bin/hx hypothesis add --claim "XSS refletido no param q do fixture local" --endpoint "GET http://127.0.0.1:8911/?q={payload}" --class XSS --confirm "reflexao crua" --refute "escapado ou ausente"
python3 /home/ngix/huntbench/runner/runner.py --engagement . --slices 1 --wall 900
kill $SRV
# esperado: runs.jsonl com 1 record (session != null), hipótese fechada (result/blocked),
# adjudicação registrada; audit/hx logs coerentes.
```

- [ ] **Step 3: docs** — README: seção curta "Runner (opt-in)" (1 worker, budgets, adjudicação via verify, sem symlink); `templates/HUNT.md`: bloco "Runner" (comando, paradas, kill: `touch hunt/runner.stop`).

- [ ] **Step 4: Commit** — `git commit -m "docs: runner v1 (opt-in) + smoke results + gitignore"`

---

**Done = phase 2 v1:** runner executa fila sequencial com budgets/kill/health, adjudica via `hx verify`, injeta `hunt-auto` inline; smoke sintético registrado; REI (ou qualquer alvo vivo) só com aprovação explícita do dj.
