"""Accepted evidence filenames must remain usable in portable workspace backups."""

import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from app import APIError, ReviewHTTPServer, ReviewStore
from submissions import SubmissionError, prepare_submission
from test_app import QuietHandler
from workspace_archive import export_workspace, inspect_archive, prepare_workspace, restore_workspace


RESERVED = ("CON", "prn.txt", "Aux.csv", "NUL.log", "COM1", "com9.csv",
            "LPT1.log", "lpt9", "aux.results.csv")


def submission(name="original.txt", **extra):
    return {
        "id": "PORTABLE-NAMES", "title": "Synthetic portable-file check",
        "summary": "Disposable evidence fixture.", "owner": "", "actor": "Fixture",
        "note": "Synthetic submission.", "is_demo": True,
        "evidence": [{"name": name, "content": "Synthetic auxiliary observations.\n"}],
        **extra,
    }


def evidence_hashes(store):
    return {path.relative_to(store.evidence_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in store.evidence_dir.rglob("*") if path.is_file()}


class PortableFilenameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.store = ReviewStore(self.workspace, seed_demo=False)
        self.original = self.store.submit(submission())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_device_name_predicate_covers_cases_extensions_and_all_numbered_devices(self):
        names = ["CON", "PRN", "AUX", "NUL"] + [
            prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10)]
        for name in names:
            for variant in (name, name.lower() + ".txt", name + ".results.csv"):
                with self.subTest(name=variant), self.assertRaisesRegex(SubmissionError, "reserved device name"):
                    prepare_submission(submission(variant))
        for name in ("auxiliary.txt", "COM0.txt", "COM10.txt", "LPT0.txt", "LPT10.txt", "console.csv", "null.txt"):
            with self.subTest(allowed=name):
                self.assertEqual(prepare_submission(submission(name))["files"][0]["name"], name)

    def test_rejected_create_and_revision_leave_records_events_and_files_unchanged(self):
        before = self.store.detail("PORTABLE-NAMES")
        files = evidence_hashes(self.store)
        directories = sorted(str(path) for path in self.store.evidence_dir.rglob("*") if path.is_dir())
        changes = self.store.db.total_changes
        for name in RESERVED:
            for extra in ({"id": "NEW-REJECTED"}, {"expected_version": 1}):
                with self.subTest(name=name, extra=extra), self.assertRaises(APIError) as raised:
                    self.store.submit(submission(name, **extra))
                self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.store.detail("PORTABLE-NAMES"), before)
        self.assertEqual(len(self.store.state()["runs"]), 1)
        self.assertEqual(evidence_hashes(self.store), files)
        self.assertEqual(sorted(str(path) for path in self.store.evidence_dir.rglob("*") if path.is_dir()), directories)

    def test_cli_rejects_reserved_display_names_without_changing_existing_history(self):
        before = self.store.detail("PORTABLE-NAMES")
        files = evidence_hashes(self.store)
        source = self.root / "source.txt"
        source.write_text("Synthetic local source.\n")
        manifest = self.root / "manifest.json"
        for name in RESERVED:
            manifest.write_text(json.dumps(submission(name, expected_version=1,
                evidence=[{"name": name, "path": "source.txt"}])))
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "app.py"),
                 "--data-dir", str(self.workspace), "--import-manifest", str(manifest)],
                capture_output=True, text=True, timeout=10)
            with self.subTest(name=name):
                self.assertEqual(result.returncode, 1)
                self.assertIn("reserved device name", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.store.detail("PORTABLE-NAMES"), before)
        self.assertEqual(evidence_hashes(self.store), files)

    def test_valid_auxiliary_file_imports_and_round_trips_through_backup(self):
        self.store.close()
        content = b"\xef\xbb\xbfSYNTHETIC,auxiliary\r\n1,2\r\n"
        (self.root / "auxiliary.txt").write_bytes(content)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(submission("auxiliary.txt", expected_version=1,
            evidence=[{"name": "auxiliary.txt", "path": "auxiliary.txt"}])))
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "app.py"),
             "--data-dir", str(self.workspace), "--import-manifest", str(manifest)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = json.loads(result.stdout)
        self.assertEqual(imported["evidence"][0]["name"], "auxiliary.txt")
        self.assertEqual(imported["evidence"][0]["actual_sha256"], hashlib.sha256(content).hexdigest())
        prepare_workspace(self.workspace)
        archive = self.root / "backup.zip"
        exported = export_workspace(self.workspace, archive)
        self.assertEqual(exported["database"], {"runs": 1, "events": 2, "snapshots": 2})
        self.assertEqual(inspect_archive(archive)["database"], exported["database"])
        destination = self.root / "restored"
        restore_workspace(archive, destination)
        restored = ReviewStore(destination, seed_demo=False)
        try:
            detail = restored.detail("PORTABLE-NAMES")
            self.assertEqual(detail, imported)
            self.assertEqual((restored.evidence_dir / detail["evidence"][0]["storage_name"]).read_bytes(), content)
        finally:
            restored.close()


class PortableFilenameHTTPTests(unittest.TestCase):
    def test_browser_facing_api_rejects_reserved_names_before_creating_any_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ReviewStore(Path(temporary) / "workspace", seed_demo=False)
            server = ReviewHTTPServer(("127.0.0.1", 0), store)
            server.RequestHandlerClass = QuietHandler
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            try:
                changes = store.db.total_changes
                for name in RESERVED:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                    try:
                        connection.request("POST", "/api/submissions", json.dumps(submission(name)), {
                            "Content-Type": "application/json", "X-ReviewOps-Token": server.token,
                        })
                        response = connection.getresponse()
                        body = json.loads(response.read())
                        with self.subTest(name=name):
                            self.assertEqual(response.status, 400)
                            self.assertIn("reserved device name", body["error"])
                    finally:
                        connection.close()
                self.assertEqual(store.db.total_changes, changes)
                self.assertEqual(store.state()["runs"], [])
                self.assertEqual(store.events(), [])
                self.assertEqual(list(store.evidence_dir.iterdir()), [])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                store.close()
