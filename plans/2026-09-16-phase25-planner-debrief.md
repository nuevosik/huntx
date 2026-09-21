# Hunt Harness — Phase 2.5 Implementation Plan (planner + debrief)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the worldview→queue loop: `hx debrief` (deterministic post-wave summary), `hx hypothesis drop` + `source` (queue pruning/origin), and `runner.py --plan` (one headless planner session that proposes new hypotheses from coverage gaps).

**Architecture:** Debrief is code (facts from `runs.jsonl`/verdicts/coverage — no model). The planner is a single ephemeral headless session invoked by the runner, which reads a bounded digest and ADDS hypotheses itself via `hx hypothesis add --source planner`; the runner records the run (`mode: plan`) and counts proposals. No target requests in plan mode. The planner suggests; `hx verify` and the dj decide.

**Tech Stack:** Python 3.14 stdlib. Suites: `python3 tests/test_hx.py`, `python3 tests/test_runner.py` (both local-only; tests are untracked).

**Spec:** `specs/2026-09-15-hunt-harness-design.md` (Rev 8 — §4.2 source/dropped, §5 debrief/drop, §9 planner).

## Global Constraints

- NO comments and NO docstrings anywhere in code (strict). Portuguese prose in user messages/docs.
- Debrief is **deterministic** (no model). Planner is **one** session, never runs slices or target requests, cap via `--plan-n` (default 5), dedup instructed.
- `dropped` = outside the queue; `result` only becomes `dropped` with `--force`.
- Planner proposals carry `source: "planner"`; `hx next`/brief show `[planner]`.
- Commits per task touch `bin/hx`, `runner/runner.py`, `tests/*` (local), `README.md`, `templates/HUNT.md`.

---

### Task 1: `hx` — `source`, `drop`, `[planner]` marker

**Files:** Modify `bin/hx`; Modify `tests/test_hx.py`.

**Interfaces:**
- `cmd_hypothesis_add`: new `--source` flag → stored as `hyp["source"]` (default `None`).
- `cmd_hypothesis_drop(args)`: status → `"dropped"`, `owner=None`; refuses when `status == "result"` and no `--force` (`HxError`, `EXIT_GUARD`); unknown id → `EXIT_GUARD`.
- `cmd_next`/`cmd_brief`: print `[planner]` after the id when `source == "planner"`.

- [ ] **Step 1: Write the failing tests**

```python
class SourceDropTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(Path(self.tmp.name))

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def add(self, source=None):
        argv = ["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y"]
        if source:
            argv += ["--source", source]
        return hx.main(argv)

    def test_source_recorded_and_marker_printed(self):
        self.add("planner")
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["source"], "planner")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            hx.main(["next", "--peek"])
        self.assertIn("[planner]", buffer.getvalue())

    def test_drop_open_removes_from_queue(self):
        self.add()
        self.assertEqual(hx.main(["hypothesis", "drop", "h001"]), 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["status"], "dropped")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            hx.main(["next", "--peek"])
        self.assertNotIn("h001", buffer.getvalue())

    def test_drop_result_requires_force(self):
        self.add()
        hx.main(["next"])
        hx.main(["result", "h001", "--verdict", "refuted", "--note", "n"])
        self.assertEqual(hx.main(["hypothesis", "drop", "h001"]), hx.EXIT_GUARD)
        self.assertEqual(hx.main(["hypothesis", "drop", "h001", "--force"]), 0)
```

(imports: `io`, `contextlib` — add to the test file's imports.)

- [ ] **Step 2: RED** — `python3 tests/test_hx.py SourceDropTest -v`.

- [ ] **Step 3: Implement** (código real):

```python
def cmd_hypothesis_drop(args):
    repo = repo_from_cwd()
    with flock(repo.hunt / ".lock.hyps"):
        hyps, _ = load_hyps(repo)
        for hyp in hyps:
            if hyp["id"] != args.id:
                continue
            if hyp.get("status") == "result" and not args.force:
                raise HxError("hipotese fechada (result) — drop exige --force", EXIT_GUARD)
            hyp["status"] = "dropped"
            hyp["owner"] = None
            save_hyps(repo, hyps)
            print(f"{hyp['id']} -> dropped")
            return
    raise HxError(f"hipotese nao encontrada: {args.id}", EXIT_GUARD)
```

`cmd_hypothesis_add`: dict gains `"source": args.source,` (após `"class"`). Parser: `add_p.add_argument("--source")`; `drop_p = hyp_sub.add_parser("drop")` com `id` + `--force`; dispatch branch.

`cmd_next` e `cmd_brief`: montar `label = f"[{hyp['id']}]" + (" [planner]" if hyp.get("source") == "planner" else "")` e usar no print.

- [ ] **Step 4: GREEN** — `python3 tests/test_hx.py`.

- [ ] **Step 5: Commit** — `git add bin/hx tests/test_hx.py && git commit -m "feat(hx): hypothesis source + drop + planner marker"`

---

### Task 2: `hx debrief` — passe determinístico

**Files:** Modify `bin/hx`; Modify `tests/test_hx.py`.

**Interfaces:**
- `cmd_debrief(args)`: cutoff = mtime do `sessions/*.md` mais novo (0 se não há); lê `runs.jsonl` (ausente → `[]`; linha inválida → ignora) e mantém records com `ts > cutoff`; hypes com `closed_ts > cutoff`; counts de coverage (fatorar o parser do `cmd_brief` em `coverage_counts(repo)` reusado pelos dois); escreve `sessions/YYYY-MM-DD-NN.md` (NN = arquivos de hoje + 1, 2 dígitos); imprime o path relativo.
- Seções (PT): `# Debrief — <data>` · `## Wave` (runs/tokens/custo/por-hipótese) · `## Vereditos` · `## Coverage` · `## Fila` (abertas, `[planner]` em N, dropped) · `## Observações` (stderr curtos, reconciliados).

- [ ] **Step 1: Write the failing tests**

```python
class DebriefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = str(int(time.time()) + 60)
        self.repo = hx.Repo(Path(self.tmp.name))
        self.add = lambda: hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_debrief_writes_file_with_totals(self):
        self.add()
        hx.main(["next"])
        hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        record = {"ts": hx.now(), "hyp": "h001", "session": "ses_x", "rc": 0, "tokens": 1234, "cost": 0.05}
        (self.repo.hunt / "runs.jsonl").write_text(json.dumps(record) + "\n")
        rc = hx.main(["debrief"])
        self.assertEqual(rc, 0)
        files = sorted((self.repo.hunt / "sessions").glob("*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        for needle in ("# Debrief", "runs: 1", "tokens: 1234", "h001", "refuted", "## Coverage", "## Fila"):
            self.assertIn(needle, text)

    def test_second_debrief_is_numbered_and_cuts_off(self):
        self.add()
        hx.main(["debrief"])
        (self.repo.hunt / "runs.jsonl").write_text(json.dumps({"ts": hx.now(), "hyp": "h001", "rc": 0}) + "\n")
        hx.main(["debrief"])
        files = sorted((self.repo.hunt / "sessions").glob("*.md"))
        self.assertEqual(len(files), 2)
        self.assertIn("-02.md", files[1].name)
```

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement** — `cmd_debrief` conforme interfaces; fatorar `coverage_counts(repo) -> dict` extraído do `cmd_brief` (o brief passa a chamá-lo). Parser `debrief` sem flags + dispatch.

- [ ] **Step 4: GREEN** — ambos os testes; e `python3 tests/test_hx.py` completo.

- [ ] **Step 5: Commit** — `git commit -m "feat(hx): deterministic debrief"`

---

### Task 3: `runner.py --plan` — sessão de planejamento

**Files:** Modify `runner/runner.py`; Modify `tests/test_runner.py`.

**Interfaces:**
- `build_plan_digest(repo, plan_n) -> str`: coverage (bounded ~3KB) + TARGET.md (60 linhas) + rabo do último `sessions/*.md` (40 linhas) + fila aberta (`- [id] [planner]? claim (class endpoint)`) + refutadas (endpoint+class das linhas refuted do coverage) + `plan_n`.
- `PLAN_TEMPLATE` (PT): papel de planejador; **proibido** rodar `hx run`/requests; cap exato (`plan_n`); dedup explícita contra a lista dada; cada proposta vira um comando `hx hypothesis add ... --source planner`; fechar com resumo.
- `run_plan(ctx) -> dict`: conta `source == "planner"` antes/depois; `retry_invoke(ctx, prompt)`; record `{"ts", "mode": "plan", "session", "rc", "tokens", "cost", "proposed": delta}` → `record_run`; print `planner: +N propostas (session ..., tokens ...)`; retorna o record.
- `runner_main`: flags `--plan` (bool) + `--plan-n` (int, default 5); com `--plan`, monta ctx (config/owner/budget já existentes), roda `run_plan` e **encerra sem slices**.

- [ ] **Step 1: Write the failing tests**

```python
class PlanModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "BOLA x", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        self.orig_invoke = runner.invoke_opencode

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_digest_contains_queue_and_caps(self):
        digest = runner.build_plan_digest(self.repo, 3)
        self.assertIn("h001", digest)
        self.assertIn("3", digest)

    def test_run_plan_counts_proposals(self):
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            runner.hx.main(["hypothesis", "add", "--claim", "nova", "--endpoint", "GET /b", "--class", "IDOR", "--confirm", "c", "--refute", "r", "--source", "planner"])
            return (0, '{"sessionID":"ses_plan"}', "")
        runner.invoke_opencode = fake
        ctx = {"repo": self.repo, "config": "{}", "model": None, "slice_timeout_s": 900, "owner": "runner-test", "budget": dict(runner.DEFAULTS), "started_ts": 1000}
        record = runner.run_plan(ctx)
        self.assertEqual(record["mode"], "plan")
        self.assertEqual(record["proposed"], 1)
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(lines[-1])["proposed"], 1)
```

- [ ] **Step 2: RED.**

- [ ] **Step 3: Implement** conforme interfaces (reusar `retry_invoke`, `export_usage`, `record_run`; o prompt é montado com `PLAN_TEMPLATE.format(digest=..., n=plan_n)`).

- [ ] **Step 4: GREEN** — ambos os suites.

- [ ] **Step 5: Commit** — `git commit -m "feat(runner): plan mode (planner session)"`

---

### Task 4: docs + smoke

**Files:** Modify `README.md`, `templates/HUNT.md`; record outputs.

- [ ] **Step 1: docs** — README: planner (`runner.py --plan`, sugestões marcadas, drop) e debrief (`hx debrief`) em 3-4 linhas cada; `templates/HUNT.md`: bloco "Planner e debrief" (comandos, cap, `[planner]`, drop, debrief determinístico).

- [ ] **Step 2: smokes (locais)**

```bash
# (a) debrief sobre o engagement do smoke da fase 2
cd /tmp/opencode/smoke-runner-run5 && /home/ngix/huntbench/bin/hx debrief && ls hunt/sessions/

# (b) planner real (sessão headless, cap 3) num engagement sintético com fila
mkdir -p /tmp/opencode/smoke-plan && cd /tmp/opencode/smoke-plan
/home/ngix/huntbench/bin/hx init --dir . >/dev/null
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("hunt/scope.json"); s = json.loads(p.read_text()); s["in_scope"] = ["127.0.0.1"]; p.write_text(json.dumps(s))
EOF
/home/ngix/huntbench/bin/hx hypothesis add --claim "XSS refletido no param q" --endpoint "GET http://127.0.0.1:8911/?q={payload}" --class XSS --confirm "reflexao crua" --refute "escapado"
python3 /home/ngix/huntbench/runner/runner.py --engagement . --plan --plan-n 3

# (c) poda
/home/ngix/huntbench/bin/hx next --peek   # propostas com [planner]
/home/ngix/huntbench/bin/hx hypothesis drop <id-da-primeira-proposta>   # se houver
```

Expected (registrar verbatim): (a) arquivo novo em `sessions/` com os totais do smoke; (b) record `mode: plan` em `runs.jsonl` + propostas novas `[planner]` (ou `+0` honesto); (c) proposta some da fila.

- [ ] **Step 3: Commit** — `git commit -m "docs: planner + debrief (2.5) + smoke"`

---

**Done = phase 2.5:** debrief determinístico, `source`/`drop` na fila, `--plan` registrando propostas; smokes locais registrados; testes seguem locais.
