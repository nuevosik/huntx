import contextlib
import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HX_PATH = Path(__file__).resolve().parent.parent / "bin" / "hx"


def load_hx():
    loader = importlib.machinery.SourceFileLoader("hx", str(HX_PATH))
    spec = importlib.util.spec_from_file_location("hx", HX_PATH, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hx = load_hx()


def make_engagement(root):
    hunt = Path(root) / "hunt"
    hunt.mkdir(parents=True)
    (hunt / "scope.json").write_text(json.dumps({
        "engagement": "t",
        "in_scope": ["example.com"],
        "out_of_scope_hosts": [],
        "out_of_scope_paths": [],
        "allow_extra_hosts": [],
        "rate": {"per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30},
        "allowed_mutations": [],
        "evidence_dir": "scans/evidence",
    }))
    return Path(root)


class DiscoveryTest(unittest.TestCase):
    def test_find_from_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engagement(tmp)
            sub = eng / "scans" / "deep"
            sub.mkdir(parents=True)
            os.environ.pop("HX_ENGAGEMENT", None)
            self.assertEqual(hx.find_engagement(sub), eng)

    def test_missing_engagement_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("HX_ENGAGEMENT", None)
            with self.assertRaises(hx.HxError):
                hx.find_engagement(tmp)


class InitTest(unittest.TestCase):
    def test_init_creates_tree_and_refuses_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = hx.main(["init", "--dir", tmp])
            self.assertEqual(rc, 0)
            hunt = Path(tmp) / "hunt"
            for name in ("scope.json", "TARGET.md", "COVERAGE.md", "HYPOTHESES.json", ".gitignore"):
                self.assertTrue((hunt / name).exists(), name)
            self.assertTrue((hunt / "FINDINGS" / "drafts").is_dir())
            self.assertTrue((hunt / "pending").is_dir())
            self.assertTrue((Path(tmp) / "HUNT.md").exists())
            scope = json.loads((hunt / "scope.json").read_text())
            self.assertEqual(scope["engagement"], Path(tmp).name)
            self.assertIn("audit.log", (hunt / ".gitignore").read_text())
            self.assertIn("runner.pid", (hunt / ".gitignore").read_text())
            self.assertIn(".lock.*", (hunt / ".gitignore").read_text())
            self.assertIn(".runnerstate.json", (hunt / ".gitignore").read_text())
            self.assertIn(".jev.json", (hunt / ".gitignore").read_text())
            self.assertEqual(hx.main(["init", "--dir", tmp]), hx.EXIT_GUARD)


class AuditTest(unittest.TestCase):
    def test_audit_uses_args_digest_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = hx.Repo(tmp)
            hx.audit(repo, "actor", "tool", "payload-x", "allowed", "reason")
            entry = json.loads((repo.hunt / "audit.log").read_text().splitlines()[0])
            self.assertIn("args_digest", entry)
            self.assertNotIn("payload", entry)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def add(self, claim="BOLA em wishlist"):
        return hx.main(["hypothesis", "add", "--claim", claim, "--endpoint", "GET /wishlist/{id}", "--class", "BOLA", "--confirm", "200 com dado da vitima", "--refute", "403/404"])

    def test_add_next_claim_and_owner(self):
        self.add()
        self.add("v=2 em orders")
        repo = hx.repo_from_cwd()
        hyps, _ = hx.load_hyps(repo)
        self.assertEqual([h["id"] for h in hyps], ["h001", "h002"])
        self.assertEqual(hx.main(["next", "2"]), 0)
        hyps, _ = hx.load_hyps(repo)
        self.assertEqual([h["status"] for h in hyps], ["claimed", "claimed"])
        self.assertEqual(hyps[0]["owner"], "sess-a")

    def test_peek_does_not_claim(self):
        self.add()
        hx.main(["next", "--peek"])
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual(hyps[0]["status"], "open")

    def test_next_skips_already_claimed(self):
        self.add()
        self.add("v=2 em orders")
        self.assertEqual(hx.main(["next", "1"]), 0)
        self.assertEqual(hx.main(["next", "1"]), 0)
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual([h["status"] for h in hyps], ["claimed", "claimed"])
        self.assertEqual([h["owner"] for h in hyps], ["sess-a", "sess-a"])

    def test_next_prefers_operator_over_planner(self):
        hx.main(["hypothesis", "add", "--claim", "p", "--endpoint", "GET /p", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--source", "planner"])
        self.add("m")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hx.main(["next", "1"]), 0)
        self.assertIn("h002", out.getvalue())
        self.assertNotIn("[planner]", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hx.main(["next", "1"]), 0)
        self.assertIn("h001", out.getvalue())
        self.assertIn("[planner]", out.getvalue())

    def test_next_orders_by_priority_within_group(self):
        self.add("m1")
        self.add("m2")
        repo = hx.repo_from_cwd()
        hyps, _ = hx.load_hyps(repo)
        hyps[0]["priority"] = 0.1
        hyps[1]["priority"] = 2.0
        hx.save_hyps(repo, hyps)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hx.main(["next", "1"]), 0)
        self.assertIn("h002", out.getvalue())
        self.assertNotIn("h001", out.getvalue())

    def test_next_operator_beats_planner_priority(self):
        hx.main(["hypothesis", "add", "--claim", "p", "--endpoint", "GET /p", "--class", "BOLA", "--confirm", "c", "--refute", "r", "--source", "planner"])
        self.add("m")
        repo = hx.repo_from_cwd()
        hyps, _ = hx.load_hyps(repo)
        hyps[0]["priority"] = 9.0
        hyps[1]["priority"] = 0.5
        hx.save_hyps(repo, hyps)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hx.main(["next", "1"]), 0)
        self.assertIn("h002", out.getvalue())
        self.assertNotIn("h001", out.getvalue())

    def test_next_tolerates_malformed_priority(self):
        self.add("m1")
        self.add("m2")
        repo = hx.repo_from_cwd()
        hyps, _ = hx.load_hyps(repo)
        hyps[0]["priority"] = "x"
        hx.save_hyps(repo, hyps)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(hx.main(["next", "2"]), 0)
        self.assertIn("h001", out.getvalue())
        self.assertIn("h002", out.getvalue())

    def test_corrupt_state_returns_guard_without_traceback(self):
        hx.repo_from_cwd().hyps.write_text("{corrompido")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = hx.main(["next"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertIn("estado corrompido", stderr.getvalue())

    def test_ttl_reclaim(self):
        self.add()
        hx.main(["next"])
        os.environ["HX_FAKE_NOW"] = "5000"
        hyps, changed = hx.load_hyps(hx.repo_from_cwd())
        self.assertTrue(changed)
        self.assertEqual(hyps[0]["status"], "open")

    def test_claim_ttl_env_extends(self):
        self.add()
        hx.main(["next"])
        os.environ["HX_FAKE_NOW"] = "5000"
        os.environ["HX_CLAIM_TTL_S"] = "99999"
        try:
            hyps, changed = hx.load_hyps(hx.repo_from_cwd())
        finally:
            os.environ.pop("HX_CLAIM_TTL_S", None)
        self.assertFalse(changed)
        self.assertEqual(hyps[0]["status"], "claimed")

    def test_release_requires_owner(self):
        self.add()
        hx.main(["next"])
        os.environ["HX_SESSION"] = "sess-b"
        self.assertEqual(hx.main(["release", "h001"]), hx.EXIT_GUARD)

    def test_release_refuses_terminal_result(self):
        self.add()
        hx.main(["next"])
        self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "n"]), 0)
        self.assertEqual(hx.main(["release", "h001"]), hx.EXIT_GUARD)
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual(hyps[0]["status"], "result")


    def test_hypothesis_tag_must_match_probe(self):
        scope_path = Path(self.tmp.name) / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        bad = hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "zz"])
        self.assertEqual(bad, hx.EXIT_GUARD)
        good = hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "b"])
        self.assertEqual(good, 0)


class ResultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y"])

    def tearDown(self):
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_confirmed_requires_verified_finding(self):
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), hx.EXIT_GUARD)
        hx.dump_json(self.eng / "hunt" / "FINDINGS" / "h001.json", {"id": "h001", "verified_at": 1, "proof": {"attacker": {"marker_present": True}}})
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), 0)
        cov = (self.eng / "hunt" / "COVERAGE.md").read_text()
        self.assertIn("GET /w", cov)
        self.assertIn("confirmed", cov)
        hyps, _ = hx.load_hyps(hx.repo_from_cwd())
        self.assertEqual(hyps[0]["status"], "result")
        self.assertEqual(hyps[0]["result"]["verdict"], "confirmed")

    def test_confirmed_rejects_unverified_finding_file(self):
        hx.dump_json(self.eng / "hunt" / "FINDINGS" / "h001.json", {"id": "h001"})
        self.assertEqual(hx.main(["result", "h001", "--verdict", "confirmed", "--note", "n"]), hx.EXIT_GUARD)

    def test_owner_check(self):
        hx.main(["next"])
        os.environ["HX_SESSION"] = "sess-b"
        self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "n"]), hx.EXIT_GUARD)

    def test_brief_bounded(self):
        rc = hx.main(["brief", "--max-bytes", "400"])
        self.assertEqual(rc, 0)

    def test_coverage_no_prefix_overwrite(self):
        cov = self.eng / "hunt" / "COVERAGE.md"
        cov.write_text("| endpoint | classe | status | evidência | nota |\n|---|---|---|---|---|\n| GET /w/42 | BOLA | confirmed | e | n |\n")
        self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "n"]), 0)
        text = cov.read_text()
        self.assertIn("| GET /w/42 | BOLA | confirmed | e | n |", text)
        self.assertIn("| GET /w | BOLA | refuted |", text)


class RateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_tty = hx.stdin_is_tty
        hx.stdin_is_tty = lambda: False

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.stdin_is_tty = self.orig_tty
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_spacing_uses_scheduled_time(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_acquire(scope, repo, "example.com", 30)
        hx.rate_acquire(scope, repo, "example.com", 30)
        state = json.loads(repo.rate.read_text())
        self.assertEqual(state["hosts"]["example.com"]["last"], 1002.0)

    def test_max_wait_exceeded(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_acquire(scope, repo, "example.com", 30)
        state = json.loads(repo.rate.read_text())
        state["hosts"]["example.com"]["last"] = 1100
        repo.rate.write_text(json.dumps(state))
        with self.assertRaises(hx.HxError):
            hx.rate_acquire(scope, repo, "example.com", 1)

    def test_step_up_doubles_and_caps(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        self.assertEqual(hx.rate_step_up(scope, repo, "example.com", "429"), 4.0)
        self.assertEqual(hx.rate_step_up(scope, repo, "example.com", "429"), 8.0)

    def test_decay_after_clean_hour(self):
        repo = hx.repo_from_cwd()
        scope = repo.scope()
        hx.rate_step_up(scope, repo, "example.com", "429")
        state = json.loads(repo.rate.read_text())
        os.environ["HX_FAKE_NOW"] = str(1000 + 3600)
        self.assertEqual(hx.rate_effective_interval(scope, state, "example.com"), 2.0)

    def test_reset_requires_tty(self):
        self.assertEqual(hx.main(["rate", "reset"]), hx.EXIT_TTY)


class BurstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(Path(self.tmp.name))
        self.orig_sleep = hx.sleep
        self.slept = []
        hx.sleep = lambda seconds: self.slept.append(seconds)
        self.scope = hx.load_json(self.repo.scope_path, {})
        self.scope["rate"] = dict(self.scope["rate"])
        self.scope["rate"]["burst"] = [3, 10]

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
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


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["acme.com", "login.acme.com"]
        scope["out_of_scope_hosts"] = ["vpn.acme.com", "*.rentals.acme.com"]
        scope["out_of_scope_paths"] = ["/blog", "/used"]
        scope["allow_extra_hosts"] = ["api.mail.tm"]
        scope["allowed_mutations"] = ["POST /rest/user", "POST /mobile-gateway/rest/user/guest/V1"]
        scope_path.write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        self.scope = json.loads(scope_path.read_text())

    def tearDown(self):
        os.environ.pop("HX_ENGAGEMENT", None)
        os.environ.pop("HX_FAKE_NOW", None)
        self.tmp.cleanup()

    def test_scope_rules(self):
        self.assertTrue(hx.check_scope(self.scope, "https://www.acme.com/x")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://vpn.acme.com/x")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://foo.rentals.acme.com/x")[0])
        self.assertTrue(hx.check_scope(self.scope, "https://api.mail.tm/messages")[0])
        self.assertFalse(hx.check_scope(self.scope, "https://www.acme.com/blog/post")[0])
        self.assertTrue(hx.check_scope(self.scope, "https://www.acme.com/usedcars")[0])
        self.assertTrue(hx.check_scope(self.scope, "http://127.0.0.1:9999/x")[0])

    def test_classify(self):
        self.assertEqual(hx.classify_method(self.scope, "GET", "https://www.acme.com/x"), "auto")
        self.assertEqual(hx.classify_method(self.scope, "POST", "https://www.acme.com/rest/user"), "auto")
        self.assertEqual(hx.classify_method(self.scope, "POST", "https://www.acme.com/rest/orders/cancel-order/1"), "staged")
        self.assertEqual(hx.classify_method(self.scope, "DELETE", "https://www.acme.com/rest/user/giftcard/1"), "staged")

    def test_staging_writes_pending(self):
        repo = hx.Repo(self.eng)
        pid = hx.stage_pending(repo, {"method": "DELETE", "url": "https://www.acme.com/rest/user/giftcard/1", "body": None, "session": None})
        self.assertTrue((repo.hunt / "pending" / f"{pid}.json").exists())

    def test_staging_ids_unique_within_same_second(self):
        os.environ["HX_FAKE_NOW"] = "3000"
        repo = hx.Repo(self.eng)
        spec = {"method": "DELETE", "url": "https://www.acme.com/rest/user/giftcard/1", "body": None, "session": None}
        first = hx.stage_pending(repo, spec)
        second = hx.stage_pending(repo, spec)
        self.assertNotEqual(first, second)
        self.assertTrue((repo.hunt / "pending" / f"{first}.json").exists())
        self.assertTrue((repo.hunt / "pending" / f"{second}.json").exists())
        self.assertEqual(len(list((repo.hunt / "pending").glob("*.json"))), 2)

    def test_classifier_skips_pathless_mutation_entry(self):
        scope = dict(self.scope)
        scope["allowed_mutations"] = ["POST", "POST /rest/user", "POST /mobile-gateway/rest/user/guest/V1"]
        self.assertEqual(hx.classify_method(scope, "POST", "https://www.acme.com/anything"), "staged")
        self.assertEqual(hx.classify_method(scope, "POST", "https://www.acme.com/rest/user"), "auto")
        self.assertEqual(hx.classify_method(scope, "POST", "https://www.acme.com/mobile-gateway/rest/user/guest/V1/x"), "auto")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["out_of_scope_paths"] = ["/blog"]
        scope["allowed_mutations"] = ["POST /rest/user"]
        scope_path.write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_fetch = hx.fetch
        self.orig_http_request = hx.http_request
        self.orig_tty = hx.stdin_is_tty
        self.orig_read = hx.read_line

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            return 200, {"Set-Cookie": "sess=supersecrettoken123456"}, '{"last4": "4242", "email": "a@b.com"}'

        hx.fetch = fake_fetch

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        hx.http_request = self.orig_http_request
        hx.stdin_is_tty = self.orig_tty
        hx.read_line = self.orig_read
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_transport_error_maps_to_exit_4(self):
        hx.fetch = self.orig_fetch

        def boom(session, method, url, headers, body):
            raise RuntimeError("boom")

        hx.http_request = boom
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/_misc"])
        self.assertEqual(rc, hx.EXIT_TRANSPORT)

    def test_dead_session_refused_before_fetch(self):
        repo = hx.repo_from_cwd()
        hx.health_probe_result(repo, "b", False, "invalid")
        hx.health_probe_result(repo, "b", False, "invalid")
        calls = []
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: calls.append(url)
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--session", "recon/session_b.json"])
        self.assertEqual(rc, hx.EXIT_SESSION)
        self.assertEqual(calls, [])

    def test_get_saves_redacted_evidence_with_marker(self):
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/h001", "--marker", r'"last4": "(\d+)"'])
        self.assertEqual(rc, 0)
        files = sorted((self.eng / "scans" / "evidence" / "h001").glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text())
        self.assertEqual(data["response"]["status"], 200)
        blob = json.dumps(data)
        self.assertNotIn("supersecrettoken123456", blob)
        self.assertIn("sha256:", blob)
        self.assertEqual(data["proof"]["marker"], "4242")
        self.assertNotIn("a@b.com", data["response"]["body"])
        self.assertTrue(data["integrity"]["raw_sha256"])

    def test_banned_path_and_host_exit_2(self):
        self.assertEqual(hx.main(["run", "GET", "https://www.acme.com/blog/x"]), hx.EXIT_GUARD)
        self.assertEqual(hx.main(["run", "GET", "https://other.com/x"]), hx.EXIT_GUARD)

    def test_staged_method_exit_5_and_pending_flow(self):
        self.assertEqual(hx.main(["run", "DELETE", "https://www.acme.com/api/x"]), hx.EXIT_STAGED)
        pending = list((self.eng / "hunt" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        pending_id = pending[0].stem
        hx.stdin_is_tty = lambda: False
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_TTY)
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "www.acme.com"
        self.assertEqual(hx.main(["pending", "run", pending_id]), 0)
        self.assertEqual(len(list((self.eng / "hunt" / "pending").glob("*.json"))), 0)

    def test_pending_run_wrong_typed_host_aborts(self):
        hx.main(["run", "DELETE", "https://www.acme.com/api/y"])
        pending_id = list((self.eng / "hunt" / "pending").glob("*.json"))[0].stem
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "wrong.host"
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_GUARD)

    def test_pending_list_marks_stale_and_drop(self):
        hx.main(["run", "DELETE", "https://www.acme.com/api/stale"])
        pending = list((self.eng / "hunt" / "pending").glob("*.json"))
        record = json.loads(pending[0].read_text())
        record["staged_ts"] = hx.now() - 8 * 86400
        pending[0].write_text(json.dumps(record))
        out = io.StringIO()
        with redirect_stdout(out):
            hx.main(["pending", "list"])
        self.assertIn("[stale]", out.getvalue())
        self.assertEqual(hx.main(["pending", "drop", "../x"]), hx.EXIT_GUARD)
        self.assertEqual(hx.main(["pending", "drop", pending[0].stem]), 0)
        self.assertEqual(len(list((self.eng / "hunt" / "pending").glob("*.json"))), 0)
        self.assertEqual(hx.main(["pending", "drop", pending[0].stem]), hx.EXIT_GUARD)

    def test_json_output(self):
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/_misc", "--json"])
        self.assertEqual(rc, 0)

    def test_header_passed_and_429_steps_up(self):
        orig = hx.fetch
        try:
            hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "ok")
            hx.main(["run", "GET", "https://www.acme.com/api/x", "--header", "Accept: application/json;v=1", "--save", "scans/evidence/_misc"])
            hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (429, {}, "slow down")
            hx.main(["run", "GET", "https://www.acme.com/api/x", "--save", "scans/evidence/_misc"])
        finally:
            hx.fetch = orig
        state = json.loads((self.eng / "hunt" / ".ratelimit.json").read_text())
        self.assertEqual(state["effective"]["www.acme.com"], 4.0)
        files = list((self.eng / "scans" / "evidence" / "_misc").glob("*.json"))
        self.assertEqual(len(files), 2)

    def test_secret_redaction_hashes_or_drops(self):
        token = "abcdef0123456789abcdef0123456789"
        long_secret = "supersecretpassword123"
        rc = hx.main(["run", "POST", "https://www.acme.com/rest/user", "--header", f"X-Auth-Token: {token}", "--data", f"email=victim@x.com&password={long_secret}", "--save", "scans/evidence/_misc"])
        self.assertEqual(rc, 0)
        rc = hx.main(["run", "POST", "https://www.acme.com/rest/user", "--data", "password=hunter2", "--save", "scans/evidence/_misc"])
        self.assertEqual(rc, 0)
        blobs = "\n".join(path.read_text() for path in (self.eng / "scans" / "evidence" / "_misc").glob("*.json"))
        self.assertIn("sha256:", blobs)
        self.assertIn("<dropped>", blobs)
        for raw in (token, long_secret, "hunter2", "victim@x.com"):
            self.assertNotIn(raw, blobs)

    def test_run_redacts_url_query_secrets(self):
        secret = "urlsecrettoken0123456789abcdef"
        rc = hx.main(["run", "GET", f"https://www.acme.com/api/x?token={secret}", "--save", "scans/evidence/_misc"])
        self.assertEqual(rc, 0)
        files = sorted((self.eng / "scans" / "evidence" / "_misc").glob("*.json"))
        blob = files[-1].read_text()
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, files[-1].name)
        self.assertIn("sha256:", blob)

    def test_headers_reach_fetch_and_request_body_is_redacted(self):
        seen = {}
        orig = hx.fetch
        try:
            def fake_fetch(method, url, headers=None, body=None, cookies=None):
                seen["headers"] = headers
                return 200, {}, "ok"
            hx.fetch = fake_fetch
            rc = hx.main(["run", "POST", "https://www.acme.com/rest/user", "--header", "Accept: application/json;v=1", "--data", "email=victim@x.com&password=hunter2secret", "--save", "scans/evidence/_misc"])
        finally:
            hx.fetch = orig
        self.assertEqual(rc, 0)
        self.assertEqual(seen["headers"]["Accept"], "application/json;v=1")
        blob = sorted((self.eng / "scans" / "evidence" / "_misc").glob("*.json"))[-1].read_text()
        self.assertNotIn("victim@x.com", blob)
        self.assertNotIn("hunter2secret", blob)

    def test_pending_reruns_scope_check(self):
        hx.main(["run", "DELETE", "https://www.acme.com/api/z"])
        pending_id = list((self.eng / "hunt" / "pending").glob("*.json"))[0].stem
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = []
        scope_path.write_text(json.dumps(scope))
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "www.acme.com"
        self.assertEqual(hx.main(["pending", "run", pending_id]), hx.EXIT_GUARD)


    def test_session_file_must_match_probe_tag(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        rc = hx.main(["run", "GET", "https://www.acme.com/api/x", "--session", "recon/weird.json"])
        self.assertEqual(rc, hx.EXIT_GUARD)


class HealthTest(unittest.TestCase):
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

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_probe_returns_status_and_excerpt(self):
        scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/mobile-gateway/rest/cart/V3"}]
        body = '{"error":\n  "secureToken must not be null", "x": "' + "y" * 400 + '"}'
        hx.fetch = lambda *a, **k: (400, {}, body)
        ok, signal, status, excerpt = hx.probe_once(self.repo, scope, "a")
        self.assertTrue(ok)
        self.assertEqual(status, 400)
        self.assertNotIn("\n", excerpt)
        self.assertLessEqual(len(excerpt), 160)

    def test_probe_excerpt_strips_control_bytes(self):
        scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/probe"}]
        body = "\x1b[31mfail\x00" + "z" * 300
        hx.fetch = lambda *a, **k: (500, {}, body)
        ok, signal, status, excerpt = hx.probe_once(self.repo, scope, "a")
        self.assertNotIn("\x1b", excerpt)
        self.assertNotIn("\x00", excerpt)
        self.assertLessEqual(len(excerpt), 160)

    def test_health_check_prints_status_and_excerpt(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.rei.com"]
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/probe"}]
        scope_path.write_text(json.dumps(scope))
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (400, {}, '{"error": "secureToken must not be null"}')
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = hx.main(["health", "check", "--session", "a"])
        self.assertEqual(rc, 0)
        out = buffer.getvalue()
        self.assertIn("a: ok (http 400)", out)
        self.assertIn("secureToken must not be null", out)
        state = json.loads(self.repo.health.read_text())
        self.assertEqual(state["a"]["probes"][-1]["status"], 400)
        self.assertIn("secureToken must not be null", state["a"]["probes"][-1]["excerpt"])

    def test_two_strikes_declare_death_with_first_strike_ts(self):
        os.environ["HX_FAKE_NOW"] = "1000"
        hx.health_probe_result(self.repo, "a", False, "invalid")
        state = json.loads(self.repo.health.read_text())
        self.assertFalse(state["a"]["dead_since"])
        os.environ["HX_FAKE_NOW"] = "1100"
        declared = hx.health_probe_result(self.repo, "a", False, "invalid")
        state = json.loads(self.repo.health.read_text())
        self.assertTrue(declared)
        self.assertEqual(state["a"]["dead_since"], 1000)

    def test_valid_probe_resets_strikes(self):
        hx.health_probe_result(self.repo, "a", False, "invalid")
        hx.health_probe_result(self.repo, "a", True, None)
        state = json.loads(self.repo.health.read_text())
        self.assertEqual(state["a"]["strikes"], 0)

    def test_reopen_sweep_restores_refuted_in_window(self):
        os.environ["HX_SESSION"] = "sess-a"
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"] = {"probes": [{"session": "a", "file": "recon/session_a.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "a"])
        hx.main(["next"])
        os.environ["HX_FAKE_NOW"] = "1050"
        hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        reopened = hx.reopen_sweep(self.repo, "a", 1000)
        self.assertEqual(reopened, ["h001"])
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["status"], "open")
        self.assertEqual(hyps[0]["reopened_reason"], "session_death_window")

    def test_signal_matches(self):
        self.assertTrue(hx.signal_matches("status:401", 401, {}, ""))
        self.assertTrue(hx.signal_matches("redirect_host:login", 302, {"Location": "https://login.acme.com/x"}, ""))
        self.assertTrue(hx.signal_matches("body_contains:px-captcha", 403, {}, "blocked px-captcha here"))
        self.assertFalse(hx.signal_matches("status:401", 200, {}, ""))

    def test_health_check_probes_steps_up_and_reports(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["health"] = {
            "probes": [{"session": "a", "file": None, "method": "GET", "url": "https://www.acme.com/probe"}],
            "fresh_max_s": 600,
            "session_invalid_if": ["status:401"],
            "waf_block_if": ["body_contains:px-captcha"],
            "rate_limited_if": ["status:429"],
        }
        scope_path.write_text(json.dumps(scope))
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (429, {}, "")
        try:
            rc = hx.main(["health", "check", "--session", "a"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, 0)
        state = json.loads((self.eng / "hunt" / ".ratelimit.json").read_text())
        self.assertEqual(state["effective"]["www.acme.com"], 4.0)
        health = json.loads(self.repo.health.read_text())
        self.assertEqual(health["a"]["probes"][-1]["signal"], "ratelimit")


class ResultGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_SESSION"] = "sess-a"
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["health"] = {
            "probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/probe"}],
            "fresh_max_s": 600,
            "session_invalid_if": ["status:401"],
            "waf_block_if": ["body_contains:px-captcha"],
            "rate_limited_if": ["status:429"],
        }
        scope_path.write_text(json.dumps(scope))
        evidence_dir = self.eng / "scans" / "evidence" / "h001"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "r.json").write_text(json.dumps({"response": {"status": 403}}))
        hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", "--session-tag", "b", "--evidence", "scans/evidence/h001"])
        hx.main(["next"])

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_SESSION", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_refute_with_dead_probe_refused(self):
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (401, {}, "")
        try:
            rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, hx.EXIT_SESSION)

    def test_refute_with_live_probe_passes(self):
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "ok")
        try:
            rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(hyps[0]["result"]["session_check"]["session_tag"], "b")

    def test_refute_with_redirect_to_login_hits_session_gate(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["health"]["session_invalid_if"] = ["status:401", "redirect_host:login.acme.com"]
        scope_path.write_text(json.dumps(scope))
        evidence = self.eng / "scans" / "evidence" / "h001" / "r.json"
        evidence.write_text(json.dumps({"response": {"status": 302, "headers": {"location": "https://login.acme.com/x"}}}))
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (401, {}, "")
        try:
            rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "302 login"])
        finally:
            hx.fetch = orig_fetch
        self.assertEqual(rc, hx.EXIT_SESSION)

    def test_refute_with_dead_session_already_flagged(self):
        hx.health_probe_result(self.repo, "b", False, "invalid")
        hx.health_probe_result(self.repo, "b", False, "invalid")
        rc = hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"])
        self.assertEqual(rc, hx.EXIT_SESSION)

    def test_second_result_requires_force(self):
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "ok")
        try:
            self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"]), 0)
            self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"]), hx.EXIT_GUARD)
            self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "403", "--force"]), 0)
        finally:
            hx.fetch = orig_fetch

    def test_flock_is_reentrant(self):
        lock_path = self.eng / "hunt" / ".lock.reentrant"
        with hx.flock(lock_path):
            with hx.flock(lock_path):
                pass
        with hx.flock(lock_path):
            pass

    def test_evidence_auth_failure_requires_evidence_and_int_status(self):
        self.assertFalse(hx.evidence_auth_failure(self.repo, {"id": "hx-no-evidence"}))
        hyp = {"id": "hx-null-status", "evidence": "scans/evidence/hx-null-status"}
        evidence_dir = self.eng / "scans" / "evidence" / "hx-null-status"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "r.json").write_text(json.dumps({"response": {"status": None}}))
        self.assertFalse(hx.evidence_auth_failure(self.repo, hyp))

    def test_probe_death_reopens_refuted_window_and_refuses(self):
        hyps_path = self.eng / "hunt" / "HYPOTHESES.json"
        hyps = json.loads(hyps_path.read_text())
        hyps.append({
            "id": "h002",
            "status": "result",
            "claim": "c2",
            "endpoint": "GET /w2",
            "class": "BOLA",
            "confirm": "x",
            "refute": "y",
            "evidence": "scans/evidence/h002",
            "session_tag": "b",
            "owner": None,
            "created_ts": 1000,
            "claimed_ts": None,
            "closed_ts": 1050,
            "result": {"verdict": "refuted", "note": "403", "evidence": None, "session_check": None},
            "reopened_reason": None,
        })
        hx.dump_json(hyps_path, hyps)
        orig_fetch = hx.fetch
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (401, {}, "")
        try:
            self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"]), hx.EXIT_SESSION)
            os.environ["HX_FAKE_NOW"] = "1100"
            self.assertEqual(hx.main(["result", "h001", "--verdict", "refuted", "--note", "403"]), hx.EXIT_SESSION)
        finally:
            hx.fetch = orig_fetch
        hyps, _ = hx.load_hyps(self.repo)
        reopened = next(h for h in hyps if h["id"] == "h002")
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["reopened_reason"], "session_death_window")
        state = json.loads(self.repo.health.read_text())
        self.assertEqual(state["b"]["dead_since"], 1000)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["jev"] = {"enabled": False}
        scope_path.write_text(json.dumps(scope))
        recon = self.eng / "recon"
        recon.mkdir()
        (recon / "session_a.json").write_text(json.dumps([{"name": "s", "value": "victim", "domain": "www.acme.com"}]))
        (recon / "session_b.json").write_text(json.dumps([{"name": "s", "value": "attacker", "domain": "www.acme.com"}]))
        self.responses = {
            ("victim", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "B2"): (200, {}, '{"code": "ACME-111111"}'),
        }
        self.orig_fetch = hx.fetch

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            tag = (cookies or [{}])[0].get("value")
            object_id = url.rsplit("/", 1)[-1]
            return self.responses[(tag, object_id)]

        hx.fetch = fake_fetch
        draft = {
            "id": "h001",
            "title": "BOLA giftcard",
            "scenario": "differential",
            "attacker_session": "recon/session_b.json",
            "victim_session": "recon/session_a.json",
            "request": {"method": "GET", "url": "https://www.acme.com/giftcard/{cardId}", "victim_object": {"cardId": "A1"}},
            "marker": {"extract": r'"code": "([A-Z0-9-]+)"'},
            "control": {"attacker_own_object": {"cardId": "B2"}},
        }
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.fetch = self.orig_fetch
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_differential_pass_moves_to_findings(self):
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        self.assertFalse((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())
        proof = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(proof["proof"]["attacker"]["marker_present"], True)
        self.assertEqual(proof["proof"]["control"]["marker_absent"], True)
        steps = list((self.repo.hunt / "FINDINGS" / "drafts" / "h001").glob("*.json"))
        self.assertEqual(len(steps), 3)

    def test_differential_fail_keeps_draft_marked(self):
        self.responses[("attacker", "A1")] = (200, {}, '{"code": "ACME-111111"}')
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertIn("failed_at", draft)
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_verify_rejects_traversal_ids(self):
        self.assertEqual(hx.main(["verify", "../h001"]), hx.EXIT_GUARD)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["id"] = "../../evil"
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        self.assertEqual(hx.main(["verify", "h001"]), hx.EXIT_GUARD)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())

    def test_short_marker_requires_attest(self):
        self.responses[("victim", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "B2")] = (200, {}, '{"last4": "1111"}')
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["marker"] = {"extract": r'"last4": "(\d+)"'}
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(stored["pass_requires_attest"])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_non_2xx_attacker_cannot_pass(self):
        self.responses[("attacker", "A1")] = (403, {}, '{"error": "forbidden"}')
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertIn("failed_at", stored)

    def test_out_of_scope_draft_refused_without_fetch(self):
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["request"]["url"] = "https://evil.example/giftcard/{cardId}"
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        calls = []
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: calls.append(url)
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        self.assertEqual(calls, [])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_staged_method_refused_without_fetch(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["request"]["method"] = "DELETE"
        path.write_text(json.dumps(draft))
        calls = []
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: calls.append(url)
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_STAGED)
        self.assertEqual(calls, [])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())

    def test_draft_without_request_refused(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        path.write_text(json.dumps({"id": "h001", "scenario": "differential"}))
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_GUARD)

    def test_short_marker_pass_can_be_attested(self):
        self.responses[("victim", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "A1")] = (200, {}, '{"last4": "9999"}')
        self.responses[("attacker", "B2")] = (200, {}, '{"last4": "1111"}')
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        draft["marker"] = {"extract": r'"last4": "(\d+)"'}
        (self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").write_text(json.dumps(draft))
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        orig_tty, orig_read = hx.stdin_is_tty, hx.read_line
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        try:
            rc = hx.main(["verify", "h001", "--attest"])
        finally:
            hx.stdin_is_tty, hx.read_line = orig_tty, orig_read
        self.assertEqual(rc, 0)
        attested = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(attested["verdict"], "verified-manual")
        self.assertTrue(attested["proof"])


    def test_differential_applies_health_signals(self):
        self.responses[("victim", "A1")] = (429, {}, "slow down")
        rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, 0)
        state = json.loads(self.repo.rate.read_text())
        self.assertEqual(state["effective"]["www.acme.com"], 4.0)


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
        scope["jev"] = {"enabled": False}
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
            if mode == "escaped_upper":
                escaped = payload.replace("<", "&#x3C;").replace(">", "&gt;")
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

    def test_attest_prints_canary_and_lands_finding(self):
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        canary = draft["proof"]["canary"]
        orig_tty, orig_read = hx.stdin_is_tty, hx.read_line
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                rc = hx.main(["verify", "h001", "--attest"])
        finally:
            hx.stdin_is_tty, hx.read_line = orig_tty, orig_read
        self.assertEqual(rc, 0)
        self.assertIn(canary, out.getvalue())
        self.assertIn("escaped presente", out.getvalue())
        attested = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(attested["verdict"], "verified-manual")

    def test_payload_raw_stays_unencoded(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["request"]["url"] = "https://www.acme.com/s?q={payload_raw}"
        path.write_text(json.dumps(draft))
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertIn("<hx", self.seen_urls[0])
        self.assertNotIn("%3C", self.seen_urls[0])

    def test_template_without_canary_refused(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["payload_template"] = "<hx>"
        path.write_text(json.dumps(draft))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        self.assertIn("{CANARY}", stderr.getvalue())
        self.assertEqual(self.seen_urls, [])

    def test_stale_proof_cleared_on_later_fail(self):
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        draft_path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        stored = json.loads(draft_path.read_text())
        self.assertTrue(stored["pass_requires_attest"])
        self.mode = "escaped"
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        failed = json.loads(draft_path.read_text())
        self.assertNotIn("proof", failed)
        self.assertNotIn("pass_requires_attest", failed)
        orig_tty, orig_read = hx.stdin_is_tty, hx.read_line
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                rc = hx.main(["verify", "h001", "--attest"])
        finally:
            hx.stdin_is_tty, hx.read_line = orig_tty, orig_read
        self.assertEqual(rc, 0)
        self.assertIn("sem prova mecanica", out.getvalue())
        finding = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertNotIn("proof", finding)

    def test_uppercase_entity_escape_fails(self):
        self.mode = "escaped_upper"
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        stored = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(stored["fail_detail"]["escaped_present"])

    def test_escaped_forms_derived_from_real_payload(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["payload_template"] = "<script>{CANARY}</script>"
        self.mode = "escaped"
        passed, steps = hx.run_echo(self.repo, self.repo.scope(), draft)
        self.assertFalse(passed)
        self.assertFalse(steps["raw_present"])
        self.assertTrue(steps["escaped_present"])

    def test_raw_real_payload_not_flagged_escaped(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["payload_template"] = "<script>{CANARY}</script>"
        self.mode = "raw"
        passed, steps = hx.run_echo(self.repo, self.repo.scope(), draft)
        self.assertTrue(passed)
        self.assertTrue(steps["raw_present"])
        self.assertFalse(steps["escaped_present"])

    def test_url_without_placeholder_refused(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["request"]["url"] = "https://www.acme.com/search?q=fixed"
        path.write_text(json.dumps(draft))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        self.assertEqual(self.seen_urls, [])


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
        scope["jev"] = {"enabled": False}
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

    def test_missing_collaborator_refused(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["collaborator"] = {}
        path.write_text(json.dumps(draft))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            rc = hx.main(["verify", "h001"])
        self.assertEqual(rc, hx.EXIT_GUARD)
        self.assertIn("collaborator", stderr.getvalue())
        self.assertEqual(self.target_urls, [])

    def test_retry_after_timeout_drops_fail_metadata(self):
        self.record_hit = False
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertEqual(draft["fail_reason"], "no_interaction_timeout")
        self.record_hit = True
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        promoted = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertTrue(promoted["proof"]["interaction"])
        self.assertNotIn("fail_reason", promoted)
        self.assertNotIn("failed_at", promoted)
        self.assertNotIn("fail_detail", promoted)

    def test_snapshot_http_error_is_transport(self):
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (500, {}, "err")
        self.assertEqual(hx.main(["verify", "h001"]), hx.EXIT_TRANSPORT)
        self.assertEqual(self.target_urls, [])

    def test_snapshot_non_json_is_transport(self):
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (200, {}, "<html>")
        self.assertEqual(hx.main(["verify", "h001"]), hx.EXIT_TRANSPORT)
        self.assertEqual(self.target_urls, [])

    def test_callback_writes_audit_line(self):
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        entries = [json.loads(line) for line in (self.repo.hunt / "audit.log").read_text().splitlines()]
        self.assertTrue(any(entry["tool"] == "callback" for entry in entries))

    def test_interval_above_timeout_polls_once_without_sleep(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["poll_interval_s"] = 60
        draft["interaction_timeout_s"] = 45
        path.write_text(json.dumps(draft))
        self.record_hit = False
        snapshots = []
        poll_rounds = []
        orig = hx.fetch

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            if url == self.poll_url:
                (poll_rounds if self.target_urls else snapshots).append(url)
                return 200, {}, "[]"
            self.target_urls.append(url)
            return 200, {}, "ok"

        hx.fetch = fake_fetch
        try:
            self.assertEqual(hx.main(["verify", "h001"]), 0)
        finally:
            hx.fetch = orig
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(poll_rounds, [self.poll_url])
        self.assertEqual(self.sleeps, [])

    def test_subsecond_interval_clamps_to_one_second(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["poll_interval_s"] = 0.5
        draft["interaction_timeout_s"] = 3
        path.write_text(json.dumps(draft))
        self.record_hit = False
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertTrue(self.sleeps)
        self.assertEqual(set(self.sleeps), {1.0})

    def test_negative_interval_clamps_without_error(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["poll_interval_s"] = -1
        draft["interaction_timeout_s"] = 3
        path.write_text(json.dumps(draft))
        self.record_hit = False
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertTrue(self.sleeps)
        self.assertEqual(set(self.sleeps), {1.0})


class AttestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_tty = hx.stdin_is_tty
        self.orig_read = hx.read_line
        draft = {"id": "h001", "title": "race condition", "scenario": "manual"}
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.stdin_is_tty = self.orig_tty
        hx.read_line = self.orig_read
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_attest_requires_tty(self):
        hx.stdin_is_tty = lambda: False
        self.assertEqual(hx.main(["verify", "h001", "--attest"]), hx.EXIT_TTY)

    def test_attest_wrong_id_aborts(self):
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h999"
        self.assertEqual(hx.main(["verify", "h001", "--attest"]), hx.EXIT_GUARD)

    def test_attest_happy_path(self):
        hx.stdin_is_tty = lambda: True
        hx.read_line = lambda prompt: "h001"
        rc = hx.main(["verify", "h001", "--attest"])
        self.assertEqual(rc, 0)
        attested = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(attested["verdict"], "verified-manual")
        self.assertEqual(attested["attest"]["draft_id"], "h001")
        self.assertIn("uid", attested["attest"])
        self.assertFalse((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())


class ProxyTest(unittest.TestCase):
    def setUp(self):
        import socketserver
        import threading
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        scope = json.loads((self.repo.hunt / "scope.json").read_text())
        scope["in_scope"] = ["127.0.0.1"]
        scope["rate"] = {"per_host_interval_s": 0.0, "global_interval_s": 0.0, "max_wait_s": 5}
        (self.repo.hunt / "scope.json").write_text(json.dumps(scope))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None

        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(1024)
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.proxy = hx.build_proxy(self.repo, scope, 0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()
        hx.sleep = self.orig_sleep
        os.environ.pop("HX_ENGAGEMENT", None)
        self.tmp.cleanup()

    def test_allowed_tunnels_and_denied_403(self):
        import socket
        proxy_port = self.proxy.server_address[1]
        upstream_port = self.upstream.server_address[1]
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        head = client.recv(1024)
        self.assertIn(b"200", head.split(b"\r\n")[0])
        client.sendall(b"ping")
        self.assertIn(b"ok", client.recv(1024))
        client.close()
        client2 = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client2.sendall(b"CONNECT evil.example.org:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        head2 = client2.recv(1024)
        self.assertIn(b"403", head2.split(b"\r\n")[0])
        client2.close()

    def test_second_connect_rate_limited_and_audited(self):
        import socket
        self.proxy.scope["rate"] = {"per_host_interval_s": 30.0, "global_interval_s": 0.0, "max_wait_s": 0}
        proxy_port = self.proxy.server_address[1]
        upstream_port = self.upstream.server_address[1]
        request = f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(request)
        self.assertIn(b"200", client.recv(1024).split(b"\r\n")[0])
        client.close()
        client2 = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client2.sendall(request)
        self.assertIn(b"429", client2.recv(1024).split(b"\r\n")[0])
        client2.close()
        entries = [json.loads(line) for line in (self.repo.hunt / "audit.log").read_text().splitlines()]
        self.assertTrue(any(entry["verdict"] == "rate" for entry in entries))

    def test_odd_port_denied_on_remote_host(self):
        import socket
        self.proxy.scope["in_scope"] = ["127.0.0.1", "acme.com"]
        proxy_port = self.proxy.server_address[1]
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(b"CONNECT acme.com:8443 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"403", client.recv(1024).split(b"\r\n")[0])
        client.close()

    def test_malformed_port_returns_400(self):
        import socket
        proxy_port = self.proxy.server_address[1]
        client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        client.sendall(b"CONNECT acme.com:abc HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"400", client.recv(1024).split(b"\r\n")[0])
        client.close()

    def test_burst_allows_quick_connects(self):
        import socket
        self.proxy.scope["rate"] = {"per_host_interval_s": 30.0, "global_interval_s": 0.0, "max_wait_s": 0, "burst": [2, 10]}
        proxy_port = self.proxy.server_address[1]
        upstream_port = self.upstream.server_address[1]
        request = f"CONNECT 127.0.0.1:{upstream_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        for _ in range(2):
            client = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
            client.sendall(request)
            self.assertIn(b"200", client.recv(1024).split(b"\r\n")[0])
            client.close()


class ProbeHeadersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.repo = hx.Repo(Path(self.tmp.name))
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_probe_headers_passed_to_fetch(self):
        seen = {}
        original_fetch = hx.fetch

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            seen["headers"] = headers
            return 200, {}, "ok"

        hx.fetch = fake_fetch
        scope = {
            "health": {
                "probes": [{"session": "x", "file": None, "method": "GET", "url": "https://www.acme.com/x", "headers": {"X-Requested-With": "ACME-Android"}}],
                "session_invalid_if": [],
                "waf_block_if": [],
                "rate_limited_if": [],
            },
            "rate": {"per_host_interval_s": 2, "global_interval_s": 0.5, "max_wait_s": 30},
        }
        try:
            ok, signal, _, _ = hx.probe_once(self.repo, scope, "x")
        finally:
            hx.fetch = original_fetch
        self.assertTrue(ok)
        self.assertIsNone(signal)
        self.assertEqual(seen["headers"]["X-Requested-With"], "ACME-Android")


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

    def test_debrief_labels_plan_runs(self):
        (self.repo.hunt / "runs.jsonl").write_text(json.dumps({"ts": hx.now(), "mode": "plan", "rc": 0, "tokens": 10, "cost": 0.0}) + "\n")
        hx.main(["debrief"])
        text = sorted((self.repo.hunt / "sessions").glob("*.md"))[0].read_text()
        self.assertIn("- plan: runs 1", text)

    def test_debrief_ignores_malformed_run_lines(self):
        self.add()
        (self.repo.hunt / "runs.jsonl").write_text(json.dumps({"ts": hx.now(), "hyp": "h001", "tokens": 5}) + "\nnot-json\n")
        hx.main(["debrief"])
        text = sorted((self.repo.hunt / "sessions").glob("*.md"))[0].read_text()
        self.assertIn("runs: 1", text)
        self.assertIn("tokens: 5", text)


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_tty = hx.stdin_is_tty
        hx.stdin_is_tty = lambda: False
        self.orig_fetch = hx.fetch
        self.scope = hx.load_json(self.repo.scope_path, {})

    def tearDown(self):
        hx.sleep = self.orig_sleep
        hx.stdin_is_tty = self.orig_tty
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _waf_fire(self, host="www.rei.com"):
        return hx.rate_waf_cooldown(self.scope, self.repo, host)

    def test_first_waf_cooldown_is_30min(self):
        minutes = self._waf_fire()
        state = hx.load_json(self.repo.rate, {})
        entry = state["cooldowns"]["www.rei.com"]
        self.assertEqual(minutes, 30)
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["until"], 1000 + 30 * 60)

    def test_consecutive_waf_doubles_and_caps_at_120(self):
        self.assertEqual(self._waf_fire("h"), 30)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 30 * 60)
        self.assertEqual(self._waf_fire("h"), 60)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 60 * 60)
        self.assertEqual(self._waf_fire("h"), 120)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 120 * 60)
        self.assertEqual(self._waf_fire("h"), 120)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["until"], 1000 + 120 * 60)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["count"], 4)

    def test_rate_acquire_refused_during_cooldown(self):
        self._waf_fire()
        with self.assertRaises(hx.HxError) as ctx:
            hx.rate_acquire(self.scope, self.repo, "www.rei.com", 30)
        self.assertEqual(ctx.exception.code, hx.EXIT_RATE)
        self.assertIn("waf cooldown", str(ctx.exception))

    def test_rate_acquire_ok_after_expiry(self):
        self._waf_fire()
        os.environ["HX_FAKE_NOW"] = str(1000 + 31 * 60)
        self.assertEqual(hx.rate_acquire(self.scope, self.repo, "www.rei.com", 30), 0)

    def test_reset_clears_cooldowns_and_count(self):
        self._waf_fire("h")
        hx.stdin_is_tty = lambda: True
        self.assertEqual(hx.main(["rate", "reset"]), 0)
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"], {})
        self._waf_fire("h")
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["h"]["count"], 1)

    def test_apply_health_signals_sets_cooldown(self):
        scope = dict(self.scope)
        scope["health"] = dict(scope["health"])
        scope["health"]["waf_block_if"] = ["body_contains:_px"]
        hx.apply_health_signals(self.repo, scope, "www.rei.com", 403, {}, "blocked _px")
        state = hx.load_json(self.repo.rate, {})
        self.assertEqual(state["cooldowns"]["www.rei.com"]["count"], 1)

    def test_probe_waf_signal_sets_cooldown(self):
        scope = hx.load_json(self.repo.scope_path, {})
        scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.rei.com/x"}]
        hx.fetch = lambda method, url, headers=None, body=None, cookies=None: (403, {}, "px-captcha aqui")
        ok, signal, _, _ = hx.probe_once(self.repo, scope, "a")
        self.assertFalse(ok)
        self.assertEqual(signal, "waf")
        self.assertEqual(hx.load_json(self.repo.rate, {})["cooldowns"]["www.rei.com"]["count"], 1)


class CoverageLockTest(unittest.TestCase):
    def test_concurrent_updates_keep_both_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engagement(tmp)
            repo = hx.Repo(eng)
            orig_write = hx.atomic_write

            def slow_write(path, text):
                if Path(path).name == "COVERAGE.md":
                    time.sleep(0.5)
                return orig_write(path, text)

            hx.atomic_write = slow_write
            try:
                ctx = multiprocessing.get_context("fork")
                procs = [
                    ctx.Process(target=hx.update_coverage, args=(repo, f"GET /e{i}", "BOLA", "refuted", "ev", "n"))
                    for i in range(2)
                ]
                for proc in procs:
                    proc.start()
                for proc in procs:
                    proc.join()
                self.assertEqual([proc.exitcode for proc in procs], [0, 0])
            finally:
                hx.atomic_write = orig_write
            text = (eng / "hunt" / "COVERAGE.md").read_text()
            self.assertIn("GET /e0", text)
            self.assertIn("GET /e1", text)


class FetchProxyTest(unittest.TestCase):
    def test_fetch_disables_ambient_proxy(self):
        import curl_cffi.requests as creq
        captured = {}

        class FakeCookies:
            def set(self, *args, **kwargs):
                pass

        class FakeResp:
            status_code = 200
            headers = {}
            text = "ok"

        class FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.cookies = FakeCookies()

            def request(self, method, url, headers=None, data=None, timeout=None, allow_redirects=None):
                return FakeResp()

        orig = creq.Session
        creq.Session = FakeSession
        try:
            status, headers, body = hx.fetch("GET", "https://example.com/")
        finally:
            creq.Session = orig
        self.assertEqual(captured.get("proxies"), {"all": ""})
        self.assertEqual(captured.get("trust_env"), False)
        self.assertEqual(status, 200)


class JevCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(self.eng)
        self.scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        self.scope["jev"] = {"enabled": True}
        os.environ["TYPESAFE_API_KEY"] = "tst_key"
        self.orig_http = hx.jev_http

    def tearDown(self):
        hx.jev_http = self.orig_http
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "TYPESAFE_API_KEY", "HX_RUN_ID"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_config_defaults_and_merge(self):
        self.assertEqual(hx.jev_config({})["enabled"], True)
        self.assertEqual(hx.jev_config({"jev": {"enabled": False}})["enabled"], False)
        cfg = hx.jev_config({"jev": {"enabled": True, "thresholds": {"health": 0.9}}})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["thresholds"]["health"], 0.9)
        self.assertEqual(cfg["thresholds"]["dedup"], 0.85)
        self.assertEqual(cfg["model"], "jev-latest")

    def test_malformed_jev_config_falls_back_to_defaults(self):
        cfg = hx.jev_config({"jev": "ligado"})
        self.assertEqual(cfg["enabled"], True)
        self.assertEqual(cfg["max_calls_per_run"], 50)
        self.assertEqual(cfg["thresholds"], hx.JEV_DEFAULTS["thresholds"])
        cfg = hx.jev_config({"jev": {"enabled": True, "max_calls_per_run": "abc", "thresholds": {"health": "alto", "dedup": 0.5}}})
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["max_calls_per_run"], 50)
        self.assertEqual(cfg["thresholds"]["health"], 0.8)
        self.assertEqual(cfg["thresholds"]["dedup"], 0.5)

    def test_numeric_string_cap_coerced(self):
        cfg = hx.jev_config({"jev": {"max_calls_per_run": "2"}})
        self.assertEqual(cfg["max_calls_per_run"], 2)
        self.assertEqual(hx.jev_config({"jev": {"max_calls_per_run": 3.7}})["max_calls_per_run"], 3)

    def test_budget_malformed_cap_uses_default(self):
        self.scope["jev"] = {"enabled": True, "max_calls_per_run": "abc"}
        self.assertTrue(hx.jev_budget_take(self.repo, self.scope))
        self.assertTrue(hx.jev_budget_take(self.repo, {"jev": "ligado"}))

    def test_jev_http_ignores_env_proxy(self):
        handlers = []

        class FakeResponse:
            status = 200
            def read(self):
                return b'{"answers": {}}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                return FakeResponse()

        orig = hx.urllib.request.build_opener
        hx.urllib.request.build_opener = lambda *args: handlers.extend(args) or FakeOpener()
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        try:
            result = hx.jev_http({"a": 1}, "k")
        finally:
            hx.urllib.request.build_opener = orig
            os.environ.pop("HTTPS_PROXY", None)
        self.assertEqual(result, (200, {"answers": {}}))
        self.assertIsInstance(handlers[0], hx.urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})

    def test_redact_state_keeps_json_scalars(self):
        state = {"ts": 1758000000000, "excerpt": "mail a@b.com", "nested": [1758000000000, {"to": "c@d.com"}]}
        out = hx.redact_state(state)
        self.assertEqual(out["ts"], 1758000000000)
        self.assertEqual(out["nested"][0], 1758000000000)
        self.assertIn("email-redacted", out["excerpt"])
        self.assertIn("email-redacted", out["nested"][1]["to"])

    def test_redacts_state_without_breaking_json_types(self):
        captured = {}
        def fake_http(payload, key, timeout=10):
            captured.update(payload)
            return 200, {"answers": {"q": {"type": "noul", "noul": 0.9}}}
        hx.jev_http = fake_http
        answers = hx.jev_ask(self.repo, self.scope, "health", {"ts": 1758000000000, "excerpt": "mail a@b.com"}, {"q": {"type": "noul", "instructions": "?"}}, 0.8)
        self.assertEqual(answers["q"]["noul"], 0.9)
        self.assertEqual(captured["state"]["ts"], 1758000000000)
        self.assertIn("email-redacted", captured["state"]["excerpt"])

    def test_non_dict_answers_is_unavailable(self):
        hx.jev_http = lambda *a, **k: (200, {"answers": ["nope"]})
        self.assertIsNone(hx.jev_ask(self.repo, self.scope, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        self.assertIn("answers invalido", (self.eng / "hunt" / "audit.log").read_text())

    def test_disabled_makes_no_call_and_no_audit(self):
        called = []
        hx.jev_http = lambda *a, **k: called.append(1)
        self.assertIsNone(hx.jev_ask(self.repo, {"jev": {"enabled": False}}, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        self.assertEqual(called, [])

    def test_missing_key_is_unavailable(self):
        os.environ.pop("TYPESAFE_API_KEY", None)
        hx.jev_http = lambda *a, **k: (_ for _ in ()).throw(AssertionError("nao deveria chamar"))
        self.assertIsNone(hx.jev_ask(self.repo, self.scope, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        audit_text = (self.eng / "hunt" / "audit.log").read_text()
        self.assertIn("unavailable", audit_text)

    def test_redacts_state_before_post(self):
        captured = {}
        def fake_http(payload, key, timeout=10):
            captured.update(payload)
            return 200, {"answers": {"q": {"type": "noul", "noul": 0.9}}}
        hx.jev_http = fake_http
        answers = hx.jev_ask(self.repo, self.scope, "health", {"excerpt": "mail me a@b.com"}, {"q": {"type": "noul", "instructions": "?"}}, 0.8)
        self.assertEqual(answers["q"]["noul"], 0.9)
        self.assertNotIn("a@b.com", json.dumps(captured["state"]))
        self.assertIn("email-redacted", json.dumps(captured["state"]))

    def test_http_error_is_unavailable_and_returns_none(self):
        hx.jev_http = lambda *a, **k: (429, {})
        self.assertIsNone(hx.jev_ask(self.repo, self.scope, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        self.assertIn("unavailable", (self.eng / "hunt" / "audit.log").read_text())

    def test_budget_cap_blocks_after_max_calls(self):
        hx.jev_http = lambda *a, **k: (200, {"answers": {"q": {"type": "noul", "noul": 0.9}}})
        self.scope["jev"] = {"enabled": True, "max_calls_per_run": 2}
        for _ in range(2):
            self.assertIsNotNone(hx.jev_ask(self.repo, self.scope, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        self.assertIsNone(hx.jev_ask(self.repo, self.scope, "health", {"x": 1}, {"q": {"type": "noul", "instructions": "?"}}, 0.8))
        self.assertIn("cap", (self.eng / "hunt" / "audit.log").read_text())

    def test_budget_bucket_by_run_id_and_hour(self):
        os.environ["HX_RUN_ID"] = "run-a"
        self.scope["jev"] = {"enabled": True, "max_calls_per_run": 1}
        hx.jev_http = lambda *a, **k: (200, {"answers": {}})
        self.assertTrue(hx.jev_budget_take(self.repo, self.scope))
        self.assertFalse(hx.jev_budget_take(self.repo, self.scope))
        os.environ["HX_RUN_ID"] = "run-b"
        self.assertTrue(hx.jev_budget_take(self.repo, self.scope))
        os.environ.pop("HX_RUN_ID")
        os.environ["HX_FAKE_NOW"] = "7600"
        self.assertTrue(hx.jev_budget_take(self.repo, self.scope))


class JevHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        hx.main(["init", "--dir", str(self.eng)])
        os.environ["HX_ENGAGEMENT"] = str(self.eng)
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(self.eng)
        self.scope = hx.load_json(self.eng / "hunt" / "scope.json", {})
        self.scope["jev"] = {"enabled": True}
        self.orig_ask = hx.jev_ask
        self.orig_fetch = hx.fetch

    def tearDown(self):
        hx.jev_ask = self.orig_ask
        hx.fetch = self.orig_fetch
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW", "TYPESAFE_API_KEY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_apply_health_signals_jev_block_sets_cooldown(self):
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "block", "probabilities": {"block": 0.95, "session_invalid": 0.03, "normal": 0.02}}}
        hx.apply_health_signals(self.repo, self.scope, "www.acme.com", 403, {}, "Access denied")
        state = hx.load_json(self.repo.rate, {})
        self.assertEqual(state["cooldowns"]["www.acme.com"]["count"], 1)

    def test_low_probability_is_noop(self):
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "block", "probabilities": {"block": 0.4, "session_invalid": 0.3, "normal": 0.3}}}
        hx.apply_health_signals(self.repo, self.scope, "www.acme.com", 403, {}, "Access denied")
        self.assertNotIn("cooldowns", hx.load_json(self.repo.rate, {}))

    def test_non_dict_probabilities_is_noop(self):
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "block", "probabilities": "alta"}}
        hx.apply_health_signals(self.repo, self.scope, "www.acme.com", 403, {}, "Access denied")
        self.assertNotIn("cooldowns", hx.load_json(self.repo.rate, {}))

    def test_non_dict_answer_is_noop(self):
        hx.jev_ask = lambda *a, **k: {"health": "bloqueio"}
        hx.apply_health_signals(self.repo, self.scope, "www.acme.com", 403, {}, "Access denied")
        self.assertNotIn("cooldowns", hx.load_json(self.repo.rate, {}))

    def test_probe_non_dict_probabilities_is_noop(self):
        self.scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.acme.com/x"}]
        self.scope["health"]["session_invalid_if"] = []
        self.scope["health"]["waf_block_if"] = []
        hx.fetch = lambda *a, **k: (403, {}, "denied")
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "block", "probabilities": 1}}
        ok, signal, status, excerpt = hx.probe_once(self.repo, self.scope, "a")
        self.assertTrue(ok)
        self.assertIsNone(signal)

    def test_disabled_never_calls_jev(self):
        called = []
        hx.jev_ask = lambda *a, **k: called.append(1)
        scope = dict(self.scope)
        scope["jev"] = {"enabled": False}
        hx.apply_health_signals(self.repo, scope, "www.acme.com", 403, {}, "Access denied")
        self.assertEqual(called, [])

    def test_probe_jev_session_invalid_returns_signal(self):
        self.scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.acme.com/x"}]
        self.scope["health"]["session_invalid_if"] = []
        self.scope["health"]["waf_block_if"] = []
        hx.fetch = lambda *a, **k: (403, {}, "Please sign in")
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "session_invalid", "probabilities": {"session_invalid": 0.9, "block": 0.05, "normal": 0.05}}}
        ok, signal, status, excerpt = hx.probe_once(self.repo, self.scope, "a")
        self.assertFalse(ok)
        self.assertEqual(signal, "invalid")

    def test_apply_health_signals_session_invalid_is_audit_only(self):
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "session_invalid", "probabilities": {"session_invalid": 0.95, "block": 0.03, "normal": 0.02}}}
        hx.apply_health_signals(self.repo, self.scope, "www.acme.com", 403, {}, "Please sign in")
        state = hx.load_json(self.repo.rate, {})
        self.assertNotIn("cooldowns", state)
        self.assertNotIn("effective", state)
        self.assertFalse(self.repo.health.exists())

    def test_probe_jev_block_sets_cooldown_and_signal(self):
        self.scope["health"]["probes"] = [{"session": "a", "file": None, "method": "GET", "url": "https://www.acme.com/x"}]
        self.scope["health"]["session_invalid_if"] = []
        self.scope["health"]["waf_block_if"] = []
        hx.fetch = lambda *a, **k: (403, {}, "Just a moment")
        hx.jev_ask = lambda *a, **k: {"health": {"type": "choice", "choice": "block", "probabilities": {"block": 0.95, "session_invalid": 0.03, "normal": 0.02}}}
        ok, signal, status, excerpt = hx.probe_once(self.repo, self.scope, "a")
        self.assertFalse(ok)
        self.assertEqual(signal, "waf")
        state = hx.load_json(self.repo.rate, {})
        self.assertEqual(state["cooldowns"]["www.acme.com"]["count"], 1)


class JevVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        self.repo = hx.Repo(self.eng)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None
        self.orig_ask = hx.jev_ask
        self.orig_fetch = hx.fetch
        self.ask_calls = []
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["jev"] = {"enabled": True}
        scope_path.write_text(json.dumps(scope))
        recon = self.eng / "recon"
        recon.mkdir()
        (recon / "session_a.json").write_text(json.dumps([{"name": "s", "value": "victim", "domain": "www.acme.com"}]))
        (recon / "session_b.json").write_text(json.dumps([{"name": "s", "value": "attacker", "domain": "www.acme.com"}]))
        self.responses = {
            ("victim", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "A1"): (200, {}, '{"code": "ACME-778899"}'),
            ("attacker", "B2"): (200, {}, '{"code": "ACME-111111"}'),
        }

        def fake_fetch(method, url, headers=None, body=None, cookies=None):
            tag = (cookies or [{}])[0].get("value")
            object_id = url.rsplit("/", 1)[-1]
            return self.responses[(tag, object_id)]

        hx.fetch = fake_fetch
        draft = {
            "id": "h001",
            "title": "BOLA giftcard",
            "scenario": "differential",
            "attacker_session": "recon/session_b.json",
            "victim_session": "recon/session_a.json",
            "request": {"method": "GET", "url": "https://www.acme.com/giftcard/{cardId}", "victim_object": {"cardId": "A1"}},
            "marker": {"extract": r'"code": "([A-Z0-9-]+)"'},
            "control": {"attacker_own_object": {"cardId": "B2"}},
        }
        hx.dump_json(self.repo.hunt / "FINDINGS" / "drafts" / "h001.json", draft)

    def tearDown(self):
        hx.jev_ask = self.orig_ask
        hx.fetch = self.orig_fetch
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def stub_jev(self, value):
        def fake(*args, **kwargs):
            self.ask_calls.append(args)
            if value is None:
                return None
            return {"sustains": {"type": "noul", "noul": value}}
        hx.jev_ask = fake

    def test_jev_high_promotes(self):
        self.stub_jev(0.9)
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        self.assertFalse((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").exists())
        finding = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(finding["jev"]["noul"], 0.9)
        self.assertFalse(finding["jev"]["weak"])

    def test_jev_mid_promotes_weak(self):
        self.stub_jev(0.6)
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        finding = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertEqual(finding["jev"]["noul"], 0.6)
        self.assertTrue(finding["jev"]["weak"])

    def test_jev_low_retains_draft(self):
        self.stub_jev(0.2)
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(draft["pass_requires_attest"])
        self.assertEqual(draft["jev"], {"noul": 0.2, "fail": True})

    def test_jev_unavailable_with_enabled_retains(self):
        self.stub_jev(None)
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertFalse((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(draft["pass_requires_attest"])
        self.assertEqual(draft["jev"], {"unavailable": True})

    def test_jev_disabled_unchanged(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["jev"] = {"enabled": False}
        scope_path.write_text(json.dumps(scope))
        self.stub_jev(0.9)
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        self.assertTrue((self.repo.hunt / "FINDINGS" / "h001.json").exists())
        finding = json.loads((self.repo.hunt / "FINDINGS" / "h001.json").read_text())
        self.assertNotIn("jev", finding)
        self.assertEqual(self.ask_calls, [])

    def test_jev_non_numeric_noul_fails_closed(self):
        self.stub_jev("alta")
        self.assertEqual(hx.main(["verify", "h001"]), 0)
        draft = json.loads((self.repo.hunt / "FINDINGS" / "drafts" / "h001.json").read_text())
        self.assertTrue(draft["pass_requires_attest"])
        self.assertEqual(draft["jev"], {"noul": 0.0, "fail": True})

    def test_verify_jev_state_drops_callback_hit(self):
        path = self.repo.hunt / "FINDINGS" / "drafts" / "h001.json"
        draft = json.loads(path.read_text())
        draft["scenario"] = "callback"
        path.write_text(json.dumps(draft))
        steps = {"status": 200, "baseline_hits": 0, "interaction": True,
                 "hit": [{"headers": {"authorization": "Bearer segredo"}, "body": "cookie=123"}]}
        orig_callback = hx.run_callback
        hx.run_callback = lambda repo, scope, draft: (True, steps)
        self.stub_jev(0.9)
        try:
            rc = hx.main(["verify", "h001"])
        finally:
            hx.run_callback = orig_callback
        self.assertEqual(rc, 0)
        state = self.ask_calls[0][3]
        self.assertNotIn("hit", state["steps"])
        self.assertTrue(state["steps"]["interaction"])
        self.assertEqual(state["steps"]["baseline_hits"], 0)


class JevDedupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = Path(self.tmp.name)
        hx.main(["init", "--dir", self.tmp.name])
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(self.eng)
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["jev"] = {"enabled": True}
        scope_path.write_text(json.dumps(scope))
        self.orig_ask = hx.jev_ask
        self.ask_calls = []
        self.add()

    def tearDown(self):
        hx.jev_ask = self.orig_ask
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def add(self, *extra):
        return hx.main(["hypothesis", "add", "--claim", "c", "--endpoint", "GET /w", "--class", "BOLA", "--confirm", "x", "--refute", "y", *extra])

    def stub_ask(self, choice, probability):
        def fake(*args, **kwargs):
            self.ask_calls.append(args)
            return {"same": {"type": "choice", "choice": choice, "probabilities": {choice: probability, "new": 1 - probability}}}
        hx.jev_ask = fake

    def test_equivalent_refuses_without_force(self):
        self.stub_ask("h001", 0.95)
        self.assertEqual(self.add(), hx.EXIT_GUARD)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(len(hyps), 1)
        self.assertEqual(len(self.ask_calls), 1)

    def test_force_records_duplicate_of(self):
        self.stub_ask("h001", 0.95)
        self.assertEqual(self.add("--force"), 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(len(hyps), 2)
        self.assertEqual(hyps[1]["id"], "h002")
        self.assertEqual(hyps[1]["duplicate_of"], "h001")

    def test_low_probability_adds(self):
        self.stub_ask("h001", 0.4)
        self.assertEqual(self.add(), 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(len(hyps), 2)
        self.assertIsNone(hyps[1]["duplicate_of"])

    def test_force_low_probability_does_not_record_duplicate(self):
        self.stub_ask("h001", 0.4)
        self.assertEqual(self.add("--force"), 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(len(hyps), 2)
        self.assertIsNone(hyps[1]["duplicate_of"])

    def test_disabled_never_calls(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["jev"] = {"enabled": False}
        scope_path.write_text(json.dumps(scope))
        self.assertEqual(self.add(), 0)
        hyps, _ = hx.load_hyps(self.repo)
        self.assertEqual(len(hyps), 2)
        self.assertEqual(self.ask_calls, [])


class AuditFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hx.main(["init", "--dir", self.tmp.name])
        self.eng = Path(self.tmp.name)
        os.environ["HX_ENGAGEMENT"] = self.tmp.name
        os.environ["HX_FAKE_NOW"] = "1000"
        self.repo = hx.Repo(self.eng)
        self.orig_sleep = hx.sleep
        hx.sleep = lambda seconds: None

    def tearDown(self):
        hx.sleep = self.orig_sleep
        for key in ("HX_ENGAGEMENT", "HX_FAKE_NOW"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_save_outside_eng_refused(self):
        request = {"method": "GET", "url": "https://www.acme.com/x", "headers": {}, "body": None}
        response = {"status": 200, "headers": {}, "body": "ok"}
        with self.assertRaises(hx.HxError) as ctx:
            hx.save_evidence(self.repo, "../../tmp/evil-huntx", {"ts": 1}, request, response, {})
        self.assertEqual(ctx.exception.code, hx.EXIT_GUARD)
        self.assertFalse(Path("/tmp/evil-huntx").exists())

    def test_save_absolute_traversal_refused(self):
        request = {"method": "GET", "url": "https://www.acme.com/x", "headers": {}, "body": None}
        response = {"status": 200, "headers": {}, "body": "ok"}
        evil = self.repo.hunt / "FINDINGS" / "drafts" / ".." / ".." / ".." / ".." / ".." / ".." / "tmp" / "hx-evil-audit"
        with self.assertRaises(hx.HxError) as ctx:
            hx.save_evidence(self.repo, evil, {"ts": 1}, request, response, {})
        self.assertEqual(ctx.exception.code, hx.EXIT_GUARD)
        self.assertFalse(Path("/tmp/hx-evil-audit").exists())

    def test_save_inside_eng_allowed(self):
        request = {"method": "GET", "url": "https://www.acme.com/x", "headers": {}, "body": None}
        response = {"status": 200, "headers": {}, "body": "ok", "integrity": {"raw_sha256": "x", "body_len": 2}}
        path = hx.save_evidence(self.repo, "scans/evidence/_misc", {"ts": 1}, request, response, {})
        self.assertTrue(path.exists())

    def test_evidence_traversal_refused(self):
        with self.assertRaises(hx.HxError) as ctx:
            hx.evidence_auth_failure(self.repo, {"id": "h", "evidence": "../outside"})
        self.assertEqual(ctx.exception.code, hx.EXIT_GUARD)

    def test_marker_redos_refused(self):
        with self.assertRaises(hx.HxError) as ctx:
            hx.extract_marker("(a+)+$", "aaab")
        self.assertEqual(ctx.exception.code, hx.EXIT_GUARD)
        for pattern in ("(a|a)+$", "((a|a))+", r"(\w+|\d+)+$"):
            with self.assertRaises(hx.HxError):
                hx.extract_marker(pattern, "aaab")
        self.assertEqual(hx.extract_marker(r'"last4": "(\d+)"', '{"last4": "4242"}'), "4242")

    def test_rate_sleep_outside_lock(self):
        scope = self.repo.scope()
        hx.rate_acquire(scope, self.repo, "example.com", 30)
        held_during_sleep = []
        holding = {"v": False}
        orig_flock = hx.flock
        orig_sleep = hx.sleep

        @contextlib.contextmanager
        def spy_flock(path):
            holding["v"] = True
            try:
                with orig_flock(path):
                    yield
            finally:
                holding["v"] = False

        hx.flock = spy_flock
        hx.sleep = lambda seconds: held_during_sleep.append(holding["v"])
        try:
            hx.rate_acquire(scope, self.repo, "example.com", 30)
        finally:
            hx.flock = orig_flock
            hx.sleep = orig_sleep
        self.assertEqual(held_during_sleep, [False])

    def test_init_gitignores_sessions(self):
        self.assertIn("recon/session_", (self.eng / ".gitignore").read_text())

    def test_alias_missing_file_refused(self):
        scope_path = self.eng / "hunt" / "scope.json"
        scope = json.loads(scope_path.read_text())
        scope["in_scope"] = ["www.acme.com"]
        scope["health"] = {"probes": [{"session": "b", "file": "recon/session_b.json", "method": "GET", "url": "https://www.acme.com/x"}], "fresh_max_s": 600, "session_invalid_if": [], "waf_block_if": [], "rate_limited_if": []}
        scope_path.write_text(json.dumps(scope))
        with self.assertRaises(hx.HxError) as ctx:
            hx.resolve_session_path(self.repo, self.repo.scope(), "b")
        self.assertEqual(ctx.exception.code, hx.EXIT_GUARD)


if __name__ == "__main__":
    unittest.main()
