"""Prova de premissa do huntx: rede real (loopback), processos reais do CLI, PTY real.

Nenhum mock: servidor HTTP local com dois usuários, sessões em arquivo, cada comando
`hx` roda como processo separado (amnesia entre invocações) e o terminal é um pty
compartilhado entre os comandos da "mesma sessão de operador".
"""

import hashlib
import html
import http.server
import json
import os
import pty
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

HX = Path(__file__).resolve().parent.parent / "bin" / "hx"

VALID = {"victim", "attacker"}
OWNER = {"42": "victim", "7": "attacker"}


class App(http.server.BaseHTTPRequestHandler):
    rate_hits = 0
    posts = []
    poll_calls = 0

    def _sid(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sid":
                return value
        return ""

    def _send(self, status, body, ctype="application/json", headers=None):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        sid = self._sid()
        if path == "/whoami":
            if sid in VALID:
                return self._send(200, json.dumps({"sid": sid}))
            return self._send(401, json.dumps({"error": "login required"}))
        if path.startswith("/obj/"):
            oid = path.rsplit("/", 1)[-1]
            return self._send(200, json.dumps({"owner": OWNER.get(oid, "none"), "sid": sid}))
        if path in ("/raw", "/esc"):
            value = urllib.parse.parse_qs(query).get("q", [""])[0]
            body = value if path == "/raw" else html.escape(value)
            return self._send(200, f"<html><body>{body}</body></html>", "text/html")
        if path == "/rate":
            App.rate_hits += 1
            if App.rate_hits > 2:
                return self._send(429, '{"error":"slow down"}', headers={"Retry-After": "30"})
            return self._send(200, json.dumps({"hits": App.rate_hits}))
        if path == "/waf":
            return self._send(200, "<html><script src='/_px/abc'></script>px-captcha</html>", "text/html")
        if path == "/hits":
            App.poll_calls += 1
            if App.poll_calls == 1:
                return self._send(200, "[]")
            return self._send(200, json.dumps([{"uuid": "hit-1", "path": "/cb"}]))
        if path == "/secret":
            return self._send(200, json.dumps({"email": "alvo@example.com", "token": "AKIAIOSFODNN7EXAMPLE1234"}), headers={"Set-Cookie": "sid=supersecretcookieval123; Path=/"})
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        App.posts.append({"path": self.path, "body": body, "sid": self._sid()})
        return self._send(200, json.dumps({"posted": True, "path": self.path}))

    def log_message(self, *args):
        pass


class PremiseE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        App.rate_hits = 0
        App.posts = []
        App.poll_calls = 0
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), App)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        App.rate_hits = 0
        App.posts = []
        App.poll_calls = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HX_") and k != "TYPESAFE_API_KEY"}
        self.env["HX_ENGAGEMENT"] = str(self.eng)
        self.master, self.slave = pty.openpty()
        self.assert_rc(self.run_cli("init", "--dir", "."), 0)
        (self.eng / "recon").mkdir(exist_ok=True)
        for tag, sid in (("victim", "victim"), ("dead", "expired")):
            (self.eng / "recon" / f"session_{tag}.json").write_text(json.dumps([{"name": "sid", "value": sid, "domain": "127.0.0.1", "path": "/"}]))
        self.write_scope()

    def tearDown(self):
        os.close(self.slave)
        os.close(self.master)
        self.tmp.cleanup()

    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def write_scope(self, **overrides):
        scope = {
            "engagement": "premissa-e2e",
            "in_scope": ["127.0.0.1"],
            "out_of_scope_hosts": ["169.254.169.254"],
            "out_of_scope_paths": ["/admin"],
            "netns": {"allow_hosts": []},
            "allow_extra_hosts": [],
            "rate": {"per_host_interval_s": 0, "global_interval_s": 0, "max_wait_s": 5, "adaptive": True, "step_up_max_s": 30, "decay_clean_min": 60, "burst": [6, 10]},
            "health": {
                "probes": [
                    {"session": "victim", "file": "recon/session_victim.json", "method": "GET", "url": f"{self.base()}/whoami"},
                    {"session": "dead", "file": "recon/session_dead.json", "method": "GET", "url": f"{self.base()}/whoami"},
                ],
                "fresh_max_s": 600,
                "session_invalid_if": ["status:401"],
                "waf_block_if": ["body_contains:px-captcha"],
                "rate_limited_if": ["status:429"],
            },
            "jev": {"enabled": False},
            "allowed_mutations": ["POST /mut"],
            "evidence_dir": "scans/evidence",
        }
        scope.update(overrides)
        (self.eng / "hunt" / "scope.json").write_text(json.dumps(scope))

    def run_cli(self, *args, feed=None, timeout=60, tty=True):
        if feed is not None:
            os.write(self.master, (feed + "\n").encode())
        stdin = self.slave if tty else subprocess.DEVNULL
        return subprocess.run([str(HX), *args], cwd=str(self.eng), stdin=stdin, capture_output=True, text=True, timeout=timeout, env=self.env)

    def assert_rc(self, proc, expected):
        self.assertEqual(proc.returncode, expected, f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")

    def add_hyp(self, claim="BOLA em /obj", endpoint="GET /obj/{id}", cls="BOLA", tag=None):
        args = ["hypothesis", "add", "--claim", claim, "--endpoint", endpoint, "--class", cls, "--confirm", "objeto de outro usuario", "--refute", "403"]
        if tag:
            args += ["--session-tag", tag]
        proc = self.run_cli(*args)
        self.assert_rc(proc, 0)
        return proc.stdout.strip()

    def write_draft(self, text):
        path = self.eng / "hunt" / "FINDINGS" / "drafts" / "h001.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if isinstance(text, str) else json.dumps(text))

    def load_json(self, rel):
        return json.loads((self.eng / rel).read_text())

    def test_premise_state_lives_outside_the_model(self):
        claimed = self.add_hyp()
        open_hyp = self.add_hyp("segunda hipotese aberta", "GET /obj/{id}/notes", "BOLA")
        self.assert_rc(self.run_cli("next", "1"), 0)
        hyps = self.load_json("hunt/HYPOTHESES.json")
        self.assertEqual((hyps[0]["status"], hyps[1]["status"]), ("claimed", "open"))
        brief = self.run_cli("brief")
        self.assert_rc(brief, 0)
        self.assertIn("hipoteses abertas", brief.stdout)
        self.assertIn(open_hyp, brief.stdout)
        self.assertNotIn(claimed, brief.stdout)
        peek = self.run_cli("next", "--peek")
        self.assert_rc(peek, 0)
        self.assertIn(open_hyp, peek.stdout)
        self.assertEqual(self.load_json("hunt/HYPOTHESES.json")[0]["status"], "claimed")

    def test_premise_work_order_closes_from_another_process(self):
        hid = self.add_hyp()
        self.assert_rc(self.run_cli("next", "1"), 0)
        result = self.run_cli("result", hid, "--verdict", "blocked", "--note", "cooldown do alvo")
        self.assert_rc(result, 0)
        hyps = self.load_json("hunt/HYPOTHESES.json")
        self.assertEqual(hyps[0]["status"], "result")
        self.assertEqual(hyps[0]["result"]["verdict"], "blocked")
        self.assert_rc(self.run_cli("debrief"), 0)
        sessions = sorted((self.eng / "hunt" / "sessions").glob("*.md"))
        self.assertEqual(len(sessions), 1)
        self.assertIn("blocked", sessions[0].read_text())

    def test_premise_two_operators_do_not_steal_claims(self):
        hid = self.add_hyp()
        self.assert_rc(self.run_cli("next", "1"), 0)
        other_master, other_slave = pty.openpty()
        try:
            proc = subprocess.run([str(HX), "result", hid, "--verdict", "blocked", "--note", "x"], cwd=str(self.eng), stdin=other_slave, capture_output=True, text=True, env=self.env)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("dono", proc.stderr)
        finally:
            os.close(other_slave)
            os.close(other_master)

    def test_premise_finding_only_exists_when_verified(self):
        self.add_hyp()
        self.assert_rc(self.run_cli("result", "h001", "--verdict", "confirmed", "--note", "sem prova"), 2)
        self.write_draft({
            "id": "h001",
            "title": "BOLA em /obj",
            "scenario": "differential",
            "request": {"method": "GET", "url": f"{self.base()}/obj/{{id}}", "victim_object": {"id": "42"}},
            "victim_session": "recon/session_victim.json",
            "attacker_session": "recon/session_attacker.json",
            "control": {"attacker_own_object": {"id": "7"}},
            "marker": {"extract": '"owner": "(\\w+)"'},
        })
        (self.eng / "recon" / "session_attacker.json").write_text(json.dumps([{"name": "sid", "value": "attacker", "domain": "127.0.0.1", "path": "/"}]))
        verify = self.run_cli("verify", "h001")
        self.assert_rc(verify, 0)
        self.assertIn("PASS", verify.stdout)
        self.assertTrue((self.eng / "hunt" / "FINDINGS" / "h001.json").exists())
        self.assertFalse((self.eng / "hunt" / "FINDINGS" / "drafts" / "h001.json").exists())
        steps = sorted((self.eng / "hunt" / "FINDINGS" / "drafts" / "h001").glob("*.json"))
        self.assertEqual(len(steps), 3)
        self.assert_rc(self.run_cli("result", "h001", "--verdict", "confirmed", "--note", "BOLA real", "--evidence", "scans/evidence/h001"), 0)
        coverage = (self.eng / "hunt" / "COVERAGE.md").read_text()
        self.assertIn("| GET /obj/{id} | BOLA | confirmed |", coverage)

    def test_premise_echo_needs_human_attestation(self):
        self.add_hyp("XSS refletido em /raw", "GET /raw?q=", "XSS")
        self.write_draft({
            "id": "h001",
            "title": "XSS",
            "scenario": "echo",
            "request": {"method": "GET", "url": f"{self.base()}/raw?q={{payload}}"},
            "payload_template": "<hx{CANARY}>",
        })
        verify = self.run_cli("verify", "h001")
        self.assert_rc(verify, 0)
        self.assertIn("exige atestacao", verify.stdout)
        self.assertFalse((self.eng / "hunt" / "FINDINGS" / "h001.json").exists())
        self.assertTrue(self.load_json("hunt/FINDINGS/drafts/h001.json")["pass_requires_attest"])
        attest = self.run_cli("verify", "h001", "--attest", feed="h001")
        self.assert_rc(attest, 0)
        finding = self.load_json("hunt/FINDINGS/h001.json")
        self.assertEqual(finding["verdict"], "verified-manual")
        self.assert_rc(self.run_cli("result", "h001", "--verdict", "confirmed", "--note", "assinado"), 0)

    def test_premise_echo_that_escapes_is_refuted(self):
        self.add_hyp("XSS refletido em /esc", "GET /esc?q=", "XSS")
        self.write_draft({
            "id": "h001",
            "title": "XSS",
            "scenario": "echo",
            "request": {"method": "GET", "url": f"{self.base()}/esc?q={{payload}}"},
            "payload_template": "<hx{CANARY}>",
        })
        verify = self.run_cli("verify", "h001")
        self.assert_rc(verify, 0)
        self.assertIn("FAIL", verify.stdout)
        draft = self.load_json("hunt/FINDINGS/drafts/h001.json")
        self.assertEqual(draft["fail_reason"], "reflection_absent_or_escaped")
        self.assert_rc(self.run_cli("result", "h001", "--verdict", "refuted", "--note", "escapa"), 0)

    def test_premise_callback_requires_registered_collaborator(self):
        self.add_hyp("SSRF via callback", "GET /cb?u=", "SSRF")
        self.write_draft({
            "id": "h001",
            "title": "SSRF",
            "scenario": "callback",
            "request": {"method": "GET", "url": f"{self.base()}/raw?q={{collaborator}}"},
            "collaborator": {"url": "http://nao-registrado.tld:9/", "poll": {"url": "http://nao-registrado.tld:9/hits"}},
            "interaction_timeout_s": 2,
            "poll_interval_s": 1,
        })
        proc = self.run_cli("verify", "h001", timeout=30)
        self.assert_rc(proc, 2)
        self.assertIn("escopo", proc.stderr)

    def test_premise_callback_interaction_promotes(self):
        self.add_hyp("SSRF via callback", "GET /cb?u=", "SSRF")
        self.write_draft({
            "id": "h001",
            "title": "SSRF",
            "scenario": "callback",
            "request": {"method": "GET", "url": f"{self.base()}/raw?q={{collaborator}}"},
            "collaborator": {"url": f"{self.base()}/cb", "poll": {"url": f"{self.base()}/hits"}},
            "interaction_timeout_s": 5,
            "poll_interval_s": 1,
        })
        verify = self.run_cli("verify", "h001", timeout=60)
        self.assert_rc(verify, 0)
        self.assertIn("PASS", verify.stdout)
        self.assertTrue((self.eng / "hunt" / "FINDINGS" / "h001.json").exists())

    def test_premise_dead_session_blocks_refutation(self):
        hid = self.add_hyp("403 por sessao", "GET /whoami", "authz", tag="dead")
        run = self.run_cli("run", "GET", f"{self.base()}/whoami", "--session", "recon/session_dead.json", "--save", f"scans/evidence/{hid}")
        self.assert_rc(run, 0)
        self.assertIn("401", run.stdout)
        for _ in range(2):
            self.assert_rc(self.run_cli("health", "check", "--session", "dead"), 0)
        health = self.load_json("hunt/.health.json")
        self.assertTrue(health["dead"]["dead_since"])
        refute = self.run_cli("result", hid, "--verdict", "refuted", "--note", "401")
        self.assert_rc(refute, 6)
        self.assertIn("morta", refute.stderr)
        self.assert_rc(self.run_cli("result", hid, "--verdict", "blocked", "--note", "sessao morta"), 0)

    def test_premise_dead_session_reopens_recent_refutations(self):
        self.add_hyp("refutada antes da morte", "GET /whoami", "authz", tag="dead")
        self.assert_rc(self.run_cli("health", "check", "--session", "dead"), 0)
        hyps = self.load_json("hunt/HYPOTHESES.json")
        hyps[0].update({"status": "result", "owner": None, "closed_ts": time.time(), "result": {"verdict": "refuted", "note": "401", "evidence": "scans/evidence/h001"}})
        (self.eng / "hunt" / "HYPOTHESES.json").write_text(json.dumps(hyps))
        self.add_hyp("tentativa atual", "GET /whoami2", "authz", tag="dead")
        run = self.run_cli("run", "GET", f"{self.base()}/whoami", "--session", "recon/session_dead.json", "--save", "scans/evidence/h002")
        self.assert_rc(run, 0)
        refute = self.run_cli("result", "h002", "--verdict", "refuted", "--note", "401")
        self.assert_rc(refute, 6)
        self.assertIn("reabertas: h001", refute.stderr)
        reopened = self.load_json("hunt/HYPOTHESES.json")[0]
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["reopened_reason"], "session_death_window")

    def test_premise_mutation_is_staged_and_only_dj_executes(self):
        App.posts = []
        staged = self.run_cli("run", "POST", f"{self.base()}/obj/42", "--data", "x=1")
        self.assert_rc(staged, 5)
        self.assertIn("staged", staged.stdout)
        self.assertEqual(App.posts, [])
        pending = self.run_cli("pending", "list").stdout.strip().split()[0]
        self.assert_rc(self.run_cli("pending", "run", pending, tty=False), 7)
        wrong = self.run_cli("pending", "run", pending, feed="outro.host")
        self.assert_rc(wrong, 2)
        self.assertEqual(App.posts, [])
        executed = self.run_cli("pending", "run", pending, feed="127.0.0.1")
        self.assert_rc(executed, 0)
        self.assertEqual(len(App.posts), 1)
        self.assertEqual(len(list((self.eng / "hunt" / "pending").glob("*.json"))), 0)
        self.assertTrue(list((self.eng / "scans" / "evidence" / "_pending").glob("*.json")))
        allowed = self.run_cli("run", "POST", f"{self.base()}/mut", "--data", "x=1")
        self.assert_rc(allowed, 0)
        self.assertEqual(len(App.posts), 2)

    def test_premise_guard_blocks_out_of_scope_and_banned_paths(self):
        self.assert_rc(self.run_cli("run", "GET", "https://169.254.169.254/latest/meta-data/"), 2)
        self.assert_rc(self.run_cli("run", "GET", f"{self.base()}/admin"), 2)
        for sneaky in ("//admin", "/./admin", "/x/../admin", "/%61dmin"):
            proc = self.run_cli("run", "GET", f"{self.base()}{sneaky}")
            self.assertEqual(proc.returncode, 2, f"{sneaky} passou o guard: {proc.stdout}")

    def test_premise_evidence_is_redacted_at_capture(self):
        run = self.run_cli("run", "GET", f"{self.base()}/secret", "--marker", '"token": "([^"]+)"')
        self.assert_rc(run, 0)
        files = sorted((self.eng / "scans" / "evidence" / "_misc").glob("*.json"))
        self.assertEqual(len(files), 1)
        record = json.loads(files[0].read_text())
        raw = files[0].read_text()
        self.assertRegex(record["response"]["headers"]["set-cookie"], r"^sha256:[0-9a-f]{12}$")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE1234", record["response"]["body"])
        self.assertIn("[email-redacted]", record["response"]["body"])
        self.assertEqual(record["proof"]["marker"], "AKIAIOSFODNN7EXAMPLE1234")
        self.assertIn("AKIAIOSFODNN7EXAMPLE1234", raw)
        self.assertEqual(record["integrity"]["body_len"], 66)
        self.assertEqual(record["integrity"]["raw_sha256"], hashlib.sha256(f'{{"email": "alvo@example.com", "token": "AKIAIOSFODNN7EXAMPLE1234"}}'.encode()).hexdigest())

    def test_premise_rate_steps_up_and_waf_cools_down(self):
        for _ in range(3):
            self.run_cli("run", "GET", f"{self.base()}/rate")
        state = self.load_json("hunt/.ratelimit.json")
        self.assertGreaterEqual(state["effective"]["127.0.0.1"], 1.0)
        self.assert_rc(self.run_cli("run", "GET", f"{self.base()}/waf"), 0)
        state = self.load_json("hunt/.ratelimit.json")
        self.assertIn("127.0.0.1", state["cooldowns"])
        blocked = self.run_cli("run", "GET", f"{self.base()}/obj/42")
        self.assert_rc(blocked, 3)
        self.assertIn("cooldown", blocked.stderr)
        self.assert_rc(self.run_cli("rate", "reset"), 0)
        self.assert_rc(self.run_cli("run", "GET", f"{self.base()}/obj/42"), 0)

    def test_premise_health_check_without_probes_fails_clearly(self):
        self.write_scope(**{"health": {"probes": [], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}})
        proc = self.run_cli("health", "check")
        self.assert_rc(proc, 2)
        self.assertIn("probe", proc.stderr)

    def test_premise_partial_scope_config_uses_defaults(self):
        scope = json.loads((self.eng / "hunt" / "scope.json").read_text())
        del scope["rate"]
        del scope["health"]
        (self.eng / "hunt" / "scope.json").write_text(json.dumps(scope))
        proc = self.run_cli("run", "GET", f"{self.base()}/obj/42")
        self.assert_rc(proc, 0)
        self.assertNotIn("Traceback", proc.stderr)
        health = self.run_cli("health", "check")
        self.assert_rc(health, 2)
        self.assertIn("probe", health.stderr)
        self.assertNotIn("Traceback", health.stderr)


if __name__ == "__main__":
    unittest.main()
