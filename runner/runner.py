#!/usr/bin/env python3
import argparse
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
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
    path = Path(repo_root) / "bin" / "hx"
    loader = importlib.machinery.SourceFileLoader("hx", str(path))
    spec = importlib.util.spec_from_file_location("hx", path, loader=loader)
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
- Para `confirmed`: monte o draft em `hunt/FINDINGS/drafts/{id}.json` (cenário differential/echo/callback conforme o caso) e rode `hx verify {id}`; só então `hx result {id} --verdict confirmed`.
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
    argv = ["opencode", "run", "--format", "json"]
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
            ok, sig = hx_mod.probe_once(repo, scope, tag)
            declared = hx_mod.health_probe_result(repo, tag, ok, sig)
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


def runner_main(argv=None):
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("--engagement", default=".")
    parser.add_argument("--slices", type=int, default=DEFAULTS["slices_max"])
    parser.add_argument("--wall", type=int, default=DEFAULTS["wall_s"])
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-adjudicate", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--plan-n", dest="plan_n", type=int, default=5)
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    parser.add_argument("--max-cost", dest="max_cost", type=float, default=None)
    args = parser.parse_args(argv)
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
    (repo.hunt / "runner.stop").unlink(missing_ok=True)
    os.environ["HX_ENGAGEMENT"] = str(repo.eng)
    owner = f"runner-{os.getpid()}"
    os.environ["HX_SESSION"] = owner
    budget = {**DEFAULTS, "slices_max": args.slices, "wall_s": args.wall, "token_cap": args.max_tokens, "cost_cap": args.max_cost}
    config = os.environ.get("RUNNER_CONFIG_CONTENT") or build_config_content(REPO_ROOT / "opencode" / "agents" / "hunt-auto.md", plan=args.plan)
    ctx = {
        "repo": repo, "hyp": None, "brief": "", "config": config, "owner": owner,
        "model": args.model, "budget": budget, "started_ts": hx.now(), "slices_done": 0, "tokens_used": 0, "cost_used": 0,
        "no_adjudicate": args.no_adjudicate, "plan_n": args.plan_n,
    }
    try:
        write_pid(repo)
        install_signals(ctx)
        if args.plan:
            run_plan(ctx)
            return 0
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
                hx.main(["result", hyp["id"], "--verdict", "blocked", "--note", f"sessao {tag} morta"])
                ctx["slices_done"] += 1
                continue
            ctx["hyp"] = hyp
            record = run_slice(ctx)
            ctx["slices_done"] += 1
            ctx["tokens_used"] += record.get("tokens") or 0
            ctx["cost_used"] += record.get("cost") or 0
    finally:
        (repo.hunt / "runner.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(runner_main())
