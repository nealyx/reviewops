"""Preview labels must describe the bytes read, including atomic replacements."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import APIError, MAX_EVIDENCE_BYTES, MAX_PREVIEW_BYTES, ReviewStore


class PreviewConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ReviewStore(self.root / "runtime", seed_demo=False)
        self.original = b"SYNTHETIC initial evidence.\n"
        self.detail = self.submit(self.original)
        self.path = self.store.evidence_dir / self.detail["evidence"][0]["storage_name"]

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def submit(self, content, **extra):
        return self.store.submit({
            "id": "PREVIEW-CONSISTENCY", "title": "Synthetic preview check",
            "summary": "Synthetic evidence for file replacement regressions.",
            "owner": "", "actor": "Test fixture", "note": "Synthetic submission.",
            "is_demo": True,
            "evidence": [{"name": "result.txt", "content": content.decode("utf-8")}],
            **extra,
        })

    def readers(self):
        run = self.detail["run"]
        return (
            ("current", lambda: self.store.evidence_preview(run["id"], 0)),
            ("snapshot", lambda: self.store.snapshot_preview(run["id"], run["snapshot_id"], 0)),
        )

    def test_replacement_between_entry_lookup_and_preview_is_not_verified(self):
        changed = b"SYNTHETIC replacement evidence.\n"
        original_preview = self.store._preview
        for name, read in self.readers():
            with self.subTest(route=name):
                self.path.write_bytes(self.original)
                replacement = self.root / "replacement.txt"
                replacement.write_bytes(changed)

                def replace_then_preview(entry):
                    replacement.replace(self.path)
                    return original_preview(entry)

                with patch.object(self.store, "_preview", side_effect=replace_then_preview):
                    preview = read()
                self.assertEqual(preview["text"], changed.decode())
                self.assertEqual(preview["status"], "changed")

    def test_replacement_after_open_keeps_text_and_digest_on_the_opened_file(self):
        changed = b"SYNTHETIC replacement after open.\n"
        original_open = Path.open
        for name, read in self.readers():
            with self.subTest(route=name):
                self.path.write_bytes(self.original)
                replacement = self.root / "replacement.txt"
                replacement.write_bytes(changed)
                replaced = False

                def open_then_replace(path, *args, **kwargs):
                    nonlocal replaced
                    stream = original_open(path, *args, **kwargs)
                    mode = args[0] if args else kwargs.get("mode", "r")
                    if path == self.path and mode == "rb" and not replaced:
                        replacement.replace(self.path)
                        replaced = True
                    return stream

                with patch.object(Path, "open", open_then_replace):
                    preview = read()
                self.assertTrue(replaced)
                self.assertEqual(preview["text"], self.original.decode())
                self.assertEqual(preview["status"], "verified")
                self.assertEqual(self.path.read_bytes(), changed)

    def test_changed_tail_after_preview_limit_is_still_hashed(self):
        prefix = b"x" * MAX_PREVIEW_BYTES
        self.detail = self.submit(prefix + b"original tail", expected_version=1)
        self.path = self.store.evidence_dir / self.detail["evidence"][0]["storage_name"]
        self.path.write_bytes(prefix + b"changed tail")
        for name, read in self.readers():
            with self.subTest(route=name):
                preview = read()
                self.assertEqual(preview["text"], prefix.decode())
                self.assertTrue(preview["truncated"])
                self.assertEqual(preview["status"], "changed")

    def test_oversized_replacement_is_rejected_at_preview_read(self):
        original_preview = self.store._preview
        for name, read in self.readers():
            with self.subTest(route=name):
                self.path.write_bytes(self.original)
                replacement = self.root / "oversized.txt"
                with replacement.open("wb") as stream:
                    stream.seek(MAX_EVIDENCE_BYTES)
                    stream.write(b"x")

                def replace_then_preview(entry):
                    replacement.replace(self.path)
                    return original_preview(entry)

                with patch.object(self.store, "_preview", side_effect=replace_then_preview):
                    with self.assertRaises(APIError) as raised:
                        read()
                self.assertEqual(raised.exception.status, 404)
