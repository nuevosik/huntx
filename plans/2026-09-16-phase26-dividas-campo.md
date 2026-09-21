# Fase 2.6 — Dívidas de campo (cooldown WAF, burst, probe visível, worker fecha)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fechar as quatro dívidas de campo antes de retomar volume no REI: cooldown WAF 30→60→120min, burst de navegação no proxy, visibilidade do probe de health, e fechamento obrigatório do worker autônomo.

**Architecture:** Tudo já existe; isto são extensões cirúrgicas em `bin/hx` (rate, probe) e `runner/runner.py` (prompt). Estado de cooldown vive no `.ratelimit.json` (mesmo lock `.lock.rate`). Burst é um parâmetro opcional de `rate_acquire` que só o proxy usa. `probe_once` passa a devolver `(ok, signal, status, excerpt)` e o `.health.json` persiste status+snippet. O prompt do runner ganha fechamento obrigatório explícito.

**Tech Stack:** Python 3 stdlib + curl_cffi (sem novas deps), unittest local (nunca commitado).

**Spec:** `specs/2026-09-15-hunt-harness-design.md` §9.1 (cooldown 30→60→120min + retomada manual), §5.3 (proxy conexão-granular com burst pra navegação), §3 (probe de health). Veredictos jev (2026-09-16): cooldown bloqueia TODO egress do host; escalada por eventos consecutivos; burst proxy 6/10s; probe persiste excerpt ≤160 chars; worker via prompt duro.

## Global Constraints

- `bin/hx` continua um arquivo único, stdlib-only, **zero comentários/docstrings**, mensagens de usuário em PT (identificadores em inglês). `runner/runner.py` idem (sem comentários).
- Testes são locais: `tests/` está no `.gitignore` — **nunca** staged/committed. Commits tocam só código/docs.
- Nenhuma dependência nova. Nenhum request de rede em teste (patching dos seams).
- `.health.json` e `.ratelimit.json` são estado local do engajamento; migração é aditiva (campo ausente = comportamento antigo).
- `EXIT_RATE = 3`, `EXIT_GUARD = 2`, `EXIT_SESSION = 6` (constantes existentes).
- Commits em PT, um por task.

---

### Task 1: Cooldown WAF (bin/hx)

**Files:**
- Modify: `bin/hx` (rate_waf_cooldown novo; `rate_acquire`; `apply_health_signals`; `probe_once`; `cmd_rate_reset`)
- Modify: `tests/test_hx.py` (novo `CooldownTest`)
- Modify: `templates/HUNT.md` (uma linha na seção de saúde/rate)

**Interfaces:**
- Produces: `rate_waf_cooldown(scope, repo, host) -> int` (minutos); estado `state["cooldowns"][host] = {"count": n, "until": ts}`.
- Consumes: `flock`, `load_json`, `dump_json`, `now`, `EXIT_RATE` (existentes).

- [ ] **Step 1: Write the failing tests** (append `CooldownTest` to `tests/test_hx.py`)

```python
class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        hx.main(["init", "--dir", str(self.eng)])
        self.repo = hx.repo_from_cwd = hx.repo_from_cwd
        self.repo = hx.Repo(self.eng, self.eng / "hunt") if hasattr(hx, "Repo") else None
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        self.scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
    def tearDown(self):
        os.environ.pop("HX_ENGAGEMENT", None)
        os.environ.pop("HX_FAKE_NOW", None)
        self.tmp.cleanup()

    def _waf_fire(self, host="www.rei.com"):
        hx.rate_waf_cooldown(self.scope, hx.repo_from_cwd(), host)

    def test_first_waf_cooldown_is_30min(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        self._waf_fire()
        state = hx.load_json(hx.repo_from_cwd().rate, {})
        entry = state["cooldowns"]["www.rei.com"]
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["until"], 1000 + 30 * 60)

    def test_consecutive_waf_doubles_and_caps_at_120(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        hx.rate_waf_cooldown(self.scope, repo, "h")
        self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 60 * 60)
        hx.rate_waf_cooldown(self.scope, repo, "h")
        self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 120 * 60)
        hx.rate_waf_cooldown(self.scope, repo, "h")
        self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 120 * 60)
        self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["h"]["count"], 4)

    def test_rate_acquire_refused_during_cooldown(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        hx.rate_waf_cooldown(self.scope, repo, "www.rei.com")
        with self.assertRaises(hx.HxError) as ctx:
            hx.rate_acquire(self.scope, repo, "www.rei.com", 30)
        self.assertEqual(ctx.exception.code, hx.EXIT_RATE)
        self.assertIn("waf cooldown", str(ctx.exception))

    def test_rate_acquire_ok_after_expiry(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        hx.rate_waf_cooldown(self.scope, repo, "www.rei.com")
        os.environ["HX_FAKE_NOW"] = str(1000 + 31 * 60)
        hx.sleep = lambda _s: None
        self.assertEqual(hx.rate_acquire(self.scope, repo, "www.rei.com", 30), 0)

    def test_reset_clears_cooldowns_and_count(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        hx.rate_waf_cooldown(self.scope, repo, "h")
        with hx.flock(repo.hunt / ".lock.rate"):
            state = hx.load_json(repo.rate, {})
            state["cooldowns"] = {}
            state.setdefault("events", []).append({"ts": hx.now(), "host": "*", "cause": "manual reset", "interval": None})
            hx.dump_json(repo.rate, state)
        hx.rate_waf_cooldown(self.scope, repo, "h")
        self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["h"]["count"], 1)

    def test_apply_health_signals_sets_cooldown(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        scope = dict(self.scope)
        scope["health"] = dict(scope["health"])
        scope["health"]["waf_block_if"] = ["body_contains:_px"]
        hx.apply_health_signals(repo, scope, "www.rei.com", 403, {}, "blocked _px")
        state = hx.load_json(repo.rate, {})
        self.assertEqual(state["cooldowns"]["www.rei.com"]["count"], 1)

    def test_probe_waf_signal_sets_cooldown(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        repo = hx.repo_from_cwd()
        scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/x"}]
        hx.fetch = lambda *a, **k: (403, {}, "px-captcha aqui")
        try:
            ok, signal = hx.probe_once(repo, scope, "a")[:2]
            self.assertFalse(ok)
            self.assertEqual(signal, "waf")
            self.assertEqual(hx.load_json(repo.rate, {})["cooldowns"]["www.rei.com"]["count"], 1)
        finally:
            hx.fetch = lambda *a, **k: (200, {}, "")
```

Nota: se `hx.Repo` não for construtor público, usar o padrão existente dos outros testes (`hx.repo_from_cwd()` com `HX_ENGAGEMENT`). O implementer deve espelhar o setUp de `RateTest`/`HealthTest` do arquivo.

- [ ] **Step 2: Run to verify RED** — `python3 tests/test_hx.py -v` → erros `AttributeError: module 'hx' has no attribute 'rate_waf_cooldown'`.

- [ ] **Step 3: Implement** — em `bin/hx`:

Adicionar após `rate_step_up`:

```python
def rate_waf_cooldown(scope, repo, host):
    with flock(repo.hunt / ".lock.rate"):
        state = load_json(repo.rate, {})
        entry = state.setdefault("cooldowns", {}).setdefault(host, {"count": 0, "until": 0})
        entry["count"] += 1
        minutes = min(120, 30 * (2 ** (entry["count"] - 1)))
        entry["until"] = now() + minutes * 60
        state.setdefault("events", []).append({"ts": now(), "host": host, "cause": "waf-cooldown", "minutes": minutes})
        dump_json(repo.rate, state)
        return minutes
```

Em `rate_acquire`, dentro do lock, logo após `state = load_json(...)`:

```python
        cooldown = (state.get("cooldowns") or {}).get(host)
        if cooldown and cooldown.get("until", 0) > now():
            remaining = int(cooldown["until"] - now())
            raise HxError(f"waf cooldown: {host} pausado por mais {remaining}s (nivel {cooldown.get('count', 1)}) — retomada manual: hx rate reset", EXIT_RATE)
```

Em `apply_health_signals`, branch waf:

```python
    for sig in health_cfg.get("waf_block_if", []):
        if signal_matches(sig, status, headers, body):
            rate_step_up(scope, repo, host, "waf-marker")
            minutes = rate_waf_cooldown(scope, repo, host)
            audit(repo, "hx run", "health", host, "cooldown", f"{minutes}min")
            return
```

Em `probe_once`, branch waf (antes do `return False, "waf"`):

```python
    for sig in scope["health"]["waf_block_if"]:
        if signal_matches(sig, status, headers, body):
            rate_waf_cooldown(scope, repo, host)
            return False, "waf"
```

Em `cmd_rate_reset`, após `state["effective"] = {}`:

```python
        state["cooldowns"] = {}
```

e o print vira `print("rate reiniciado — piso e cooldowns restaurados")`.

Em `templates/HUNT.md`, adicionar após a linha "Probes contam no rate...":

```
- `waf_block_if` marcado → cooldown do host (30→60→120min, dobra a cada reincidência); retomada manual: `hx rate reset`.
```

- [ ] **Step 4: Run full suite** — `python3 tests/test_hx.py` (tudo verde; `python3 tests/test_runner.py` também, 52+). Atenção: se algum teste existente disparar `waf_block_if` e depois chamar `rate_acquire` no mesmo host, ele agora pega exit 3 — ajustar o teste para limpar cooldown ou usar outro host, anotando no report.

- [ ] **Step 5: Commit**

```bash
git add bin/hx templates/HUNT.md
git commit -m "feat(hx): waf cooldown 30-60-120min com retomada manual"
```

---

### Task 2: Burst de navegação no proxy (bin/hx)

**Files:**
- Modify: `bin/hx` (`rate_acquire` ganha `burst=None`; `ProxyHandler` passa `scope["rate"].get("burst")`)
- Modify: `templates/scope.json` (rate ganha `"burst": [6, 10]`)
- Modify: `tests/test_hx.py` (novo `BurstTest`; ProxyTest ganha teste de burst)
- Modify: `/home/ngix/rei-bbp/hunt/scope.json` (rate ganha `"burst": [6, 10]`) — engajamento, não repo
- Modify: `README.md` (cláusula do proxy: "with a navigation burst")

**Interfaces:**
- Consumes: `rate_acquire(scope, repo, host, max_wait, burst=None)` da Task 1 (com cooldown check no topo).
- Produces: `burst` é par `[n, window_s]`; fast path quando `len(recent) < n`; `state["hosts"][host]["recent"]` lista dos últimos timestamps (máx 10).

- [ ] **Step 1: Write the failing tests**

```python
class BurstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        self.scope["rate"] = dict(self.scope["rate"])
        self.scope["rate"]["burst"] = [3, 10]
        self.slept = []
        self._orig_sleep = hx.sleep
        hx.sleep = lambda s: self.slept.append(s)

    def tearDown(self):
        hx.sleep = self._orig_sleep
        os.environ.pop("HX_ENGAGEMENT", None)
        os.environ.pop("HX_FAKE_NOW", None)
        self.tmp.cleanup()

    def test_burst_allows_n_quick_then_strict(self):
        repo = hx.repo_from_cwd()
        for _ in range(3):
            hx.rate_acquire(self.scope, repo, "www.rei.com", 30, burst=self.scope["rate"]["burst"])
        self.assertEqual(self.slept, [])
        hx.rate_acquire(self.scope, repo, "www.rei.com", 30, burst=self.scope["rate"]["burst"])
        self.assertEqual(self.slept, [2.0])

    def test_without_burst_stays_strict(self):
        repo = hx.repo_from_cwd()
        hx.rate_acquire(self.scope, repo, "www.rei.com", 30)
        hx.rate_acquire(self.scope, repo, "www.rei.com", 30)
        self.assertEqual(self.slept, [2.0])

    def test_proxy_uses_burst_from_scope(self):
        server = None
        repo = hx.repo_from_cwd()
        scope = self.scope
        scope["in_scope"] = ["127.0.0.1"]
        upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler) if False else None
```

Nota: o teste de integração do proxy deve espelhar `ProxyTest` existente (upstream echo local + `build_proxy`); a asserção é: com `burst=[2,10]` no scope, dois CONNECTs seguidos recebem `200 Connection Established` (sem 429), enquanto o `test_second_connect_rate_limited_and_audited` existente continua 429 porque o scope dele **não tem** a chave `burst`. Se o custo de montar o segundo teste de proxy for alto, o unitário acima já cobre a lógica; o de proxy é desejável mas não bloqueia.

- [ ] **Step 2: RED** — `python3 tests/test_hx.py -v` → `TypeError: rate_acquire() got an unexpected keyword argument 'burst'`.

- [ ] **Step 3: Implement** — `rate_acquire` vira (corpo completo):

```python
def rate_acquire(scope, repo, host, max_wait, burst=None):
    with flock(repo.hunt / ".lock.rate"):
        state = load_json(repo.rate, {})
        cooldown = (state.get("cooldowns") or {}).get(host)
        if cooldown and cooldown.get("until", 0) > now():
            remaining = int(cooldown["until"] - now())
            raise HxError(f"waf cooldown: {host} pausado por mais {remaining}s (nivel {cooldown.get('count', 1)}) — retomada manual: hx rate reset", EXIT_RATE)
        interval = rate_effective_interval(scope, state, host)
        global_interval = float(scope["rate"].get("global_interval_s", 0.5))
        t = now()
        host_entry = state.setdefault("hosts", {}).setdefault(host, {})
        last = host_entry.get("last") or 0
        global_last = state.get("global") or 0
        window = burst[1] if burst else 10
        recent = [ts for ts in host_entry.get("recent", []) if ts > t - window]
        if burst and len(recent) < burst[0]:
            scheduled = t
            wait = 0.0
        else:
            scheduled = max(t, last + interval, global_last + global_interval)
            wait = scheduled - t
            if wait > max_wait:
                raise HxError(f"rate: esperaria {wait:.1f}s (max {max_wait}s)", EXIT_RATE)
            if wait > 0:
                sleep(wait)
        host_entry["recent"] = (recent + [scheduled])[-10:]
        host_entry["last"] = scheduled
        state["global"] = scheduled
        dump_json(repo.rate, state)
        return wait
```

`ProxyHandler`: linha do `rate_acquire` vira:

```python
                rate_acquire(scope, repo, host, float(scope["rate"].get("max_wait_s", 30)), burst=scope["rate"].get("burst"))
```

`templates/scope.json` linha 7 vira:

```json
  "rate": { "per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30, "adaptive": true, "step_up_max_s": 30, "decay_clean_min": 60, "burst": [6, 10] },
```

`/home/ngix/rei-bbp/hunt/scope.json`: adicionar `"burst": [6, 10]` à seção rate (mesma forma).

`README.md` linha 9: `... allowlist CONNECT proxy sharing the rate state (with a navigation burst for browsing).`

- [ ] **Step 4: Full suite** — `python3 tests/test_hx.py` e `python3 tests/test_runner.py` verdes; `test_second_connect_rate_limited_and_audited` inalterado (scope sem `burst`).

- [ ] **Step 5: Commit**

```bash
git add bin/hx templates/scope.json README.md
git commit -m "feat(hx): burst de navegacao no proxy (6 conexoes/10s)"
```

---

### Task 3: Probe visível (status + excerpt) e sinal de sessão morta do REI

**Files:**
- Modify: `bin/hx` (`probe_once` devolve 4-tupla; `health_probe_result` persiste status/excerpt; `cmd_health_check` imprime; `refute_gate` desempacota)
- Modify: `runner/runner.py` (`health_gate` desempacota e repassa)
- Modify: `tests/test_hx.py` (HealthTest ganha asserts de status/excerpt)
- Modify: `tests/test_runner.py` (stubs de probe, se houver, viram 4-tupla)
- Modify: `/home/ngix/rei-bbp/hunt/scope.json` (`session_invalid_if` ganha `"body_contains:secureToken must not be null"`)

**Interfaces:**
- Produces: `probe_once(repo, scope, tag) -> (ok, signal, status, excerpt)` com `excerpt = re.sub(r"\s+", " ", body or "").strip()[:160]`; probes gravam `{"ts", "ok", "signal", "status", "excerpt"}`.
- Consumes: `rate_waf_cooldown` (T1) já chamado no branch waf.

- [ ] **Step 1: Write the failing tests** (HealthTest)

```python
    def test_probe_returns_status_and_excerpt(self):
        scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/mobile-gateway/rest/cart/V3"}]
        body = '{"error":\n  "secureToken must not be null", "x": "' + "y" * 400 + '"}'
        hx.fetch = lambda *a, **k: (400, {}, body)
        try:
            ok, signal, status, excerpt = hx.probe_once(hx.repo_from_cwd(), scope, "a")
            self.assertTrue(ok)
            self.assertEqual(status, 400)
            self.assertNotIn("\n", excerpt)
            self.assertLessEqual(len(excerpt), 160)
        finally:
            hx.fetch = lambda *a, **k: (200, {}, "")

    def test_health_check_prints_status_and_excerpt(self):
        (espelhar test_health_check existente; capturar stdout e assertar "http 400" e um trecho do corpo)
```

- [ ] **Step 2: RED** — `ValueError: too many values to unpack` / asserts de status.

- [ ] **Step 3: Implement** — em `bin/hx`:

`probe_once` — calcular excerpt uma vez e devolver 4-tupla em TODOS os returns:

```python
    status, headers, body = fetch(probe["method"], probe["url"], probe.get("headers"), None, cookies)
    excerpt = re.sub(r"\s+", " ", body or "").strip()[:160]
    for sig in scope["health"]["rate_limited_if"]:
        if signal_matches(sig, status, headers, body):
            return False, "ratelimit", status, excerpt
    for sig in scope["health"]["waf_block_if"]:
        if signal_matches(sig, status, headers, body):
            rate_waf_cooldown(scope, repo, host)
            return False, "waf", status, excerpt
    for sig in scope["health"]["session_invalid_if"]:
        if signal_matches(sig, status, headers, body):
            return False, "invalid", status, excerpt
    return True, None, status, excerpt
```

`health_probe_result`:

```python
def health_probe_result(repo, tag, ok, signal, status=None, excerpt=None):
    ...
        entry["probes"].append({"ts": ts, "ok": ok, "signal": signal, "status": status, "excerpt": excerpt})
```

`cmd_health_check`:

```python
    for tag in tags:
        ok, signal, status, excerpt = probe_once(repo, scope, tag)
        ...
        detail = f" (http {status})" if status is not None else ""
        tail = f" — {excerpt}" if excerpt else ""
        print(f"{tag}: {'ok' if ok else signal}{detail}{tail}")
```

`refute_gate`: `ok, signal, _, _ = probe_once(repo, scope, tag)`.

`runner/runner.py` no `health_gate`: desempacotar 4 e repassar (`hx.health_probe_result(repo, tag, ok, signal, status, excerpt)`). Se algum teste do runner monkeypatcha `probe_once`/`health_probe_result`, atualizar para a nova forma.

`/home/ngix/rei-bbp/hunt/scope.json`: `"session_invalid_if"` vira `["status:401", "status:403", "redirect_host:login.rei.com", "body_contains:logonId", "body_contains:secureToken must not be null"]` (manter os existentes; conferir no arquivo antes).

- [ ] **Step 4: Full suite + smoke local**

```bash
python3 tests/test_hx.py && python3 tests/test_runner.py
cd /home/ngix/rei-bbp && hx health check --session b
```
O smoke é 1 request real (probe); esperado: `b: ok (http 200) — <snippet do carrinho>`. Registrar a saída no report.

- [ ] **Step 5: Commit**

```bash
git add bin/hx runner/runner.py
git commit -m "feat(hx): probe visivel (status + excerpt) e sinal de sessao morta"
```

---

### Task 4: Fechamento obrigatório do worker (prompt) + aceite da fase

**Files:**
- Modify: `runner/runner.py` (`PROMPT_TEMPLATE`)
- Modify: `tests/test_runner.py` (needle do prompt)

**Interfaces:**
- Consumes: nada novo; reconciliação existente segue como rede de segurança.
- Produces: prompt com `FECHAMENTO OBRIGATÓRIO` e o resumo movido para dentro da `--note`.

- [ ] **Step 1: Failing test** — no teste de prompt do `tests/test_runner.py` (o que já asserta a linha do `hx verify`), adicionar:

```python
        self.assertIn("FECHAMENTO OBRIGATÓRIO", prompt)
        self.assertNotIn("Termine com um resumo curto", prompt)
```

- [ ] **Step 2: RED** — `AssertionError` na primeira linha.

- [ ] **Step 3: Implement** — `PROMPT_TEMPLATE` vira exatamente:

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
- FECHAMENTO OBRIGATÓRIO: sua ÚLTIMA ação DEVE ser `hx result {id} --verdict ... --note ...` (ou `hx release {id}` se nem começou). Sem isso o supervisor fecha a hipótese como blocked.
- O resumo (veredito, evidência, o que falta) vai na `--note` do result.
- Para `confirmed`: monte o draft em `hunt/FINDINGS/drafts/{id}.json` (cenário differential/echo/callback conforme o caso) e rode `hx verify {id}`; só então `hx result {id} --verdict confirmed`.
- Hipótese nova descoberta: `hx hypothesis add ...`.
- Sem conclusão em {minutes} min: `hx result {id} --verdict blocked --note "give_up"` e pare."""
```

- [ ] **Step 4: Aceite da fase** — rodar as três suítes e registrar no report: `python3 tests/test_hx.py`, `python3 tests/test_runner.py`, `node --test tests/plugin_guard.test.mjs`. Conferir o prompt formatado: `python3 -c "import runner; print(runner.build_prompt({'id':'h999','claim':'c','endpoint':'e','class':'x','confirm':'y','refute':'z','evidence':'ev'}, 'brief', 900))"` (cwd `runner/`).

- [ ] **Step 5: Commit**

```bash
git add runner/runner.py
git commit -m "fix(runner): fechamento obrigatorio no prompt do worker"
```

---

## Self-Review (preenchido pelo autor do plano)

- **Spec coverage:** §9.1 cooldown → T1; §5.3 burst → T2; §3 probe → T3; dívida worker (smokes 5/5) → T4. Coberto.
- **Type consistency:** `rate_acquire(..., burst=None)` definido em T1/T2 e usado só no proxy; `probe_once` 4-tupla em T3 com TODOS os call sites listados (`cmd_health_check`, `refute_gate`, `runner.health_gate`, testes). `rate_waf_cooldown` retorna minutos (int) e é usado em T1 (audit) e T3 não.
- **Ordem:** T1 antes de T2 (mesmo `rate_acquire`); T3 depois de T1 (branch waf do probe já com cooldown). T4 independente.
- **Rede de segurança:** reconciliação do runner permanece; nenhuma deleção de comportamento existente.
