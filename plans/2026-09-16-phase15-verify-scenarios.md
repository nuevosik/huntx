# Hunt Harness — Phase 1.5 Implementation Plan (verify: echo + callback)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `echo` (reflected XSS) and `callback` (SSRF/blind) scenarios to `hx verify`, with a shared `guarded_fetch` helper so every verify fetch participates in scope/rate/health like `hx run`.

**Architecture:** Extend the existing verify registry (`cmd_verify` dispatch by `scenario`). One shared helper (`guarded_fetch`) replaces the differential-local `call()`. Echo uses a per-run canary; always requires `--attest` to promote. Callback uses a BYO webhook.site collaborator; timeout produces `fail_reason: no_interaction_timeout` and mechanically blocks `refuted` in `hx result`.

**Tech Stack:** Python 3.14, stdlib only (+ existing curl_cffi in `fetch`). Node untouched. Suites: `python3 tests/test_hx.py`, `node --test tests/plugin_guard.test.mjs`.

**Spec:** `specs/2026-09-15-hunt-harness-design.md` (Rev 6 — §8 gates).

## Global Constraints

- `bin/hx` stays ONE file. NO comments and NO docstrings (strict). Portuguese prose in user messages.
- Every verify fetch goes through `guarded_fetch` — no scenario may call `fetch` for target traffic directly.
- Echo: canonical template `<hx{CANARY}>"'`; canary `secrets.token_hex(6)` per run; PASS always `pass_requires_attest`; match only the current canary.
- Callback: BYO collaborator (dj-created webhook.site URL); poll loop with real sleep (default 3s, min 1s), iteration-bounded (deterministic under `HX_FAKE_NOW`); polls bypass `check_scope`/`rate_acquire` (our infra) but are audited/saved; timeout **never** refutes.
- `hx result --verdict refuted` refuses when the hypothesis draft has `fail_reason: "no_interaction_timeout"` (exit 2) unless `--force`.
- Tests: stdlib unittest; NO real network in the suite (stub `hx.fetch`); fake-clock safe (loops must be iteration-bounded).
- Work happens in repo `/home/ngix/huntbench` (branch `phase1`, public `huntx` mirrors it — push happens once at the end of each task: commit locally; controller pushes).

---

### Task 1: `guarded_fetch` extraction (refactor)

**Files:** Modify `bin/hx` (add helper after `execute_request`; refactor `run_differential`'s `call`); Modify `tests/test_hx.py` (2 focused tests).

**Interfaces:**
- Produces: `guarded_fetch(repo, scope, method, url, session_file=None, headers=None) -> (status, headers, body)` — `check_scope` → `classify_method` staged → `EXIT_STAGED` → `rate_acquire` → `fetch` → `apply_health_signals`.
- Consumes: existing `check_scope`, `classify_method`, `rate_acquire`, `fetch`, `apply_health_signals`, `load_session`.

- [ ] **Step 1: Write the failing tests**

```python
class GuardedFetchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_fetch = hx.fetch
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["allowed_mutations"] = ["POST /rest/user"]
        scope_path.write_text(json.dumps(scope))
        self.scope = json.loads(scope_path.read_text())

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_guarded_fetch_refuses_staged_method(self):
        calls = []
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: calls.append(url) or (200, {}, "ok")
        with self.assertRaises(hx.HxError) as ctx:
            hx.guarded_fetch(self.repo, self.scope, "DELETE", "https://www.acme.com/api/x")
        self.assertEqual(ctx.exception.code, hx.EXIT_STAGED)
        self.assertEqual(calls, [])

    def test_guarded_fetch_applies_health_signals(self):
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (429, {}, "slow down")
        hx.guarded_fetch(self.repo, self.scope, "GET", "https://www.acme.com/api/x")
        state = json.loads(self.repo.rate.read_text())
        self.assertEqual(state["effective"]["www.acme.com"], 4.0)
```

- [ ] **Step 2: RED** — `python3 tests/test_hx.py GuardedFetchTest -v` → `AttributeError: guarded_fetch`.

- [ ] **Step 3: Implement**

```python
def guarded_fetch(repo, scope, method, url, session_file=None, headers=None):
    allowed, reason = check_scope(scope, url)
    if not allowed:
        raise HxError(f"guard: {reason}", EXIT_GUARD)
    if classify_method(scope, method, url) == "staged":
        raise HxError(f"metodo estagiado: {method} {url}", EXIT_STAGED)
    cookies = load_session(repo.eng / session_file) if session_file else []
    host = urlparse(url).hostname or ""
    rate_acquire(scope, repo, host, float(scope["rate"].get("max_wait_s", 30)))
    status, resp_headers, body = fetch(method, url, headers, None, cookies)
    apply_health_signals(repo, scope, host, status, resp_headers, body)
    return status, resp_headers, body
```

And in `run_differential`, replace `call`'s body:

```python
    def call(session_file, url):
        return guarded_fetch(repo, scope, request["method"], url, session_file)
```

- [ ] **Step 4: GREEN** — `python3 tests/test_hx.py -v` (all existing + 2 new pass; the old staged-method message changes — tests assert exit code and no-fetch only, so OK).

- [ ] **Step 5: Commit** — `git add bin/hx tests/test_hx.py && git commit -m "refactor(hx): guarded_fetch for all verify scenarios"`

---

### Task 2: `echo` scenario (reflected XSS, attest-always)

**Files:** Modify `bin/hx` (imports `secrets`, `quote`; add `run_echo`; dispatch `echo` in `cmd_verify`; scenario-specific `fail_reason`); Modify `tests/test_hx.py` (new `EchoTest`).

**Interfaces:**
- Consumes: `guarded_fetch` (T1), `save_evidence`, `redact_headers`, `redact_body`, `hashlib`.
- Produces: `run_echo(repo, scope, draft) -> (passed, steps)`; draft schema (spec §8, echo row).

- [ ] **Step 1: Write the failing tests**

```python
class EchoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_fetch = hx.fetch
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope_path.write_text(json.dumps(scope))
        self.seen_urls = []

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            self.seen_urls.append(url)
            from urllib.parse import urlparse, parse_qs, unquote
            payload = unquote(parse_qs(urlparse(url).query).get("q", [""])[0])
            mode = self.mode if hasattr(self, "mode") else "raw"
            if mode == "raw":
                return 200, {"content-type": "text/html"}, f"<div>{payload}</div>"
            if mode == "escaped":
                escaped = payload.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                return 200, {"content-type": "text/html"}, f"<div>{escaped}</div>"
            return 404, {}, "nope"

        hx.fetch = fake_fetch
        draft = {
            "id": "h001",
            "scenario": "echo",
            "request": {"method": "GET", "url": "https://www.acme.com/search?q={payload}"},
            "payload_template": "<hx{CANARY}>\"'",
        }
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_raw_reflection_requires_attest(self):
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        draft_path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        stored = json.loads(draft_path.read_text())
        self.assertTrue(stored["pass_requires_attest"])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        self.assertIn("%3C", self.seen_urls[0])

    def test_escaped_reflection_fails(self):
        self.mode = "escaped"
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertEqual(stored["fail_reason"], "reflection_absent_or_escaped")

    def test_error_status_fails(self):
        self.mode = "404"
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertIn("failed_at", stored)

    def test_canary_is_unique_per_run(self):
        hx.main(["verify", "h001"])
        hx.main(["verify", "h001"])
        self.assertEqual(len(self.seen_urls), 2)
        self.assertNotEqual(self.seen_urls[0], self.seen_urls[1])
```

- [ ] **Step 2: RED** — `python3 tests/test_hx.py EchoTest -v` → `cenario nao suportado no v1: echo`.

- [ ] **Step 3: Implement** — add imports (`import secrets`, extend `from urllib.parse import urlparse, quote`), then:

```python
def run_echo(repo, scope, draft):
    request = draft["request"]
    canary = secrets.token_hex(6)
    payload = draft["payload_template"].replace("{CANARY}", canary)
    url = request["url"].replace("{payload}", quote(payload, safe="")).replace("{payload_raw}", payload)
    status, headers, body = guarded_fetch(repo, scope, request["method"], url, draft.get("session"), draft.get("headers"))
    raw_present = payload in body
    escapes = [f"&lt;hx{canary}", f"&#60;hx{canary}", f"&#x3c;hx{canary}"]
    escaped_present = any(token in body for token in escapes)
    index = body.find(payload)
    snippet = body[max(0, index - 80): index + len(payload) + 80] if index >= 0 else ""
    output_dir = repo.hunt / "FINDINGS" / "drafts" / draft["id"]
    save_evidence(
        repo,
        output_dir,
        {"kind": "verify", "step": "echo", "canary": canary},
        {"method": request["method"], "url": url, "headers": {}, "body": None},
        {"status": status, "headers": redact_headers(headers), "body": (redact_body(body) or "")[:8192],
         "integrity": {"raw_sha256": hashlib.sha256((body or "").encode("utf-8", errors="ignore")).hexdigest(), "body_len": len(body or "")}},
        {"canary": canary, "snippet": snippet},
    )
    steps = {"status": status, "canary": canary, "raw_present": raw_present, "escaped_present": escaped_present}
    passed = 200 <= status < 300 and raw_present and not escaped_present
    return passed, steps
```

In `cmd_verify`, extend the dispatch and the fail path:

```python
    if scenario == "differential":
        passed, steps, requires_attest = run_differential(repo, scope, draft)
    elif scenario == "echo":
        passed, steps = run_echo(repo, scope, draft)
        requires_attest = passed
    else:
        raise HxError(f"cenario nao suportado no v1: {scenario}", EXIT_GUARD)
```

```python
    draft["failed_at"] = now()
    draft["fail_detail"] = steps
    if scenario == "echo":
        draft["fail_reason"] = "reflection_absent_or_escaped"
    dump_json(draft_path, draft)
```

- [ ] **Step 4: GREEN** — `python3 tests/test_hx.py -v`.

- [ ] **Step 5: Commit** — `git commit -m "feat(hx): echo verify scenario (canary, attest-always)"`

---

### Task 3: `callback` scenario + refute gate

**Files:** Modify `bin/hx` (add `run_callback`, `_parse_hits`, `_poll_for_new_hit`; dispatch `callback`; `fail_reason: no_interaction_timeout`; mechanical gate in `cmd_result`); Modify `tests/test_hx.py` (new `CallbackTest`).

**Interfaces:**
- Produces: `run_callback(repo, scope, draft) -> (passed, steps)`; `_parse_hits(body) -> list`; `_poll_for_new_hit(poll_url, base_count, interval_s, timeout_s) -> list | None`; draft schema (spec §8, callback row); `hx result` refute gate on `no_interaction_timeout`.
- Consumes: `guarded_fetch` (T1), `fetch` (polls only), `save_evidence`.

- [ ] **Step 1: Write the failing tests**

```python
class CallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        self.sleeps = []
        hx.sleep = lambda seconds: self.sleeps.append(seconds)
        self.orig_fetch = hx.fetch
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope_path.write_text(json.dumps(scope))
        self.poll_url = "https://webhook.site/token/abc/requests"
        self.hits = []
        self.target_urls = []
        self.record_hit = True

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            if url == self.poll_url:
                return 200, {}, json.dumps({"total": len(self.hits), "data": list(self.hits)})
            self.target_urls.append(url)
            if self.record_hit:
                self.hits.append({"method": "GET", "url": "https://webhook.site/xyz"})
            return 200, {}, "ok"

        hx.fetch = fake_fetch
        draft = {
            "id": "h001",
            "scenario": "callback",
            "request": {"method": "GET", "url": "https://www.acme.com/fetch?url={collaborator}"},
            "collaborator": {"url": "https://webhook.site/xyz", "poll": {"url": self.poll_url}},
            "interaction_timeout_s": 3,
            "poll_interval_s": 1,
        }
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_interaction_promotes(self):
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        promoted = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertTrue(promoted["proof"]["interaction"])
        self.assertEqual(self.target_urls, ["https://www.acme.com/fetch?url=https://webhook.site/xyz"])

    def test_timeout_marks_fail_reason(self):
        self.record_hit = False
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertEqual(stored["fail_reason"], "no_interaction_timeout")
        self.assertTrue(self.sleeps)

    def test_refute_refused_after_callback_timeout(self):
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /f", "--class", "SSRF", "--confirm", "x", "--refute", "y"])
        hx.main(["next"])
        self.record_hit = False
        hx.main(["verify", "h001"])
        rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "sem interacao"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "prova positiva", "--force"])
        self.assertEqual(rc, 0)
```

(Note: the draft id and the hypothesis id share `h001` in the gate test — that is the linkage the gate reads.)

- [ ] **Step 2: RED** — `python3 tests/test_hx.py CallbackTest -v`.

- [ ] **Step 3: Implement**

```python
def _parse_hits(body):
    try:
        data = json.loads(body or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []
    return data if isinstance(data, list) else []


def _poll_for_new_hit(poll_url, base_count, interval_s, timeout_s):
    max_polls = max(2, int(timeout_s / max(interval_s, 1)))
    for _ in range(max_polls):
        status, headers, body = fetch("GET", poll_url, {"Accept": "application/json"}, None, None)
        hits = _parse_hits(body)
        if len(hits) > base_count:
            return hits
        sleep(interval_s)
    return None
```

```python
def run_callback(repo, scope, draft):
    request = draft["request"]
    collaborator = draft.get("collaborator") or {}
    collaborator_url = collaborator.get("url") or ""
    poll_url = (collaborator.get("poll") or {}).get("url") or ""
    if not collaborator_url or not poll_url:
        raise HxError("callback exige collaborator.url e collaborator.poll.url", EXIT_GUARD)
    interval_s = float(draft.get("poll_interval_s", 3))
    timeout_s = float(draft.get("interaction_timeout_s", 45))
    snapshot_status, snapshot_headers, snapshot_body = fetch("GET", poll_url, {"Accept": "application/json"}, None, None)
    base_count = len(_parse_hits(snapshot_body))
    target_url = request["url"].replace("{collaborator}", collaborator_url)
    status, headers, body = guarded_fetch(repo, scope, request["method"], target_url, draft.get("session"), draft.get("headers"))
    hit = _poll_for_new_hit(poll_url, base_count, interval_s, timeout_s)
    output_dir = repo.hunt / "FINDINGS" / "drafts" / draft["id"]
    save_evidence(
        repo,
        output_dir,
        {"kind": "verify", "step": "callback", "baseline_hits": base_count},
        {"method": request["method"], "url": target_url, "headers": {}, "body": None},
        {"status": status, "headers": redact_headers(headers), "body": (redact_body(body) or "")[:8192],
         "integrity": {"raw_sha256": hashlib.sha256((body or "").encode("utf-8", errors="ignore")).hexdigest(), "body_len": len(body or "")}},
        {"baseline_hits": base_count, "hit": (hit or [])[:1]},
    )
    steps = {"status": status, "baseline_hits": base_count, "interaction": hit is not None, "hit": (hit or [])[:1]}
    return hit is not None, steps
```

In `cmd_verify`:

```python
    elif scenario == "callback":
        passed, steps = run_callback(repo, scope, draft)
        requires_attest = False
```

```python
    if scenario == "callback" and steps.get("interaction") is False:
        draft["fail_reason"] = "no_interaction_timeout"
```

In `cmd_result`, before saving (after the existing refute gate), the mechanical block:

```python
        if args.verdict == "refuted" and not args.force:
            draft_path = repo.hunt / "FINDINGS" / "drafts" / f"{hyp['id']}.json"
            blocked_draft = load_json(draft_path, None)
            if blocked_draft and blocked_draft.get("fail_reason") == "no_interaction_timeout":
                raise HxError("timeout de callback nao refuta — use blocked/unverified, ou --force com prova de ausencia", EXIT_GUARD)
```

- [ ] **Step 4: GREEN** — `python3 tests/test_hx.py -v` (watch for hangs: the poll loop is iteration-bounded, keep it that way).

- [ ] **Step 5: Commit** — `git commit -m "feat(hx): callback verify scenario; timeout never refutes"`

---

### Task 4: docs + smoke

**Files:** Modify `templates/HUNT.md` (verify scenarios section), `README.md` (components line + test counts); Run smoke.

- [ ] **Step 1: templates/HUNT.md — add a "Cenários do verify" section** (PT-BR): differential (vítima/atacante/controle; PASS auto), echo (param refletidor; **sempre exige `--attest`**; payload canônico), callback (BYO webhook.site: criar URL no browser → colar `collaborator.url` + `poll.url` no draft; timeout **não** refuta — use `blocked`/`unverified`).

- [ ] **Step 2: README.md** — extend the `bin/hx` bullet with "verify: differential + echo + callback (attest-gated where proof is contextual)"; update the test-count comment after the suite run.

- [ ] **Step 3: local echo smoke (no target needed)**

Fixture que reflete cru, num engagement descartável com localhost in-scope:

```bash
cd /tmp/opencode && python3 -c "
import http.server, urllib.parse
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('q', [''])[0]
        body = f'<div>{q}</div>'.encode()
        self.send_response(200); self.send_header('Content-Type', 'text/html'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', 8901), H).serve_forever()" &
SRV=$!
mkdir -p /tmp/opencode/smoke-echo && cd /tmp/opencode/smoke-echo && /home/ngix/huntbench/bin/hx init --dir . >/dev/null
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("hunt/scope.json"); s = json.loads(p.read_text()); s["in_scope"] = ["127.0.0.1"]; p.write_text(json.dumps(s))
draft = {"id": "h001", "scenario": "echo", "request": {"method": "GET", "url": "http://127.0.0.1:8901/?q={payload}"}, "payload_template": "<hx{CANARY}>\"'"}
pathlib.Path("hunt/FINDINGS/drafts/h001.json").write_text(json.dumps(draft))
EOF
/home/ngix/huntbench/bin/hx verify h001           # esperado: PASS do echo (com o canário) + draft mantido com pass_requires_attest
/home/ngix/huntbench/bin/hx verify h001 --attest  # sem TTY → exit 7; a assinatura interativa é passo do dj
kill $SRV
```

Expected: mensagem de PASS com o canário; draft intacto com `pass_requires_attest: true`; `--attest` não-interativo → `EXIT_TTY` (7).

- [ ] **Step 4: local callback smoke (all-local, zero external)**

```bash
# terminal loop: collab fixture (records hits, serves JSON) + target fixture (SSRF-fetch via urllib)
python3 - <<'EOF' &
import http.server, json, urllib.request, urllib.parse, threading
hits = []
class Collab(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            hits.append({"url": "hit"}); body = b"ok"
        else:
            body = json.dumps({"total": len(hits), "data": hits}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
class Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try: urllib.request.urlopen(q.get("url", [""])[0], timeout=5).read()
        except Exception: pass
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass
threading.Thread(target=lambda: http.server.HTTPServer(("127.0.0.1", 8902), Collab).serve_forever(), daemon=True).start()
http.server.HTTPServer(("127.0.0.1", 8903), Target).serve_forever()
EOF
SRV=$!
# draft: collaborator.url http://127.0.0.1:8902/ , poll http://127.0.0.1:8902/requests , request http://127.0.0.1:8903/fetch?url={collaborator}
# scope in_scope ["127.0.0.1"]; run: hx verify h002 → expect promotion (interaction observed)
kill $SRV
```

Expected: `findings/h002.json` promoted with `proof.interaction == true`; second run (collab não vira hit — flaky-less: the fixture always records) — record actual outputs in the report.

- [ ] **Step 5: update spec fases line? (already Rev 6) — skip; commit docs:** `git commit -m "docs: verify scenarios v1.5 (echo/callback) in templates + README"`

---

**Done = phase 1.5:** suites green (python + node), echo and callback exercised end-to-end locally, spec Rev 6 matches behavior, README/HUNT updated.

---

## Errata da review final (2026-09-16)

O caminho de FAIL do `cmd_verify` **descarta `proof` e `pass_requires_attest`** (fix wave): sem isso, um PASS fraco seguido de um FAIL deixava o `--attest` imprimir/assinar a prova antiga (Critical #1 da review final).

`run_callback` endurecido (fix wave): snapshot **estrito** (2xx + JSON válido, senão `EXIT_TRANSPORT`); clamp do poll (`interval_s = min(max(interval_s, 1.0), max(timeout_s, 1.0))`, `max_polls = max(1, int(timeout_s / interval_s))`, sleep só **entre** polls); linha de `audit` do callback; checagem anti-cache endurecida (escapes case-insensitive); guard de placeholder (`request.url` precisa conter `{payload}` ou `{payload_raw}`).
