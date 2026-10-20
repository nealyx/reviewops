"""Submission, retained-revision, and manifest boundary regressions."""

import copy
import hashlib
import http.client
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import APIError, MAX_PREVIEW_BYTES, PROJECT_DIR, ReviewStore
from submissions import (
    MAX_FILE_BYTES, MAX_MANIFEST_BYTES, SubmissionError,
    parse_json, prepare_submission, read_manifest,
)
import test_app


def submission(**changes):
    body = {
        "id": "DEMO-100", "title": "Synthetic replication",
        "summary": "Compare two synthetic measurements.",
        "actor": "Demo submitter", "owner": "Demo reviewer",
        "note": "Ready for methods review.", "is_demo": True,
        "evidence": [{"name": "results.txt", "content": "SYNTHETIC\nvalue=0.5\n"}],
    }
    body.update(changes)
    return body


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReviewStore(self.root / "runtime", seed_demo=False)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_empty_workspace_remains_empty_after_restart(self):
        self.assertEqual(self.store.state()["runs"], [])
        self.store.close()
        self.store = ReviewStore(self.root / "runtime")
        self.assertEqual(self.store.state()["runs"], [])

    def test_submission_hashes_exact_bytes_and_records_manifest(self):
        content = "SYNTHETIC\r\nπ = 3.14\n"
        detail = self.store.submit(submission(evidence=[{"name": "result.txt", "content": content}]))
        entry = detail["evidence"][0]
        self.assertEqual(entry["expected_sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(entry["status"], "verified")
        self.assertEqual(self.store.evidence_preview("DEMO-100", 0)["text"], content)
        snapshot = detail["snapshots"][0]
        canonical = json.dumps(snapshot["manifest"], ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(snapshot["manifest_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(detail["events"][0]["snapshot_id"], snapshot["id"])
        self.assertEqual(snapshot["manifest"]["evidence"][0]["size_bytes"], len(content.encode()))
        self.assertEqual(snapshot["manifest"]["owner"], "Demo reviewer")

    def test_snapshots_preserve_initial_owner_after_assignments_and_revisions(self):
        first = self.store.submit(submission(owner="First reviewer"))
        self.store.assign("DEMO-100", {
            "owner": "Second reviewer", "actor": "Demo coordinator", "expected_version": 1})
        revised = self.store.submit(submission(owner="Third reviewer", expected_version=2))
        self.assertEqual(revised["run"]["owner"], "Third reviewer")
        self.assertEqual(revised["snapshots"][0]["manifest"]["owner"], "Third reviewer")
        self.assertEqual(revised["snapshots"][1]["manifest"]["owner"], "First reviewer")
        self.assertEqual(revised["events"][1]["snapshot_id"], first["run"]["snapshot_id"])

    def test_revision_resets_approval_and_retains_old_bytes_and_decision_link(self):
        original = self.store.submit(submission())
        snapshot = original["run"]["snapshot_id"]
        self.store.decision("DEMO-100", {
            "actor": "Demo reviewer", "note": "Checked methods.", "decision": "approved",
            "expected_version": 1,
        })
        revised = self.store.submit(submission(expected_version=2,
            evidence=[{"name": "results.txt", "content": "SYNTHETIC\nvalue=0.8\n"}]))
        self.assertEqual(revised["run"]["status"], "needs_review")
        self.assertEqual(revised["run"]["version"], 3)
        self.assertNotEqual(revised["run"]["snapshot_id"], snapshot)
        self.assertEqual(len(revised["snapshots"]), 2)
        self.assertIn("0.5", self.store.snapshot_preview("DEMO-100", snapshot, 0)["text"])
        self.assertIn("0.8", self.store.evidence_preview("DEMO-100", 0)["text"])
        self.assertEqual(revised["events"][1]["action"], "approved")
        self.assertEqual(revised["events"][1]["snapshot_id"], snapshot)
        self.store.close()
        self.store = ReviewStore(self.root / "runtime")
        self.assertEqual(self.store.detail("DEMO-100"), revised)

    def test_stale_or_duplicate_submission_leaves_no_files_or_events(self):
        self.store.submit(submission())
        before = self.store.detail("DEMO-100")
        files = sorted(self.store.evidence_dir.rglob("*"))
        for version in (None, True, 0, 2):
            with self.subTest(version=version), self.assertRaises(APIError):
                self.store.submit(submission(expected_version=version))
        self.assertEqual(self.store.detail("DEMO-100"), before)
        self.assertEqual(sorted(self.store.evidence_dir.rglob("*")), files)
        with self.assertRaises(APIError):
            self.store.submit(submission(id="NEW", expected_version=1))
        self.assertEqual(len(self.store.state()["runs"]), 1)

    def test_file_write_failure_rolls_back_database_and_owned_snapshot(self):
        with patch.object(Path, "write_bytes", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                self.store.submit(submission())
        self.assertEqual(self.store.state()["runs"], [])
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(list((self.store.evidence_dir / "snapshots").iterdir()), [])

    def test_partial_revision_write_failure_preserves_previous_state_and_files(self):
        before = self.store.submit(submission())
        files_before = sorted(self.store.evidence_dir.rglob("*"))
        write_bytes = Path.write_bytes
        writes = []

        def fail_second(path, data):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("Synthetic failure after the first evidence file.")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", new=fail_second):
            with self.assertRaises(OSError):
                self.store.submit(submission(expected_version=1, evidence=[
                    {"name": "first.txt", "content": "First new file."},
                    {"name": "second.txt", "content": "Second new file."},
                ]))
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.store.detail("DEMO-100"), before)
        self.assertEqual(sorted(self.store.evidence_dir.rglob("*")), files_before)
        self.assertIn("0.5", self.store.evidence_preview("DEMO-100", 0)["text"])

    def test_audit_write_failure_rolls_back_revision_and_new_files(self):
        before = self.store.submit(submission())
        files_before = sorted(self.store.evidence_dir.rglob("*"))
        with patch.object(self.store, "_event", side_effect=sqlite3.OperationalError("Synthetic failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.submit(submission(expected_version=1))
        self.assertEqual(self.store.detail("DEMO-100"), before)
        self.assertEqual(sorted(self.store.evidence_dir.rglob("*")), files_before)

    def test_empty_submission_is_blocked_and_can_be_revised(self):
        empty = self.store.submit(submission(evidence=[]))
        self.assertEqual(empty["run"]["status"], "blocked")
        revised = self.store.submit(submission(expected_version=1))
        self.assertEqual(revised["run"]["status"], "needs_review")

    def test_user_record_export_is_not_mislabeled_synthetic(self):
        self.store.submit(submission(is_demo=False))
        exported = self.store.export()
        self.assertFalse(exported["demo"])
        self.assertEqual(exported["schema_version"], 2)
        self.assertIn("user-submitted", exported["notice"])
        self.assertFalse(exported["runs"][0]["run"]["is_demo"])

    def test_synthetic_revision_does_not_relabel_retained_real_history_as_demo(self):
        self.store.submit(submission(is_demo=False, summary="User-submitted original."))
        self.store.submit(submission(is_demo=True, expected_version=1))
        self.assertTrue(self.store.detail("DEMO-100")["run"]["is_demo"])
        self.assertFalse(self.store.state()["demo"])
        exported = self.store.export()
        self.assertFalse(exported["demo"])
        self.assertIn("user-submitted", exported["notice"])
        self.assertFalse(exported["runs"][0]["snapshots"][1]["manifest"]["is_demo"])
        self.store.close()
        self.store = ReviewStore(self.root / "runtime")
        self.assertFalse(self.store.state()["demo"])

    def test_snapshot_is_scoped_to_run_and_detects_tampering(self):
        first = self.store.submit(submission())
        self.store.submit(submission(id="OTHER"))
        snapshot = first["run"]["snapshot_id"]
        with self.assertRaises(APIError) as raised:
            self.store.snapshot_preview("OTHER", snapshot, 0)
        self.assertEqual(raised.exception.status, 404)
        (self.store.evidence_dir / first["evidence"][0]["storage_name"]).write_text("Changed")
        self.assertEqual(self.store.snapshot_preview("DEMO-100", snapshot, 0)["status"], "changed")
        for sql in ("UPDATE snapshots SET manifest_json='{}'", "DELETE FROM snapshots"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql)
            self.store.db.rollback()

    def test_legacy_capture_preserves_expected_hash_and_prior_events(self):
        self.store.close()
        self.store = ReviewStore(self.root / "legacy")
        first = self.store.detail("SYN-003")
        snapshot = first["snapshots"][0]
        self.assertTrue(snapshot["manifest"]["legacy_capture"])
        self.assertEqual(self.store.snapshot_preview("SYN-003", snapshot["id"], 0)["status"],
                         "changed")
        self.assertIsNone(first["events"][0]["snapshot_id"])
        self.store.submit(submission(id="SYN-003", expected_version=1))
        self.assertEqual(self.store.snapshot_preview("SYN-003", snapshot["id"], 0)["status"],
                         "changed")
        self.assertEqual(len(self.store.detail("SYN-003")["snapshots"]), 2)

    def test_historical_preview_remains_bounded_and_refuses_missing_or_symlinked_files(self):
        first = self.store.submit(submission(evidence=[
            {"name": "long.txt", "content": "<script>text only</script>" + "x" * MAX_PREVIEW_BYTES}
        ]))
        snapshot = first["run"]["snapshot_id"]
        self.store.submit(submission(expected_version=1))
        preview = self.store.snapshot_preview("DEMO-100", snapshot, 0)
        self.assertEqual(len(preview["text"].encode()), MAX_PREVIEW_BYTES)
        self.assertTrue(preview["truncated"])
        self.assertTrue(preview["text"].startswith("<script>"))
        path = self.store.evidence_dir / first["evidence"][0]["storage_name"]
        path.unlink()
        with self.assertRaises(APIError) as missing:
            self.store.snapshot_preview("DEMO-100", snapshot, 0)
        self.assertEqual(missing.exception.status, 404)
        outside = self.root / "private.txt"
        outside.write_text("Must not be returned.")
        path.symlink_to(outside)
        with self.assertRaises(APIError) as symlinked:
            self.store.snapshot_preview("DEMO-100", snapshot, 0)
        self.assertEqual(symlinked.exception.status, 403)


class LegacyMigrationTests(unittest.TestCase):
    """Build an actual old database instead of exercising a newly seeded schema."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.evidence_dir = self.runtime / "evidence"
        self.evidence_dir.mkdir()
        self.store = None

    def tearDown(self):
        if self.store is not None:
            self.store.close()
        self.temporary.cleanup()

    def create_old_database(self, with_versions=True):
        self.db_path = self.runtime / "reviewops.sqlite3"
        db = sqlite3.connect(str(self.db_path))
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES ('seeded', '2026-01-01T00:00:00Z');
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
                owner TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                name TEXT NOT NULL, expected_sha256 TEXT NOT NULL, UNIQUE(run_id, name));
        """)
        version_column = ", run_version INTEGER NOT NULL" if with_versions else ""
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "run_id TEXT NOT NULL REFERENCES runs(id), actor TEXT NOT NULL, "
                   "action TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL"
                   + version_column + ")")
        timestamp = "2026-01-01T00:00:00Z"
        expected = hashlib.sha256(b"Original evidence.\n").hexdigest()
        for index, kind in enumerate(("OK", "CHANGED", "MISSING", "EMPTY"), 1):
            run_id = "OLD-" + kind
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                       (run_id, "Synthetic " + kind, "Old local record", "Legacy reviewer",
                        "approved" if kind in ("OK", "CHANGED") else "needs_review", index, timestamp))
            columns = "run_id,actor,action,note,created_at" + (",run_version" if with_versions else "")
            values = [run_id, "Legacy actor", "approved", "Old immutable note", timestamp]
            if with_versions:
                values.append(index)
            db.execute("INSERT INTO events ({}) VALUES ({})".format(
                columns, ",".join("?" for _ in values)), values)
            if kind != "EMPTY":
                name = kind.lower() + ".txt"
                db.execute("INSERT INTO evidence (run_id,name,expected_sha256) VALUES (?,?,?)",
                           (run_id, name, expected))
                if kind != "MISSING":
                    (self.evidence_dir / name).write_bytes(
                        b"Original evidence.\n" if kind == "OK" else b"Changed before migration.\n")
        db.commit()
        self.old_events = list(db.execute("SELECT * FROM events ORDER BY id"))
        self.old_runs = list(db.execute("SELECT * FROM runs ORDER BY id"))
        db.close()

    def test_v01_migration_preserves_records_and_retains_bytes_before_later_edits(self):
        self.create_old_database()
        self.store = ReviewStore(self.runtime)
        state = self.store.state()
        self.assertEqual(len(state["runs"]), 4)  # Never seed over an existing workspace.
        self.assertEqual({r["id"]: r["evidence_status"] for r in state["runs"]}, {
            "OLD-OK": "verified", "OLD-CHANGED": "changed", "OLD-MISSING": "missing",
            "OLD-EMPTY": "empty",
        })
        for before in self.old_runs:
            after = self.store.db.execute(
                "SELECT id,title,summary,owner,status,version,updated_at FROM runs WHERE id=?",
                (before[0],)).fetchone()
            self.assertEqual(tuple(after), before)
        after_events = self.store.db.execute(
            "SELECT id,run_id,actor,action,note,created_at,run_version FROM events ORDER BY id")
        self.assertEqual([tuple(row) for row in after_events], self.old_events)
        self.assertTrue(all(e["snapshot_id"] is None for e in self.store.events()))
        detail = self.store.detail("OLD-OK")
        snapshot = detail["snapshots"][0]
        self.assertTrue(snapshot["manifest"]["legacy_capture"])
        self.assertEqual(snapshot["manifest"]["owner"], "Legacy reviewer")
        self.assertTrue(detail["evidence"][0]["storage_name"].startswith("snapshots/"))
        self.assertEqual((self.evidence_dir / "ok.txt").read_bytes(), b"Original evidence.\n")
        (self.evidence_dir / "ok.txt").write_text("Original path changed after migration.")
        self.assertEqual(self.store.snapshot_preview("OLD-OK", snapshot["id"], 0)["text"],
                         "Original evidence.\n")
        self.assertEqual(self.store.detail("OLD-OK")["run"]["status"], "approved")
        self.store.submit(submission(id="OLD-OK", expected_version=1))
        self.assertEqual(self.store.snapshot_preview("OLD-OK", snapshot["id"], 0)["text"],
                         "Original evidence.\n")
        before_restart = self.store.export()["runs"]
        self.store.close()
        self.store = ReviewStore(self.runtime)
        self.assertEqual(self.store.export()["runs"], before_restart)
        self.assertEqual(len(self.store.snapshots("OLD-OK")), 2)

    def test_pre_release_events_keep_unknown_version_and_new_events_record_version(self):
        self.create_old_database(with_versions=False)
        self.store = ReviewStore(self.runtime)
        self.assertTrue(all(e["run_version"] is None for e in self.store.events()))
        detail = self.store.assign("OLD-OK", {
            "owner": "New owner", "actor": "New actor", "expected_version": 1})
        self.assertEqual(detail["events"][0]["run_version"], 2)
        self.assertIsNone(detail["events"][1]["run_version"])
        self.assertIsNotNone(detail["events"][0]["snapshot_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE events SET run_version=1 WHERE id=1")
        self.store.db.rollback()

    def test_partial_migration_copy_failure_rolls_back_and_can_be_retried(self):
        self.create_old_database()
        write_bytes = Path.write_bytes
        writes = []

        def fail_second(path, data):
            writes.append(path)
            if len(writes) == 2:
                raise OSError("Synthetic migration disk failure.")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", new=fail_second):
            with self.assertRaises(OSError):
                ReviewStore(self.runtime)
        db = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)
            self.assertTrue(all(row[0] is None for row in db.execute("SELECT snapshot_id FROM runs")))
            self.assertEqual([r[0] for r in db.execute("SELECT name FROM evidence ORDER BY id")],
                             ["ok.txt", "changed.txt", "missing.txt"])
            self.assertEqual(list(db.execute(
                "SELECT id,run_id,actor,action,note,created_at,run_version FROM events ORDER BY id")),
                self.old_events)
        finally:
            db.close()
        self.assertEqual(list((self.evidence_dir / "snapshots").iterdir()), [])
        self.assertEqual((self.evidence_dir / "ok.txt").read_bytes(), b"Original evidence.\n")
        self.store = ReviewStore(self.runtime)
        self.assertEqual(self.store.detail("OLD-OK")["run"]["status"], "approved")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "manifest.json"

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, body):
        self.path.write_text(json.dumps(body))
        return self.path

    def test_path_and_inline_inputs_have_identical_content_hashes(self):
        body = submission()
        (self.root / "results.txt").write_bytes(body["evidence"][0]["content"].encode())
        local = copy.deepcopy(body)
        local["evidence"] = [{"path": "results.txt"}]
        loaded = read_manifest(self.save(local), actor="Local reviewer")
        self.assertEqual(loaded["actor"], "Local reviewer")
        self.assertEqual(prepare_submission(loaded)["manifest_sha256"],
                         prepare_submission(body)["manifest_sha256"])

    def test_invalid_metadata_filenames_and_content_are_rejected(self):
        bodies = [
            submission(id="../escape"), submission(is_demo=1), submission(actor=" "),
            submission(title="\ud800"),
            submission(evidence=[{"name": "../x", "content": "x"}]),
            submission(evidence=[{"name": "x", "content": "a"}, {"name": "X", "content": "b"}]),
            submission(evidence=[{"name": "x", "content": "\ud800"}]),
            submission(evidence=[{"name": "x", "content": "a" * (MAX_FILE_BYTES + 1)}]),
            submission(evidence=[{"name": str(i), "content": ""} for i in range(21)]),
            submission(evidence=[{"name": str(i), "content": "a" * MAX_FILE_BYTES}
                                 for i in range(5)]),
        ]
        for index, body in enumerate(bodies):
            with self.subTest(index=index), self.assertRaises(SubmissionError):
                prepare_submission(body)

    def test_manifest_cannot_read_outside_or_symlinked_files(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_text("Out of scope")
        try:
            (self.root / "link.txt").symlink_to(outside)
            for name in ("../" + outside.name, str(outside), "link.txt", "missing.txt",
                         "./link.txt", "x\\file", "x//file"):
                with self.subTest(name=name), self.assertRaises(SubmissionError):
                    read_manifest(self.save(submission(evidence=[{"path": name}])))
        finally:
            outside.unlink()

    def test_binary_and_ambiguous_sources_rejected(self):
        (self.root / "binary.txt").write_bytes(b"\xff")
        for entry in ({"path": "binary.txt"}, {"path": "binary.txt", "content": "x"}):
            with self.assertRaises(SubmissionError):
                read_manifest(self.save(submission(evidence=[entry])))

    def test_manifest_rejects_unpaired_surrogate_paths_without_filesystem_error(self):
        for path in ("\ud800", "nested/\udcff.txt"):
            with self.subTest(path=repr(path)), self.assertRaises(SubmissionError):
                read_manifest(self.save(submission(evidence=[{"path": path}])))
        with self.assertRaises(SubmissionError):
            read_manifest(Path("\ud800"))

    def test_json_rejects_ambiguous_nonfinite_deep_and_invalid_encoding_inputs(self):
        cases = [
            b'{"id":"FIRST","id":"SECOND"}',
            b'{"unused":NaN}', b'{"unused":Infinity}', b'{"unused":-Infinity}',
            b'{"unused":1e400}',
            b"\xff", b"[" * 2000 + b"]" * 2000,
        ]
        for content in cases:
            with self.subTest(content=content[:40]), self.assertRaises(SubmissionError):
                parse_json(content)
            self.path.write_bytes(content)
            with self.assertRaises(SubmissionError):
                read_manifest(self.path)

    def test_json_depth_limit_is_explicit_and_ignores_brackets_in_text(self):
        accepted = "[" * 32 + "0" + "]" * 32
        self.assertIsInstance(parse_json(accepted), list)
        with self.assertRaisesRegex(SubmissionError, "32 levels"):
            parse_json("[" * 33 + "0" + "]" * 33)
        text_with_brackets = "[" * 2000
        self.assertEqual(parse_json(json.dumps({"content": text_with_brackets})),
                         {"content": text_with_brackets})

    def test_manifest_enforces_total_package_and_manifest_size_limits(self):
        (self.root / "large.txt").write_bytes(b"a" * MAX_FILE_BYTES)
        body = submission(evidence=[{"path": "large.txt", "name": "part-{}.txt".format(i)}
                                    for i in range(5)])
        with self.assertRaises(SubmissionError):
            read_manifest(self.save(body))
        self.path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
        with self.assertRaises(SubmissionError):
            read_manifest(self.path)

    def test_cli_import_is_local_and_fails_cleanly_on_duplicate(self):
        self.save(submission())
        command = [sys.executable, str(PROJECT_DIR / "app.py"), "--empty",
                   "--data-dir", str(self.root / "runtime"), "--import-manifest", str(self.path)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["run"]["id"], "DEMO-100")
        second = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 1)
        self.assertIn("Import failed:", second.stderr)
        self.assertNotIn("Traceback", second.stderr)


class SubmissionHTTPTests(unittest.TestCase):
    setUp = test_app.HTTPTests.setUp
    tearDown = test_app.HTTPTests.tearDown
    request = test_app.HTTPTests.request

    def test_submission_and_historical_preview_routes(self):
        headers = {"X-ReviewOps-Token": self.server.token}
        status, first, _ = self.request("POST", "/api/submissions", submission(), headers)
        self.assertEqual(status, 201)
        snapshot = first["run"]["snapshot_id"]
        status, second, _ = self.request("POST", "/api/submissions",
            submission(expected_version=1, evidence=[{"name": "revised.txt", "content": "New"}]),
            headers)
        self.assertEqual(status, 201)
        route = "/api/runs/DEMO-100/snapshots/{}/evidence/0".format(snapshot)
        status, old, _ = self.request("GET", route)
        self.assertEqual(status, 200)
        self.assertIn("0.5", old["text"])
        status, _, _ = self.request("POST", "/api/submissions", submission())
        self.assertEqual(status, 403)

    def test_malformed_json_is_rejected_without_disconnecting_or_mutating(self):
        for body in (b"[" * 2000 + b"]" * 2000, b'{"id":"A","id":"B"}',
                     b'{"\\ud800":0,"\\ud800":1}', b'{"value":NaN}', b"\xff"):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.request("POST", "/api/submissions", body=body, headers={
                    "Content-Type": "application/json", "X-ReviewOps-Token": self.server.token})
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 400)
                self.assertIn("error", payload)
            finally:
                connection.close()
        status, state, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["events"]), 5)


if __name__ == "__main__":
    unittest.main()
