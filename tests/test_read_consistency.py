"""Keep responses coherent when a separate CLI connection commits a revision."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import APIError, ReviewStore


class ReadConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = ReviewStore(self.root, seed_demo=False)
        self.original = {
            "id": "READ-CONSISTENCY", "title": "Original synthetic package",
            "summary": "Synthetic fixture for concurrent CLI reads.",
            "owner": "", "actor": "Demo submitter", "note": "Original evidence.",
            "is_demo": True,
            "evidence": [{"name": "original.txt", "content": "SYNTHETIC ORIGINAL\n"}],
        }
        self.store.submit(self.original)
        self.writer = ReviewStore(self.root, seed_demo=False)

    def tearDown(self):
        self.writer.close()
        self.store.close()
        self.directory.cleanup()

    def revise(self):
        # The bytes remain a test fixture; false exercises real-data labeling.
        self.writer.submit({
            **self.original, "title": "Replacement package", "is_demo": False,
            "expected_version": 1, "note": "A separate CLI submitted a revision.",
            "evidence": [{"name": "replacement.txt", "content": "TEST REPLACEMENT\n"}],
        })

    def test_detail_keeps_metadata_and_evidence_from_one_database_snapshot(self):
        original_row = self.store._row
        revised = False

        def row_then_revision(run_id):
            nonlocal revised
            row = original_row(run_id)
            if not revised:
                revised = True
                self.revise()
            return row

        with patch.object(self.store, "_row", side_effect=row_then_revision):
            detail = self.store.detail(self.original["id"])
        self.assertTrue(revised)
        self.assertEqual(detail["run"]["version"], 1)
        self.assertEqual(detail["evidence"][0]["name"], "original.txt")
        self.assertEqual(len(detail["snapshots"]), 1)
        self.assertEqual(detail["snapshots"][0]["id"], detail["run"]["snapshot_id"])
        self.assertEqual([event["run_version"] for event in detail["events"]], [1])
        self.assertEqual(self.store.detail(self.original["id"])["run"]["version"], 2)

    def test_queue_labels_and_history_share_the_returned_run_snapshot(self):
        original_detail = self.store.detail

        def detail_then_revision(run_id):
            detail = original_detail(run_id)
            self.revise()
            return detail

        with patch.object(self.store, "detail", side_effect=detail_then_revision):
            state = self.store.state()
        self.assertEqual(state["runs"][0]["version"], 1)
        self.assertIs(state["demo"], True)
        self.assertEqual([event["run_version"] for event in state["events"]], [1])
        self.assertIs(self.store.state()["demo"], False)

    def test_export_cannot_label_a_new_user_revision_as_synthetic(self):
        original_state = self.store.state

        def state_then_revision():
            state = original_state()
            self.revise()
            return state

        with patch.object(self.store, "state", side_effect=state_then_revision):
            exported = self.store.export()
        self.assertIs(exported["demo"], True)
        detail = exported["runs"][0]
        self.assertIs(detail["run"]["is_demo"], True)
        self.assertEqual(detail["run"]["version"], 1)
        self.assertEqual(detail["evidence"][0]["name"], "original.txt")
        self.assertTrue(all(snapshot["manifest"]["is_demo"]
                            for snapshot in detail["snapshots"]))
        self.assertIs(self.store.export()["demo"], False)

    def test_read_cleanup_preserves_the_callers_transaction_boundary(self):
        with self.assertRaises(APIError):
            self.store.detail("MISSING")
        self.assertFalse(self.store.db.in_transaction)
        self.store.db.execute("BEGIN IMMEDIATE")
        self.store.db.execute("UPDATE runs SET owner='Uncommitted owner'")
        detail = self.store.detail(self.original["id"])
        self.assertEqual(detail["run"]["owner"], "Uncommitted owner")
        self.assertTrue(self.store.db.in_transaction)
        with self.assertRaises(APIError):
            self.store.detail("MISSING")
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertEqual(self.store.detail(self.original["id"])["run"]["owner"], "")
        self.assertFalse(self.store.db.in_transaction)
