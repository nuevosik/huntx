# hunt-panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `hx ui --serve`, a read-only localhost panel for one engagement (queue, coverage, findings opt-in, runs/sessions).

**Architecture:** One pure render function `render_ui(repo, show_findings, interval) -> str` (fresh disk read per request, per-section degrade, never raises on bad data) plus a stdlib `http.server` handler serving only `GET /` on `127.0.0.1`. No JS, no deps, no mutations.

**Tech Stack:** Python 3 stdlib only (`http.server`, `html` — both already importable; `html` is already imported in `bin/hx`).

**Spec:** `plans/2026-09-18-hunt-panel.md`

## Global Constraints

- Read-only: render never calls `save_hyps`/`dump_json`/`atomic_write`; only `GET /` exists, everything else 404.
- Bind fixed `127.0.0.1`; no flag for `0.0.0.0`.
- Every engagement-derived string passes through `html.escape`.
- Findings render only when `show_findings=True`; never render evidence bodies or proof-marker blocks (id, verdict, note, evidence path only).
- `--interval` clamped: `max(int(v), 3)`.
- Missing/empty/corrupt input degrades the section ("sem dados" / "cobertura ilegível"), never 500.
- Only files touched: `bin/hx`, `tests/test_hx.py`.

## File Structure

- `bin/hx` (modify, ~1660 lines today): add `import http.server`; add helpers `fmt_age`, `fmt_ts`, `clamp_interval`, `parse_coverage_rows`, `ui_findings`, `ui_runs`, `ui_sessions`, `render_ui`, `_UiHandler`, `cmd_ui`; extend `main()` with the `ui` subparser + dispatch. Reuse: `Repo`, `load_hyps` (no save), `finding_verified`, `coverage_counts`, `read_runs`, `fmt_money`, `TTL_CLAIM_S`, `HxError`/`EXIT_GUARD`/`EXIT_OK`, `html`.
- `tests/test_hx.py` (modify): fixture engagement via `hx.main(["init", "--dir", tmp])` + `HX_ENGAGEMENT`/`HX_SESSION`/`HX_FAKE_NOW` env (same pattern as existing `QueueTest`).

---

### Task 1: Shell + header + queue render

**Files:**
- Modify: `bin/hx` (append helpers + `render_ui` skeleton before `def main`)
- Test: `tests/test_hx.py` (new `UiRenderTest` class)

**Interfaces:**
- Consumes: `Repo`, `load_hyps(repo) -> (list, bool)`, `TTL_CLAIM_S`, `html`, `now()`
- Produces: `fmt_age(secs: float) -> str`; `fmt_ts(ts: float) -> str`; `render_ui(repo, show_findings=False, interval=15) -> str` (full HTML page; this task fills header + queue, later tasks append sections inside the same body builder)

- [ ] **Step 1: Write the failing test**

```python
class UiRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000000"

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_header_counts_and_queue_escape(self):
        hx.main(["hypothesis", "add", "--claim", "BOLA em <script>alert(1)</script>",
                 "--endpoint", "GET /wishlist/{id}", "--class", "BOLA",
                 "--confirm", "200 com dado", "--refute", "403/404"])
        repo = hx.repo_from_cwd()
        page = hx.render_ui(repo)
        self.assertIn("open:", page)
        self.assertIn("BOLA em &lt;script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn('http-equiv="refresh"', page)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_hx.UiRenderTest -v`
Expected: FAIL with `AttributeError: module 'hx' has no attribute 'render_ui'`

- [ ] **Step 3: Write minimal implementation**

```python
def fmt_age(secs):
    secs = max(0, int(secs or 0))
    if secs < 3600:
        return f"{secs // 60}min"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def fmt_ts(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts or 0))


UI_CSS = """:root{--background:oklch(0.15 0.003 160);--foreground:oklch(0.92 0.01 160);--card:oklch(0.18 0.003 160);--card-foreground:oklch(0.92 0.01 160);--primary:oklch(0.78 0.21 148);--primary-foreground:oklch(0.16 0.02 148);--muted:oklch(0.21 0.003 160);--muted-foreground:oklch(0.62 0.01 160);--destructive:oklch(0.6 0.23 27);--warning:oklch(0.78 0.16 75);--info:oklch(0.7 0.13 235);--border:oklch(0.29 0.004 160);--radius:0.25rem}
body{margin:0;background:var(--background);color:var(--foreground);font-family:"Space Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.01em}
.wrap{max-width:56rem;margin:0 auto;padding:1.5rem}
h1{font-size:1.25rem;margin:0 0 .25rem}h2{font-size:1rem;margin:2rem 0 .5rem;color:var(--foreground)}
.sub{color:var(--muted-foreground);font-size:.8rem;margin:0 0 1rem}
.counts{display:flex;gap:.5rem;flex-wrap:wrap;margin:.75rem 0}
.pill{border:1px solid var(--border);border-radius:var(--radius);padding:.15rem .6rem;font-size:.8rem}
.pill b{color:var(--primary)}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:.6rem .8rem;margin:.4rem 0}
.muted{color:var(--muted-foreground);font-size:.8rem}
table{border-collapse:collapse;width:100%;font-size:.8rem}
th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid var(--border)}
th{color:var(--muted-foreground);font-weight:400}
.chip{display:inline-block;border:1px solid var(--border);border-radius:var(--radius);padding:0 .45rem;font-size:.75rem}
.stale{color:var(--warning)}"""


def _esc(value):
    return html.escape("" if value is None else str(value))


def _section(title, inner):
    return f"<h2>{_esc(title)}</h2>\n{inner}"


def _ui_queue(hyps, t):
    groups = {"open": [], "claimed": [], "running": [], "result": []}
    for hyp in hyps:
        if hyp.get("status") in groups:
            groups[hyp.get("status")].append(hyp)
    parts = []
    for status in ("open", "claimed", "running", "result"):
        items = groups[status]
        parts.append(f"<h2>Fila — {_esc(status)} ({len(items)})</h2>")
        if not items:
            parts.append('<p class="muted">sem dados</p>')
            continue
        for hyp in items:
            label = f"[{_esc(hyp.get('id'))}]" + (" [planner]" if hyp.get("source") == "planner" else "")
            lines = [f"<div class=\"card\"><div><b>{label}</b> {_esc(hyp.get('claim'))}</div>",
                     f"<div class=\"muted\">{_esc(hyp.get('endpoint'))} | {_esc(hyp.get('class'))}</div>"]
            if status == "open":
                lines.append(f"<div class=\"muted\">confirm: {_esc(hyp.get('confirm'))} — refute: {_esc(hyp.get('refute'))}</div>")
            if status in ("claimed", "running"):
                age = t - (hyp.get("claimed_ts") or t)
                stale = (hyp.get("claimed_ts") or 0) + TTL_CLAIM_S < t
                extra = ' <span class="stale">claim expirado</span>' if stale else ""
                lines.append(f"<div class=\"muted\">owner: {_esc(hyp.get('owner'))} — há {fmt_age(age)}{extra}</div>")
            if status == "result":
                result = hyp.get("result") or {}
                lines.append(f"<div class=\"muted\">verdict: {_esc(result.get('verdict'))} — {_esc(result.get('note'))}</div>")
            lines.append("</div>")
            parts.append("\n".join(lines))
    return "\n".join(parts)


def render_ui(repo, show_findings=False, interval=15):
    t = now()
    try:
        hyps, _ = load_hyps(repo)
    except (OSError, json.JSONDecodeError):
        hyps = []
    counts = {"open": 0, "claimed": 0, "running": 0, "result": 0, "dropped": 0}
    for hyp in hyps:
        if hyp.get("status") in counts:
            counts[hyp.get("status")] += 1
    try:
        engagement = _esc(repo.scope().get("engagement") or repo.eng.name)
    except (OSError, json.JSONDecodeError):
        engagement = _esc(repo.eng.name)
    pills = " ".join(f"<span class=\"pill\">{name}: <b>{counts[name]}</b></span>" for name in ("open", "claimed", "running", "result", "dropped"))
    body = [f"<h1>hunt — {engagement}</h1>",
            f"<p class=\"sub\">lido em {fmt_ts(t)} — refresh {max(int(interval), 3)}s</p>",
            f"<div class=\"counts\">{pills}</div>",
            _ui_queue(hyps, t)]
    page = ("<!doctype html><meta charset=utf8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
            f"<meta http-equiv=\"refresh\" content=\"{max(int(interval), 3)}\">"
            f"<title>hunt — {engagement}</title><style>{UI_CSS}</style>"
            f"<div class=\"wrap\">" + "\n".join(body) + "</div>")
    return page
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_hx.UiRenderTest.test_header_counts_and_queue_escape -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `python3 -m unittest tests.test_hx -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(ui): render header + queue (escaped, never 500)"
```

---

### Task 2: Coverage table (tolerant parse)

**Files:**
- Modify: `bin/hx` (add `parse_coverage_rows`; extend `render_ui` body)
- Test: `tests/test_hx.py` (extend `UiRenderTest`)

**Interfaces:**
- Consumes: `coverage_counts(repo) -> dict`; `render_ui` body list from Task 1
- Produces: `parse_coverage_rows(repo) -> (rows: list[list[str]], ok: bool)` — rows are `[endpoint, classe, status, evidencia, nota]`; `ok=False` only when the file exists, is non-blank, and yields zero rows

- [ ] **Step 1: Write the failing test**

```python
    def test_coverage_table_and_malformed(self):
        hx.main(["hypothesis", "add", "--claim", "x", "--endpoint", "GET /a",
                 "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        hx.main(["result", "h001", "--verdict", "unverified", "--note", "nada ainda", "--force"])
        repo = hx.repo_from_cwd()
        page = hx.render_ui(repo)
        self.assertIn("GET /a", page)
        self.assertIn("untested", page)
        (repo.hunt / "COVERAGE.md").write_text("lixo sem tabela\nmais lixo\n")
        page2 = hx.render_ui(repo)
        self.assertIn("cobertura ilegível", page2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_hx.UiRenderTest.test_coverage_table_and_malformed -v`
Expected: FAIL with `AttributeError: module 'hx' has no attribute 'parse_coverage_rows'` (or assertion, if you assert via page text — either red is fine, note which)

- [ ] **Step 3: Write minimal implementation**

```python
def parse_coverage_rows(repo):
    try:
        text = repo.coverage.read_text()
    except OSError:
        return [], True
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0].lower() == "endpoint":
            continue
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells[:5])
    if not rows and text.strip():
        return [], False
    return rows, True
```

In `render_ui`, after the queue section (before closing `body` list), insert:

```python
    try:
        rows, cov_ok = parse_coverage_rows(repo)
    except (OSError, UnicodeDecodeError):
        rows, cov_ok = [], False
    try:
        counts_txt = " ".join(f"<span class=\"pill\">{_esc(k)}: <b>{v}</b></span>" for k, v in sorted(coverage_counts(repo).items()))
    except (OSError, UnicodeDecodeError):
        counts_txt = ""
    if not rows:
        cov_inner = "<p class=\"muted\">cobertura ilegível</p>" if not cov_ok else "<p class=\"muted\">sem dados</p>"
    else:
        trs = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>" for row in rows)
        cov_inner = (f"<div class=\"counts\">{counts_txt}</div>" if counts_txt else "") + \
            "<table><tr><th>endpoint</th><th>classe</th><th>status</th><th>evidência</th><th>nota</th></tr>" + trs + "</table>"
    body.append(_section("Cobertura", cov_inner))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_hx.UiRenderTest.test_coverage_table_and_malformed -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `python3 -m unittest tests.test_hx -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(ui): coverage table with tolerant parse"
```

---

### Task 3: Findings (gated) + runs + sessions

**Files:**
- Modify: `bin/hx` (add `ui_findings`, `ui_runs`, `ui_sessions`; extend `render_ui` body)
- Test: `tests/test_hx.py` (extend `UiRenderTest`)

**Interfaces:**
- Consumes: `finding_verified(repo, hid) -> (bool, str)`; `read_runs(path, cutoff)`; `fmt_money`
- Produces: `ui_findings(repo) -> list[dict{id, verdict, note, evidence}]` (verified only); `ui_runs(repo) -> list[dict]` (last 10, keys `ts,hyp,mode,tokens,cost`); `ui_sessions(repo) -> list[str]` (last 5 names)

- [ ] **Step 1: Write the failing test**

```python
    def test_findings_gated_runs_sessions(self):
        hx.main(["hypothesis", "add", "--claim", "BOLA real", "--endpoint", "GET /b",
                 "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        (repo_hunt := hx.repo_from_cwd().hunt)
        (repo_hunt / "FINDINGS" / "h001.json").write_text(json.dumps({"verdict": "verified-manual"}))
        hx.main(["result", "h001", "--verdict", "confirmed", "--note", "ok", "--force"])
        repo = hx.repo_from_cwd()
        (repo.hunt / "runs.jsonl").write_text(json.dumps({"ts": 1000001, "hyp": "h001", "tokens": 120, "cost": 0.02}) + "\n")
        (repo.hunt / "sessions").mkdir(exist_ok=True)
        (repo.hunt / "sessions" / "2026-09-18-01.md").write_text("# s\n")
        hidden = hx.render_ui(repo)
        self.assertNotIn("verified-manual", hidden)
        self.assertNotIn("Findings verificados", hidden)
        shown = hx.render_ui(repo, show_findings=True)
        self.assertIn("verified-manual", shown)
        self.assertIn("120", shown)
        self.assertIn("2026-09-18-01.md", shown)
```

Gate check: the hidden page must not contain the findings section at all (the queue `result` card legitimately still shows `[h001]`).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_hx.UiRenderTest.test_findings_gated_runs_sessions -v`
Expected: FAIL (`AttributeError` on `ui_findings`, or assertion)

- [ ] **Step 3: Write minimal implementation**

```python
def ui_findings(repo):
    out = []
    try:
        paths = sorted((repo.hunt / "FINDINGS").glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = load_json(path, {})
        except json.JSONDecodeError:
            continue
        verified, _ = finding_verified(repo, path.stem)
        if not verified or not isinstance(data, dict):
            continue
        out.append({"id": path.stem,
                    "verdict": data.get("verdict"),
                    "note": data.get("note"),
                    "evidence": data.get("evidence") or data.get("proof_ref")})
    return out


def ui_runs(repo):
    try:
        records = read_runs(repo.hunt / "runs.jsonl", 0)
    except (OSError, UnicodeDecodeError):
        return []
    return records[-10:]


def ui_sessions(repo):
    try:
        names = sorted(path.name for path in (repo.hunt / "sessions").glob("*.md"))
    except OSError:
        return []
    return names[-5:]
```

In `render_ui`, after the coverage section:

```python
    if show_findings:
        try:
            findings = ui_findings(repo)
        except (OSError, json.JSONDecodeError):
            findings = None
        if findings is None:
            f_inner = "<p class=\"muted\">sem dados</p>"
        elif not findings:
            f_inner = "<p class=\"muted\">nenhum finding verificado</p>"
        else:
            cards = "".join(
                f"<div class=\"card\"><div><b>[{_esc(f['id'])}]</b> {_esc(f['verdict'])}</div>"
                f"<div class=\"muted\">{_esc(f['note'])} — evidência: {_esc(f['evidence'])}</div></div>"
                for f in findings)
            f_inner = cards
        body.append(_section("Findings verificados", f_inner))
    runs = ui_runs(repo)
    if not runs:
        r_inner = "<p class=\"muted\">sem dados</p>"
    else:
        trs = "".join(
            f"<tr><td>{_esc(fmt_ts(r.get('ts')))}</td><td>{_esc(r.get('hyp') or r.get('mode') or '-')}</td>"
            f"<td>{_esc(r.get('tokens'))}</td><td>{_esc(fmt_money(r.get('cost') or 0.0))}</td></tr>"
            for r in runs)
        r_inner = "<table><tr><th>hora</th><th>hipótese</th><th>tokens</th><th>custo</th></tr>" + trs + "</table>"
    body.append(_section("Runs recentes", r_inner))
    sessions = ui_sessions(repo)
    s_inner = "<p class=\"muted\">sem dados</p>" if not sessions else \
        "<div class=\"muted\">" + "<br>".join(_esc(n) for n in sessions) + "</div>"
    body.append(_section("Sessões", s_inner))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_hx.UiRenderTest.test_findings_gated_runs_sessions -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `python3 -m unittest tests.test_hx -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(ui): gated findings + runs + sessions"
```

---

### Task 4: `hx ui --serve` wiring + handler

**Files:**
- Modify: `bin/hx` (add `import http.server`; add `clamp_interval`, `_UiHandler`, `cmd_ui`; extend `main()`)
- Test: `tests/test_hx.py` (new `UiServeTest` class; needs `threading`, `urllib.request` imports)

**Interfaces:**
- Consumes: `render_ui`, `Repo`, `HxError`/`EXIT_GUARD`/`EXIT_OK`
- Produces: `clamp_interval(v: int) -> int`; `cmd_ui(args) -> int` (blocks; NOT covered by subprocess-style test — covered indirectly); `_UiHandler` (attrs `repo`, `show_findings`, `interval`)

- [ ] **Step 1: Write the failing test**

```python
class UiServeTest(unittest.TestCase):
    def test_get_root_200_and_other_404(self):
        import threading
        import urllib.request
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            hx.main(["init", "--dir", tmp])
            handler = type("_T", (hx._UiHandler,), {})()
            hx._UiHandler.repo = hx.Repo(tmp)
            hx._UiHandler.show_findings = False
            hx._UiHandler.interval = 15
            srv = hx.http.server.ThreadingHTTPServer(("127.0.0.1", 0), hx._UiHandler)
            thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
            thread.start()
            try:
                url = f"http://127.0.0.1:{srv.server_port}/"
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertIn("text/html", resp.headers.get("Content-Type"))
                    self.assertIn("hunt", resp.read().decode("utf-8"))
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(url + "x", timeout=5)
                self.assertEqual(ctx.exception.code, 404)
            finally:
                srv.shutdown()
                thread.join()
                srv.server_close()

    def test_interval_floor(self):
        self.assertEqual(hx.clamp_interval(0), 3)
        self.assertEqual(hx.clamp_interval(-5), 3)
        self.assertEqual(hx.clamp_interval(15), 15)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_hx.UiServeTest -v`
Expected: FAIL with `AttributeError: module 'hx' has no attribute '_UiHandler'`

- [ ] **Step 3: Write minimal implementation**

Top of `bin/hx`, after `import html`:

```python
import http.server
```

Before `def main`:

```python
def clamp_interval(value):
    return max(int(value), 3)


class _UiHandler(http.server.BaseHTTPRequestHandler):
    repo = None
    show_findings = False
    interval = 15

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        body = render_ui(type(self).repo, show_findings=type(self).show_findings, interval=type(self).interval)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def cmd_ui(args):
    eng = Path(args.dir).resolve()
    if not (eng / "hunt" / "scope.json").exists():
        raise HxError("engagement nao encontrado (hunt/scope.json)", EXIT_GUARD)
    repo = Repo(eng)
    _UiHandler.repo = repo
    _UiHandler.show_findings = args.show_findings
    _UiHandler.interval = clamp_interval(args.interval)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), _UiHandler)
    print(f"hunt panel: http://127.0.0.1:{srv.server_port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return EXIT_OK
```

In `main()`, after the `verify_p` block:

```python
    ui_p = sub.add_parser("ui")
    ui_p.add_argument("--serve", action="store_true", required=True)
    ui_p.add_argument("--port", type=int, default=8137)
    ui_p.add_argument("--dir", default=".")
    ui_p.add_argument("--interval", type=int, default=15)
    ui_p.add_argument("--show-findings", dest="show_findings", action="store_true")
```

And in the dispatch chain, after `verify`:

```python
        elif args.cmd == "ui":
            return cmd_ui(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_hx.UiServeTest -v`
Expected: PASS

- [ ] **Step 5: Run the full suite for regressions**

Run: `python3 -m unittest tests.test_hx -v`
Expected: all PASS

- [ ] **Step 6: Manual smoke (foreground, Ctrl-C to stop)**

Run: `mkdir -p /tmp/hxdemo && python3 bin/hx init --dir /tmp/hxdemo && python3 bin/hx ui --serve --port 8137 --dir /tmp/hxdemo`
Expected: prints `hunt panel: http://127.0.0.1:8137/`; page shows header + empty sections in a browser/curl

- [ ] **Step 7: Commit**

```bash
git add bin/hx tests/test_hx.py
git commit -m "feat(ui): hx ui --serve on 127.0.0.1 (read-only)"
```

---

## Self-Review

1. **Spec coverage:** serve/flags/bind/interval-clamp → Task 4; header+counts+queue+escape → Task 1; coverage tolerant → Task 2; findings gate + no bodies/proof + runs + sessions → Task 3; read-only/escape/never-500 → constraints enforced in every task; threat-model note → documented in spec (code consequence: findings flag, Task 3). All covered.
2. **Placeholder scan:** no TBD/TODO; every step has exact code/commands; no "similar to Task N".
3. **Type consistency:** `render_ui(repo, show_findings=False, interval=15) -> str` identical in all tasks; `parse_coverage_rows -> (rows, ok)`; `ui_findings -> list[dict]` with keys `id/verdict/note/evidence`; `ui_runs` last-10 dicts with `ts/hyp/mode/tokens/cost`; `ui_sessions -> list[str]`. Handler attrs match `cmd_ui` assignments.
