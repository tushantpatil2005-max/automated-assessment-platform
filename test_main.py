"""Tests for the single-file application (main.py).

    pip install httpx            # plus the requirements listed at the top of main.py
    python -m unittest test_main -v

The sandbox used here runs code in a local subprocess. It is NOT a security boundary; it exists so the
tests run without Docker.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import main
except ImportError as exc:                      # FastAPI stack not installed
    raise unittest.SkipTest(f"install the requirements listed in main.py ({exc})")

# Everything lives in one module, so the helpers are all attributes of `main`.
scoring = security = main
evaluate_submission = main.evaluate_submission
SandboxError, SandboxResult, SandboxTimeout, read_capped = (
    main.SandboxError, main.SandboxResult, main.SandboxTimeout, main.read_capped)

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "sqlalchemy", "pydantic_settings", "httpx", "email_validator"))


class SubprocessSandbox:
    def __init__(self, timeout: int = 5) -> None:
        self.timeout = timeout

    def run_python(self, code, stdin="", timeout=None):
        timeout = timeout or self.timeout
        start = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "solution.py")
            with open(path, "w") as fh:
                fh.write(code)
            try:
                proc = subprocess.run([sys.executable, "-I", "-B", path], input=stdin, capture_output=True,
                                      text=True, timeout=timeout, cwd=tmp)
            except subprocess.TimeoutExpired as exc:
                raise SandboxTimeout("timeout") from exc
        return SandboxResult(proc.stdout, proc.stderr, proc.returncode, (time.monotonic() - start) * 1000)

class ScoringTests(unittest.TestCase):
    def test_output_normalisation(self):
        self.assertTrue(scoring.outputs_match("4\r\n", "4"))
        self.assertTrue(scoring.outputs_match("a  \nb\n\n", "a\nb"))
        self.assertFalse(scoring.outputs_match("a b", "a  b"))

    def test_correctness_percent(self):
        self.assertEqual(scoring.correctness_percent(1, 3), 33.33)
        self.assertEqual(scoring.correctness_percent(0, 0), 0.0)

    def test_perfect_solution_without_rubric_scores_100(self):
        # The original code capped this at 70 because the rubric score was hard-coded to 0.
        self.assertEqual(scoring.total_score(100.0, None), 100.0)

    def test_total_with_rubric_uses_weight(self):
        self.assertEqual(scoring.total_score(100.0, 50.0, 0.7), 85.0)

    def test_rubric_weighted_average(self):
        self.assertEqual(scoring.rubric_percent({"a": 100, "b": 50}, {"a": 1, "b": 3}), 62.5)
        self.assertEqual(scoring.rubric_percent({"a": 100}, {"a": 1, "b": 1}), 50.0)   # missing counts as 0

    def test_points_scale_to_max_score(self):
        self.assertEqual(scoring.points(80.0, 50), 40.0)

    def test_hidden_test_cases_removed_from_assessment(self):
        data = {"questions": [{"test_cases": [{"input": "1", "expected_output": "2", "is_hidden": False},
                                              {"input": "9", "expected_output": "18", "is_hidden": True}]}]}
        shown = scoring.redact_assessment(data, include_hidden=False)
        self.assertEqual(len(shown["questions"][0]["test_cases"]), 1)
        self.assertEqual(len(data["questions"][0]["test_cases"]), 2)          # original untouched
        self.assertEqual(len(scoring.redact_assessment(data, True)["questions"][0]["test_cases"]), 2)

    def test_hidden_results_lose_detail(self):
        bd = {"results": [{"test_case_index": 0, "hidden": False, "passed": False, "stderr": "boom"},
                          {"test_case_index": 1, "hidden": True, "passed": False, "stderr": "secret"}]}
        out = scoring.redact_breakdown(bd, include_hidden=False)["results"]
        self.assertEqual(out[0]["stderr"], "boom")
        self.assertEqual(out[1], {"test_case_index": 1, "hidden": True, "passed": False})


class SecurityTests(unittest.TestCase):
    def test_keys_are_unique_and_hash_verifies(self):
        (raw1, h1), (raw2, h2) = security.generate_api_key(), security.generate_api_key()
        self.assertNotEqual(raw1, raw2)
        self.assertEqual(security.hash_api_key(raw1), h1)
        self.assertNotIn(raw1, h1)

    def test_admin_key_check(self):
        self.assertTrue(security.verify_admin_key("secret", "secret"))
        self.assertFalse(security.verify_admin_key("wrong", "secret"))
        self.assertFalse(security.verify_admin_key("", ""))          # disabled when no key configured
        self.assertFalse(security.verify_admin_key("anything", ""))


class ReadCappedTests(unittest.TestCase):
    def test_under_cap(self):
        self.assertEqual(read_capped([b"ab", b"cd"], 10), ("abcd", False))

    def test_over_cap_truncates(self):
        self.assertEqual(read_capped([b"abc", b"def"], 4), ("abcd", True))

    def test_infinite_stream_stops(self):
        def endless():
            while True:
                yield b"x" * 1000
        text, cut = read_capped(endless(), 2500)
        self.assertEqual((len(text), cut), (2500, True))


class EvaluatorTests(unittest.TestCase):
sandbox = SubprocessSandbox(timeout=2)
    cases = [{"input": "2", "expected_output": "4"}, {"input": "5", "expected_output": "10", "is_hidden": True}]

    def test_correct_solution(self):
        r = evaluate_submission(self.sandbox, "print(int(input()) * 2)", self.cases)
        self.assertEqual((r["passed"], r["total"], r["correctness_score"]), (2, 2, 100.0))

    def test_partially_correct(self):
        r = evaluate_submission(self.sandbox, "print(4)", self.cases)
        self.assertEqual((r["passed"], r["correctness_score"]), (1, 50.0))

    def test_runtime_error_reports_stderr(self):
        r = evaluate_submission(self.sandbox, "raise ValueError('nope')", self.cases)
        self.assertEqual(r["correctness_score"], 0.0)
        self.assertIn("ValueError", r["results"][0]["stderr"])

    def test_infinite_loop_times_out(self):
        r = evaluate_submission(self.sandbox, "while True: pass", self.cases[:1], timeout=1)
        self.assertEqual(r["results"][0]["error"], "timeout")
        self.assertFalse(r["results"][0]["passed"])

    def test_sandbox_failure_does_not_leak_details(self):
        class Broken:
            def run_python(self, *a, **k):
                raise SandboxError("docker daemon at /var/run/docker.sock refused")
        r = evaluate_submission(Broken(), "print(1)", self.cases[:1])
        self.assertEqual(r["results"][0]["error"], "sandbox_error")
        self.assertNotIn("docker", str(r))

    def test_truncated_or_oom_output_fails(self):
        class Noisy:
            def run_python(self, *a, **k):
                return SandboxResult("4", "", 0, 1.0, truncated=True)
        self.assertEqual(evaluate_submission(Noisy(), "x", self.cases[:1])["passed"], 0)


@unittest.skipUnless(HAVE_DEPS, "FastAPI stack not installed")
class ApiTests(unittest.TestCase):
    ADMIN = "admin-secret"

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from main import InMemoryCache, Settings, create_app

        settings = Settings(DATABASE_URL="sqlite+pysqlite:///:memory:", AUTO_CREATE_TABLES=True,
                            ADMIN_API_KEY=cls.ADMIN, CACHE_BACKEND="memory")
        cls.app = create_app(settings, sandbox=SubprocessSandbox(timeout=2), cache=InMemoryCache())
        cls._ctx = TestClient(cls.app)
        cls.client = cls._ctx.__enter__()
        cls.key_a = cls.provision("Tenant A", "tenant-a")
        cls.key_b = cls.provision("Tenant B", "tenant-b")

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    @classmethod
    def provision(cls, name, slug):
        r = cls.client.post("/api/v1/tenants", json={"name": name, "slug": slug}, headers={"X-Admin-Key": cls.ADMIN})
        assert r.status_code == 201, r.text
        return r.json()["api_key"]

    @staticmethod
    def h(key):
        return {"X-API-Key": key}

    def make_candidate(self, key, email):
        r = self.client.post("/api/v1/candidates", json={"email": email, "full_name": "Test Candidate"}, headers=self.h(key))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def make_assessment(self, key, criteria=None):
        body = {"title": "Doubler", "questions": [{
            "prompt": "Print twice the input", "max_score": 50,
            "test_cases": [{"input": "2", "expected_output": "4"},
                           {"input": "5", "expected_output": "10", "is_hidden": True}],
            "rubric_criteria": criteria or []}]}
        r = self.client.post("/api/v1/assessments", json=body, headers=self.h(key))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def submit(self, key, candidate_id, question_id, code):
        r = self.client.post("/api/v1/submissions", json={"candidate_id": candidate_id, "question_id": question_id,
                                                         "code": code}, headers=self.h(key))
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["id"]     # TestClient finishes background tasks before returning

    # ---- auth -----------------------------------------------------------
    def test_provisioning_requires_admin_key(self):
        body = {"name": "X", "slug": "tenant-x"}
        self.assertEqual(self.client.post("/api/v1/tenants", json=body).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/tenants", json=body, headers={"X-Admin-Key": "wrong"}).status_code, 401)

    def test_tenant_endpoints_require_valid_api_key(self):
        self.assertEqual(self.client.get("/api/v1/candidates").status_code, 422)          # header missing
        self.assertEqual(self.client.get("/api/v1/candidates", headers=self.h("apk_bad")).status_code, 401)

    def test_pagination_bounds(self):
        self.assertEqual(self.client.get("/api/v1/candidates?limit=0", headers=self.h(self.key_a)).status_code, 422)

    # ---- candidates -----------------------------------------------------
    def test_duplicate_candidate_email_conflicts(self):
        self.make_candidate(self.key_a, "dup@example.com")
        r = self.client.post("/api/v1/candidates", json={"email": "DUP@example.com", "full_name": "Again"},
                             headers=self.h(self.key_a))
        self.assertEqual(r.status_code, 409)
        self.make_candidate(self.key_b, "dup@example.com")     # same email in another tenant is fine

    # ---- assessments and isolation ----------------------------------------
    def test_hidden_test_cases_hidden_by_default(self):
        a = self.make_assessment(self.key_a)
        shown = self.client.get(f"/api/v1/assessments/{a['id']}", headers=self.h(self.key_a)).json()
        full = self.client.get(f"/api/v1/assessments/{a['id']}?include_hidden=true", headers=self.h(self.key_a)).json()
        self.assertEqual(len(shown["questions"][0]["test_cases"]), 1)
        self.assertEqual(len(full["questions"][0]["test_cases"]), 2)

    def test_tenants_cannot_see_each_others_data(self):
        a = self.make_assessment(self.key_a)
        self.assertEqual(self.client.get(f"/api/v1/assessments/{a['id']}", headers=self.h(self.key_b)).status_code, 404)
        cand_b = self.make_candidate(self.key_b, "b-only@example.com")
        r = self.client.post("/api/v1/submissions", headers=self.h(self.key_b), json={
            "candidate_id": cand_b, "question_id": a["questions"][0]["id"], "code": "print(1)"})
        self.assertEqual(r.status_code, 404)

    def test_assessment_needs_a_test_case(self):
        r = self.client.post("/api/v1/assessments", headers=self.h(self.key_a),
                             json={"title": "Bad", "questions": [{"prompt": "p", "test_cases": []}]})
        self.assertEqual(r.status_code, 422)

    # ---- evaluation -------------------------------------------------------
    def test_correct_solution_scores_full_points(self):
        a, cand = self.make_assessment(self.key_a), self.make_candidate(self.key_a, "ok@example.com")
        sid = self.submit(self.key_a, cand, a["questions"][0]["id"], "print(int(input()) * 2)")
        sub = self.client.get(f"/api/v1/submissions/{sid}", headers=self.h(self.key_a)).json()
        self.assertEqual(sub["status"], "evaluated")
        ev = self.client.get(f"/api/v1/submissions/{sid}/evaluation", headers=self.h(self.key_a)).json()
        self.assertEqual((ev["total_score"], ev["points_awarded"], ev["max_score"]), (100.0, 50.0, 50))
        hidden = [r for r in ev["breakdown"]["results"] if r["hidden"]][0]
        self.assertEqual(set(hidden), {"test_case_index", "hidden", "passed"})

    def test_wrong_solution_scores_zero_and_timeouts_are_reported(self):
        a, cand = self.make_assessment(self.key_a), self.make_candidate(self.key_a, "bad@example.com")
        qid = a["questions"][0]["id"]
        wrong = self.submit(self.key_a, cand, qid, "print(0)")
        loop = self.submit(self.key_a, cand, qid, "while True: pass")
        ev1 = self.client.get(f"/api/v1/submissions/{wrong}/evaluation", headers=self.h(self.key_a)).json()
        ev2 = self.client.get(f"/api/v1/submissions/{loop}/evaluation?include_hidden=true", headers=self.h(self.key_a)).json()
        self.assertEqual(ev1["total_score"], 0.0)
        self.assertEqual(ev2["breakdown"]["results"][0]["error"], "timeout")

    def test_rubric_grading_recomputes_total(self):
        a = self.make_assessment(self.key_a, [{"name": "Clarity", "weight": 1}, {"name": "Efficiency", "weight": 3}])
        cand = self.make_candidate(self.key_a, "rubric@example.com")
        q = a["questions"][0]
        sid = self.submit(self.key_a, cand, q["id"], "print(int(input()) * 2)")
        c1, c2 = q["rubric_criteria"][0]["id"], q["rubric_criteria"][1]["id"]
        r = self.client.put(f"/api/v1/submissions/{sid}/rubric", json={"scores": {c1: 100, c2: 50}}, headers=self.h(self.key_a))
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual((out["rubric_score"], out["total_score"]), (62.5, 88.75))
        partial = self.client.put(f"/api/v1/submissions/{sid}/rubric", json={"scores": {c1: 100}}, headers=self.h(self.key_a))
        self.assertEqual(partial.status_code, 422)


if __name__ == "__main__":
    unittest.main()
