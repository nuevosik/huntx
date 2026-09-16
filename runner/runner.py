#!/usr/bin/env python3
import argparse
import importlib.machinery
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
    return any(marker in lowered for marker in ("rate", "429", "waf", "captcha"))


def retry_invoke(ctx, prompt):
    attempts = 0
    session = None
    budget = ctx.get("budget") or DEFAULTS
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
    if not ctx.get("no_adjudicate"):
        record["adjudication"] = adjudicate(repo, hyp["id"])
    record_run(repo, record)
    return record


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
        "no_adjudicate": args.no_adjudicate,
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
