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
    proc = subprocess.run(["opencode", "export", session_id], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return {}
    usage = data.get("usage") or {}
    return {"tokens": usage.get("tokens") or usage.get("total_tokens"), "cost": usage.get("cost")}


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
