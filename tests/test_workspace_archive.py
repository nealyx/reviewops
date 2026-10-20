"""Offline backups preserve review history and reject unsafe or incomplete imports."""

import hashlib
import json
import os
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import ReviewStore
from test_submissions import submission
import workspace_archive as backup


class WorkspaceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "source"
        store = ReviewStore(self.workspace, seed_demo=False)
        first = store.submit(submission())
        store.decision("DEMO-100", {
            "actor": "Reviewer", "note": "Checked the original.",
            "decision": "approved", "expected_version": 1,
        })
        revised = store.submit(submission(expected_version=2, evidence=[
            {"name": "revision.txt", "content": "Revised evidence.\n"}]))
        store.submit(submission(id="MISSING"))
        store.decision("MISSING", {
            "actor": "Reviewer", "note": "Checked before removal.",
            "decision": "approved", "expected_version": 1,
        })
        missing = store.detail("MISSING")["evidence"][0]["storage_name"]
        (store.evidence_dir / missing).unlink()
        (store.evidence_dir / revised["evidence"][0]["storage_name"]).write_bytes(
            b"Bytes changed outside the app.\n")
        (store.evidence_dir / "unreferenced.txt").write_bytes(b"Retain this orphan too.\n")
        self.snapshot = first["run"]["snapshot_id"]
        self.before = {item: store.detail(item) for item in ("DEMO-100", "MISSING")}
        store.close()
        backup.prepare_workspace(self.workspace)
        self.database_hash = hashlib.sha256((self.workspace / backup.DATABASE).read_bytes()).hexdigest()
        self.archive = self.root / "backup.zip"
        self.serial = 0

    def tearDown(self):
        self.temporary.cleanup()

    def export(self):
        return backup.export_workspace(self.workspace, self.archive)

    def changed_archive(self, change):
        if not self.archive.exists():
            self.export()
        with zipfile.ZipFile(self.archive) as source:
            entries = [(item.filename, source.read(item)) for item in source.infolist()]
        entries = change(entries)
        self.serial += 1
        target = self.root / ("altered-{}.zip".format(self.serial))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # Intentional duplicate fixture.
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as package:
                for name, data in entries:
                    package.writestr(name, data)
        return target

    def replace_payload(self, entries, name, content, update_manifest=False):
        changed = []
        for old_name, data in entries:
            if old_name == name:
                data = content
            elif old_name == backup.MANIFEST and update_manifest:
                manifest = json.loads(data)
                row = next(row for row in manifest["files"] if row["path"] == name)
                row.update(sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))
                data = json.dumps(manifest).encode()
            changed.append((old_name, data))
        return changed

    def test_round_trip_retains_raw_database_decisions_revisions_and_bad_evidence(self):
        exported = self.export()
        self.assertEqual(exported["database"], {"runs": 2, "events": 5, "snapshots": 3})
        inspected = backup.inspect_archive(self.archive)
        self.assertEqual(inspected["status"], "verified")
        restored = self.root / "restored"
        backup.restore_workspace(self.archive, restored)
        self.assertEqual(hashlib.sha256((restored / backup.DATABASE).read_bytes()).hexdigest(),
                         self.database_hash)
        self.assertEqual((restored / "evidence/unreferenced.txt").read_bytes(),
                         b"Retain this orphan too.\n")
        self.assertFalse((restored / backup.MANIFEST).exists())
        store = ReviewStore(restored)
        try:
            self.assertEqual({item: store.detail(item) for item in self.before}, self.before)
            self.assertEqual(store.detail("DEMO-100")["run"]["evidence_status"], "changed")
            missing = store.detail("MISSING")["run"]
            self.assertEqual((missing["status"], missing["recorded_status"]),
                             ("blocked", "approved"))
            self.assertIn("0.5", store.snapshot_preview("DEMO-100", self.snapshot, 0)["text"])
        finally:
            store.close()
        self.assertEqual(hashlib.sha256((self.workspace / backup.DATABASE).read_bytes()).hexdigest(),
                         self.database_hash)

    def test_live_workspace_and_crash_sidecars_are_refused_without_deleting_them(self):
        store = ReviewStore(self.workspace)
        try:
            with self.assertRaisesRegex(backup.ArchiveError, "Stop ReviewOps"):
                self.export()
        finally:
            store.close()
        for name in backup.SIDECARS:
            path = self.workspace / name
            path.write_bytes(b"Do not discard pending database state.")
            with self.subTest(name=name), self.assertRaisesRegex(backup.ArchiveError, "Do not delete"):
                self.export()
            self.assertTrue(path.exists())
            path.unlink()  # Remove only our synthetic sidecar fixture.
        self.assertFalse(self.archive.exists())

    def test_prepare_refuses_active_connection_then_preserves_records_and_evidence(self):
        store = ReviewStore(self.workspace)
        evidence_before = {p.relative_to(store.evidence_dir).as_posix(): p.read_bytes()
                           for p in store.evidence_dir.rglob("*") if p.is_file()}
        try:
            with self.assertRaisesRegex(backup.ArchiveError, "exclusively"):
                backup.prepare_workspace(self.workspace)
            self.assertEqual({item: store.detail(item) for item in self.before}, self.before)
        finally:
            store.close()
        result = backup.prepare_workspace(self.workspace)
        self.assertEqual(result["journal_mode"], "delete")
        self.assertFalse(any((self.workspace / name).exists() for name in backup.SIDECARS))
        # A standalone SHM is coordination metadata, not content to copy or delete.
        shared = self.workspace / backup.SHARED_MEMORY
        if not shared.exists():
            shared.write_bytes(b"synthetic residual shared memory")
        shared_before = shared.read_bytes()
        self.export()
        self.assertEqual(shared.read_bytes(), shared_before)
        with zipfile.ZipFile(self.archive) as package:
            self.assertNotIn(backup.SHARED_MEMORY, package.namelist())
        destination = self.root / "prepared-restore"
        backup.restore_workspace(self.archive, destination)
        self.assertEqual({p.relative_to(destination / "evidence").as_posix(): p.read_bytes()
                          for p in (destination / "evidence").rglob("*") if p.is_file()},
                         evidence_before)
        restored = ReviewStore(destination)
        try:
            self.assertEqual({item: restored.detail(item) for item in self.before}, self.before)
        finally:
            restored.close()

    def test_subprocess_close_prepare_and_export_handles_platform_sidecars(self):
        workspace = self.root / "subprocess-workspace"
        script = ("from app import ReviewStore; import sys; "
                  "store=ReviewStore(sys.argv[1],seed_demo=False); store.close()")
        subprocess.run([sys.executable, "-c", script, str(workspace)], check=True,
                       cwd=Path(backup.__file__).parent, capture_output=True)
        if any((workspace / name).exists() for name in backup.SIDECARS):
            with self.assertRaisesRegex(backup.ArchiveError, "prepare"):
                backup.export_workspace(workspace, self.root / "process.zip")
        backup.prepare_workspace(workspace)
        result = backup.export_workspace(workspace, self.root / "process.zip")
        self.assertEqual(result["database"], {"runs": 0, "events": 0, "snapshots": 0})

    def test_unknown_source_files_and_unsafe_shared_memory_are_refused(self):
        unexpected = self.workspace / "unrelated.txt"
        unexpected.write_bytes(b"Do not silently omit this file.")
        with self.assertRaisesRegex(backup.ArchiveError, "Unexpected workspace payload"):
            self.export()
        unexpected.unlink()
        shared = self.workspace / backup.SHARED_MEMORY
        shared.unlink(missing_ok=True)
        shared.symlink_to(self.workspace / backup.DATABASE)
        with self.assertRaisesRegex(backup.ArchiveError, "shared memory"):
            self.export()
        shared.unlink()
        shared.write_bytes(b"x" * 16)
        with patch.object(backup, "MAX_SHARED_MEMORY_BYTES", 8):
            with self.assertRaisesRegex(backup.ArchiveError, "shared memory"):
                self.export()

    def test_output_inside_workspace_or_existing_file_is_never_overwritten(self):
        with self.assertRaisesRegex(backup.ArchiveError, "outside"):
            backup.export_workspace(self.workspace, self.workspace / "backup.zip")
        self.archive.write_bytes(b"Existing archive.")
        with self.assertRaisesRegex(backup.ArchiveError, "already exists"):
            self.export()
        self.assertEqual(self.archive.read_bytes(), b"Existing archive.")

    def test_existing_restore_destinations_including_empty_directory_are_unchanged(self):
        self.export()
        for kind in ("directory", "file", "symlink"):
            destination = self.root / kind
            if kind == "directory":
                destination.mkdir()
            elif kind == "file":
                destination.write_bytes(b"Keep me.")
            else:
                destination.symlink_to(self.workspace, target_is_directory=True)
            with self.subTest(kind=kind), self.assertRaisesRegex(backup.ArchiveError, "already exists"):
                backup.restore_workspace(self.archive, destination)
            self.assertTrue(os.path.lexists(str(destination)))
        self.assertEqual((self.root / "file").read_bytes(), b"Keep me.")
        self.assertEqual(list((self.root / "directory").iterdir()), [])

    def test_destination_created_at_publication_is_not_replaced(self):
        self.export()
        destination = self.root / "raced"
        publish = backup._publish_directory

        def create_then_publish(staging, target):
            target.mkdir()
            publish(staging, target)

        with patch.object(backup, "_publish_directory", side_effect=create_then_publish):
            with self.assertRaises((backup.ArchiveError, FileExistsError)):
                backup.restore_workspace(self.archive, destination)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(self.root.glob(".reviewops-restore-*")), [])

    def test_source_change_and_failed_export_publication_leave_no_partial_archive(self):
        copier = backup._copy_bytes
        changed = []

        def change_after_copy(source, target, limit):
            result = copier(source, target, limit)
            if not changed:
                (self.workspace / "evidence/unreferenced.txt").write_bytes(b"Changed mid-backup.")
                changed.append(True)
            return result

        with patch.object(backup, "_copy_bytes", side_effect=change_after_copy):
            with self.assertRaisesRegex(backup.ArchiveError, "changed during backup"):
                self.export()
        self.assertFalse(self.archive.exists())
        with patch.object(backup.os, "link", side_effect=OSError("Synthetic publication failure")):
            with self.assertRaises(OSError):
                self.export()
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob(".reviewops-backup-*")), [])

    def test_hash_mismatch_missing_extra_and_duplicate_members_fail_before_restore(self):
        def corrupt(entries):
            name = next(name for name, unused in entries if name.startswith("evidence/"))
            return self.replace_payload(entries, name, b"Changed archive bytes.")

        variants = [
            corrupt,
            lambda entries: entries[1:],
            lambda entries: entries + [("evidence/unlisted.txt", b"Extra")],
            lambda entries: entries + [entries[0]],
        ]
        for change in variants:
            archive = self.changed_archive(change)
            destination = self.root / "should-not-exist"
            with self.subTest(change=change), self.assertRaises(backup.ArchiveError):
                backup.restore_workspace(archive, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(self.root.glob(".reviewops-restore-*")), [])

    def test_unsafe_paths_case_aliases_and_symlinks_are_rejected(self):
        for name in ("../outside", "/absolute", "evidence/../outside",
                     "evidence\\outside", "evidence/CON.txt", "evidence/file:stream",
                     "evidence/trailing."):
            archive = self.changed_archive(lambda entries: entries + [(name, b"Unsafe")])
            with self.subTest(name=name), self.assertRaises(backup.ArchiveError):
                backup.inspect_archive(archive)
        archive = self.changed_archive(lambda entries: entries + [
            ("evidence/Case/file.txt", b"A"), ("evidence/case/other.txt", b"B")])
        with self.assertRaises(backup.ArchiveError):
            backup.inspect_archive(archive)
        link = zipfile.ZipInfo("evidence/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.changed_archive(lambda entries: entries + [(link, b"../../outside")])
        with self.assertRaisesRegex(backup.ArchiveError, "ordinary"):
            backup.inspect_archive(archive)
        self.assertFalse((self.root / "outside").exists())

    def test_source_symlinks_and_nonregular_files_are_not_followed(self):
        link = self.workspace / "evidence/link"
        link.symlink_to(self.workspace / backup.DATABASE)
        with self.assertRaisesRegex(backup.ArchiveError, "symbolic link"):
            self.export()
        link.unlink()
        if hasattr(os, "mkfifo"):
            fifo = self.workspace / "evidence/fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(backup.ArchiveError, "non-regular"):
                self.export()

    def test_manifest_duplicates_and_unsupported_version_are_rejected(self):
        def changed(entries, mode):
            result = []
            for name, data in entries:
                if name == backup.MANIFEST:
                    manifest = json.loads(data)
                    if mode == "duplicate":
                        manifest["files"].append(manifest["files"][0])
                    else:
                        manifest["version"] = 99
                    data = json.dumps(manifest).encode()
                result.append((name, data))
            return result
        for mode in ("duplicate", "version"):
            archive = self.changed_archive(lambda entries: changed(entries, mode))
            with self.subTest(mode=mode), self.assertRaises(backup.ArchiveError):
                backup.inspect_archive(archive)

    def test_expansion_total_size_and_member_count_are_bounded(self):
        self.export()
        with patch.object(backup, "MAX_EVIDENCE_BYTES", 8):
            with self.assertRaisesRegex(backup.ArchiveError, "uncompressed-size"):
                backup.inspect_archive(self.archive)
        with patch.object(backup, "MAX_TOTAL_BYTES", 64):
            with self.assertRaises(backup.ArchiveError):
                backup.inspect_archive(self.archive)
        data = bytearray(self.archive.read_bytes())
        struct.pack_into("<HH", data, len(data) - 22 + 8, 65535, 65535)
        excessive = self.root / "excessive.zip"
        excessive.write_bytes(data)
        with patch.object(backup.zipfile, "ZipFile", side_effect=AssertionError("Must reject before parsing")):
            with self.assertRaisesRegex(backup.ArchiveError, "ZIP directory"):
                backup.inspect_archive(excessive)

    def test_zip64_member_metadata_is_rejected_without_zip64_end_records(self):
        self.export()
        with zipfile.ZipFile(self.archive) as package:
            entries = [(item.filename, package.read(item)) for item in package.infolist()]
        for variant in ("central-extra", "local-extra", "local-sentinels"):
            archive = self.root / (variant + ".zip")
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                for name, content in entries:
                    if variant == "central-extra":
                        info = zipfile.ZipInfo(name)
                        info.extra = struct.pack("<HHQQ", 1, 16, len(content), len(content))
                        package.writestr(info, content)
                    else:
                        with package.open(name, "w", force_zip64=True) as output:
                            output.write(content)
            if variant != "central-extra":
                with zipfile.ZipFile(archive) as package:
                    offsets = [entry.header_offset for entry in package.infolist()]
                data = bytearray(archive.read_bytes())
                central = struct.unpack_from("<I", data, len(data) - 6)[0]
                # Keep the central directory ordinary to exercise local-header checks.
                while data[central:central + 4] == b"PK\x01\x02":
                    data[central + 6] = 20
                    lengths = struct.unpack_from("<HHH", data, central + 28)
                    central += 46 + sum(lengths)
                for offset, (name, content) in zip(offsets, entries):
                    data[offset + 4] = 20 if variant == "local-extra" else 45
                    if variant == "local-extra":
                        # Apple Python writes these sizes directly; normalize other
                        # Python versions so this case reaches the extra-field check.
                        extra_offset = offset + 30 + len(name.encode())
                        _, _, uncompressed, compressed = struct.unpack_from(
                            "<HHQQ", data, extra_offset)
                        struct.pack_into("<II", data, offset + 18, compressed, uncompressed)
                    else:
                        struct.pack_into("<II", data, offset + 18, 0xffffffff, 0xffffffff)
                archive.write_bytes(data)
            destination = self.root / ("restore-" + variant)
            with self.subTest(variant=variant), self.assertRaisesRegex(backup.ArchiveError, "ZIP64"):
                backup.restore_workspace(archive, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(self.root.glob(".reviewops-restore-*")), [])

    def test_invalid_database_and_foreign_keys_fail_even_with_matching_hashes(self):
        archive = self.changed_archive(lambda entries: self.replace_payload(
            entries, backup.DATABASE, b"This is not SQLite.", update_manifest=True))
        with self.assertRaisesRegex(backup.ArchiveError, "Database read-only"):
            backup.inspect_archive(archive)
        altered = self.root / "orphaned.sqlite3"
        altered.write_bytes((self.workspace / backup.DATABASE).read_bytes())
        with sqlite3.connect(altered) as database:
            database.execute("PRAGMA foreign_keys=OFF")
            database.execute("DELETE FROM runs WHERE id='MISSING'")
        archive = self.changed_archive(lambda entries: self.replace_payload(
            entries, backup.DATABASE, altered.read_bytes(), update_manifest=True))
        with self.assertRaisesRegex(backup.ArchiveError, "foreign-key"):
            backup.inspect_archive(archive)

    def test_archive_database_view_is_rejected_without_evaluating_it(self):
        altered = self.root / "view.sqlite3"
        altered.write_bytes((self.workspace / backup.DATABASE).read_bytes())
        database = sqlite3.connect(altered)
        database.execute("CREATE VIEW unsafe AS SELECT load_extension('not-a-real-library')")
        database.commit()
        database.close()
        archive = self.changed_archive(lambda entries: self.replace_payload(
            entries, backup.DATABASE, altered.read_bytes(), update_manifest=True))
        with self.assertRaisesRegex(backup.ArchiveError, "cannot contain views"):
            backup.inspect_archive(archive)

    def test_cli_inspect_reports_validation_and_clean_error_without_creating_workspace(self):
        self.export()
        command = [sys.executable, str(Path(backup.__file__)), "inspect", str(self.archive)]
        checked = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["database"]["runs"], 2)
        missing = subprocess.run(command[:-1] + [str(self.root / "absent.zip")],
                                 text=True, capture_output=True)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("Backup error:", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)
