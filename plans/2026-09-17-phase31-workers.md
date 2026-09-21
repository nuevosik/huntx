# Fase 3.1 — Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `runner.py --workers N` — supervisor + N processos worker presos a sessões distintas, gate mecânico no start, orçamento agregado e claim sem duplo trabalho.

**Architecture:** O supervisor (processo atual) valida o gate, inicializa estado compartilhado e forka N workers (`multiprocessing.get_context("fork")`, Linux; Python 3.14 usa forkserver por default — fork é explícito para herdar env/estado e funcionar nos testes). Cada worker roda o loop de fatia existente com `HX_SESSION` = tag da sessão e claim filtrado por `session_tag`. Orçamento agregado em `hunt/.runnerstate.json` sob `.lock.runner`; `runner.stop` segue global. `update_coverage` do hx passa a escrever `COVERAGE.md` sob lock próprio (dois `result` concorrentes não perdem linha).

**Tech Stack:** Python 3 stdlib (`multiprocessing`, `fcntl`), `curl_cffi` só no `hx`; nenhuma dependência nova.

**Spec:** `specs/2026-09-15-hunt-harness-design.md` §9.2 (Rev 9) e design travado em `plans/2026-09-16-phase3-campo-paralelismo.md` §3.1.

## Global Constraints

- `bin/hx` continua arquivo único, stdlib-only, **zero comentários/docstrings**, mensagens de usuário em PT (identificadores em inglês). `runner/runner.py` idem (sem comentários).
- Testes são locais: `tests/` está no `.gitignore` — **nunca** staged/committed. Commits tocam só `bin/hx`, `runner/runner.py` e `README.md`.
- Nenhuma dependência nova; nenhum request de rede em teste.
- `flock` do hx é reentrante **por processo** (set `_LOCKS_HELD`), não por thread: testes de concorrência usam processos (`multiprocessing.get_context("fork")`), nunca threads.
- `HX_FAKE_NOW` para determinismo de tempo; `HX_ENGAGEMENT` para localizar o engagement.
- Commits em PT, um por task.

---

### Task 1: COVERAGE.md sob lock + `.runnerstate.json` no gitignore (bin/hx)

**Files:**
- Modify: `bin/hx` (`update_coverage`; string do `.gitignore` em `cmd_init`)
- Modify: `tests/test_hx.py` (novo `CoverageLockTest`; asserção no `InitTest`)

**Interfaces:**
- Produces: `update_coverage(repo, endpoint, cls, status, evidence, note)` serializado por `hunt/.lock.coverage` (mesma assinatura, sem mudança de contrato).
- Produces: `.gitignore` do engagement passa a listar `.runnerstate.json`.

- [ ] **Step 1: Write the failing tests**

Adicionar `import multiprocessing` no topo do `tests/test_hx.py` (junto dos outros imports) e, ao final do arquivo, antes do `if __name__ == "__main__":`:

```python
class CoverageLockTest(unittest.TestCase):
    def test_concurrent_updates_keep_both_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engagement(tmp)
            repo = hx.Repo(eng)
            orig_write = hx.atomic_write

            def slow_write(path, text):
                if Path(path).name == "COVERAGE.md":
                    time.sleep(0.5)
                return orig_write(path, text)

            hx.atomic_write = slow_write
            try:
                ctx = multiprocessing.get_context("fork")
                procs = [
                    ctx.Process(target=hx.update_coverage, args=(repo, f"GET /e{i}", "BOLA", "refuted", "ev", "n"))
                    for i in range(2)
                ]
                for proc in procs:
                    proc.start()
                for proc in procs:
                    proc.join()
                self.assertEqual([proc.exitcode for proc in procs], [0, 0])
            finally:
                hx.atomic_write = orig_write
            text = (eng / "hunt" / "COVERAGE.md").read_text()
            self.assertIn("GET /e0", text)
            self.assertIn("GET /e1", text)
```

E no `InitTest.test_init_creates_tree_and_refuses_second_run`, após a asserção de `.lock.*`:

```python
            self.assertIn(".runnerstate.json", (hunt / ".gitignore").read_text())
```

- [ ] **Step 2: Run to verify RED**

Run: `python3 tests/test_hx.py -v CoverageLockTest`
Expected: FAIL — sem lock, as duas escritas leem o arquivo vazio e a última sobrescreve a primeira (uma linha some). A asserção do `.gitignore` também falha.

- [ ] **Step 3: Implement**

Em `bin/hx`, `update_coverage` (linha ~270) vira (corpo indentado dentro do lock):

```python
def update_coverage(repo, endpoint, cls, status, evidence, note):
    with flock(repo.hunt / ".lock.coverage"):
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
```

Em `cmd_init` (linha ~140), a string do `.gitignore` vira:

```python
    atomic_write(hunt / ".gitignore", "scans/\nsessions/\n.health.json\n.ratelimit.json\n.runnerstate.json\npending/\naudit.log\nrunner.pid\nrunner.stop\nruns.jsonl\n.lock.*\n")
```

- [ ] **Step 4: Run full suite**

Run: `python3 tests/test_hx.py`
Expected: OK (111+ testes, incluindo `CoverageLockTest` e `InitTest` atualizado).

- [ ] **Step 5: Commit**

```bash
git add bin/hx
git commit -m "fix(hx): COVERAGE.md sob lock; runnerstate no gitignore do init"
```

---

### Task 2: Estado compartilhado do runner (runner/runner.py)

**Files:**
- Modify: `runner/runner.py` (novas funções de estado logo após `hx = load_hx()`; `check_stop` reescrito)
- Modify: `tests/test_runner.py` (novo `RunnerStateTest`; `StopGateTest.test_stop_reasons` e `test_token_and_cost_caps` atualizados)

**Interfaces:**
- Produces: `RUNNER_STATE_DEFAULTS = {"started_ts": 0, "slices": 0, "tokens": 0, "cost": 0.0}`; `runner_state_path(repo) -> Path`; `runner_state_init(repo) -> None` (grava `started_ts=hx.now()`); `runner_state_read(repo) -> dict`; `runner_state_add(repo, tokens=0, cost=0.0) -> dict`; `runner_state_reserve_slice(repo, slices_max) -> bool`; `runner_state_release_slice(repo) -> None`.
- Produces: `check_stop(ctx)` passa a ler o estado compartilhado; o teto de slices sai do `check_stop` e vira reserva atômica (`runner_state_reserve_slice`).

- [ ] **Step 1: Write the failing tests**

Ao final do `tests/test_runner.py`, nova classe:

```python
class RunnerStateTest(unittest.TestCase):
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

    def test_init_sets_started_ts_and_defaults(self):
        runner.runner_state_init(self.repo)
        state = runner.runner_state_read(self.repo)
        self.assertEqual(state["started_ts"], 1000)
        self.assertEqual(state["slices"], 0)
        self.assertEqual(state["tokens"], 0)
        self.assertEqual(state["cost"], 0.0)

    def test_add_accumulates_tokens_and_cost(self):
        runner.runner_state_init(self.repo)
        runner.runner_state_add(self.repo, tokens=10, cost=0.5)
        runner.runner_state_add(self.repo, tokens=5)
        state = runner.runner_state_read(self.repo)
        self.assertEqual(state["tokens"], 15)
        self.assertEqual(state["cost"], 0.5)

    def test_reserve_caps_and_release(self):
        runner.runner_state_init(self.repo)
        self.assertTrue(runner.runner_state_reserve_slice(self.repo, 2))
        self.assertTrue(runner.runner_state_reserve_slice(self.repo, 2))
        self.assertFalse(runner.runner_state_reserve_slice(self.repo, 2))
        runner.runner_state_release_slice(self.repo)
        self.assertTrue(runner.runner_state_reserve_slice(self.repo, 2))
        self.assertEqual(runner.runner_state_read(self.repo)["slices"], 2)

    def test_read_without_file_returns_defaults(self):
        state = runner.runner_state_read(self.repo)
        self.assertEqual(state, runner.RUNNER_STATE_DEFAULTS)
```

Atualizar `StopGateTest.test_stop_reasons` (o caso `slices` sai do check_stop e vira reserva) e `test_token_and_cost_caps`:

```python
    def test_stop_reasons(self):
        runner.runner_state_init(self.repo)
        runner.request_stop(self.repo)
        self.assertEqual(runner.check_stop(self.ctx()), "kill")
        (self.repo.hunt / "runner.stop").unlink()
        self.assertEqual(runner.check_stop(self.ctx(**{"budget": {**runner.DEFAULTS, "wall_s": 0}})), "wall")

    def test_token_and_cost_caps(self):
        runner.runner_state_init(self.repo)
        runner.runner_state_add(self.repo, tokens=10, cost=0.6)
        self.assertEqual(runner.check_stop(self.ctx(budget={**runner.DEFAULTS, "token_cap": 10})), "tokens")
        self.assertEqual(runner.check_stop(self.ctx(budget={**runner.DEFAULTS, "cost_cap": 0.5})), "cost")
```

- [ ] **Step 2: Run to verify RED**

Run: `python3 tests/test_runner.py -v RunnerStateTest`
Expected: FAIL com `AttributeError: module 'runner_mod' has no attribute 'runner_state_init'`.

- [ ] **Step 3: Implement**

Em `runner/runner.py`, após `hx = load_hx()`:

```python
RUNNER_STATE_DEFAULTS = {"started_ts": 0, "slices": 0, "tokens": 0, "cost": 0.0}


def runner_state_path(repo):
    return repo.hunt / ".runnerstate.json"


def runner_state_read(repo):
    return {**RUNNER_STATE_DEFAULTS, **hx.load_json(runner_state_path(repo), {})}


def runner_state_init(repo):
    with hx.flock(repo.hunt / ".lock.runner"):
        hx.dump_json(runner_state_path(repo), {**RUNNER_STATE_DEFAULTS, "started_ts": hx.now()})


def runner_state_add(repo, tokens=0, cost=0.0):
    with hx.flock(repo.hunt / ".lock.runner"):
        state = runner_state_read(repo)
        state["tokens"] += tokens or 0
        state["cost"] += cost or 0.0
        hx.dump_json(runner_state_path(repo), state)
        return state


def runner_state_reserve_slice(repo, slices_max):
    with hx.flock(repo.hunt / ".lock.runner"):
        state = runner_state_read(repo)
        if state["slices"] >= slices_max:
            return False
        state["slices"] += 1
        hx.dump_json(runner_state_path(repo), state)
        return True


def runner_state_release_slice(repo):
    with hx.flock(repo.hunt / ".lock.runner"):
        state = runner_state_read(repo)
        state["slices"] = max(0, state["slices"] - 1)
        hx.dump_json(runner_state_path(repo), state)
```

E `check_stop` vira:

```python
def check_stop(ctx):
    repo = ctx["repo"]
    budget = ctx["budget"]
    if (repo.hunt / "runner.stop").exists():
        return "kill"
    state = runner_state_read(repo)
    if hx.now() - state["started_ts"] >= budget["wall_s"]:
        return "wall"
    if budget.get("token_cap") and state["tokens"] >= budget["token_cap"]:
        return "tokens"
    if budget.get("cost_cap") and state["cost"] >= budget["cost_cap"]:
        return "cost"
    if claim_peek_empty(repo):
        return "empty_queue"
    return None
```

- [ ] **Step 4: Run full suite**

Run: `python3 tests/test_runner.py`
Expected: OK — `RunnerStateTest` verde, `StopGateTest` atualizado verde, demais 52+ verdes.

- [ ] **Step 5: Commit**

```bash
git add runner/runner.py
git commit -m "feat(runner): estado compartilhado de orcamento (runnerstate sob flock)"
```

---

### Task 3: Claim filtrado por sessão (runner/runner.py)

**Files:**
- Modify: `runner/runner.py` (`claim_next` ganha `session=None`)
- Modify: `tests/test_runner.py` (novo `ClaimSessionTest`)

**Interfaces:**
- Produces: `claim_next(repo, owner, session=None) -> dict | None` — com `session` dado, só claima `open` com `session_tag in (None, session)`; sem `session`, comportamento atual (primeira `open`).
- Consumes: `hx.flock`, `hx.load_hyps`, `hx.save_hyps` (existentes).

- [ ] **Step 1: Write the failing test**

Ao final do `tests/test_runner.py`:

```python
class ClaimSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {
            "probes": [
                {"session": "a", "file": "recon/session_a.json", "method": "GET", "url": "https://t.local/a"},
                {"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://t.local/b"},
            ],
            "fresh_max_s": 600,
            "session_invalid_if": [],
            "waf_block_if": [],
            "rate_limited_if": [],
        }
        scope_path.write_text(json.dumps(scope))
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "a", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "a"])
        runner.hx.main(["hypothesis", "add", "--claim", "b", "--endpoint", "GET /b", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "b"])
        runner.hx.main(["hypothesis", "add", "--claim", "livre", "--endpoint", "GET /c", "--class", "IDOR", "--confirm", "c", "--refute", "r"])

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_claim_filters_by_session(self):
        hyp = runner.claim_next(self.repo, "wa", "a")
        self.assertEqual((hyp["id"], hyp["session_tag"]), ("h001", "a"))
        hyp = runner.claim_next(self.repo, "wa", "a")
        self.assertEqual(hyp["id"], "h003")
        self.assertIsNone(hyp["session_tag"])
        hyp = runner.claim_next(self.repo, "wb", "b")
        self.assertEqual((hyp["id"], hyp["session_tag"]), ("h002", "b"))
        self.assertIsNone(runner.claim_next(self.repo, "wc", "c"))

    def test_claim_without_session_ignores_tags(self):
        hyp = runner.claim_next(self.repo, "w")
        self.assertEqual(hyp["id"], "h001")
```

Nota: `--session-tag` exige que o probe exista — por isso o scope é editado antes dos adds.

- [ ] **Step 2: Run to verify RED**

Run: `python3 tests/test_runner.py -v ClaimSessionTest`
Expected: FAIL — `claim_next` sem filtro devolve `h002` (tag b) para o worker da sessão `a`.

- [ ] **Step 3: Implement**

`claim_next` vira:

```python
def claim_next(repo, owner, session=None):
    with hx.flock(repo.hunt / ".lock.hyps"):
        hyps, _ = hx.load_hyps(repo)
        for hyp in hyps:
            if hyp["status"] != "open":
                continue
            if session and hyp.get("session_tag") not in (None, session):
                continue
            hyp["status"] = "claimed"
            hyp["owner"] = owner
            hyp["claimed_ts"] = hx.now()
            hx.save_hyps(repo, hyps)
            return hyp
    return None
```

- [ ] **Step 4: Run full suite**

Run: `python3 tests/test_runner.py`
Expected: OK — `ClaimSessionTest` verde e `SliceTest.test_claim_next_marks_owner` (chamada sem `session`) continua verde.

- [ ] **Step 5: Commit**

```bash
git add runner/runner.py
git commit -m "feat(runner): claim filtrado por sessao do worker"
```

---

### Task 4: Gate mecânico de workers (runner/runner.py)

**Files:**
- Modify: `runner/runner.py` (`workers_gate`; `--workers` no argparse; wiring no `runner_main`)
- Modify: `tests/test_runner.py` (novo `WorkersGateTest`)

**Interfaces:**
- Produces: `workers_gate(repo, workers) -> list[str]` — vazio se ok; lista de problemas caso contrário. `workers <= 1` → sempre `[]`.
- Produces: `runner_main` aceita `--workers N`, recusa `--plan` com `N>1` (exit 2) e recusa gate (exit 2) antes de tocar `runner.stop`/`runner.pid`.

- [ ] **Step 1: Write the failing tests**

Ao final do `tests/test_runner.py`:

```python
class WorkersGateTest(unittest.TestCase):
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

    def set_scope(self, hosts, probes):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = hosts
        scope["health"] = {
            "probes": [{"session": tag, "file": f"recon/session_{tag}.json", "method": "GET", "url": f"https://{host}/x"} for tag, host in zip(probes, hosts)],
            "fresh_max_s": 600,
            "session_invalid_if": [],
            "waf_block_if": [],
            "rate_limited_if": [],
        }
        scope_path.write_text(json.dumps(scope))

    def test_workers_1_always_ok(self):
        self.assertEqual(runner.workers_gate(self.repo, 1), [])

    def test_gate_lists_all_problems(self):
        problems = runner.workers_gate(self.repo, 3)
        self.assertEqual(len(problems), 3)
        joined = " ".join(problems)
        self.assertIn("sessoes", joined)
        self.assertIn("20", joined)
        self.assertIn("hosts", joined)

    def test_gate_ok_when_criteria_met(self):
        self.set_scope(["t1.local", "t2.local"], ["a", "b"])
        for i in range(10):
            runner.hx.main(["hypothesis", "add", "--claim", f"c{i}", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "a"])
            runner.hx.main(["hypothesis", "add", "--claim", f"c{i}", "--endpoint", "GET /b", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "b"])
        self.assertEqual(runner.workers_gate(self.repo, 2), [])

    def test_runner_main_refuses_gate(self):
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--workers", "2"]), 2)
        self.assertFalse((self.repo.hunt / "runner.pid").exists())

    def test_runner_main_refuses_plan_with_workers(self):
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--plan", "--workers", "2"]), 2)
```

- [ ] **Step 2: Run to verify RED**

Run: `python3 tests/test_runner.py -v WorkersGateTest`
Expected: FAIL — `AttributeError: module 'runner_mod' has no attribute 'workers_gate'` e `--workers` não reconhecido pelo argparse.

- [ ] **Step 3: Implement**

Em `runner/runner.py`, antes de `runner_main`:

```python
def workers_gate(repo, workers):
    if workers <= 1:
        return []
    scope = repo.scope()
    probes = (scope.get("health") or {}).get("probes") or []
    hyps, _ = hx.load_hyps(repo)
    open_count = sum(1 for hyp in hyps if hyp.get("status") == "open")
    host_count = len(scope.get("in_scope") or [])
    problems = []
    if workers > len(probes):
        problems.append(f"--workers {workers} > sessoes nos probes ({len(probes)})")
    if open_count < 20:
        problems.append(f"hipoteses abertas {open_count} < 20")
    if host_count < 2:
        problems.append(f"hosts in-scope {host_count} < 2")
    return problems
```

No `runner_main`: adicionar `parser.add_argument("--workers", type=int, default=1)` logo após a linha do `--plan-n`, e inserir o bloco abaixo logo depois do `if other_pid is not None: ... return 1` do check de runner vivo (antes de `(repo.hunt / "runner.stop").unlink(missing_ok=True)`):

```python
    if args.plan and args.workers > 1:
        print("runner: --plan nao combina com --workers > 1")
        return 2
    problems = workers_gate(repo, args.workers)
    if problems:
        print("runner: gate de workers recusou:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
```

- [ ] **Step 4: Run full suite**

Run: `python3 tests/test_runner.py`
Expected: OK — `WorkersGateTest` verde, `RunnerMainTest` inalterado.

- [ ] **Step 5: Commit**

```bash
git add runner/runner.py
git commit -m "feat(runner): gate mecanico de workers"
```

---

### Task 5: worker_loop + supervisor + campo worker (runner/runner.py)

**Files:**
- Modify: `runner/runner.py` (`import multiprocessing`; `worker_loop`, `worker_entry`, `run_workers`; `run_slice` grava `worker`; `runner_main` usa `worker_loop`/`run_workers` e inicializa estado)
- Modify: `tests/test_runner.py` (novo `test_run_slice_records_worker` no `SliceTest`; novo `WorkersRunE2ETest`)

**Interfaces:**
- Produces: `worker_loop(ctx) -> str` (motivo da parada: `kill|wall|tokens|cost|empty_queue|slices`); `worker_entry(queue, ctx)` (põe o motivo na queue); `run_workers(ctx, workers) -> list[str]` (supervisor fork; um motivo por worker).
- Produces: `runs.jsonl` ganha o campo `worker` (tag da sessão ou `null` no modo single).
- Consumes: `runner_state_*` (Task 2), `claim_next(repo, owner, session)` (Task 3), `workers_gate` (Task 4).

- [ ] **Step 1: Write the failing tests**

No `SliceTest`, adicionar:

```python
    def test_run_slice_records_worker(self):
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None, "worker": "a"})
        self.assertEqual(record["worker"], "a")
```

Ao final do `tests/test_runner.py`, o e2e sintético (2 workers, sem LLM, sem rede):

```python
class WorkersRunE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["t1.local", "t2.local"]
        scope["health"] = {
            "probes": [
                {"session": "a", "file": "recon/session_a.json", "method": "GET", "url": "https://t1.local/x"},
                {"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://t2.local/x"},
            ],
            "fresh_max_s": 600,
            "session_invalid_if": [],
            "waf_block_if": [],
            "rate_limited_if": [],
        }
        scope_path.write_text(json.dumps(scope))
        self.repo = runner.hx.Repo(self.eng)
        for i in range(10):
            runner.hx.main(["hypothesis", "add", "--claim", f"a{i}", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "a"])
            runner.hx.main(["hypothesis", "add", "--claim", f"b{i}", "--endpoint", "GET /b", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--session-tag", "b"])
        runner.hx.health_probe_result(self.repo, "a", True, None, 200, "ok")
        runner.hx.health_probe_result(self.repo, "b", True, None, 200, "ok")
        self.orig_invoke = runner.invoke_opencode
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (0, '{"type":"text"}', "")

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_two_workers_drain_queue_without_overlap(self):
        rc = runner.runner_main(["--engagement", str(self.eng), "--workers", "2", "--slices", "20"])
        self.assertEqual(rc, 0)
        state = runner.runner_state_read(self.repo)
        self.assertEqual(state["slices"], 20)
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        rows = [json.loads(line) for line in lines]
        self.assertEqual(len(rows), 20)
        self.assertEqual({row["worker"] for row in rows}, {"a", "b"})
        hyps, _ = runner.hx.load_hyps(self.repo)
        by_id = {hyp["id"]: hyp for hyp in hyps}
        closed = []
        for row in rows:
            hyp = by_id[row["hyp"]]
            self.assertEqual(hyp["session_tag"], row["worker"])
            self.assertEqual(hyp["status"], "result")
            closed.append(row["hyp"])
        self.assertEqual(len(set(closed)), 20)
```

Nota: os `health_probe_result` deixam o probe fresco (ts 1000, `fresh_max_s` 600), então o `health_gate` não chama `fetch` — nenhum request de rede no teste.

- [ ] **Step 2: Run to verify RED**

Run: `python3 tests/test_runner.py -v WorkersRunE2ETest`
Expected: FAIL — sem `run_workers`/`worker_loop`, o `runner_main` não roda workers (ou `AttributeError`), e `record["worker"]` não existe.

- [ ] **Step 3: Implement**

Em `runner/runner.py`, adicionar `import multiprocessing` no topo (junto de `import json`).

Em `run_slice`, o dict `record` ganha o campo `worker` logo após `"session": session,`:

```python
    record = {
        "ts": hx.now(),
        "hyp": hyp["id"],
        "session": session,
        "worker": ctx.get("worker"),
        "rc": rc,
        "tokens": usage.get("tokens"),
        "cost": usage.get("cost"),
        "stderr": (stderr or "")[:400],
    }
```

Antes de `runner_main`, adicionar:

```python
def release_claim(repo, hyp_id):
    with hx.flock(repo.hunt / ".lock.hyps"):
        hyps, _ = hx.load_hyps(repo)
        for hyp in hyps:
            if hyp["id"] == hyp_id and hyp["status"] in ("claimed", "running"):
                hyp["status"] = "open"
                hyp["owner"] = None
                hyp["claimed_ts"] = None
                hx.save_hyps(repo, hyps)
                return True
    return False


def worker_loop(ctx):
    repo = ctx["repo"]
    budget = ctx["budget"]
    worker = ctx.get("worker")
    owner = ctx.get("owner") or f"runner-{os.getpid()}"
    os.environ["HX_SESSION"] = owner
    while True:
        reason = check_stop(ctx)
        if reason:
            return reason
        hyp = claim_next(repo, owner, worker)
        if hyp is None:
            return "empty_queue"
        if not runner_state_reserve_slice(repo, budget["slices_max"]):
            release_claim(repo, hyp["id"])
            return "slices"
        alive, tag = health_gate(hx, repo, hyp)
        if not alive:
            hx.main(["result", hyp["id"], "--verdict", "blocked", "--note", f"sessao {tag} morta"])
            continue
        ctx["hyp"] = hyp
        record = run_slice(ctx)
        runner_state_add(repo, tokens=record.get("tokens") or 0, cost=record.get("cost") or 0)


def worker_entry(queue, ctx):
    queue.put(worker_loop(ctx))


def run_workers(ctx, workers):
    repo = ctx["repo"]
    probes = (repo.scope().get("health") or {}).get("probes") or []
    proc_ctx = multiprocessing.get_context("fork")
    queue = proc_ctx.Queue()
    procs = []
    for i in range(workers):
        wctx = dict(ctx)
        wctx["worker"] = probes[i]["session"]
        wctx["owner"] = probes[i]["session"]
        proc = proc_ctx.Process(target=worker_entry, args=(queue, wctx))
        proc.start()
        procs.append(proc)
    for proc in procs:
        proc.join()
    reasons = []
    for _ in procs:
        try:
            reasons.append(queue.get(timeout=1))
        except Exception:
            reasons.append("crashed")
    return reasons
```

No `runner_main`, dentro do `try`, substituir o bloco do loop:

```python
        if args.plan:
            run_plan(ctx)
            return 0
        runner_state_init(repo)
        if args.workers <= 1:
            print(f"runner: parada ({worker_loop(ctx)})")
        else:
            for reason in run_workers(ctx, args.workers):
                print(f"runner: worker parada ({reason})")
```

e no dict `ctx` remover `"slices_done": 0, "tokens_used": 0, "cost_used": 0` (o `started_ts` fica, usado pelo `retry_invoke`).

- [ ] **Step 4: Run full suite**

Run: `python3 tests/test_runner.py`
Expected: OK — `WorkersRunE2ETest` verde (20 slices exatas, 2 workers, sem overlap), `SliceTest` verde, `RunnerMainTest`/`PlanModeTest` verdes.

- [ ] **Step 5: Commit**

```bash
git add runner/runner.py
git commit -m "feat(runner): workers N com sessao por worker e orcamento agregado"
```

---

### Task 6: README + aceite da fase

**Files:**
- Modify: `README.md` (seção Runner)
- Run: as três suítes

**Interfaces:**
- Consumes: tudo das Tasks 1–5.

- [ ] **Step 1: Implement (README)**

Após o parágrafo do runner (que termina em `...the latch is also cleared at the next runner start.`), adicionar:

```
Com `--workers N` (fase 3.1): supervisor + N processos worker, cada um preso a uma sessão distinta de `scope.health.probes` (`HX_SESSION` + claim filtrado por `session_tag`); orçamento agregado em `hunt/.runnerstate.json`; `runner.stop` para todos. Gate no start (exit 2): `N ≤ probes`, hipóteses `open` ≥ 20, hosts in-scope ≥ 2 — a wave de campo (3.0) é decisão do dj registrada no debrief.
```

- [ ] **Step 2: Aceite — rodar as três suítes**

```bash
python3 tests/test_hx.py
python3 tests/test_runner.py
node --test tests/plugin_guard.test.mjs
```

Expected: `test_hx` OK (111+), `test_runner` OK (60+), plugin 10/10. Registrar os números no report.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: workers N no README (fase 3.1)"
```

---

## Self-Review (preenchido pelo autor do plano)

- **Spec coverage (§9.2):** supervisor + N processos → T5; gate mecânico → T4; sessão por worker + claim filtrado → T3/T5; N=1 sem bind → T3 (`session=None`) e T5 (`worker_loop` inline); orçamento agregado + reserve exato → T2/T5; `runner.stop` global/SIGTERM → T5 (inalterado) ; `COVERAGE.md` sob lock → T1; reconciliação por worker + campo `worker` → T5 (reconciliação existente em `run_slice`, disparada por worker); teste de concorrência sintético antes de alvo vivo → T5 (`WorkersRunE2ETest`, fork + invoke stub, sem rede). Coberto.
- **Type consistency:** `claim_next(repo, owner, session=None)` definido em T3 e usado em T5; `runner_state_reserve_slice(repo, slices_max)`/`release`/`add` definidos em T2 e usados só em T5; `workers_gate(repo, workers) -> list` em T4 consumido em T4; `worker_loop(ctx) -> str`, `worker_entry(queue, ctx)`, `run_workers(ctx, workers) -> list[str]` em T5. O veredito `slices` deixa de existir no `check_stop` (T2) e passa a vir da reserva (T5) — sem dupla checagem.
- **Ordem:** T1 independente; T2 antes de T5; T3 antes de T5; T4 antes de T5 (gate); T6 fecha.
- **Segurança de concorrência:** locks por arquivo distintos (`.lock.coverage`, `.lock.runner`, `.lock.hyps`, `.lock.rate`), sem aninhamento invertido — `update_coverage` é chamado fora do `.lock.hyps` (após o `with` do `cmd_result`), e `runner_state_*` nunca é chamado com `.lock.hyps` retido.
