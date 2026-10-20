"""Keep the public audit's provenance and pending-review state intact."""

import hashlib
import json
import tempfile
import unittest

from app import PROJECT_DIR, ReviewStore
from submissions import read_manifest


class PublishedAuditTests(unittest.TestCase):
    def test_public_evidence_import_preserves_provenance_without_an_approval(self):
        package = PROJECT_DIR / "examples" / "published-audit"
        provenance = json.loads((package / "provenance.json").read_text())
        report_bytes = (package / "verification.json").read_bytes()
        manifest_bytes = (package / "saved-artifacts.json").read_bytes()
        report = json.loads(report_bytes)
        saved_manifest = json.loads(manifest_bytes)
        self.assertEqual(hashlib.sha256(report_bytes).hexdigest(),
                         provenance["verification_report_sha256"])
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(),
                         provenance["saved_manifest_sha256"])
        file_set = sorted(
            [{"path": item["path"], "sha256": item["sha256"]}
             for item in saved_manifest["artifacts"]],
            key=lambda item: item["path"],
        )
        identity = json.dumps(file_set, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(identity).hexdigest(),
                         provenance["data_file_set_sha256"])
        self.assertEqual(len(file_set), provenance["data_file_count"])
        self.assertEqual(len({item["path"] for item in file_set}), len(file_set))
        self.assertEqual(report["artifact_hashes_checked"], len(file_set))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["bootstrap_interval_bounds_compared"], 0)
        self.assertIs(provenance["bootstrap_requested"], False)
        self.assertIs(provenance["human_review_performed"], False)
        self.assertEqual(report["limits"], provenance["limits"])
        self.assertEqual(saved_manifest["snapshot_source_commit"],
                         provenance["saved_manifest_snapshot_source_commit"])
        self.assertRegex(provenance["source_commit"], r"^[a-f0-9]{40}$")
        self.assertEqual(provenance["source_commit_url"],
                         provenance["repository"] + "/commit/" + provenance["source_commit"])

        with tempfile.TemporaryDirectory() as directory:
            store = ReviewStore(directory, seed_demo=False)
            try:
                detail = store.submit(read_manifest(package / "manifest.json"))
                self.assertIs(detail["run"]["is_demo"], False)
                self.assertEqual(detail["run"]["status"], "needs_review")
                self.assertEqual(detail["run"]["version"], 1)
                self.assertEqual(detail["run"]["owner"], "")
                self.assertEqual(len(detail["evidence"]), 4)
                self.assertTrue(all(item["status"] == "verified"
                                    for item in detail["evidence"]))
                self.assertEqual([event["action"] for event in detail["events"]],
                                 ["submitted"])
                self.assertEqual(detail["events"][0]["actor"],
                                 "Automated verification import")
                self.assertIs(store.export()["demo"], False)
            finally:
                store.close()
