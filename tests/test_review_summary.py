"""Portable summaries retain review scope without active content or stale exports."""

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from app import APIError, ReviewHTTPServer, ReviewStore
from review_summary import SUMMARY_CSP, render_review_summary
from test_app import QuietHandler


class ParsedSummary(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.content = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.content.append(data)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReviewStore(self.root / "workspace", seed_demo=False)
        self.body = {
            "id": "SUMMARY-TEST", "title": "Original synthetic title",
            "summary": "Synthetic experiment description.", "owner": "Assigned label",
            "actor": "Submitter label", "note": "Original submission note.",
            "is_demo": True,
            "evidence": [{"name": "result.txt", "content": "EVIDENCE-BODY-MUST-NOT-EXPORT\n"}],
        }
        self.original = self.store.submit(self.body)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def approve(self):
        return self.store.decision(self.body["id"], {
            "expected_version": 1, "actor": "Decision actor", "decision": "approved",
            "note": "Decision on the original evidence.",
        })

    def summary(self, version=1):
        return self.store.review_summary(self.body["id"], version)

    def assert_field(self, document, name, value):
        self.assertIn("<dt>{}</dt><dd>{}</dd>".format(escape(name), escape(str(value))), document)

    def test_summary_retains_identity_and_hashes_without_bodies_paths_or_writes(self):
        before = self.store.export()
        changes = self.store.db.total_changes
        document = self.summary()
        self.assert_field(document, "Experiment ID", self.body["id"])
        self.assert_field(document, "Record version", 1)
        self.assert_field(document, "Current evidence snapshot", self.original["run"]["snapshot_id"])
        self.assert_field(document, "Current assigned reviewer", "Assigned label")
        self.assert_field(document, "Observed SHA-256", hashlib.sha256(self.body["evidence"][0]["content"].encode()).hexdigest())
        self.assertNotIn(self.body["evidence"][0]["content"].strip(), document)
        self.assertNotIn(self.original["evidence"][0]["storage_name"], document)
        self.assertNotIn(str(self.root), document)
        self.assertNotIn("csrf_token", document)
        self.assertEqual(self.store.db.total_changes, changes)
        after = self.store.export()
        before.pop("exported_at")
        after.pop("exported_at")
        self.assertEqual(before, after)
        self.assertFalse(self.store.db.in_transaction)

    def test_damaged_current_evidence_preserves_recorded_approval(self):
        approved = self.approve()
        path = self.store.evidence_dir / approved["evidence"][0]["storage_name"]
        for mode in ("changed", "missing"):
            if mode == "changed":
                path.write_text("Changed synthetic bytes.")
            else:
                path.unlink()
            document = self.summary(2)
            with self.subTest(mode=mode):
                self.assert_field(document, "Effective status", "Blocked")
                self.assert_field(document, "Recorded review status", "Approved")
                self.assert_field(document, "Current evidence check", "Changed" if mode == "changed" else "Unavailable")
                self.assert_field(document, "Actor label", "Decision actor")
                self.assertIn("missing, unreadable, oversized, or unsafe", document)
                if mode == "missing":
                    self.assert_field(document, "Observed SHA-256", "Not available")

    def test_resubmission_keeps_old_decision_on_old_snapshot_and_history_unverified(self):
        self.approve()
        revised = self.store.submit({**self.body, "expected_version": 2,
                                     "title": "New synthetic title", "note": "Revision note.",
                                     "evidence": [{"name": "new.txt", "content": "New bytes."}]})
        old_path = self.store.evidence_dir / self.original["evidence"][0]["storage_name"]
        old_path.unlink()
        document = self.summary(3)
        self.assert_field(document, "Effective status", "Needs review")
        self.assert_field(document, "Current evidence snapshot", revised["run"]["snapshot_id"])
        self.assert_field(document, "Evidence snapshot reference", self.original["run"]["snapshot_id"])
        self.assertIn("Decision on the original evidence.", document)
        self.assertIn("Earlier file bytes were not rechecked", document)
        self.assert_field(document, "Submitted at record version", 1)
        self.assert_field(document, "Submitted at record version", 3)
        self.assertEqual(document.count("<dt>Observed status</dt>"), 1)

    def test_history_label_does_not_reclassify_user_submission_as_synthetic(self):
        self.store.submit({**self.body, "is_demo": False, "expected_version": 1})
        self.store.submit({**self.body, "is_demo": True, "expected_version": 2})
        document = self.summary(3)
        self.assert_field(document, "Current record label", "Synthetic example")
        self.assertIn("Contains user-submitted or unclassified experiment history", document)
        self.assertNotIn("Synthetic experiment records, including retained snapshots.", document)
        self.assertIn("User-entered text can be sensitive", document)

    def test_legacy_null_references_and_no_events_are_not_invented(self):
        legacy = ReviewStore(self.root / "legacy")
        try:
            document = legacy.review_summary("SYN-001", 1)
            self.assertIn("Legacy capture:", document)
            self.assert_field(document, "Evidence snapshot reference", "Unknown (not recorded)")
            self.assertIn("does not establish their earlier contents", document)
        finally:
            legacy.close()
        captured = self.store.detail(self.body["id"])
        captured["events"][0]["run_version"] = None
        captured["events"][0]["snapshot_id"] = None
        document = render_review_summary(captured, "test-time")
        self.assert_field(document, "Record version", "Unknown (not recorded)")
        captured["events"] = []
        captured["snapshots"][0]["manifest"].pop("owner")
        document = render_review_summary(captured, "test-time")
        self.assertIn("No actions are retained", document)
        self.assertNotIn("Actor label", document)
        self.assert_field(document, "Assigned reviewer at submission", "Unknown (not recorded)")
        captured["snapshots"][0]["manifest"]["owner"] = ""
        document = render_review_summary(captured, "test-time")
        self.assert_field(document, "Assigned reviewer at submission", "Unassigned")

    def test_text_is_escaped_and_document_has_no_active_or_external_content(self):
        hostile = '</title><script>alert("x")</script><img src="https://invalid.test/leak"> & note'
        revised = self.store.submit({**self.body, "expected_version": 1,
                                     "title": hostile, "summary": hostile, "owner": hostile,
                                     "actor": hostile, "note": hostile})
        document = self.summary(2)
        parsed = ParsedSummary(document)
        self.assertIn(hostile, "".join(parsed.content))
        self.assertIn(escape(hostile), document)
        self.assertFalse({tag for tag, _ in parsed.tags} & {"script", "img", "iframe", "a", "link", "form", "object"})
        for _, attrs in parsed.tags:
            self.assertFalse(any(key in ("href", "src", "action") or key.startswith("on") for key in attrs))
        meta = next(attrs for tag, attrs in parsed.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertEqual(meta["content"], SUMMARY_CSP)
        self.assertNotIn(revised["evidence"][0]["storage_name"], document)

    def test_versions_must_be_positive_integers_and_stale_request_makes_no_changes(self):
        changes = self.store.db.total_changes
        for invalid in (None, True, False, "1", 0, -1, 1.0):
            with self.subTest(value=invalid), self.assertRaises(APIError) as raised:
                self.summary(invalid)
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(APIError) as raised:
            self.summary(2)
        self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(APIError) as raised:
            self.store.review_summary("ABSENT", 1)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertFalse(self.store.db.in_transaction)

    def test_concurrent_revision_cannot_mix_old_and_new_summary(self):
        writer = ReviewStore(self.root / "workspace", seed_demo=False)
        original_row = self.store._row
        revised = False

        def row_then_revision(run_id):
            nonlocal revised
            row = original_row(run_id)
            if not revised:
                revised = True
                writer.submit({**self.body, "expected_version": 1, "is_demo": False,
                               "title": "CONCURRENT-NEW-TITLE", "note": "CONCURRENT-NEW-NOTE",
                               "evidence": [{"name": "concurrent.txt", "content": "Concurrent."}]})
            return row

        try:
            with patch.object(self.store, "_row", side_effect=row_then_revision), patch.object(
                    self.store, "detail", wraps=self.store.detail) as detail:
                document = self.summary()
                self.assertEqual(detail.call_count, 1)
            self.assertTrue(revised)
            self.assert_field(document, "Record version", 1)
            self.assertNotIn("CONCURRENT-NEW", document)
            self.assertNotIn("concurrent.txt", document)
            self.assertIn("Synthetic experiment records, including retained snapshots.", document)
            with self.assertRaises(APIError) as raised:
                self.summary()
            self.assertEqual(raised.exception.status, 409)
        finally:
            writer.close()


class SummaryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.temporary.name) / "workspace")
        self.server = ReviewHTTPServer(("127.0.0.1", 0), self.store)
        self.server.RequestHandlerClass = QuietHandler
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.store.close()
        self.temporary.cleanup()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def test_summary_download_headers_and_existing_json_export_are_distinct(self):
        changes = self.store.db.total_changes
        status, headers, document = self.request("/api/runs/SYN-001/summary?expected_version=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="reviewops-summary.html"')
        self.assertEqual(headers["Content-Security-Policy"], SUMMARY_CSP)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(document.encode("utf-8")))
        self.assertNotIn(self.server.token, document)
        self.assertNotIn("csrf_token", document)
        status, headers, payload = self.request("/api/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="reviewops-export.json"')
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(payload)["schema_version"], 2)
        self.assertEqual(self.store.db.total_changes, changes)

    def test_required_query_is_strict_and_stale_version_returns_conflict(self):
        changes = self.store.db.total_changes
        for query in ("", "?expected_version", "?expected_version=", "?expected_version=0",
                      "?expected_version=-1", "?expected_version=1.0", "?expected_version=true",
                      "?expected_version=%EF%BC%91", "?expected_version=+1", "?expected_version=1&",
                      "?expected_version=1&expected_version=1", "?expected_version=1&other=2",
                      "?other=1", "?expected_version=" + "9" * 100,
                      "?expected_version=1&%65xpected_version=1"):
            with self.subTest(query=query):
                status, _, payload = self.request("/api/runs/SYN-001/summary" + query)
                self.assertEqual(status, 400, payload)
        status, _, payload = self.request("/api/runs/SYN-001/summary?expected_version=2")
        self.assertEqual(status, 409, payload)
        status, _, _ = self.request("/api/runs/ABSENT/summary?expected_version=1")
        self.assertEqual(status, 404)
        self.assertEqual(self.store.db.total_changes, changes)

    def test_summary_preserves_host_and_origin_boundary(self):
        path = "/api/runs/SYN-001/summary?expected_version=1"
        for headers in ({"Host": "unrelated.test"}, {"Origin": "https://unrelated.test"}):
            with self.subTest(headers=headers):
                status, _, payload = self.request(path, headers)
                self.assertEqual(status, 403, payload)
