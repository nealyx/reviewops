"""Behavioral and HTTP tests; all writes use temporary synthetic data."""

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import (
    APIError, MAX_BODY_BYTES, MAX_PREVIEW_BYTES,
    ReviewHandler, ReviewHTTPServer, ReviewStore, safe_file,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReviewStore(self.root / "runtime")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def decision(self, run_id, decision="approved", version=1):
        return self.store.decision(run_id, {
            "decision": decision,
            "actor": "Synthetic reviewer",
            "note": "Reviewed the synthetic evidence.",
            "expected_version": version,
        })

    def test_seed_has_verified_missing_changed_and_empty_cases(self):
        statuses = {run["id"]: run["evidence_status"] for run in self.store.state()["runs"]}
        self.assertEqual(statuses, {
            "SYN-001": "verified", "SYN-002": "missing", "SYN-003": "changed",
            "SYN-004": "empty", "SYN-005": "verified",
        })

    def test_all_invalid_evidence_is_blocked_in_queue_before_review(self):
        runs = {run["id"]: run for run in self.store.state()["runs"]}
        for run_id in ("SYN-002", "SYN-003", "SYN-004"):
            self.assertEqual(runs[run_id]["status"], "blocked")
            self.assertEqual(runs[run_id]["recorded_status"], "needs_review")
        self.assertEqual(runs["SYN-001"]["status"], "needs_review")

    def test_approval_rejects_missing_tampered_and_empty_without_writing(self):
        count = len(self.store.events())
        for run_id in ("SYN-002", "SYN-003", "SYN-004"):
            with self.subTest(run=run_id), self.assertRaises(APIError) as raised:
                self.decision(run_id)
            self.assertEqual(raised.exception.status, 422)
            self.assertEqual(self.store.detail(run_id)["run"]["version"], 1)
        self.assertEqual(len(self.store.events()), count)

    def test_changed_approved_evidence_blocks_effective_status_preserving_audit(self):
        approved = self.decision("SYN-001")
        original_events = approved["events"]
        self.assertEqual(approved["run"]["status"], "approved")
        evidence_path = self.store.evidence_dir / approved["evidence"][0]["storage_name"]
        evidence_path.write_text("SYNTHETIC deliberate modification after approval.\n")
        blocked = self.store.detail("SYN-001")
        self.assertEqual(blocked["run"]["status"], "blocked")
        self.assertEqual(blocked["run"]["recorded_status"], "approved")
        self.assertEqual(blocked["run"]["evidence_status"], "changed")
        self.assertEqual(blocked["events"], original_events)
        self.assertEqual(blocked["run"]["version"], 2)

    def test_removed_approved_evidence_blocks_effective_status(self):
        approved = self.decision("SYN-005")
        (self.store.evidence_dir / approved["evidence"][0]["storage_name"]).unlink()
        detail = self.store.detail("SYN-005")
        self.assertEqual(detail["run"]["status"], "blocked")
        self.assertEqual(detail["run"]["evidence_status"], "missing")
        self.assertEqual(detail["events"][0]["action"], "approved")

    def test_assignment_conflict_does_not_overwrite_new_owner(self):
        self.store.assign("SYN-001", {
            "actor": "Reviewer A", "owner": "Researcher A", "expected_version": 1})
        with self.assertRaises(APIError) as raised:
            self.store.assign("SYN-001", {
                "actor": "Reviewer B", "owner": "Researcher B", "expected_version": 1})
        self.assertEqual(raised.exception.status, 409)
        detail = self.store.detail("SYN-001")
        self.assertEqual(detail["run"]["owner"], "Researcher A")
        self.assertEqual(detail["run"]["version"], 2)
        self.assertEqual(len(detail["events"]), 2)
        self.assertEqual(detail["events"][0]["run_version"], 2)
        self.assertEqual(detail["events"][1]["run_version"], 1)
        with self.assertRaises(APIError) as decision_conflict:
            self.decision("SYN-001", version=1)
        self.assertEqual(decision_conflict.exception.status, 409)

    def test_changes_request_can_record_review_of_incomplete_package(self):
        detail = self.decision("SYN-002", decision="changes_requested")
        self.assertEqual(detail["run"]["status"], "blocked")
        self.assertEqual(detail["run"]["recorded_status"], "changes_requested")
        self.assertEqual(detail["run"]["evidence_status"], "missing")
        self.assertEqual(detail["events"][0]["actor"], "Synthetic reviewer")
        self.assertEqual(detail["events"][0]["run_version"], 2)

    def test_export_reconstructs_versions_for_each_committed_action(self):
        self.store.assign("SYN-001", {
            "actor": "Reviewer A", "owner": "Researcher A", "expected_version": 1})
        self.decision("SYN-001", version=2)
        events = [event for event in self.store.export()["events"]
                  if event["run_id"] == "SYN-001"]
        self.assertEqual([(event["action"], event["run_version"]) for event in events],
                         [("approved", 3), ("assigned", 2), ("seeded", 1)])

    def test_restart_persists_decisions_events_and_modified_evidence(self):
        self.decision("SYN-005")
        path = self.store.evidence_dir / self.store.evidence("SYN-005")[0]["storage_name"]
        path.write_text("SYNTHETIC updated after review.\n")
        before = self.store.detail("SYN-005")
        self.store.close()
        self.store = ReviewStore(self.root / "runtime")
        after = self.store.detail("SYN-005")
        self.assertEqual(before, after)
        self.assertEqual(after["run"]["status"], "blocked")
        # Restart never recopies pristine fixtures over changed runtime files.
        self.assertEqual(path.read_text(), "SYNTHETIC updated after review.\n")

    def test_audit_events_cannot_be_updated_or_deleted(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE events SET note='changed' WHERE id=1")
        self.store.db.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM events WHERE id=1")
        self.store.db.rollback()
        self.assertEqual(len(self.store.events()), 5)

    def test_path_traversal_absolute_paths_and_symlinks_are_rejected(self):
        for name in ("../outside.txt", "/etc/passwd", "a/../../outside.txt",
                     "a\\..\\outside.txt", "./file.txt", "a//file.txt", "\ud800"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_file(self.store.evidence_dir, name)
        outside = self.root / "outside.txt"
        outside.write_text("Must not be read.")
        (self.store.evidence_dir / "shortcut.txt").symlink_to(outside)
        with self.assertRaises(ValueError):
            safe_file(self.store.evidence_dir, "shortcut.txt")
        (self.root / "linked-root").symlink_to(self.store.evidence_dir)
        with self.assertRaises(ValueError):
            safe_file(self.root / "linked-root", "synthetic-run-001-metrics.txt")

    def test_manipulated_database_path_does_not_expose_external_file(self):
        outside = self.root / "outside.txt"
        outside.write_text("Private out-of-scope content")
        with self.store.db:
            self.store.db.execute("UPDATE evidence SET name='../outside.txt' WHERE run_id='SYN-005'")
        evidence = self.store.detail("SYN-005")["evidence"][0]
        self.assertEqual(evidence["status"], "missing")
        self.assertIsNone(evidence["actual_sha256"])
        with self.assertRaises(APIError):
            self.decision("SYN-005")

    def test_seed_rejects_invalid_path_before_database_mutation(self):
        fixtures = self.root / "malicious-fixtures"
        fixtures.mkdir()
        (fixtures / "evidence").mkdir()
        fixture = {
            "demo": True,
            "runs": [{"id": "BAD", "title": "Synthetic", "summary": "",
                      "evidence": [{"name": "../outside.txt", "expected_sha256": "0" * 64}]}],
        }
        (fixtures / "fixtures.json").write_text(json.dumps(fixture))
        with self.assertRaises(ValueError):
            ReviewStore(self.root / "invalid-runtime", fixture_dir=fixtures)
        connection = sqlite3.connect(str(self.root / "invalid-runtime" / "reviewops.sqlite3"))
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        finally:
            connection.close()


class QuietHandler(ReviewHandler):
    def log_message(self, format_string, *args):
        pass


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReviewStore(self.root / "runtime")
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Synthetic fixture</title>")
        self.server = ReviewHTTPServer(("127.0.0.1", 0), self.store, static_dir=static)
        self.server.RequestHandlerClass = QuietHandler
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.store.close()
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        data = None if body is None else json.dumps(body)
        values = {"Content-Type": "application/json"}
        values.update(headers or {})
        try:
            connection.request(method, path, body=data, headers=values)
            response = connection.getresponse()
            payload = response.read()
            status = response.status
            received_headers = dict(response.getheaders())
        finally:
            connection.close()
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        return status, payload, received_headers

    def test_state_detail_and_export_contract_without_exporting_token(self):
        status, state, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["demo"])
        self.assertEqual(len(state["runs"]), 5)
        self.assertEqual(state["csrf_token"], self.server.token)
        status, detail, _ = self.request("GET", "/api/runs/SYN-001")
        self.assertEqual(status, 200)
        self.assertEqual(detail["run"]["evidence_count"], 2)
        for item in detail["evidence"]:
            self.assertEqual(item["actual_sha256"], item["expected_sha256"])
        status, exported, headers = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        serialized = json.dumps(exported)
        self.assertNotIn("csrf_token", serialized)
        self.assertNotIn(self.server.token, serialized)

    def test_evidence_preview_returns_json_text_for_selected_manifest_entry(self):
        status, preview, headers = self.request("GET", "/api/runs/SYN-001/evidence/0")
        self.assertEqual(status, 200)
        self.assertEqual(preview["name"], "synthetic-run-001-metrics.txt")
        self.assertIn("SYNTHETIC", preview["text"])
        self.assertFalse(preview["truncated"])
        self.assertEqual(preview["status"], "verified")
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn("text/html", headers["Content-Type"])
        self.assertEqual(len(self.store.events()), 5)

    def test_changed_evidence_preview_is_bounded_with_utf8_replacement(self):
        path = self.store.evidence_dir / self.store.evidence("SYN-005")[0]["storage_name"]
        path.write_bytes(b"<script>not executable</script>\n"
                         + b"A" * MAX_PREVIEW_BYTES + b"\xff")
        status, preview, _ = self.request("GET", "/api/runs/SYN-005/evidence/0")
        self.assertEqual(status, 200)
        self.assertEqual(len(preview["text"].encode("utf-8")), MAX_PREVIEW_BYTES)
        self.assertTrue(preview["truncated"])
        self.assertEqual(preview["status"], "changed")
        self.assertTrue(preview["text"].startswith("<script>not executable</script>"))
        path.write_bytes(b"SYNTHETIC invalid UTF-8: \xff\n")
        status, preview, _ = self.request("GET", "/api/runs/SYN-005/evidence/0")
        self.assertEqual(status, 200)
        self.assertIn("\ufffd", preview["text"])
        self.assertFalse(preview["truncated"])

    def test_preview_refuses_missing_out_of_range_and_arbitrary_file_requests(self):
        (self.store.evidence_dir / "unlisted.txt").write_text("Not in any manifest.")
        cases = {
            "/api/runs/SYN-002/evidence/0": 404,
            "/api/runs/SYN-001/evidence/99": 404,
            "/api/runs/SYN-001/evidence/-1": 400,
            "/api/runs/SYN-004/evidence/0": 404,
            "/api/runs/UNKNOWN/evidence/0": 404,
            "/api/runs/SYN-001/evidence/unlisted.txt": 400,
            "/api/runs/SYN-001/evidence/..%2Funlisted.txt": 404,
            "/api/runs/SYN-001/evidence/%2Fetc%2Fpasswd": 404,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                status, payload, _ = self.request("GET", path)
                self.assertEqual(status, expected)
                self.assertNotIn("text", payload)

    def test_preview_rejects_a_tampered_manifest_path_and_symlink(self):
        outside = self.root / "outside.txt"
        outside.write_text("Must not be exposed.")
        with self.store.db:
            self.store.db.execute(
                "UPDATE evidence SET name='../outside.txt' WHERE run_id='SYN-005'")
        status, payload, _ = self.request("GET", "/api/runs/SYN-005/evidence/0")
        self.assertEqual(status, 403)
        self.assertNotIn("text", payload)
        (self.store.evidence_dir / "link.txt").symlink_to(outside)
        with self.store.db:
            self.store.db.execute(
                "UPDATE evidence SET name='link.txt' WHERE run_id='SYN-005'")
        status, payload, _ = self.request("GET", "/api/runs/SYN-005/evidence/0")
        self.assertEqual(status, 403)
        self.assertNotIn("text", payload)

    def test_post_requires_token_and_valid_origin_and_host(self):
        body = {"owner": "Demo owner", "actor": "Demo actor", "expected_version": 1}
        cases = [
            {},
            {"X-ReviewOps-Token": "invalid"},
            {"X-ReviewOps-Token": "é"},
            {"X-ReviewOps-Token": self.server.token, "Origin": "https://unrelated.example"},
            {"X-ReviewOps-Token": self.server.token, "Host": "unrelated.example"},
        ]
        for headers in cases:
            with self.subTest(headers=list(headers)):
                status, _, _ = self.request("POST", "/api/runs/SYN-001/assign", body, headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.store.detail("SYN-001")["run"]["version"], 1)

    def test_valid_mutation_and_stale_decision_conflict_over_http(self):
        headers = {"X-ReviewOps-Token": self.server.token,
                   "Origin": "http://127.0.0.1:{}".format(self.port)}
        status, detail, _ = self.request("POST", "/api/runs/SYN-001/assign",
                                        {"owner": "Demo owner", "actor": "Demo actor",
                                         "expected_version": 1}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(detail["run"]["version"], 2)
        status, _, _ = self.request("POST", "/api/runs/SYN-001/decision",
                                    {"decision": "approved", "actor": "Demo actor",
                                     "note": "Synthetic review.", "expected_version": 1}, headers)
        self.assertEqual(status, 409)
        status, detail, _ = self.request("POST", "/api/runs/SYN-001/decision",
                                        {"decision": "approved", "actor": "Demo actor",
                                         "note": "Synthetic review.", "expected_version": 2}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(detail["run"]["status"], "approved")
        self.assertEqual(detail["events"][0]["action"], "approved")
        self.assertEqual(detail["events"][0]["run_version"], 3)

    def test_unexpected_read_origin_and_host_are_rejected(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "null"}):
            status, _, _ = self.request("GET", "/api/state", headers=headers)
            self.assertEqual(status, 403)

    def test_body_cap_and_validation(self):
        headers = {"X-ReviewOps-Token": self.server.token}
        # The server rejects oversized declared bodies before reading them.
        # Send headers only: streaming a rejected 2 MiB body may get BrokenPipe.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest("POST", "/api/runs/SYN-001/assign")
            connection.putheader("X-ReviewOps-Token", self.server.token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            status = response.status
            response.read()
        finally:
            connection.close()
        self.assertEqual(status, 413)
        status, _, _ = self.request("POST", "/api/runs/SYN-001/assign",
                                    {"owner": "Demo", "actor": "Demo",
                                     "expected_version": True}, headers)
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/runs/SYN-001/decision",
                                    {"decision": "approved", "actor": "Demo",
                                     "note": "", "expected_version": 1}, headers)
        self.assertEqual(status, 400)

    def test_static_traversal_cannot_serve_database_or_external_files(self):
        for path in ("/../runtime/reviewops.sqlite3",
                     "/%2e%2e/runtime/reviewops.sqlite3",
                     "/static/../runtime/reviewops.sqlite3"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 403)
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Synthetic fixture", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
