import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "runner" / "runner.py"

def load_runner():
    spec = importlib.util.spec_from_file_location("runner_mod", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

runner = load_runner()

class SliceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "BOLA x", "--endpoint", "GET /a/{id}", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        self.orig_invoke = runner.invoke_opencode
        self.orig_export = runner.export_usage
        self.orig_adjudicate = runner.adjudicate
        self.orig_run = runner.subprocess.run
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (0, '{"sessionID":"ses_test"}\n', "")
        runner.export_usage = lambda hx_mod, session_ref, runtime="opencode": {"tokens": 123, "cost": 0.01}

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        runner.export_usage = self.orig_export
        runner.adjudicate = self.orig_adjudicate
        runner.subprocess.run = self.orig_run
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_claim_next_marks_owner(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        self.assertEqual(hyp["id"], "h001")
        self.assertEqual(hyp["owner"], "runner-test")

    def test_build_prompt_contains_work_order(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        prompt = runner.build_prompt(hyp, "BRIEF-DE-TESTE", 900)
        for needle in ("h001", "BOLA x", "confirm", "refute", "hx run", "hx result", "hx verify", "BRIEF-DE-TESTE"):
            self.assertIn(needle, prompt)
        self.assertIn("Nao rode `hx next`", prompt)
        self.assertIn("FECHAMENTO OBRIGATÓRIO", prompt)
        self.assertNotIn("Termine com um resumo curto", prompt)

    def test_run_slice_records_jsonl(self):
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertEqual(record["session"], "ses_test")
        self.assertEqual(record["tokens"], 123)
        self.assertEqual(record["adjudication"], {"ran": False})
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(lines[-1])["hyp"], "h001")

    def test_run_slice_records_worker(self):
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None, "worker": "a"})
        self.assertEqual(record["worker"], "a")

    def test_run_slice_forwards_owner_as_hx_session(self):
        captured = {}
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            captured.update(kwargs)
            return (0, '{"sessionID":"ses_test"}', "")
        runner.invoke_opencode = fake
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None, "owner": "runner-1"})
        self.assertEqual(captured.get("env_extra"), {"HX_SESSION": "runner-1"})

    def test_run_slice_without_owner_passes_none(self):
        captured = {}
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            captured.update(kwargs)
            return (0, '{"sessionID":"ses_test"}', "")
        runner.invoke_opencode = fake
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertIsNone(captured.get("env_extra"))

    def test_run_slice_marks_timeout(self):
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (124, "", "timeout")
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None, "owner": "runner-test"})
        self.assertTrue(record["timeout"])

    def test_run_slice_reconciles_unclosed_hypothesis(self):
        runner.adjudicate = lambda repo, hid: {"ran": False}
        hyp = runner.claim_next(self.repo, "runner-test")
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertEqual(record["reconciled"], "blocked")
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["status"], "result")
        self.assertEqual(hyps[0]["result"]["verdict"], "blocked")

    def test_run_slice_keeps_hypothesis_closed_by_worker(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        runner.hx.main(["result", "h001", "--verdict", "refuted", "--note", "ok", "--force"])
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertNotIn("reconciled", record)
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["status"], "result")
        self.assertEqual(hyps[0]["result"]["verdict"], "refuted")

    def test_run_slice_includes_adjudication(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        runner.hx.dump_json(path, {"id": "h001", "scenario": "differential"})
        def fake(argv, **kwargs):
            path.unlink()
            class P: returncode = 0; stdout = "PASS"; stderr = ""
            return P()
        runner.subprocess.run = fake
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None})
        self.assertEqual(record["adjudication"], {"ran": True, "rc": 0, "promoted": True})
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        self.assertTrue(json.loads(lines[-1])["adjudication"]["promoted"])

    def test_run_slice_no_adjudicate_skips_call(self):
        hyp = runner.claim_next(self.repo, "runner-test")
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        runner.hx.dump_json(path, {"id": "h001", "scenario": "differential"})
        def boom(repo, hid):
            raise AssertionError("adjudicate nao deveria rodar")
        runner.adjudicate = boom
        record = runner.run_slice({"repo": self.repo, "hx": runner.hx, "hyp": hyp, "brief": "b", "config": "{}", "model": None, "no_adjudicate": True})
        self.assertNotIn("adjudication", record)
        lines = (self.repo.hunt / "runs.jsonl").read_text().strip().splitlines()
        self.assertNotIn("adjudication", json.loads(lines[-1]))


class AdjudicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        self.orig_run = runner.subprocess.run

    def tearDown(self):
        runner.subprocess.run = self.orig_run
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def draft(self, hid="h001", **extra):
        path = self.repo.hunt / "FINDINGS" / "drafts" / f"{hid}.json"
        runner.hx.dump_json(path, {"id": hid, "scenario": "differential", **extra})
        return path

    def test_no_draft_skips_subprocess(self):
        called = []
        runner.subprocess.run = lambda *a, **k: called.append(a) or None
        self.assertEqual(runner.adjudicate(self.repo, "h001"), {"ran": False})
        self.assertEqual(called, [])

    def test_failed_draft_skips_subprocess(self):
        self.draft(failed_at=123)
        called = []
        runner.subprocess.run = lambda *a, **k: called.append(a) or None
        self.assertEqual(runner.adjudicate(self.repo, "h001"), {"ran": False, "skipped": "failed_draft"})
        self.assertEqual(called, [])

    def test_promoted_when_rc0_and_draft_moved(self):
        path = self.draft()
        def fake(argv, **kwargs):
            path.unlink()
            class P: returncode = 0; stdout = "PASS"; stderr = ""
            return P()
        runner.subprocess.run = fake
        result = runner.adjudicate(self.repo, "h001")
        self.assertTrue(result["promoted"])
        self.assertEqual(result["rc"], 0)

    def test_pending_attest_from_retained_flagged_draft(self):
        self.draft(pass_requires_attest=True)
        def fake(argv, **kwargs):
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        runner.subprocess.run = fake
        result = runner.adjudicate(self.repo, "h001")
        self.assertTrue(result["pending_attest"])
        self.assertFalse(result["promoted"])

    def test_rc0_retained_draft_without_flag_is_neither(self):
        self.draft()
        def fake(argv, **kwargs):
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        runner.subprocess.run = fake
        result = runner.adjudicate(self.repo, "h001")
        self.assertNotIn("pending_attest", result)
        self.assertFalse(result["promoted"])

    def test_verify_call_argv_cwd_timeout(self):
        path = self.draft()
        calls = []
        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            path.unlink()
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        runner.subprocess.run = fake
        runner.adjudicate(self.repo, "h001")
        argv, kwargs = calls[0]
        self.assertEqual(argv[:3], [str(runner.REPO_ROOT / "bin" / "hx"), "verify", "h001"])
        self.assertEqual(kwargs["cwd"], str(self.eng))
        self.assertEqual(kwargs["timeout"], 600)

    def test_exception_maps_to_error(self):
        self.draft()
        def boom(argv, **kwargs):
            raise OSError("boom")
        runner.subprocess.run = boom
        result = runner.adjudicate(self.repo, "h001")
        self.assertEqual(result, {"ran": True, "rc": None, "error": "boom"})


class ParseSessionIdTest(unittest.TestCase):
    def test_valid_after_noise(self):
        stdout = "banner\nnot json\n{\"x\":1}\nses_outra {\"sessionID\":\"ses_errado\"}\n{\"sessionID\":\"ses_x\"}\n"
        self.assertEqual(runner.parse_session_id(stdout), "ses_x")

    def test_json_without_key_returns_none(self):
        self.assertIsNone(runner.parse_session_id("{\"x\":1}\n{\"session_id\":null}\n"))

    def test_garbage_only_returns_none(self):
        self.assertIsNone(runner.parse_session_id("garbage\nnot-json\n"))


class InvokeEnvTest(unittest.TestCase):
    def setUp(self):
        self.orig_popen = runner.subprocess.Popen

    def tearDown(self):
        runner.subprocess.Popen = self.orig_popen

    def fake(self, calls):
        class FakeProc:
            pid = 4194304
            def __init__(self, argv, **kwargs):
                calls.append(kwargs)
                self.returncode = 0
            def communicate(self, timeout=None):
                return ("", "")
            def wait(self, timeout=None):
                return 0
        return FakeProc

    def test_env_extra_merges_after_config_content(self):
        calls = []
        runner.subprocess.Popen = self.fake(calls)
        runner.invoke_opencode("p", "/tmp", "{}", 30, env_extra={"HX_SESSION": "runner-1"})
        self.assertEqual(calls[0]["env"]["HX_SESSION"], "runner-1")
        self.assertEqual(calls[0]["env"]["OPENCODE_CONFIG_CONTENT"], "{}")
        self.assertTrue(calls[0]["start_new_session"])

    def test_without_env_extra_env_is_inherited(self):
        os.environ["HX_SESSION"] = "outer"
        try:
            calls = []
            runner.subprocess.Popen = self.fake(calls)
            runner.invoke_opencode("p", "/tmp", "{}", 30)
            self.assertEqual(calls[0]["env"]["HX_SESSION"], "outer")
        finally:
            os.environ.pop("HX_SESSION", None)


class ExportUsageTest(unittest.TestCase):
    PAYLOAD = {"info": {"tokens": {"input": 95534, "output": 3088, "reasoning": 13102, "cache": {"read": 1, "write": 2}}, "cost": 0.0308}, "messages": []}

    def setUp(self):
        self.orig_run = runner.subprocess.run

    def tearDown(self):
        runner.subprocess.run = self.orig_run

    def fake_export(self, payload, rc=0):
        def fake_run(argv, **kwargs):
            kwargs["stdout"].write(payload)
            class P: returncode = rc; stdout = ""; stderr = ""
            return P()
        return fake_run

    def test_export_usage_reads_info_tokens(self):
        runner.subprocess.run = self.fake_export(json.dumps(self.PAYLOAD))
        self.assertEqual(runner.export_usage(runner.hx, "ses_x"), {"tokens": 111724, "cost": 0.0308})

    def test_export_usage_truncated_json_returns_empty(self):
        runner.subprocess.run = self.fake_export(json.dumps(self.PAYLOAD)[:100])
        self.assertEqual(runner.export_usage(runner.hx, "ses_x"), {})

    def test_export_usage_non_dict_returns_empty(self):
        runner.subprocess.run = self.fake_export(json.dumps([1, 2, 3]))
        self.assertEqual(runner.export_usage(runner.hx, "ses_x"), {})

    def test_export_usage_nonzero_rc_returns_empty(self):
        runner.subprocess.run = self.fake_export(json.dumps(self.PAYLOAD), rc=1)
        self.assertEqual(runner.export_usage(runner.hx, "ses_x"), {})

    def test_export_usage_guards_subprocess_error(self):
        def boom(argv, **kwargs):
            raise FileNotFoundError("opencode")
        runner.subprocess.run = boom
        self.assertEqual(runner.export_usage(runner.hx, "ses_x"), {})

    def test_export_usage_empty_session(self):
        self.assertEqual(runner.export_usage(runner.hx, None), {})


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        self.sleeps = []
        self.orig_sleep = runner.hx.sleep
        runner.hx.sleep = lambda seconds: self.sleeps.append(seconds)
        self.orig_invoke = runner.invoke_opencode
        self.calls = []

    def tearDown(self):
        runner.hx.sleep = self.orig_sleep
        runner.invoke_opencode = self.orig_invoke
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def make_ctx(self):
        return {"repo": self.repo, "budget": dict(runner.DEFAULTS), "config": "{}", "model": None, "slice_timeout_s": 900, "started_ts": runner.hx.now()}

    def test_backoff_doubles_then_succeeds(self):
        results = [(1, "", "boom"), (1, "", "boom"), (0, '{"sessionID":"ses_x"}', "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            self.calls.append(kwargs)
            return results.pop(0)
        runner.invoke_opencode = fake
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual((rc, attempts), (0, 3))
        self.assertEqual(sessions, ["ses_x"])
        self.assertEqual(self.sleeps, [5, 10])

    def test_rate_failure_uses_rate_backoff(self):
        results = [(1, "", "HTTP 429 too many"), (0, "", "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            return results.pop(0)
        runner.invoke_opencode = fake
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.sleeps, [30])

    def test_resume_passes_session_after_first_failure(self):
        results = [(1, '{"sessionID":"ses_r"}', "boom"), (0, "", "")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            self.calls.append(kwargs.get("resume_session"))
            return results.pop(0)
        runner.invoke_opencode = fake
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.calls, [None, "ses_r"])

    def test_exhaustion_sleeps_only_between_attempts(self):
        results = [(1, "", "boom"), (1, "", "boom"), (1, "", "boom")]
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            return results.pop(0)
        runner.invoke_opencode = fake
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual((rc, attempts), (1, 3))
        self.assertEqual(self.sleeps, [5, 10])

    def test_rate_limit_marker_uses_rate_backoff(self):
        results = [(1, "", "you hit the rate limit"), (0, "", "")]
        runner.invoke_opencode = lambda *a, **kw: results.pop(0)
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.sleeps, [30])

    def test_plain_rate_word_is_not_rate_failure(self):
        results = [(1, "", "moderate failure"), (0, "", "")]
        runner.invoke_opencode = lambda *a, **kw: results.pop(0)
        runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(self.sleeps, [5])

    def test_stop_file_breaks_between_attempts(self):
        runner.request_stop(self.repo)
        runner.invoke_opencode = lambda *a, **kw: (1, "", "boom")
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual(attempts, 1)
        self.assertEqual(self.sleeps, [5])

    def test_wall_breaks_between_attempts(self):
        runner.invoke_opencode = lambda *a, **kw: (1, "", "boom")
        ctx = self.make_ctx()
        ctx["budget"] = {**runner.DEFAULTS, "wall_s": 0}
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(ctx, "p")
        self.assertEqual(attempts, 1)
        self.assertEqual(self.sleeps, [5])

    def test_timeout_not_retried(self):
        runner.invoke_opencode = lambda *a, **kw: (124, "", "timeout")
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(self.make_ctx(), "p")
        self.assertEqual((rc, attempts), (124, 1))
        self.assertEqual(self.sleeps, [])


class StopGateTest(unittest.TestCase):
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

    def ctx(self, **kw):
        base = {"repo": self.repo, "budget": dict(runner.DEFAULTS), "started_ts": 1000, "slices_done": 0, "tokens_used": 0, "cost_used": 0}
        base.update(kw)
        return base

    def test_stop_reasons(self):
        runner.runner_state_init(self.repo)
        runner.request_stop(self.repo)
        self.assertEqual(runner.check_stop(self.ctx()), "kill")
        (self.repo.hunt / "runner.stop").unlink()
        self.assertEqual(runner.check_stop(self.ctx(**{"budget": {**runner.DEFAULTS, "wall_s": 0}})), "wall")

    def test_health_gate_dead_session(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        runner.hx.health_probe_result(self.repo, "b", False, "invalid")
        runner.hx.health_probe_result(self.repo, "b", False, "invalid")
        alive, tag = runner.health_gate(runner.hx, self.repo, {"session_tag": "b"})
        self.assertFalse(alive)
        self.assertEqual(tag, "b")
        self.assertEqual(runner.health_gate(runner.hx, self.repo, {})[0], True)

    def test_token_and_cost_caps(self):
        runner.runner_state_init(self.repo)
        runner.runner_state_add(self.repo, tokens=10, cost=0.6)
        self.assertEqual(runner.check_stop(self.ctx(budget={**runner.DEFAULTS, "token_cap": 10})), "tokens")
        self.assertEqual(runner.check_stop(self.ctx(budget={**runner.DEFAULTS, "cost_cap": 0.5})), "cost")

    def test_health_gate_probe_error_treats_session_alive(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "x", "file": "recon/session_x.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        self.assertEqual(runner.health_gate(runner.hx, self.repo, {"session_tag": "b"}), (True, "b"))

    def test_health_gate_probes_when_stale_and_declares_death(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": ["status:401"], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        orig_fetch = runner.hx.fetch
        orig_sleep = runner.hx.sleep
        runner.hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (401, {}, "denied")
        runner.hx.sleep = lambda seconds: None
        try:
            hyp = {"session_tag": "b"}
            self.assertEqual(runner.health_gate(runner.hx, self.repo, hyp), (True, "b"))
            self.assertEqual(runner.health_gate(runner.hx, self.repo, hyp), (False, "b"))
        finally:
            runner.hx.fetch = orig_fetch
            runner.hx.sleep = orig_sleep
        self.assertTrue(runner.hx.health_get(self.repo, "b").get("dead_since"))


class SliceCeilingTest(unittest.TestCase):
    def setUp(self):
        self.orig_popen = runner.subprocess.Popen
        self.orig_killpg = runner.os.killpg

    def tearDown(self):
        runner.subprocess.Popen = self.orig_popen
        runner.os.killpg = self.orig_killpg

    def test_timeout_kills_process_group(self):
        killed = []
        class FakeProc:
            def __init__(self, argv, **kwargs):
                self.pid = 4321
                self.returncode = None
                self.calls = 0
            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise runner.subprocess.TimeoutExpired("opencode", timeout)
                return ("partial", "err")
        runner.subprocess.Popen = FakeProc
        runner.os.killpg = lambda pid, sig: killed.append((pid, sig))
        rc, out, err = runner.invoke_opencode("p", "/tmp", "{}", 30)
        self.assertEqual(rc, 124)
        self.assertEqual(set(killed), {(4321, runner.signal.SIGKILL)})
        self.assertEqual(out, "partial")
        self.assertEqual(runner._INFLIGHT, {})

    def test_timeout_survives_lookup_error_and_stuck_communicate(self):
        timeouts = []
        class FakeProc:
            def __init__(self, argv, **kwargs):
                self.pid = 9999
                self.returncode = None
            def communicate(self, timeout=None):
                timeouts.append(timeout)
                raise runner.subprocess.TimeoutExpired("opencode", timeout)
        def killpg(pid, sig):
            raise ProcessLookupError()
        runner.subprocess.Popen = FakeProc
        runner.os.killpg = killpg
        rc, out, err = runner.invoke_opencode("p", "/tmp", "{}", 30)
        self.assertEqual(rc, 124)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assertEqual(timeouts, [30, 10])


class ConfigContentTest(unittest.TestCase):
    def test_builds_inline_agent_from_md(self):
        config = json.loads(runner.build_config_content(runner.REPO_ROOT / "opencode" / "agents" / "hunt-auto.md"))
        self.assertEqual(config["default_agent"], "hunt-auto")
        agent = config["agent"]["hunt-auto"]
        self.assertEqual(agent["permission"]["bash"]["*"], "deny")
        self.assertEqual(agent["permission"]["bash"]["hx *"], "allow")
        self.assertIn("headless", agent["prompt"])
        self.assertNotIn("---", agent["prompt"][:3])

    def test_plan_config_is_least_privilege(self):
        config = json.loads(runner.build_config_content(runner.REPO_ROOT / "opencode" / "agents" / "hunt-auto.md", plan=True))
        agent = config["agent"]["hunt-auto"]
        self.assertEqual(agent["permission"]["edit"], "deny")
        self.assertEqual(agent["permission"]["bash"], {"*": "deny", "hx hypothesis add *": "allow"})


class RunnerMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def tearDown(self):
        runner.invoke_opencode = getattr(self, "orig_invoke", runner.invoke_opencode)
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_pid_removed_even_when_loop_raises(self):
        self.orig_invoke = runner.invoke_opencode
        def boom(*a, **kw):
            raise RuntimeError("boom")
        runner.invoke_opencode = boom
        with self.assertRaises(RuntimeError):
            runner.runner_main(["--engagement", str(self.eng), "--slices", "1"])
        self.assertFalse((self.repo.hunt / "runner.pid").exists())

    def test_refuses_when_runner_alive(self):
        self.orig_invoke = runner.invoke_opencode
        runner.invoke_opencode = lambda *a, **kw: (0, "", "")
        (self.repo.hunt / "runner.pid").write_text("4242")
        orig_kill = runner.os.kill
        runner.os.kill = lambda pid, sig: None
        try:
            self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--slices", "1"]), 1)
        finally:
            runner.os.kill = orig_kill
        self.assertTrue((self.repo.hunt / "runner.pid").exists())

    def test_claim_ttl_covers_full_slice(self):
        orig = runner.hx.TTL_CLAIM_S
        try:
            runner.hx.TTL_CLAIM_S = 1800
            self.assertEqual(runner.claim_ttl_for(runner.DEFAULTS), 3 * 900 + 300)
            runner.hx.TTL_CLAIM_S = 99999
            self.assertEqual(runner.claim_ttl_for(runner.DEFAULTS), 99999)
        finally:
            runner.hx.TTL_CLAIM_S = orig

    def test_runner_main_extends_claim_ttl(self):
        orig_ttl = runner.hx.TTL_CLAIM_S
        orig_invoke = runner.invoke_opencode
        runner.invoke_opencode = lambda *a, **kw: (0, "", "")
        try:
            self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--slices", "1"]), 0)
            self.assertGreaterEqual(runner.hx.TTL_CLAIM_S, 3000)
            self.assertGreaterEqual(int(os.environ["HX_CLAIM_TTL_S"]), 3000)
        finally:
            runner.hx.TTL_CLAIM_S = orig_ttl
            runner.invoke_opencode = orig_invoke
            os.environ.pop("HX_CLAIM_TTL_S", None)

    def test_runner_main_returns_1_on_crashed_worker(self):
        orig_gate = runner.workers_gate
        orig_run_workers = runner.run_workers
        runner.workers_gate = lambda repo, workers: []
        runner.run_workers = lambda ctx, workers: ["crashed", "empty_queue"]
        try:
            self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--workers", "2"]), 1)
        finally:
            runner.workers_gate = orig_gate
            runner.run_workers = orig_run_workers
        self.assertFalse((self.repo.hunt / "runner.pid").exists())


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
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION", "HX_RUN_ID"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_digest_contains_queue_and_caps(self):
        digest = runner.build_plan_digest(self.repo, 3)
        self.assertIn("h001", digest)
        self.assertIn("3", digest)

    def test_digest_caps_refuted_lines(self):
        rows = [f"| GET /e{i} | BOLA | refuted | ev | n |" for i in range(150)]
        (self.repo.coverage).write_text("| endpoint | classe | status | evidência | nota |\n|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
        digest = runner.build_plan_digest(self.repo, 3)
        section = digest.split("## Já refutadas (endpoint | classe)")[1]
        self.assertEqual(len([line for line in section.splitlines() if line.startswith("- ")]), 100)

    def test_runner_main_plan_uses_least_privilege_config(self):
        captured = {}
        def fake(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            captured["config"] = config_content
            return (0, '{"sessionID":"ses_plan"}', "")
        runner.invoke_opencode = fake
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--plan"]), 0)
        agent = json.loads(captured["config"])["agent"]["hunt-auto"]
        self.assertEqual(agent["permission"]["edit"], "deny")
        self.assertEqual(agent["permission"]["bash"], {"*": "deny", "hx hypothesis add *": "allow"})

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

    def test_runner_main_sets_run_id(self):
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (0, "", "")
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--plan"]), 0)
        run_id = os.environ.get("HX_RUN_ID", "")
        self.assertTrue(run_id.startswith(f"runner-{self.eng.name}-"), run_id)


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


class ClaimPriorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "p", "--endpoint", "GET /p", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--source", "planner"])
        runner.hx.main(["hypothesis", "add", "--claim", "m", "--endpoint", "GET /m", "--class", "BOLA", "--confirm", "c", "--refute", "r"])

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_claim_prefers_operator_over_planner(self):
        first = runner.claim_next(self.repo, "w")
        second = runner.claim_next(self.repo, "w")
        self.assertEqual(first["id"], "h002")
        self.assertEqual(second["id"], "h001")

    def test_claim_orders_by_priority_within_group(self):
        runner.hx.main(["hypothesis", "add", "--claim", "m2", "--endpoint", "GET /m2", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        hyps, _ = runner.hx.load_hyps(self.repo)
        by_id = {hyp["id"]: hyp for hyp in hyps}
        by_id["h002"]["priority"] = 0.1
        by_id["h003"]["priority"] = 2.0
        runner.hx.save_hyps(self.repo, hyps)
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h003")
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h002")

    def test_claim_operator_beats_planner_priority(self):
        hyps, _ = runner.hx.load_hyps(self.repo)
        hyps[0]["priority"] = 9.0
        hyps[1]["priority"] = 0.5
        runner.hx.save_hyps(self.repo, hyps)
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h002")

    def test_claim_ties_keep_fifo(self):
        runner.hx.main(["hypothesis", "add", "--claim", "m2", "--endpoint", "GET /m2", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        hyps, _ = runner.hx.load_hyps(self.repo)
        for hyp in hyps:
            hyp["priority"] = 1.0
        runner.hx.save_hyps(self.repo, hyps)
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h002")
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h003")
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h001")

    def test_claim_tolerates_malformed_priority(self):
        hyps, _ = runner.hx.load_hyps(self.repo)
        hyps[1]["priority"] = "x"
        runner.hx.save_hyps(self.repo, hyps)
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h002")
        self.assertEqual(runner.claim_next(self.repo, "w")["id"], "h001")


class PriorityPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "a", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "c", "--refute", "r"])
        runner.hx.main(["hypothesis", "add", "--claim", "b", "--endpoint", "GET /b", "--class", "IDOR", "--confirm", "c", "--refute", "r"])
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["jev"] = {"enabled": True}
        scope_path.write_text(json.dumps(scope))
        self.orig_invoke = runner.invoke_opencode
        self.orig_ask = runner.hx.jev_ask
        runner.invoke_opencode = lambda prompt, cwd, config_content, timeout_s, model=None, **kwargs: (0, "", "")

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        runner.hx.jev_ask = self.orig_ask
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_SESSION", "HX_RUN_ID"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def ctx(self):
        return {"repo": self.repo, "config": "{}", "model": None, "slice_timeout_s": 900, "owner": "runner-test", "budget": dict(runner.DEFAULTS), "started_ts": 1000}

    def test_run_plan_scores_open_hyps(self):
        def fake_invoke(prompt, cwd, config_content, timeout_s, model=None, **kwargs):
            runner.hx.main(["hypothesis", "add", "--claim", "p", "--endpoint", "GET /p", "--class", "IDOR", "--confirm", "c", "--refute", "r", "--source", "planner"])
            return (0, "", "")
        runner.invoke_opencode = fake_invoke
        calls = []

        def fake_ask(repo, scope, use, state, questions, threshold):
            calls.append((use, state, questions))
            return {"value": {"type": "score", "score": 2.0, "confidence": 0.9}}

        runner.hx.jev_ask = fake_ask
        runner.run_plan(self.ctx())
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertEqual([hyp["priority"] for hyp in hyps], [2.0, 2.0, 2.0])
        self.assertEqual([hyp["priority_ts"] for hyp in hyps], [1000, 1000, 1000])
        priority_calls = [call for call in calls if call[0] == "priority"]
        self.assertEqual(len(priority_calls), 3)
        self.assertEqual(priority_calls[0][1]["claim"], "a")
        self.assertEqual(priority_calls[0][1]["class"], "BOLA")
        self.assertEqual(priority_calls[0][2]["value"]["criteria"], ["baixo", "medio", "alto"])

    def test_run_plan_skips_scored_and_non_open(self):
        hyps, _ = runner.hx.load_hyps(self.repo)
        hyps[0]["priority"] = 1.0
        hyps[0]["priority_ts"] = 999
        hyps[1]["status"] = "claimed"
        runner.hx.save_hyps(self.repo, hyps)
        calls = []
        runner.hx.jev_ask = lambda *args, **kwargs: calls.append(1) or {"value": {"score": 2.5}}
        runner.run_plan(self.ctx())
        self.assertEqual(calls, [])
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["priority"], 1.0)
        self.assertEqual(hyps[0]["priority_ts"], 999)
        self.assertNotIn("priority", hyps[1])

    def test_run_plan_fail_open_when_jev_unavailable(self):
        runner.hx.jev_ask = lambda *args, **kwargs: None
        record = runner.run_plan(self.ctx())
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertNotIn("priority", hyps[0])
        self.assertNotIn("priority", hyps[1])
        self.assertEqual(record["mode"], "plan")

    def test_run_plan_disabled_does_not_call_jev(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["jev"] = {"enabled": False}
        scope_path.write_text(json.dumps(scope))
        runner.hx.jev_ask = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("nao deveria chamar"))
        runner.run_plan(self.ctx())
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertNotIn("priority", hyps[0])

    def test_run_plan_does_not_revert_concurrent_claim(self):
        calls = []

        def fake_ask(repo, scope, use, state, questions, threshold):
            calls.append(use)
            if len(calls) == 1:
                hyps, _ = runner.hx.load_hyps(self.repo)
                hyps[1]["status"] = "claimed"
                runner.hx.save_hyps(self.repo, hyps)
            return {"value": {"type": "score", "score": 2.0}}

        runner.hx.jev_ask = fake_ask
        runner.run_plan(self.ctx())
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertEqual(calls, ["priority", "priority"])
        self.assertEqual(hyps[1]["status"], "claimed")
        self.assertEqual(hyps[0]["priority"], 2.0)
        self.assertNotIn("priority", hyps[1])

    def test_run_plan_non_numeric_score_is_skipped(self):
        runner.hx.jev_ask = lambda *args, **kwargs: {"value": {"type": "score", "score": "alto"}}
        runner.run_plan(self.ctx())
        hyps, _ = runner.hx.load_hyps(self.repo)
        self.assertNotIn("priority", hyps[0])
        self.assertNotIn("priority", hyps[1])


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


class NetnsAllowTest(unittest.TestCase):
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

    def write_scope(self, **extra):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["alvo.com"]
        scope.update(extra)
        scope_path.write_text(json.dumps(scope))

    def make_config(self):
        cfg = Path(self.tmp.name) / "oc"
        cfg.mkdir()
        (cfg / "opencode.jsonc").write_text(json.dumps({
            "provider": {"prov": {}},
            "model": "prov/modelo-x",
            "mcp": {"bd": {"type": "remote", "url": "https://mcp.exemplo.dev/sse"}, "local": {"type": "local"}},
        }))
        cache = Path(self.tmp.name) / "models.json"
        cache.write_text(json.dumps({"prov": {"api": "https://api.provedor.dev/zen"}}))
        auth = Path(self.tmp.name) / "auth.json"
        auth.write_text(json.dumps({"prov": {"type": "api", "key": "x"}}))
        return cfg, cache, auth

    def test_hosts_in_text_extracts_and_normalizes(self):
        self.assertEqual(runner.hosts_in_text("GET https://Api.ALVO.com/x e 1.2.3.4"), {"api.alvo.com", "1.2.3.4"})
        self.assertEqual(runner.hosts_in_text("GET /rest/user"), set())

    def test_opencode_egress_hosts_from_config_and_cache(self):
        cfg, cache, auth = self.make_config()
        hosts = runner.opencode_egress_hosts(cfg, cache, auth)
        self.assertEqual(hosts, {"mcp.exemplo.dev", "api.provedor.dev"})

    def test_allow_hosts_fail_closed_without_provider(self):
        self.write_scope()
        cfg = Path(self.tmp.name) / "vazio"
        cfg.mkdir()
        (cfg / "opencode.jsonc").write_text("{}")
        with self.assertRaises(runner.hx.HxError):
            runner.netns_allow_hosts(self.repo, config_dir=cfg, models_cache=cfg / "m.json", auth_file=cfg / "a.json")

    def test_allow_hosts_merges_scope_endpoint_target_and_extras(self):
        self.write_scope(netns={"allow_hosts": ["extra.dev"]})
        cfg, cache, auth = self.make_config()
        (self.eng / "hunt" / "TARGET.md").write_text("alvo: api.alvo.com | fora: malicioso.net")
        hyp = {"endpoint": "GET https://login.alvo.com/x"}
        hosts = runner.netns_allow_hosts(self.repo, hyp, config_dir=cfg, models_cache=cache, auth_file=auth)
        self.assertIn("alvo.com", hosts)
        self.assertIn("login.alvo.com", hosts)
        self.assertIn("api.alvo.com", hosts)
        self.assertIn("api.provedor.dev", hosts)
        self.assertIn("api.typesafe.ai", hosts)
        self.assertIn("extra.dev", hosts)
        self.assertNotIn("malicioso.net", hosts)

    def test_resolve_ips_returns_all_a_records(self):
        addrs = runner.resolve_ips(["localhost"], resolver=lambda host: ["127.0.0.2", "127.0.0.3"])
        self.assertEqual(addrs, {"127.0.0.2", "127.0.0.3"})

    def test_resolve_ips_skips_failures(self):
        self.assertEqual(runner.resolve_ips(["nao-existe.invalid"], resolver=lambda host: (_ for _ in ()).throw(OSError())), set())


class NetnsRulesTest(unittest.TestCase):
    def test_ruleset_has_policy_drop_and_elements(self):
        text = runner.netns_ruleset({"1.2.3.4", "5.6.7.8"}, ["9.9.9.9"])
        self.assertIn("policy drop", text)
        self.assertIn("oif lo accept", text)
        self.assertIn("1.2.3.4", text)
        self.assertIn("5.6.7.8", text)
        self.assertIn("9.9.9.9", text)
        self.assertIn("udp dport 53 accept", text)

    def test_ruleset_without_resolvers_has_no_dns_rule(self):
        self.assertNotIn("dport 53", runner.netns_ruleset({"1.2.3.4"}, []))

    def test_ruleset_empty_ips_raises(self):
        with self.assertRaises(runner.hx.HxError):
            runner.netns_ruleset(set(), [])

    def test_apply_passes_ruleset_to_nft_stdin(self):
        calls = []
        orig = runner.subprocess.run
        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            class P: returncode = 0; stdout = ""; stderr = ""
            return P()
        runner.subprocess.run = fake
        try:
            runner.apply_netns_rules({"1.2.3.4"}, [])
        finally:
            runner.subprocess.run = orig
        self.assertEqual(calls[0][0][:3], ["nft", "-f", "-"])
        self.assertIn("policy drop", calls[0][1]["input"])

    def test_apply_raises_on_nft_failure(self):
        orig = runner.subprocess.run
        def fake(argv, **kwargs):
            class P: returncode = 1; stdout = ""; stderr = "boom"
            return P()
        runner.subprocess.run = fake
        try:
            with self.assertRaises(runner.hx.HxError):
                runner.apply_netns_rules({"1.2.3.4"}, [])
        finally:
            runner.subprocess.run = orig

    def test_preflight_lists_missing_tools(self):
        orig = runner.shutil.which
        runner.shutil.which = lambda name: None
        try:
            problems = runner.netns_preflight()
        finally:
            runner.shutil.which = orig
        self.assertTrue(any("backend" in problem for problem in problems))

    def test_agent_argv_wraps_only_under_netns(self):
        os.environ["HX_NETNS"] = "1"
        try:
            self.assertEqual(runner.agent_argv(["opencode", "run"]), ["setpriv", "--bounding-set=-net_admin,-net_raw", "--", "opencode", "run"])
        finally:
            os.environ.pop("HX_NETNS", None)
        self.assertEqual(runner.agent_argv(["opencode", "run"]), ["opencode", "run"])


class NetnsMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)
        runner.hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "x", "--refute", "y"])
        self.orig_exec = runner.os.execvpe
        self.orig_preflight = runner.netns_preflight
        self.orig_run_workers = runner.run_workers

    def tearDown(self):
        runner.os.execvpe = self.orig_exec
        runner.netns_preflight = self.orig_preflight
        runner.run_workers = self.orig_run_workers
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_NETNS"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_reexecs_into_userns_without_env_flag(self):
        captured = {}
        def fake_exec(file, argv, env):
            captured["file"] = file
            captured["argv"] = argv
            captured["env"] = env
            raise SystemExit(0)
        runner.os.execvpe = fake_exec
        os.environ.pop("HX_NETNS", None)
        with self.assertRaises(SystemExit):
            runner.runner_main(["--engagement", str(self.eng), "--netns"])
        self.assertEqual(captured["file"], "unshare")
        self.assertEqual(captured["argv"][:2], ["unshare", "-Ur"])
        self.assertEqual(captured["env"]["HX_NETNS"], "1")

    def test_preflight_failure_exits_2(self):
        os.environ["HX_NETNS"] = "1"
        runner.netns_preflight = lambda: ["backend de NAT ausente (instale slirp4netns ou pasta)"]
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--netns"]), 2)

    def test_netns_flag_reaches_ctx(self):
        os.environ["HX_NETNS"] = "1"
        runner.netns_preflight = lambda: []
        captured = {}
        def fake_run_workers(ctx, workers):
            captured["netns"] = ctx.get("netns")
            captured["workers"] = workers
            return ["empty_queue"]
        runner.run_workers = fake_run_workers
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--netns", "--slices", "1"]), 0)
        self.assertTrue(captured["netns"])
        self.assertEqual(captured["workers"], 1)


class NetnsWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = runner.hx.Repo(self.eng)

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "HX_NETNS"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_worker_enter_unshares_netns_and_applies_rules(self):
        calls = {"enter": 0, "unshare": [], "rules": 0}
        orig_unshare = runner.os.unshare
        orig_apply = runner.apply_netns_rules
        orig_allow = runner.netns_allow_hosts
        orig_resolve = runner.resolve_ips
        class Ctrl:
            def send(self, value): calls["enter"] += 1
            def recv(self): return "go"
        runner.os.unshare = lambda flags: calls["unshare"].append(flags)
        runner.netns_allow_hosts = lambda repo, hyp=None, runtime="opencode": {"alvo.com"}
        runner.resolve_ips = lambda hosts: {"1.2.3.4"}
        runner.apply_netns_rules = lambda ips, resolvers: calls.__setitem__("rules", calls["rules"] + 1)
        try:
            ctx = {"repo": self.repo, "netns_ctrl": Ctrl(), "netns": True}
            runner.netns_worker_enter(ctx)
        finally:
            runner.os.unshare = orig_unshare
            runner.apply_netns_rules = orig_apply
            runner.netns_allow_hosts = orig_allow
            runner.resolve_ips = orig_resolve
        self.assertEqual(calls["unshare"], [runner.os.CLONE_NEWNET])
        self.assertEqual(calls["rules"], 1)

    def test_netns_refresh_reapplies_with_hyp_host(self):
        seen = []
        orig_allow = runner.netns_allow_hosts
        orig_apply = runner.apply_netns_rules
        orig_resolve = runner.resolve_ips
        runner.netns_allow_hosts = lambda repo, hyp=None, runtime="opencode": {"alvo.com"}
        runner.resolve_ips = lambda hosts: {"9.9.9.9"}
        runner.apply_netns_rules = lambda ips, resolvers: seen.append(sorted(ips))
        try:
            runner.netns_refresh({"repo": self.repo}, {"endpoint": "GET https://x.alvo.com"})
        finally:
            runner.netns_allow_hosts = orig_allow
            runner.apply_netns_rules = orig_apply
            runner.resolve_ips = orig_resolve
        self.assertEqual(seen, [["9.9.9.9"]])

    def test_start_backend_slirp_waits_ready(self):
        calls = []
        orig_popen = runner.subprocess.Popen
        orig_pipe = runner.os.pipe
        orig_read = runner.os.read
        orig_select = runner.select.select
        r, w = orig_pipe()
        class P:
            pid = 123
            def terminate(self): calls.append("terminate")
            def wait(self, timeout=None): return 0
        def fake_popen(argv, **kwargs):
            calls.append(argv)
            return P()
        runner.os.pipe = lambda: (r, w)
        runner.os.read = lambda fd, n: b"1"
        runner.subprocess.Popen = fake_popen
        runner.select.select = lambda readers, writers, errors, timeout: (readers, [], [])
        try:
            runner.start_netns_backend(4321, "slirp4netns")
        finally:
            runner.subprocess.Popen = orig_popen
            runner.os.pipe = orig_pipe
            runner.os.read = orig_read
            runner.select.select = orig_select
        self.assertEqual(calls[0], ["slirp4netns", "--configure", "--mtu=65520", "--disable-host-loopback", "--ready-fd=%d" % w, "4321", "tap0"])

    def test_run_workers_spawns_backend_and_sends_go(self):
        events = []
        orig_pipe_ctx = runner.multiprocessing.get_context
        orig_start = runner.start_netns_backend
        orig_worker_entry = runner.worker_entry
        class FakePipe:
            def send(self, value): events.append(("send", value))
            def recv(self): events.append(("recv", None)); return "netns"
            def poll(self, timeout): return True
            def close(self): pass
        class FakeProc:
            pid = 777
            def __init__(self, **kwargs):
                if "args" in kwargs:
                    events.append(("spawn", kwargs["args"][1].get("worker")))
            def start(self): events.append(("start", None))
            def join(self): events.append(("join", None))
            def is_alive(self): return False
            def terminate(self): events.append(("terminate", None))
            def wait(self, timeout=None): return 0
        class FakeCtx:
            def Pipe(self): return (FakePipe(), FakePipe())
            def Queue(self): return FakePipe()
            def Process(self, **kwargs): return FakeProc(**kwargs)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "a"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        runner.multiprocessing.get_context = lambda name: FakeCtx()
        runner.start_netns_backend = lambda pid, backend: events.append(("backend", pid)) or FakeProc()
        runner.os.environ["HX_NETNS"] = "1"
        try:
            ctx = {"repo": self.repo, "netns": True, "backend": "slirp4netns", "worker": None, "owner": None}
            reasons = runner.run_workers(ctx, 1)
        finally:
            runner.multiprocessing.get_context = orig_pipe_ctx
            runner.start_netns_backend = orig_start
        self.assertIn(("recv", None), events)
        self.assertIn(("backend", 777), events)
        self.assertIn(("send", "go"), events)

    def test_run_workers_without_probes_uses_none_worker(self):
        events = []
        orig_ctx = runner.multiprocessing.get_context
        orig_backend = runner.start_netns_backend
        class FakePipe:
            def send(self, value): pass
            def recv(self): return "netns"
            def poll(self, timeout): return True
            def close(self): pass
            def get(self, timeout=None): raise Exception("empty")
        class FakeProc:
            pid = 778
            def __init__(self, **kwargs):
                if "args" in kwargs:
                    events.append(kwargs["args"][1].get("worker"))
            def start(self): pass
            def join(self): pass
            def is_alive(self): return False
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        class FakeCtx:
            def Pipe(self): return (FakePipe(), FakePipe())
            def Queue(self): return FakePipe()
            def Process(self, **kwargs): return FakeProc(**kwargs)
        runner.multiprocessing.get_context = lambda name: FakeCtx()
        runner.start_netns_backend = lambda pid, backend: FakeProc()
        os.environ["HX_NETNS"] = "1"
        try:
            runner.run_workers({"repo": self.repo, "netns": True}, 1)
        finally:
            runner.multiprocessing.get_context = orig_ctx
            runner.start_netns_backend = orig_backend
            os.environ.pop("HX_NETNS", None)
        self.assertEqual(events, [None])

    def test_run_workers_single_netns_has_no_session_bind(self):
        events = []
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "a"}, {"session": "b"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        orig_ctx = runner.multiprocessing.get_context
        orig_backend = runner.start_netns_backend
        class FakePipe:
            def send(self, value): pass
            def recv(self): return "netns"
            def poll(self, timeout): return True
            def close(self): pass
            def get(self, timeout=None): raise Exception("empty")
        class FakeProc:
            pid = 780
            def __init__(self, **kwargs):
                if "args" in kwargs:
                    events.append(kwargs["args"][1].get("worker"))
            def start(self): pass
            def join(self): pass
            def is_alive(self): return False
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        class FakeCtx:
            def Pipe(self): return (FakePipe(), FakePipe())
            def Queue(self): return FakePipe()
            def Process(self, **kwargs): return FakeProc(**kwargs)
        runner.multiprocessing.get_context = lambda name: FakeCtx()
        runner.start_netns_backend = lambda pid, backend: FakeProc()
        os.environ["HX_NETNS"] = "1"
        try:
            runner.run_workers({"repo": self.repo, "netns": True}, 1)
        finally:
            runner.multiprocessing.get_context = orig_ctx
            runner.start_netns_backend = orig_backend
            os.environ.pop("HX_NETNS", None)
        self.assertEqual(events, [None])

    def test_run_workers_terminates_started_work_on_backend_failure(self):
        events = []
        orig_ctx = runner.multiprocessing.get_context
        orig_backend = runner.start_netns_backend
        class FakePipe:
            def send(self, value): events.append(("send", value))
            def recv(self): return "netns"
            def poll(self, timeout): return True
            def close(self): pass
            def get(self, timeout=None): raise Exception("empty")
        class FakeProc:
            pid = 779
            def __init__(self, **kwargs): pass
            def start(self): events.append(("start", None))
            def join(self): pass
            def is_alive(self): return True
            def terminate(self): events.append(("terminate", None))
            def wait(self, timeout=None): return 0
        class FakeCtx:
            def Pipe(self): return (FakePipe(), FakePipe())
            def Queue(self): return FakePipe()
            def Process(self, **kwargs): return FakeProc(**kwargs)
        runner.multiprocessing.get_context = lambda name: FakeCtx()
        runner.start_netns_backend = lambda pid, backend: (_ for _ in ()).throw(runner.hx.HxError("boom", runner.hx.EXIT_GUARD))
        os.environ["HX_NETNS"] = "1"
        try:
            with self.assertRaises(runner.hx.HxError):
                runner.run_workers({"repo": self.repo, "netns": True, "backend": "slirp4netns"}, 1)
        finally:
            runner.multiprocessing.get_context = orig_ctx
            runner.start_netns_backend = orig_backend
            os.environ.pop("HX_NETNS", None)
        self.assertIn(("terminate", None), events)

    def test_start_backend_eof_before_ready_raises(self):
        orig_popen = runner.subprocess.Popen
        orig_pipe = runner.os.pipe
        orig_read = runner.os.read
        orig_select = runner.select.select
        r, w = orig_pipe()
        class P:
            pid = 123
            def terminate(self): pass
            def wait(self, timeout=None): return 0
        runner.os.pipe = lambda: (r, w)
        runner.os.read = lambda fd, n: b""
        runner.select.select = lambda readers, writers, errors, timeout: (readers, [], [])
        runner.subprocess.Popen = lambda argv, **kwargs: P()
        try:
            with self.assertRaises(runner.hx.HxError):
                runner.start_netns_backend(4321, "slirp4netns")
        finally:
            runner.subprocess.Popen = orig_popen
            runner.os.pipe = orig_pipe
            runner.os.read = orig_read
            runner.select.select = orig_select

    def test_worker_entry_installs_cleanup_handlers(self):
        orig_term = runner.signal.getsignal(runner.signal.SIGTERM)
        orig_int = runner.signal.getsignal(runner.signal.SIGINT)
        orig_loop = runner.worker_loop
        class Q:
            def put(self, value): pass
        runner.worker_loop = lambda ctx: "empty_queue"
        try:
            runner.worker_entry(Q(), {"repo": self.repo})
            self.assertEqual(runner.signal.getsignal(runner.signal.SIGTERM), runner._stop_signal)
            self.assertEqual(runner.signal.getsignal(runner.signal.SIGINT), runner._stop_signal)
        finally:
            runner.worker_loop = orig_loop
            runner.signal.signal(runner.signal.SIGTERM, orig_term)
            runner.signal.signal(runner.signal.SIGINT, orig_int)

    def test_stop_request_kills_inflight_and_sets_latch(self):
        killed = []
        orig_killpg = runner.os.killpg
        runner.os.killpg = lambda pid, sig: killed.append(pid)
        runner._INFLIGHT[4243] = object()
        try:
            runner.install_signals({"repo": self.repo})
            runner.signal.getsignal(runner.signal.SIGINT)(runner.signal.SIGINT, None)
            self.assertTrue((self.repo.hunt / "runner.stop").exists())
            self.assertEqual(killed, [4243])
        finally:
            runner._INFLIGHT.pop(4243, None)
            runner.os.killpg = orig_killpg
            runner.signal.signal(runner.signal.SIGINT, runner.signal.SIG_DFL)
            runner.signal.signal(runner.signal.SIGTERM, runner.signal.SIG_DFL)

    def test_stop_signal_kills_inflight_group(self):
        killed = []
        orig_killpg = runner.os.killpg
        orig_exit = runner.os._exit
        runner.os.killpg = lambda pid, sig: killed.append((pid, sig))
        runner.os._exit = lambda code: killed.append(("exit", code))
        runner._INFLIGHT[4242] = object()
        try:
            runner._stop_signal(runner.signal.SIGTERM, None)
            self.assertEqual(killed, [(4242, runner.signal.SIGKILL), ("exit", 143)])
        finally:
            runner._INFLIGHT.pop(4242, None)
            runner.os.killpg = orig_killpg
            runner.os._exit = orig_exit

    def test_invoke_opencode_reaps_group_after_return(self):
        killed = []
        orig_killpg = runner.os.killpg
        orig_popen = runner.subprocess.Popen
        class Proc:
            pid = 4194305
            returncode = 0
            def communicate(self, timeout=None):
                return ("out", "")
            def wait(self, timeout=None):
                return 0
        runner.os.killpg = lambda pid, sig: killed.append(pid)
        runner.subprocess.Popen = lambda argv, **kwargs: Proc()
        try:
            self.assertEqual(runner.invoke_opencode("p", "/tmp", "{}", 30), (0, "out", ""))
            self.assertEqual(killed, [4194305])
            self.assertEqual(runner._INFLIGHT, {})
        finally:
            runner.os.killpg = orig_killpg
            runner.subprocess.Popen = orig_popen


class RunnerNewGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        self.orig_invoke = runner.invoke_opencode
        self.orig_export = runner.export_usage
        runner.invoke_opencode = lambda *a, **kw: (0, '{"sessionID":"ses_a"}\n', "")
        runner.export_usage = lambda hx_mod, ref, runtime="opencode": {"tokens": 10, "cost": 0.01}

    def tearDown(self):
        runner.invoke_opencode = self.orig_invoke
        runner.export_usage = self.orig_export
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def add(self, claim="c", endpoint="GET /a"):
        runner.hx.main(["hypothesis", "add", "--claim", claim, "--endpoint", endpoint, "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def test_endpoint_host_outside_scope_is_not_whitelisted(self):
        scope_path = self.repo.hunt / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com"]
        scope["netns"] = {"allow_hosts": ["backend.internal"]}
        scope_path.write_text(json.dumps(scope))
        hyp = {"endpoint": "GET https://exfil.evil.tld/x?next=acme.com"}
        hosts = runner.netns_allow_hosts(self.repo, hyp, config_dir=Path("/nonexistent"), models_cache=Path("/nonexistent"), auth_file=Path("/nonexistent"))
        self.assertNotIn("exfil.evil.tld", hosts)
        self.assertIn("backend.internal", hosts)

    def test_plan_failure_exit_code(self):
        runner.invoke_opencode = lambda *a, **kw: (1, "", "provider down")
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--plan"]), 1)

    def test_rejects_nonpositive_caps(self):
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--slices", "-1"]), 2)
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--max-tokens", "0"]), 2)
        self.assertEqual(runner.runner_main(["--engagement", str(self.eng), "--wall", "0"]), 2)

    def test_concurrent_runner_lock_refused(self):
        lock = runner.acquire_runner_lock(self.repo)
        try:
            self.assertIsNotNone(lock)
            self.assertIsNone(runner.acquire_runner_lock(self.repo))
        finally:
            lock.close()

    def test_invoke_opencode_kills_only_its_own_group(self):
        sibling = subprocess.Popen(["sleep", "20"])
        real_invoke = self.orig_invoke
        orig_argv = runner.agent_argv
        runner.invoke_opencode = real_invoke
        runner.agent_argv = lambda argv: ["sh", "-c", "sleep 20", "--"]
        try:
            rc, out, err = real_invoke("p", "/tmp", "{}", 1)
        finally:
            runner.agent_argv = orig_argv
            runner.invoke_opencode = self.orig_invoke
        try:
            self.assertEqual(rc, 124)
            self.assertIsNone(sibling.poll())
        finally:
            sibling.kill()
            sibling.wait(timeout=5)

    def test_dead_session_releases_slice(self):
        scope_path = self.repo.hunt / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com"]
        scope["health"] = {"probes": [{"session": "s1", "file": "recon/session_s1.json", "method": "GET", "url": "https://acme.com/whoami"}], "session_invalid_if": ["status:401"]}
        scope_path.write_text(json.dumps(scope))
        runner.hx.dump_json(self.repo.hunt / ".health.json", {"s1": {"strikes": 2, "first_strike": 1.0, "dead_since": 1.0, "suspect_since": None, "probes": []}})
        for claim in ("a", "b"):
            runner.hx.main(["hypothesis", "add", "--claim", claim, "--endpoint", "GET /x", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "s1"])
        runner.runner_state_init(self.repo)
        ctx = {"repo": self.repo, "budget": dict(runner.DEFAULTS), "owner": "s1", "config": "{}", "worker": "s1", "slice_timeout_s": 900, "started_ts": runner.hx.now()}
        self.assertEqual(runner.worker_loop(ctx), "empty_queue")
        self.assertEqual(runner.runner_state_read(self.repo)["slices"], 0)
        verdicts = [hyp["result"]["verdict"] for hyp in runner.hx.load_hyps(self.repo)[0]]
        self.assertEqual(verdicts, ["blocked", "blocked"])


class OmpRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        runner.hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_SESSION"] = "runner-test"
        self.repo = runner.hx.Repo(self.eng)
        self.orig_popen = runner.subprocess.Popen
        self.orig_invoke_omp = runner.invoke_omp
        self.orig_invoke_opencode = runner.invoke_opencode
        self.orig_invoke_agent = runner.invoke_agent
        self.orig_retry_invoke = runner.retry_invoke
        self.orig_adjudicate = runner.adjudicate
        self.orig_sleep = runner.hx.sleep
        self.orig_which = shutil.which

    def tearDown(self):
        runner.subprocess.Popen = self.orig_popen
        runner.invoke_omp = self.orig_invoke_omp
        runner.invoke_opencode = self.orig_invoke_opencode
        runner.invoke_agent = self.orig_invoke_agent
        runner.retry_invoke = self.orig_retry_invoke
        runner.adjudicate = self.orig_adjudicate
        runner.hx.sleep = self.orig_sleep
        shutil.which = self.orig_which
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_OMP_BASH_MODE", "PI_CODING_AGENT_DIR"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def fake_popen(self, argv):
        class Proc:
            pid = 4194306
            returncode = 0
            def __init__(self):
                argv.append(None)
            def communicate(self, timeout=None):
                return ("", "")
            def wait(self, timeout=None):
                return 0
        return Proc

    def test_detect_runtime_prefers_explicit_then_path(self):
        self.assertEqual(runner.detect_runtime("omp"), "omp")
        self.assertEqual(runner.detect_runtime("opencode"), "opencode")
        shutil.which = lambda name: "/usr/bin/omp" if name == "omp" else None
        self.assertEqual(runner.detect_runtime(), "omp")
        shutil.which = lambda name: "/usr/bin/opencode" if name == "opencode" else None
        self.assertEqual(runner.detect_runtime(), "opencode")
        shutil.which = lambda name: None
        with self.assertRaises(runner.hx.HxError):
            runner.detect_runtime()

    def test_omp_argv_is_headless_and_tripwired(self):
        argv = runner.omp_argv(self.eng, "trabalhe h001", "smol-model", "ses_1", 900, plan=False)
        self.assertEqual(argv[0], "omp")
        self.assertIn("--print", argv)
        self.assertIn("--auto-approve", argv)
        self.assertIn("--no-title", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "smol-model")
        self.assertEqual(argv[argv.index("--resume") + 1], "ses_1")
        self.assertEqual(argv[argv.index("--session-dir") + 1], str(self.eng / "hunt" / ".omp-sessions"))
        self.assertEqual(argv[argv.index("--hook") + 1], str(runner.OMP_GUARD_HOOK))
        self.assertEqual(argv[argv.index("--max-time") + 1], "900")
        self.assertEqual(argv[-2:], ["--", "trabalhe h001"])

    def test_invoke_omp_sets_bash_mode_and_spawns_headless(self):
        seen = {}
        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs["env"]
            seen["session"] = kwargs["start_new_session"]
            class Proc:
                pid = 4194307
                returncode = 0
                def communicate(self, timeout=None):
                    return ("out", "")
                def wait(self, timeout=None):
                    return 0
            return Proc()
        runner.subprocess.Popen = fake_popen
        rc, out, err = runner.invoke_omp("p", self.eng, 30, env_extra={"HX_SESSION": "s1"}, plan=True)
        self.assertEqual((rc, out, err), (0, "out", ""))
        self.assertEqual(seen["env"]["HX_OMP_BASH_MODE"], "plan")
        self.assertEqual(seen["env"]["HX_SESSION"], "s1")
        self.assertEqual(seen["argv"][0], "omp")
        self.assertTrue(seen["session"])

    def test_invoke_agent_dispatches_by_runtime(self):
        calls = []
        runner.invoke_omp = lambda *a, **kw: calls.append(("omp", a[1], a[6])) or (0, "", "")
        runner.invoke_opencode = lambda *a, **kw: calls.append(("opencode", a[1], None)) or (0, "", "")
        base = {"repo": self.repo, "slice_timeout_s": 30, "config": "{}"}
        runner.invoke_agent({**base, "runtime": "omp", "plan": True}, "p")
        runner.invoke_agent({**base, "runtime": "opencode"}, "p")
        runner.invoke_agent(base, "p")
        self.assertEqual(calls[0][0], "omp")
        self.assertTrue(calls[0][2])
        self.assertEqual([call[0] for call in calls[1:]], ["opencode", "opencode"])

    def write_session(self, sid="01a0d3c3-3ade-702a-ac5f-c7119b761137", tokens=(100, 23), cost=(0.01, 0.02), sub=False, flat=False, slice_name=None):
        root = self.eng / "hunt" / ".omp-sessions"
        if slice_name:
            root = root / slice_name
        elif not flat:
            root = root / "--eng--"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"2026-09-24T14-13-02-302Z_{sid}.jsonl"
        lines = [json.dumps({"type": "session", "version": 3, "id": sid, "cwd": str(self.eng)})]
        for total, money in zip(tokens, cost):
            lines.append(json.dumps({"type": "model_usage", "usage": {"totalTokens": total, "cost": {"total": money}}}))
        path.write_text("\n".join(lines) + "\n")
        if sub:
            nested = root / f"2026-09-24T14-13-02-302Z_{sid}"
            nested.mkdir(exist_ok=True)
            (nested / "SubAgent.jsonl").write_text("{}\n")
        return path

    def test_omp_session_id_and_usage(self):
        path = self.write_session(sub=True)
        self.assertEqual(runner.omp_session_id(path), "01a0d3c3-3ade-702a-ac5f-c7119b761137")
        self.assertEqual(runner.omp_session_usage(path), {"tokens": 123, "cost": 0.03})
        files = runner.omp_session_files(self.eng)
        self.assertEqual(len(files), 1)
        self.assertNotIn("SubAgent", str(next(iter(files))))

    def test_omp_session_files_handles_flat_and_nested_layouts(self):
        flat = self.write_session(sid="01a0d3c3-3ade-702a-ac5f-111111111111", flat=True)
        nested = self.write_session(sid="01a0d3c3-3ade-702a-ac5f-222222222222")
        artifacts = self.eng / "hunt" / ".omp-sessions" / "2026-09-24T14-13-02-302Z_01a0d3c3-3ade-702a-ac5f-222222222222"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "__advisor.muse.jsonl").write_text(json.dumps({"type": "session", "id": "advisor"}) + "\n")
        files = {str(path) for path in runner.omp_session_files(self.eng)}
        self.assertEqual(files, {str(flat), str(nested)})
        self.assertIsNone(runner.OMP_SESSION_NAME_RE.match("__advisor.muse.jsonl"))
        self.assertIsNone(runner.OMP_SESSION_NAME_RE.match("SubAgent.jsonl"))

    def test_omp_sessions_are_isolated_per_slice(self):
        first = self.write_session(sid="01a0d3c3-3ade-702a-ac5f-333333333333", slice_name="h001-1-1")
        second = self.write_session(sid="01a0d3c3-3ade-702a-ac5f-444444444444", slice_name="h002-2-2")
        self.assertEqual({str(path) for path in runner.omp_session_files(self.eng, "h001-1-1")}, {str(first)})
        self.assertEqual({str(path) for path in runner.omp_session_files(self.eng, "h002-2-2")}, {str(second)})
        self.assertEqual(len(runner.omp_session_files(self.eng)), 2)
        argv = runner.omp_argv(self.eng, "p", None, None, 60, plan=False, session_dir="h002-2-2")
        self.assertEqual(argv[argv.index("--session-dir") + 1], str(self.eng / "hunt" / ".omp-sessions" / "h002-2-2"))

    def test_retry_invoke_ignores_other_slices_sessions(self):
        seen = []
        def fake_invoke(ctx, prompt, resume_session=None):
            self.write_session(sid="01a0d3c3-3ade-702a-ac5f-555555555555", slice_name="outra-fatia")
            seen.append(resume_session)
            return (1, "", "boom")
        runner.invoke_agent = fake_invoke
        runner.hx.sleep = lambda seconds: None
        ctx = {"repo": self.repo, "runtime": "omp", "session_dir": "minha-fatia", "budget": {**runner.DEFAULTS, "backoff_attempts": 2}, "owner": "s1", "started_ts": runner.hx.now()}
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(ctx, "p")
        self.assertEqual((rc, attempts), (1, 2))
        self.assertEqual(sessions, [])
        self.assertEqual(seen, [None, None])

    def test_omp_session_refs_are_json_serializable(self):
        def fake_invoke(ctx, prompt, resume_session=None):
            self.write_session(slice_name=ctx.get("session_dir") or "x")
            return (0, "", "")
        runner.invoke_agent = fake_invoke
        ctx = {"repo": self.repo, "runtime": "omp", "session_dir": "fatia-x", "budget": {**runner.DEFAULTS, "backoff_attempts": 1}, "owner": "s1", "started_ts": runner.hx.now()}
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(ctx, "p")
        self.assertEqual(rc, 0)
        self.assertTrue(sessions)
        self.assertTrue(all(isinstance(ref, str) for ref in sessions))
        json.dumps({"sessions": sessions})

    def test_export_usage_dispatches_to_session_file(self):
        path = self.write_session()
        self.assertEqual(runner.export_usage(runner.hx, str(path), "omp"), {"tokens": 123, "cost": 0.03})
        self.assertEqual(runner.export_usage(runner.hx, None, "omp"), {})

    def test_omp_egress_hosts_reads_agent_config(self):
        agent_dir = Path(self.tmp.name) / "agent"
        agent_dir.mkdir()
        (agent_dir / "models.yml").write_text("providers:\n  local:\n    baseUrl: https://gateway.internal:8443/v1\n")
        (agent_dir / "config.yml").write_text("modelRoles:\n  default: acme/gpt\n")
        os.environ["PI_CODING_AGENT_DIR"] = str(agent_dir)
        self.assertEqual(runner.omp_egress_hosts(), {"gateway.internal"})

    def test_netns_allowlist_uses_omp_provider_hosts(self):
        agent_dir = Path(self.tmp.name) / "agent2"
        agent_dir.mkdir()
        (agent_dir / "models.yml").write_text("providers:\n  local:\n    baseUrl: https://gateway.internal/v1\n")
        os.environ["PI_CODING_AGENT_DIR"] = str(agent_dir)
        scope_path = self.repo.hunt / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com"]
        scope["netns"] = {"allow_hosts": []}
        scope_path.write_text(json.dumps(scope))
        hosts = runner.netns_allow_hosts(self.repo, {"endpoint": "GET https://exfil.evil.tld/x"}, runtime="omp")
        self.assertIn("gateway.internal", hosts)
        self.assertIn("acme.com", hosts)
        self.assertNotIn("exfil.evil.tld", hosts)

    def test_netns_without_derivable_omp_provider_fails_closed(self):
        scope_path = self.repo.hunt / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com"]
        scope["netns"] = {"allow_hosts": []}
        scope_path.write_text(json.dumps(scope))
        os.environ["PI_CODING_AGENT_DIR"] = str(Path(self.tmp.name) / "vazio")
        self.assertEqual(runner.omp_egress_hosts(), set())
        with self.assertRaises(runner.hx.HxError):
            runner.netns_allow_hosts(self.repo, runtime="omp")
        scope["netns"] = {"allow_hosts": ["gateway.internal"]}
        scope_path.write_text(json.dumps(scope))
        self.assertIn("gateway.internal", runner.netns_allow_hosts(self.repo, runtime="omp"))

    def test_retry_invoke_tracks_omp_sessions_and_resumes(self):
        resumed = []
        def fake_invoke(ctx, prompt, resume_session=None):
            resumed.append(resume_session)
            self.write_session(sid=f"01a0d3c3-3ade-702a-ac5f-00000000000{len(resumed)}", sub=False)
            return (1, "", "boom") if len(resumed) == 1 else (0, "", "")
        runner.invoke_agent = fake_invoke
        runner.hx.sleep = lambda seconds: None
        ctx = {"repo": self.repo, "runtime": "omp", "budget": {**runner.DEFAULTS, "backoff_attempts": 2}, "owner": "s1", "started_ts": runner.hx.now()}
        rc, stdout, stderr, attempts, sessions = runner.retry_invoke(ctx, "p")
        self.assertEqual((rc, attempts), (0, 2))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(resumed, [None, "01a0d3c3-3ade-702a-ac5f-000000000001"])

    def test_run_slice_records_omp_tokens(self):
        def fake_retry(ctx, prompt):
            path = self.write_session(slice_name=ctx["session_dir"])
            return 0, "", "", 1, [str(path)]
        runner.retry_invoke = fake_retry
        runner.adjudicate = lambda repo, hid: {"ran": False}
        runner.hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /a", "--class", "BOLA", "--confirm", "x", "--refute", "y"])
        hyps, _ = runner.hx.load_hyps(self.repo)
        ctx = {"repo": self.repo, "runtime": "omp", "hyp": hyps[0], "config": "{}", "budget": dict(runner.DEFAULTS), "started_ts": runner.hx.now()}
        record = runner.run_slice(ctx)
        self.assertTrue(record["omp_session_dir"].startswith("h001-"))
        self.assertEqual(record["tokens"], 123)
        self.assertEqual(record["cost"], 0.03)
        self.assertEqual(record["session"], "01a0d3c3-3ade-702a-ac5f-c7119b761137")


class NetnsSelfcheckTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("slirp4netns") and shutil.which("nft") and shutil.which("setpriv"), "backend netns ausente")
    def test_selfcheck_real(self):
        proc = subprocess.run([sys.executable, str(RUNNER), "--netns-selfcheck"], capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SELFCHECK OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
