#!/usr/bin/env python3
import argparse
import importlib.machinery
import importlib.util
import json
import multiprocessing
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

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
    path = Path(repo_root) / "bin" / "hx"
    loader = importlib.machinery.SourceFileLoader("hx", str(path))
    spec = importlib.util.spec_from_file_location("hx", path, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hx = load_hx()

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


HOST_RE = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9][a-z0-9.-]*\.[a-z]{2,})\b", re.I)
NETNS_BACKENDS = ("slirp4netns",)
OPENCODE_DIR = Path(os.environ.get("OPENCODE_DIR", Path.home() / ".config" / "opencode"))
OPENCODE_MODELS = Path(os.environ.get("OPENCODE_MODELS", Path.home() / ".cache" / "opencode" / "models.json"))
OPENCODE_AUTH = Path(os.environ.get("OPENCODE_AUTH", Path.home() / ".local" / "share" / "opencode" / "auth.json"))


def hosts_in_text(text):
    return {match.group(1).lower().rstrip(".") for match in HOST_RE.finditer(text or "")}


def strip_jsonc(text):
    return re.sub(r'("(?:[^"\\]|\\.)*")|//[^\n]*', lambda match: match.group(1) or "", text)


def opencode_egress_hosts(config_dir=None, models_cache=None, auth_file=None):
    config_dir = Path(config_dir or OPENCODE_DIR)
    models_cache = Path(models_cache or OPENCODE_MODELS)
    auth_file = Path(auth_file or OPENCODE_AUTH)
    hosts = set()
    config_path = config_dir / "opencode.jsonc"
    config = {}
    if config_path.exists():
        try:
            config = json.loads(strip_jsonc(config_path.read_text()))
        except Exception:
            config = {}
    for server in (config.get("mcp") or {}).values():
        if isinstance(server, dict) and server.get("type") == "remote" and server.get("url"):
            host = urlparse(server["url"]).hostname
            if host:
                hosts.add(host.lower())
    providers = set((config.get("provider") or {}).keys())
    for key in ("model", "small_model"):
        value = config.get(key)
        if isinstance(value, str) and "/" in value:
            providers.add(value.split("/", 1)[0])
    auth = hx.load_json(auth_file, {})
    if isinstance(auth, dict):
        providers |= set(auth.keys())
    models = hx.load_json(models_cache, {})
    for provider in providers:
        entry = models.get(provider) or {}
        host = urlparse(entry.get("api") or "").hostname
        if host:
            hosts.add(host.lower())
    return hosts


def netns_allow_hosts(repo, hyp=None, config_dir=None, models_cache=None, auth_file=None):
    scope = repo.scope()
    hosts = {entry.lower().lstrip("*.") for entry in (scope.get("in_scope") or [])}
    if hyp:
        hosts |= hosts_in_text(hyp.get("endpoint"))
    target = repo.hunt / "TARGET.md"
    if target.exists():
        for host in hosts_in_text(target.read_text()):
            if hx.check_scope(scope, f"https://{host}/")[0]:
                hosts.add(host)
    provider_hosts = opencode_egress_hosts(config_dir, models_cache, auth_file)
    hosts |= provider_hosts
    extras = (scope.get("netns") or {}).get("allow_hosts") or []
    hosts |= {host.lower() for host in extras}
    if not provider_hosts and not extras:
        raise hx.HxError("netns: provedor LLM nao derivavel e sem netns.allow_hosts — worker recusado", hx.EXIT_GUARD)
    return hosts


def resolve_ips(hosts, resolver=None):
    ips = set()
    for host in hosts:
        try:
            if resolver:
                addresses = resolver(host)
            else:
                addresses = [info[4][0] for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]
        except OSError:
            continue
        for address in addresses or []:
            ips.add(address)
    return ips


def netns_resolvers():
    resolvers = []
    path = Path("/etc/resolv.conf")
    if not path.exists():
        return resolvers
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver" and not parts[1].startswith("127."):
            resolvers.append(parts[1])
    return resolvers


def netns_ruleset(ips, resolvers):
    if not ips:
        raise hx.HxError("netns: regras vazias — allowlist sem IP resolvido", hx.EXIT_GUARD)
    lines = [
        "table inet hx",
        "delete table inet hx",
        "table inet hx {",
        "  set allowed {",
        "    type ipv4_addr",
        f"    elements = {{ {', '.join(sorted(ips))} }}",
        "  }",
        "  chain out {",
        "    type filter hook output priority 0; policy drop;",
        "    oif lo accept",
        "    ip daddr @allowed accept",
    ]
    for resolver in resolvers:
        lines.append(f"    ip daddr {resolver} udp dport 53 accept")
        lines.append(f"    ip daddr {resolver} tcp dport 53 accept")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def apply_netns_rules(ips, resolvers):
    result = subprocess.run(["nft", "-f", "-"], input=netns_ruleset(ips, resolvers), capture_output=True, text=True)
    if result.returncode != 0:
        raise hx.HxError(f"netns: nft falhou: {(result.stderr or '').strip()[:200]}", hx.EXIT_GUARD)


def netns_preflight():
    problems = []
    if not shutil.which("nft"):
        problems.append("nft ausente")
    if not any(shutil.which(name) for name in NETNS_BACKENDS):
        problems.append("backend de NAT ausente (instale slirp4netns)")
    if not shutil.which("setpriv"):
        problems.append("setpriv ausente (util-linux)")
    try:
        probe = subprocess.run(["unshare", "-Ur", "true"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        problems.append(f"unshare -Ur falhou: {exc}")
    else:
        if probe.returncode != 0:
            problems.append("unshare -Ur indisponivel (user namespaces desabilitados)")
    return problems


def agent_argv(argv):
    if os.environ.get("HX_NETNS") == "1":
        return ["setpriv", "--bounding-set=-net_admin", "--"] + list(argv)
    return list(argv)


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


PLAN_TEMPLATE = """Você é o PLANEJADOR desta sessão. Sua única função é propor hipóteses para a fila — você não testa nada.

PROIBIDO: rodar `hx run` ou qualquer request de rede contra o alvo. Você não interage com o alvo.

CAP: proponha no máximo {n} hipóteses novas nesta sessão.
DEDUP: não repita nada que já esteja na fila aberta ou na lista de já refutadas do digest.

DIGEST:
{digest}

COMO REGISTRAR: cada proposta deve virar um comando
`hx hypothesis add --claim "..." --endpoint "..." --class "..." --confirm "..." --refute "..." --source planner`
um comando por proposta, sem --session-tag.

Feche com um resumo curto: quantas propostas registrou e por quê."""

PLAN_DIGEST_BYTES = 3072


def build_plan_digest(repo, plan_n):
    parts = [f"== DIGEST DE PLANEJAMENTO == (cap de propostas: {plan_n})", ""]
    parts.append("## Coverage (truncado em 3KB)")
    coverage = repo.coverage.read_text() if repo.coverage.exists() else ""
    coverage = coverage.encode("utf-8")[:PLAN_DIGEST_BYTES].decode("utf-8", errors="ignore")
    parts.append(coverage or "(vazio)")
    parts.append("")
    parts.append("## TARGET.md (60 linhas)")
    target = repo.hunt / "TARGET.md"
    target_lines = target.read_text().splitlines()[:60] if target.exists() else []
    parts.extend(target_lines or ["(vazio)"])
    parts.append("")
    parts.append("## Último debrief (40 linhas)")
    sessions_dir = repo.hunt / "sessions"
    sessions = sorted(sessions_dir.glob("*.md")) if sessions_dir.exists() else []
    parts.extend(sessions[-1].read_text().splitlines()[-40:] if sessions else ["(nenhum)"])
    parts.append("")
    hyps, _ = hx.load_hyps(repo)
    parts.append("## Fila aberta")
    open_lines = []
    for hyp in hyps:
        if hyp.get("status") != "open":
            continue
        label = f"[{hyp['id']}]" + (" [planner]" if hyp.get("source") == "planner" else "")
        open_lines.append(f"- {label} {hyp['claim']} ({hyp['class']} {hyp['endpoint']})")
    parts.extend(open_lines or ["(vazia)"])
    parts.append("")
    parts.append("## Já refutadas (endpoint | classe)")
    refuted_lines = []
    if repo.coverage.exists():
        for line in repo.coverage.read_text().splitlines():
            if not line.startswith("|") or "---" in line:
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) >= 3 and cells[2] == "refuted" and len(refuted_lines) < 100:
                refuted_lines.append(f"- {cells[0]} | {cells[1]}")
    parts.extend(refuted_lines or ["(nenhuma)"])
    return "\n".join(parts)


def count_planner_hyps(repo):
    hyps, _ = hx.load_hyps(repo)
    return sum(1 for hyp in hyps if hyp.get("source") == "planner")


def invoke_opencode(prompt, cwd, config_content, timeout_s, model=None, resume_session=None, env_extra=None):
    env = dict(os.environ)
    env["OPENCODE_CONFIG_CONTENT"] = config_content
    if env_extra:
        env.update(env_extra)
    argv = agent_argv(["opencode", "run", "--format", "json"])
    if model:
        argv += ["--model", model]
    if resume_session:
        argv += ["--continue", "--session", resume_session]
    argv.append(prompt)
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        out, err = "", ""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return (124, out or "", err or "")
    return (proc.returncode, out, err)


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


def is_rate_failure(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("rate limit", "429", "waf", "captcha"))


def retry_invoke(ctx, prompt):
    attempts = 0
    session = None
    budget = ctx.get("budget") or DEFAULTS
    owner = ctx.get("owner")
    env_extra = {"HX_SESSION": owner} if owner else None
    rc, stdout, stderr = 1, "", ""
    while attempts < budget["backoff_attempts"]:
        if attempts and ((ctx["repo"].hunt / "runner.stop").exists() or hx.now() - ctx.get("started_ts", hx.now()) >= budget["wall_s"]):
            break
        attempts += 1
        rc, stdout, stderr = invoke_opencode(
            prompt, ctx["repo"].eng, ctx["config"], ctx.get("slice_timeout_s") or DEFAULTS["slice_timeout_s"], ctx.get("model"), resume_session=session, env_extra=env_extra
        )
        if rc in (0, 124):
            break
        text = f"{stdout}\n{stderr}"
        if is_rate_failure(text):
            wait = min(budget["rate_backoff_max_s"], budget["rate_backoff_s"] * (2 ** (attempts - 1)))
        else:
            wait = min(budget["backoff_max_s"], budget["backoff_base_s"] * (2 ** (attempts - 1)))
        session = session or parse_session_id(stdout)
        if attempts < budget["backoff_attempts"]:
            hx.sleep(wait)
    return rc, stdout, stderr, attempts


def export_usage(hx_mod, session_id):
    if not session_id:
        return {}
    tmp = None
    try:
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        tmp = Path(name)
        try:
            with open(tmp, "w") as handle:
                proc = subprocess.run(["opencode", "export", session_id], stdout=handle, stderr=subprocess.DEVNULL, timeout=60)
        except Exception:
            return {}
        if proc.returncode != 0:
            return {}
        try:
            data = json.loads(tmp.read_text())
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        info = data.get("info") or {}
        tokens = info.get("tokens") or {}
        total = tokens.get("total") or sum(tokens.get(key, 0) or 0 for key in ("input", "output", "reasoning"))
        return {"tokens": total or None, "cost": info.get("cost")}
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def record_run(repo, record):
    path = repo.hunt / "runs.jsonl"
    with hx.flock(repo.hunt / ".lock.runs"):
        with open(path, "a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def adjudicate(repo, hid):
    draft_path = repo.hunt / "FINDINGS" / "drafts" / f"{hid}.json"
    if not draft_path.exists():
        return {"ran": False}
    if hx.load_json(draft_path, {}).get("failed_at"):
        return {"ran": False, "skipped": "failed_draft"}
    hx_bin = REPO_ROOT / "bin" / "hx"
    try:
        proc = subprocess.run([str(hx_bin), "verify", hid], cwd=str(repo.eng), capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return {"ran": True, "rc": None, "error": str(exc)[:200]}
    result = {"ran": True, "rc": proc.returncode, "promoted": proc.returncode == 0 and not draft_path.exists()}
    if proc.returncode == 0 and draft_path.exists():
        draft = hx.load_json(draft_path, {})
        if draft.get("pass_requires_attest"):
            result["pending_attest"] = True
    return result


def run_slice(ctx):
    repo = ctx["repo"]
    hyp = ctx["hyp"]
    prompt = build_prompt(hyp, ctx.get("brief") or "", ctx.get("slice_timeout_s") or DEFAULTS["slice_timeout_s"])
    rc, stdout, stderr, attempts = retry_invoke(ctx, prompt)
    session = parse_session_id(stdout)
    usage = export_usage(hx, session) if session else {}
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
    if rc == 124:
        record["timeout"] = True
    if not ctx.get("no_adjudicate"):
        record["adjudication"] = adjudicate(repo, hyp["id"])
    hyps, _ = hx.load_hyps(repo)
    current = next((item for item in hyps if item["id"] == hyp["id"]), None)
    if current and current.get("status") in ("claimed", "running"):
        if hx.main(["result", hyp["id"], "--verdict", "blocked", "--note", "fatia sem fechamento pelo worker"]) == 0:
            record["reconciled"] = "blocked"
    record_run(repo, record)
    return record


def run_plan(ctx):
    repo = ctx["repo"]
    plan_n = ctx.get("plan_n") or 5
    prompt = PLAN_TEMPLATE.format(digest=build_plan_digest(repo, plan_n), n=plan_n)
    before = count_planner_hyps(repo)
    rc, stdout, _, _ = retry_invoke(ctx, prompt)
    session = parse_session_id(stdout)
    usage = export_usage(hx, session) if session else {}
    record = {
        "ts": hx.now(),
        "mode": "plan",
        "session": session,
        "rc": rc,
        "tokens": usage.get("tokens"),
        "cost": usage.get("cost"),
        "proposed": count_planner_hyps(repo) - before,
    }
    record_run(repo, record)
    print(f"planner: +{record['proposed']} propostas (session {session or '-'}, tokens {record['tokens'] or 0})")
    return record


def health_gate(hx_mod, repo, hyp):
    scope = repo.scope()
    probes = (scope.get("health") or {}).get("probes") or []
    tag = hyp.get("session_tag")
    if not probes or not tag:
        return True, None
    if hx_mod.health_get(repo, tag).get("dead_since"):
        return False, tag
    fresh_s = float((scope.get("health") or {}).get("fresh_max_s", 600))
    if not hx_mod.health_fresh(repo, tag, fresh_s):
        try:
            ok, sig, status, excerpt = hx_mod.probe_once(repo, scope, tag)
            declared = hx_mod.health_probe_result(repo, tag, ok, sig, status, excerpt)
            if declared:
                hx_mod.reopen_sweep(repo, tag, hx_mod.health_get(repo, tag)["dead_since"])
        except hx_mod.HxError as err:
            print(f"runner: health probe falhou ({err})")
            return True, tag
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


def build_config_content(agent_md_path, plan=False):
    text = Path(agent_md_path).read_text()
    if text.startswith("---"):
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else text
    else:
        body = text.strip()
    bash = {"*": "deny", "hx hypothesis add *": "allow"} if plan else {"*": "deny", "hx *": "allow"}
    config = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            "hunt-auto": {
                "description": "headless hunt worker (runner)",
                "mode": "primary",
                "permission": {"bash": bash, "edit": "deny" if plan else "allow"},
                "prompt": body,
            }
        },
        "default_agent": "hunt-auto",
    }
    return json.dumps(config)


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


def claim_ttl_for(budget):
    return max(hx.TTL_CLAIM_S, budget["backoff_attempts"] * budget["slice_timeout_s"] + 300)


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


def netns_worker_enter(ctx):
    os.unshare(os.CLONE_NEWNET)
    ctx["netns_ctrl"].send("netns")
    if ctx["netns_ctrl"].recv() != "go":
        raise hx.HxError("netns: supervisor nao confirmou a criacao do namespace", hx.EXIT_GUARD)
    try:
        Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").write_text("1")
    except OSError:
        pass
    ips = resolve_ips(netns_allow_hosts(ctx["repo"]))
    apply_netns_rules(ips, netns_resolvers())


def netns_refresh(ctx, hyp):
    ips = resolve_ips(netns_allow_hosts(ctx["repo"], hyp))
    apply_netns_rules(ips, netns_resolvers())


def worker_loop(ctx):
    repo = ctx["repo"]
    budget = ctx["budget"]
    worker = ctx.get("worker")
    owner = ctx.get("owner") or f"runner-{os.getpid()}"
    os.environ["HX_SESSION"] = owner
    if ctx.get("netns"):
        netns_worker_enter(ctx)
    while True:
        reason = check_stop(ctx)
        if reason:
            return reason
        hyp = claim_next(repo, owner, worker)
        if hyp is None:
            return "empty_queue"
        if ctx.get("netns"):
            netns_refresh(ctx, hyp)
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
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    queue.put(worker_loop(ctx))


def start_netns_backend(pid, backend):
    if backend != "slirp4netns":
        raise hx.HxError(f"netns: backend nao suportado: {backend}", hx.EXIT_GUARD)
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            ["slirp4netns", "--configure", "--mtu=65520", "--disable-host-loopback", f"--ready-fd={write_fd}", str(pid), "tap0"],
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise hx.HxError("netns: slirp4netns nao iniciou", hx.EXIT_GUARD)
    os.close(write_fd)
    payload = b""
    ready, _, _ = select.select([read_fd], [], [], 30)
    if ready:
        payload = os.read(read_fd, 1)
    os.close(read_fd)
    if not payload:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise hx.HxError("netns: slirp4netns nao ficou pronto", hx.EXIT_GUARD)
    return proc


def run_workers(ctx, workers):
    repo = ctx["repo"]
    probes = (repo.scope().get("health") or {}).get("probes") or []
    proc_ctx = multiprocessing.get_context("fork")
    queue = proc_ctx.Queue()
    netns = bool(ctx.get("netns"))
    procs = []
    backends = []
    try:
        for i in range(workers):
            session = probes[i]["session"] if i < len(probes) else None
            wctx = dict(ctx)
            wctx["worker"] = session
            wctx["owner"] = session or f"runner-{os.getpid()}"
            parent_ctrl = None
            if netns:
                parent_ctrl, child_ctrl = proc_ctx.Pipe()
                wctx["netns_ctrl"] = child_ctrl
            proc = proc_ctx.Process(target=worker_entry, args=(queue, wctx))
            proc.start()
            procs.append(proc)
            if netns:
                child_ctrl.close()
                if not parent_ctrl.poll(30):
                    raise hx.HxError("netns: worker nao criou o namespace", hx.EXIT_GUARD)
                try:
                    parent_ctrl.recv()
                except EOFError:
                    raise hx.HxError("netns: worker morreu antes do namespace", hx.EXIT_GUARD)
                backends.append(start_netns_backend(proc.pid, ctx.get("backend") or "slirp4netns"))
                parent_ctrl.send("go")
                parent_ctrl.close()
        for proc in procs:
            proc.join()
    finally:
        for backend in backends:
            backend.terminate()
        for backend in backends:
            try:
                backend.wait(timeout=5)
            except Exception:
                pass
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
    reasons = []
    for _ in procs:
        try:
            reasons.append(queue.get(timeout=1))
        except Exception:
            reasons.append("crashed")
    return reasons


def netns_selfcheck():
    proc_ctx = multiprocessing.get_context("fork")
    parent_ctrl, child_ctrl = proc_ctx.Pipe()
    pid = os.fork()
    if pid == 0:
        parent_ctrl.close()
        try:
            os.unshare(os.CLONE_NEWNET)
            child_ctrl.send("netns")
            if child_ctrl.recv() != "go":
                os._exit(3)
            try:
                Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").write_text("1")
            except OSError:
                pass
            ips = resolve_ips({"localhost"})
            ips.add("127.0.0.1")
            apply_netns_rules(ips, netns_resolvers())
            checks = {}
            route = Path("/proc/net/route").read_text() if Path("/proc/net/route").exists() else ""
            checks["default_route"] = any(line.split()[1] == "00000000" for line in route.splitlines()[1:])
            try:
                with socket.create_connection(("192.0.2.1", 443), timeout=3):
                    checks["out_of_scope_blocked"] = False
            except OSError:
                checks["out_of_scope_blocked"] = True
            result = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True)
            checks["rules_present"] = "policy drop" in (result.stdout or "")
            tamper = subprocess.run(["setpriv", "--bounding-set=-net_admin", "--", "nft", "add", "table", "inet", "tamper"], capture_output=True, text=True)
            checks["agent_cannot_tamper"] = tamper.returncode != 0
            print(json.dumps(checks), flush=True)
            os._exit(0 if all(checks.values()) else 1)
        except Exception as err:
            print(json.dumps({"error": str(err)[:200]}), flush=True)
            os._exit(2)
    child_ctrl.close()
    if not parent_ctrl.poll(30):
        os.kill(pid, signal.SIGKILL)
        print(json.dumps({"error": "child nao criou o netns"}))
        return 2
    try:
        parent_ctrl.recv()
    except EOFError:
        print(json.dumps({"error": "child morreu antes do namespace"}, ensure_ascii=False))
        return 2
    reaped, _ = os.waitpid(pid, os.WNOHANG)
    if reaped == pid:
        print(json.dumps({"error": "child morreu antes do namespace"}))
        return 2
    backend = start_netns_backend(pid, "slirp4netns")
    parent_ctrl.send("go")
    try:
        _, status = os.waitpid(pid, 0)
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=5)
        except Exception:
            pass
    code = os.waitstatus_to_exitcode(status)
    if code == 0:
        print("SELFCHECK OK")
    return code


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("--engagement", default=".")
    parser.add_argument("--slices", type=int, default=DEFAULTS["slices_max"])
    parser.add_argument("--wall", type=int, default=DEFAULTS["wall_s"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-adjudicate", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--plan-n", dest="plan_n", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--netns", action="store_true")
    parser.add_argument("--netns-selfcheck", dest="netns_selfcheck", action="store_true")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    parser.add_argument("--max-cost", dest="max_cost", type=float, default=None)
    args = parser.parse_args(argv)
    if args.netns_selfcheck:
        return netns_selfcheck()
    repo = hx.Repo(Path(args.engagement).resolve())
    pid_file = repo.hunt / "runner.pid"
    if pid_file.exists():
        try:
            other_pid = int(pid_file.read_text().strip())
        except ValueError:
            other_pid = None
        if other_pid is not None:
            try:
                os.kill(other_pid, 0)
            except ProcessLookupError:
                other_pid = None
            except PermissionError:
                pass
        if other_pid is not None:
            print(f"runner: ja existe runner vivo (pid {other_pid})")
            return 1
    if args.plan and args.workers > 1:
        print("runner: --plan nao combina com --workers > 1")
        return 2
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    if args.netns and os.environ.get("HX_NETNS") != "1":
        env = dict(os.environ, HX_NETNS="1")
        os.execvpe("unshare", ["unshare", "-Ur", "--", sys.executable, str(Path(__file__).resolve()), *raw_argv], env)
    if args.netns:
        problems = netns_preflight()
        if problems:
            print("runner: --netns indisponivel:")
            for problem in problems:
                print(f"  - {problem}")
            return 2
    problems = workers_gate(repo, args.workers)
    if problems:
        print("runner: gate de workers recusou:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    (repo.hunt / "runner.stop").unlink(missing_ok=True)
    os.environ["HX_ENGAGEMENT"] = str(repo.eng)
    owner = f"runner-{os.getpid()}"
    os.environ["HX_SESSION"] = owner
    budget = {**DEFAULTS, "slices_max": args.slices, "wall_s": args.wall, "token_cap": args.max_tokens, "cost_cap": args.max_cost}
    hx.TTL_CLAIM_S = claim_ttl_for(budget)
    config = os.environ.get("RUNNER_CONFIG_CONTENT") or build_config_content(REPO_ROOT / "opencode" / "agents" / "hunt-auto.md", plan=args.plan)
    ctx = {
        "repo": repo, "hyp": None, "brief": "", "config": config, "owner": owner,
        "model": args.model, "budget": budget, "started_ts": hx.now(),
        "no_adjudicate": args.no_adjudicate, "plan_n": args.plan_n,
        "netns": args.netns,
    }
    try:
        write_pid(repo)
        install_signals(ctx)
        if args.plan:
            run_plan(ctx)
            return 0
        runner_state_init(repo)
        if args.netns or args.workers > 1:
            reasons = run_workers(ctx, max(args.workers, 1))
            for reason in reasons:
                print(f"runner: worker parada ({reason})")
            if "crashed" in reasons:
                return 1
        else:
            print(f"runner: parada ({worker_loop(ctx)})")
    finally:
        (repo.hunt / "runner.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(runner_main())
