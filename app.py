#!/usr/bin/env python3
"""ReviewOps: a local experiment-review workspace (Python stdlib only)."""

import argparse
import hashlib
import json
import mimetypes
import secrets
import shutil
import socket
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from submissions import SubmissionError, parse_json, prepare_submission, read_manifest


PROJECT_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_BYTES = 32 * 1024


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_file(root, name):
    """Resolve a relative file within root, refusing traversal and symlinks."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Evidence names must be nonempty relative paths.")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Evidence names must contain valid Unicode.")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Path traversal and absolute paths are not allowed.")
    root = Path(root)
    if root.is_symlink():
        raise ValueError("The designated directory cannot be a symbolic link.")
    root = root.resolve()
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Symbolic links are not allowed.")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("File must remain within the designated directory.")
    return resolved


def file_sha256(path):
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence file exceeds the local prototype's 8 MiB limit.")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            if size > MAX_EVIDENCE_BYTES:
                raise ValueError("Evidence file exceeds the local prototype's 8 MiB limit.")
            digest.update(chunk)
    return digest.hexdigest()


def bounded_text(value, name, maximum=200, allow_empty=False):
    if not isinstance(value, str):
        raise APIError(400, "{} must be text.".format(name))
    value = value.strip()
    if not value and not allow_empty:
        raise APIError(400, "{} is required.".format(name))
    if len(value) > maximum:
        raise APIError(400, "{} exceeds {} characters.".format(name, maximum))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise APIError(400, "{} contains invalid Unicode.".format(name))
    return value


class ReviewStore:
    def __init__(self, data_dir, fixture_dir=None, seed_demo=True):
        self.data_dir = Path(data_dir).resolve()
        self.evidence_dir = self.data_dir / "evidence"
        self.fixture_dir = Path(fixture_dir or PROJECT_DIR / "demo").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.evidence_dir.is_symlink():
            raise ValueError("The designated evidence directory cannot be a symbolic link.")
        self.evidence_dir.mkdir(exist_ok=True)
        # The HTTP server serializes requests. check_same_thread=False also lets
        # the integration tests run that server in a background thread.
        self.db = sqlite3.connect(str(self.data_dir / "reviewops.sqlite3"),
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                owner TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('needs_review', 'approved', 'changes_requested')),
                version INTEGER NOT NULL CHECK (version > 0),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                name TEXT NOT NULL,
                expected_sha256 TEXT NOT NULL,
                UNIQUE(run_id, name)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id),
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                note TEXT NOT NULL,
                run_version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update
            BEFORE UPDATE ON events BEGIN
                SELECT RAISE(ABORT, 'Audit events are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete
            BEFORE DELETE ON events BEGIN
                SELECT RAISE(ABORT, 'Audit events are append-only');
            END;
        """)
        try:
            self._migrate_submissions()
            if not self.db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
                if seed_demo:
                    self._seed()
                else:
                    with self.db:
                        self.db.execute("INSERT INTO metadata VALUES ('seeded', ?)", (now(),))
            self._capture_legacy_snapshots()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def _read_snapshot(self):
        """Keep related SELECTs on one WAL snapshot, including nested readers."""
        # SELECT alone does not start an implicit sqlite3 transaction. A CLI
        # writer could otherwise commit between the metadata and evidence reads.
        # Reusing a savepoint name is safe: SQLite releases the innermost match.
        self.db.execute("SAVEPOINT reviewops_read_response")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT reviewops_read_response")
            raise
        finally:
            self.db.execute("RELEASE SAVEPOINT reviewops_read_response")

    def _migrate_submissions(self):
        """Add submission history without discarding an existing v0.1 database."""
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "is_demo" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 1")
        if "snapshot_id" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN snapshot_id TEXT")
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(evidence)")}
        if "display_name" not in columns:
            self.db.execute("ALTER TABLE evidence ADD COLUMN display_name TEXT")
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(events)")}
        if "run_version" not in columns:
            # Very early local prototypes did not record an event's version.
            # Keep that absence explicit rather than inventing historical data.
            self.db.execute("ALTER TABLE events ADD COLUMN run_version INTEGER")
        if "snapshot_id" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN snapshot_id TEXT")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                run_version INTEGER NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS snapshots_no_update
            BEFORE UPDATE ON snapshots BEGIN
                SELECT RAISE(ABORT, 'Evidence snapshots are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
            BEFORE DELETE ON snapshots BEGIN
                SELECT RAISE(ABORT, 'Evidence snapshots are append-only');
            END;
        """)
        self.db.commit()

    def _capture_legacy_snapshots(self):
        """Retain bytes present at upgrade, preserving expected hashes and events.

        Old events cannot be retroactively attributed to these captured bytes.
        Current evidence is redirected to the retained copy so new decisions and
        previews refer to the same snapshot. Original files are left untouched.
        """
        created = []
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                rows = list(self.db.execute("SELECT * FROM runs WHERE snapshot_id IS NULL"))
                for row in rows:
                    entries = self.evidence(row["id"])
                    snapshot_id = uuid.uuid4().hex
                    destination = safe_file(self.evidence_dir, "snapshots/" + snapshot_id)
                    destination.mkdir(parents=True, exist_ok=False)
                    created.append(destination)
                    retained = []
                    for index, entry in enumerate(entries):
                        storage_name = "snapshots/{}/evidence-{:04d}.txt".format(
                            snapshot_id, index)
                        size = None
                        if entry["status"] != "missing":
                            source = safe_file(self.evidence_dir, entry["storage_name"])
                            with source.open("rb") as stream:
                                data = stream.read(MAX_EVIDENCE_BYTES + 1)
                            if len(data) > MAX_EVIDENCE_BYTES:
                                raise ValueError("Legacy evidence exceeded the 8 MiB limit during capture.")
                            safe_file(self.evidence_dir, storage_name).write_bytes(data)
                            size = len(data)
                        retained.append({
                            "name": entry["name"], "sha256": entry["expected_sha256"],
                            "storage_name": storage_name, "size_bytes": size,
                        })
                        self.db.execute(
                            "UPDATE evidence SET name=?,display_name=? WHERE run_id=? AND name=?",
                            (storage_name, entry["name"], row["id"], entry["storage_name"]))
                    manifest = {
                        "id": row["id"], "title": row["title"], "summary": row["summary"],
                        "owner": row["owner"], "is_demo": bool(row["is_demo"]),
                        "legacy_capture": True, "evidence": retained,
                    }
                    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False)
                    self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                                    (snapshot_id, row["id"], row["version"],
                                     hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                                     canonical, now()))
                    self.db.execute("UPDATE runs SET snapshot_id=? WHERE id=?",
                                    (snapshot_id, row["id"]))
        except BaseException:
            for destination in created:
                shutil.rmtree(destination, ignore_errors=True)
            raise

    def _seed(self):
        fixture = json.loads((self.fixture_dir / "fixtures.json").read_text())
        if fixture.get("demo") is not True:
            raise ValueError("This prototype accepts only clearly synthetic fixtures.")
        # Validate all paths and hashes before copying or writing any records.
        for run in fixture["runs"]:
            for entry in run["evidence"]:
                safe_file(self.evidence_dir, entry["name"])
                safe_file(self.fixture_dir / "evidence", entry["name"])
                expected = entry["expected_sha256"]
                if (not isinstance(expected, str) or len(expected) != 64
                        or any(c not in "0123456789abcdef" for c in expected)):
                    raise ValueError("Fixture evidence needs a SHA-256 digest.")
        with self.db:
            for run in fixture["runs"]:
                timestamp = now()
                self.db.execute(
                    "INSERT INTO runs (id,title,summary,owner,status,version,updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (run["id"], run["title"], run["summary"],
                     run.get("owner", ""), run.get("status", "needs_review"), timestamp))
                for entry in run["evidence"]:
                    source = safe_file(self.fixture_dir / "evidence", entry["name"])
                    destination = safe_file(self.evidence_dir, entry["name"])
                    if source.is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(str(source), str(destination))
                    self.db.execute(
                        "INSERT INTO evidence (run_id,name,expected_sha256) VALUES (?,?,?)",
                        (run["id"], entry["name"], entry["expected_sha256"]))
                self._event(run["id"], "Demo fixture", "seeded",
                            "SYNTHETIC demonstration record; not a real research result.")
            self.db.execute("INSERT INTO metadata VALUES ('seeded', ?)", (now(),))

    def _row(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise APIError(404, "Run not found.")
        return row

    def _event(self, run_id, actor, action, note):
        row = self._row(run_id)
        self.db.execute(
            "INSERT INTO events (run_id,actor,action,note,run_version,created_at,snapshot_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, actor, action, note, row["version"], now(), row["snapshot_id"]))

    def events(self, run_id=None):
        if run_id is None:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC")
        else:
            rows = self.db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id DESC", (run_id,))
        return [dict(row) for row in rows]

    def evidence(self, run_id):
        entries = []
        for row in self.db.execute(
                "SELECT name,expected_sha256,display_name FROM evidence WHERE run_id=? ORDER BY id",
                (run_id,)):
            entry = dict(row)
            entry["storage_name"] = entry["name"]
            entry["name"] = entry.pop("display_name") or entry["name"]
            entry["actual_sha256"] = None
            try:
                path = safe_file(self.evidence_dir, entry["storage_name"])
                if not path.is_file():
                    entry["status"] = "missing"
                else:
                    entry["actual_sha256"] = file_sha256(path)
                    entry["status"] = (
                        "verified" if entry["actual_sha256"] == entry["expected_sha256"]
                        else "changed")
            except (OSError, ValueError):
                # Unreadable, oversized, or invalid files cannot count as evidence.
                entry["status"] = "missing"
            entries.append(entry)
        return entries

    @staticmethod
    def evidence_status(entries):
        if not entries:
            return "empty"
        statuses = {entry["status"] for entry in entries}
        if "changed" in statuses:
            return "changed"
        if "missing" in statuses:
            return "missing"
        return "verified"

    def detail(self, run_id):
        with self._read_snapshot():
            run = dict(self._row(run_id))
            run["is_demo"] = bool(run["is_demo"])
            entries = self.evidence(run_id)
            run["recorded_status"] = run["status"]
            run["evidence_status"] = self.evidence_status(entries)
            run["evidence_count"] = len(entries)
            if run["evidence_status"] != "verified":
                run["status"] = "blocked"
            return {"run": run, "evidence": entries, "events": self.events(run_id),
                    "snapshots": self.snapshots(run_id), "demo": run["is_demo"]}

    def snapshots(self, run_id):
        rows = self.db.execute("SELECT * FROM snapshots WHERE run_id=? ORDER BY run_version DESC",
                               (run_id,))
        values = []
        for row in rows:
            value = dict(row)
            value["manifest"] = json.loads(value.pop("manifest_json"))
            values.append(value)
        return values

    def submit(self, body):
        """Create or revise a run with a fresh, retained evidence snapshot."""
        try:
            submission = prepare_submission(body)
        except SubmissionError as error:
            raise APIError(400, str(error))
        run_id = submission["id"]
        snapshot_id = uuid.uuid4().hex
        destination = safe_file(self.evidence_dir, "snapshots/" + snapshot_id)
        created = False
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    if body.get("expected_version") is not None:
                        raise APIError(409, "This run does not exist. Create it without a revision.")
                    version = 1
                    self.db.execute(
                        "INSERT INTO runs (id,title,summary,owner,status,version,updated_at,is_demo,snapshot_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, submission["title"], submission["summary"], submission["owner"],
                         "needs_review", version, now(), int(submission["is_demo"]), snapshot_id))
                    action = "submitted"
                else:
                    if self._version(body) != row["version"]:
                        raise APIError(409, "This run changed. Refresh before submitting a revision.")
                    version = row["version"] + 1
                    self.db.execute(
                        "UPDATE runs SET title=?,summary=?,owner=?,status='needs_review',version=?,"
                        "updated_at=?,is_demo=?,snapshot_id=? WHERE id=?",
                        (submission["title"], submission["summary"], submission["owner"], version,
                         now(), int(submission["is_demo"]), snapshot_id, run_id))
                    action = "resubmitted"
                destination.mkdir(parents=True, exist_ok=False)
                created = True
                for item in submission["files"]:
                    (destination / item["name"]).write_bytes(item["data"])
                self.db.execute(
                    "INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                    (snapshot_id, run_id, version, submission["manifest_sha256"],
                     json.dumps(submission["manifest"], ensure_ascii=False), now()))
                self.db.execute("DELETE FROM evidence WHERE run_id=?", (run_id,))
                for item in submission["files"]:
                    self.db.execute(
                        "INSERT INTO evidence (run_id,name,expected_sha256,display_name) VALUES (?,?,?,?)",
                        (run_id, "snapshots/{}/{}".format(snapshot_id, item["name"]),
                         item["sha256"], item["name"]))
                self._event(run_id, submission["actor"], action, submission["note"])
        except BaseException:
            if created:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        return self.detail(run_id)

    def evidence_preview(self, run_id, index):
        self._row(run_id)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise APIError(400, "Evidence index must be a nonnegative integer.")
        entries = list(self.db.execute(
            "SELECT name,expected_sha256,display_name FROM evidence WHERE run_id=? ORDER BY id",
            (run_id,)))
        if index >= len(entries):
            raise APIError(404, "Evidence entry not found.")
        entry = entries[index]
        return self._preview({
            "name": entry["display_name"] or entry["name"],
            "storage_name": entry["name"],
            "expected_sha256": entry["expected_sha256"],
        })

    def snapshot_preview(self, run_id, snapshot_id, index):
        self._row(run_id)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise APIError(400, "Evidence index must be a nonnegative integer.")
        row = self.db.execute("SELECT * FROM snapshots WHERE id=? AND run_id=?",
                              (snapshot_id, run_id)).fetchone()
        if row is None:
            raise APIError(404, "Evidence snapshot not found for this run.")
        manifest = json.loads(row["manifest_json"])
        entries = manifest["evidence"]
        if index >= len(entries):
            raise APIError(404, "Evidence entry not found.")
        item = entries[index]
        entry = {
            "name": item["name"],
            "storage_name": item.get("storage_name", "snapshots/{}/{}".format(
                snapshot_id, item["name"])),
            "expected_sha256": item["sha256"],
        }
        return self._preview(entry)

    def _preview(self, entry):
        try:
            path = safe_file(self.evidence_dir, entry["storage_name"])
        except ValueError:
            raise APIError(403, "Invalid evidence file path.")
        digest = hashlib.sha256()
        content = bytearray()
        size = 0
        try:
            if not path.is_file():
                raise APIError(404, "Evidence is missing or unreadable.")
            # Hash and preview the same opened file. A replacement must not
            # inherit a verification result from an earlier, separate read.
            with path.open("rb") as stream:
                while True:
                    chunk = stream.read(min(65536, MAX_EVIDENCE_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_EVIDENCE_BYTES:
                        raise APIError(404, "Evidence exceeds the 8 MiB file limit.")
                    digest.update(chunk)
                    content.extend(chunk[:MAX_PREVIEW_BYTES - len(content)])
        except OSError:
            raise APIError(404, "Evidence file is no longer readable.")
        return {
            "name": entry["name"],
            "text": content.decode("utf-8", errors="replace"),
            "truncated": size > MAX_PREVIEW_BYTES,
            "status": "verified" if digest.hexdigest() == entry["expected_sha256"] else "changed",
        }

    def state(self):
        with self._read_snapshot():
            run_ids = [row["id"] for row in self.db.execute("SELECT id FROM runs ORDER BY id")]
            runs = [self.detail(run_id)["run"] for run_id in run_ids]
            # A later synthetic revision cannot relabel retained real history as demo
            # data. Export includes every snapshot, not just each run's current state.
            history_is_demo = all(
                json.loads(row["manifest_json"]).get("is_demo") is True
                for row in self.db.execute("SELECT manifest_json FROM snapshots"))
            return {"runs": runs, "events": self.events(),
                    "demo": all(run["is_demo"] for run in runs) and history_is_demo}

    def export(self):
        with self._read_snapshot():
            state = self.state()
            return {
                "schema_version": 2,
                "demo": state["demo"],
                "notice": ("SYNTHETIC demonstration data." if state["demo"]
                           else "Local workspace export. Contains user-submitted records; review before sharing."),
                "exported_at": now(),
                "runs": [self.detail(run["id"]) for run in state["runs"]],
                "events": state["events"],
            }

    @staticmethod
    def _version(body):
        version = body.get("expected_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise APIError(400, "expected_version must be a positive integer.")
        return version

    def assign(self, run_id, body):
        expected = self._version(body)
        actor = bounded_text(body.get("actor"), "actor")
        owner = bounded_text(body.get("owner"), "owner", allow_empty=True)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._row(run_id)
            if row["version"] != expected:
                raise APIError(409, "This run changed. Refresh before saving your assignment.")
            self.db.execute(
                "UPDATE runs SET owner=?,version=version+1,updated_at=? WHERE id=?",
                (owner, now(), run_id))
            self._event(run_id, actor, "assigned",
                        "Owner changed from {} to {}.".format(
                            row["owner"] or "Unassigned", owner or "Unassigned"))
        return self.detail(run_id)

    def decision(self, run_id, body):
        expected = self._version(body)
        actor = bounded_text(body.get("actor"), "actor")
        note = bounded_text(body.get("note"), "note", maximum=4000)
        decision = body.get("decision")
        if decision not in ("approved", "changes_requested"):
            raise APIError(400, "decision must be approved or changes_requested.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._row(run_id)
            if row["version"] != expected:
                raise APIError(409, "This run changed. Refresh before recording your decision.")
            entries = self.evidence(run_id)
            if decision == "approved" and self.evidence_status(entries) != "verified":
                raise APIError(422, "Approval requires at least one evidence file and all "
                               "evidence must be present with matching SHA-256 hashes.")
            self.db.execute(
                "UPDATE runs SET status=?,version=version+1,updated_at=? WHERE id=?",
                (decision, now(), run_id))
            self._event(run_id, actor, decision, note)
        return self.detail(run_id)


class ReviewHTTPServer(HTTPServer):
    # Keep serialized SQLite access, but do not let an idle browser connection
    # hold the only request handler forever. This is a per-I/O inactivity
    # timeout, not an absolute deadline for a client that keeps sending bytes.
    connection_timeout_seconds = 5.0

    def __init__(self, address, store, static_dir=None):
        if address[0] != "127.0.0.1":
            raise ValueError("ReviewOps binds only to 127.0.0.1.")
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.static_dir = Path(static_dir or PROJECT_DIR / "static").resolve()
        super().__init__(address, ReviewHandler)

    def get_request(self):
        connection, address = super().get_request()
        # Applies before the first header is read, and also bounds blocked
        # response writes. Handler threads are deliberately not introduced.
        connection.settimeout(self.connection_timeout_seconds)
        return connection, address


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ReviewOps/0.2"

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}

    def _check_request(self, write=False):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self._allowed_hosts():
            raise APIError(403, "Unexpected Host. Use the printed local ReviewOps address.")
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] not in {
                "http://" + host for host in self._allowed_hosts()}):
            raise APIError(403, "Unexpected Origin.")
        if write:
            tokens = self.headers.get_all("X-ReviewOps-Token", [])
            if (len(tokens) != 1 or not tokens[0].isascii()
                    or not secrets.compare_digest(tokens[0], self.server.token)):
                raise APIError(403, "Missing or invalid local request token. Refresh the page.")

    def _send(self, status, payload, content_type="application/json; charset=utf-8",
              download=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="reviewops-export.json"')
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, error):
        self._send(error.status, {"error": error.message})

    def do_GET(self):
        try:
            self._check_request()
            path = unquote(urlsplit(self.path).path)
            if path == "/api/state":
                state = self.server.store.state()
                state["csrf_token"] = self.server.token
                self._send(200, state)
            elif path == "/api/export":
                self._send(200, self.server.store.export(), download=True)
            elif path.startswith("/api/runs/") and len(path.split("/")) == 4:
                self._send(200, self.server.store.detail(path.split("/")[3]))
            elif (path.startswith("/api/runs/") and len(path.split("/")) == 6
                  and path.split("/")[4] == "evidence"):
                parts = path.split("/")
                index = parts[5]
                if len(index) > 9 or not index.isascii() or not index.isdigit():
                    raise APIError(400, "Evidence index must be a nonnegative integer.")
                self._send(200, self.server.store.evidence_preview(parts[3], int(index)))
            elif (path.startswith("/api/runs/") and len(path.split("/")) == 8
                  and path.split("/")[4] == "snapshots" and path.split("/")[6] == "evidence"):
                parts = path.split("/")
                index = parts[7]
                if len(index) > 9 or not index.isascii() or not index.isdigit():
                    raise APIError(400, "Evidence index must be a nonnegative integer.")
                self._send(200, self.server.store.snapshot_preview(parts[3], parts[5], int(index)))
            elif path.startswith("/api/"):
                raise APIError(404, "API route not found.")
            else:
                self._static(path)
        except APIError as error:
            self._error(error)
        except (OSError, sqlite3.Error):
            self._error(APIError(500, "Local storage could not be read. See the terminal."))

    def _static(self, path):
        name = "index.html" if path == "/" else path.lstrip("/")
        if name.startswith("static/"):
            name = name[len("static/"):]
        try:
            file_path = safe_file(self.server.static_dir, name)
        except ValueError:
            raise APIError(403, "Invalid static file path.")
        if not file_path.is_file():
            raise APIError(404, "Page not found.")
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self._send(200, file_path.read_bytes(), content_type)

    def do_POST(self):
        try:
            self._check_request(write=True)
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "Transfer encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise APIError(411, "One Content-Length header is required.")
            try:
                length = int(lengths[0])
            except ValueError:
                raise APIError(400, "Invalid Content-Length.")
            if length < 0:
                raise APIError(400, "Invalid Content-Length.")
            if length > MAX_BODY_BYTES:
                raise APIError(413, "Request exceeds the 2 MiB limit.")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise APIError(415, "Use application/json.")
            try:
                content = self.rfile.read(length)
            except socket.timeout:
                raise APIError(408, "The request body was not received in time.")
            if len(content) != length:
                raise APIError(400, "The request body ended before its declared length.")
            try:
                body = parse_json(content)
            except SubmissionError as error:
                raise APIError(400, str(error))
            if not isinstance(body, dict):
                raise APIError(400, "The request body must be a JSON object.")
            if urlsplit(self.path).path == "/api/submissions":
                self._send(201, self.server.store.submit(body))
                return
            parts = unquote(urlsplit(self.path).path).split("/")
            if len(parts) != 5 or parts[1:3] != ["api", "runs"]:
                raise APIError(404, "API route not found.")
            run_id, operation = parts[3:]
            if operation == "assign":
                result = self.server.store.assign(run_id, body)
            elif operation == "decision":
                result = self.server.store.decision(run_id, body)
            else:
                raise APIError(404, "API route not found.")
            self._send(200, result)
        except APIError as error:
            self._error(error)
        except (OSError, sqlite3.Error):
            self._error(APIError(500, "Local storage could not be updated. See the terminal."))

    def log_message(self, format_string, *args):
        # Paths and status only; never log request tokens or request bodies.
        sys.stderr.write("{} {}\n".format(self.log_date_time_string(), format_string % args))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_DIR / "runtime")
    parser.add_argument("--import-manifest", type=Path,
                        help="Import one explicitly selected JSON manifest and exit.")
    parser.add_argument("--actor", help="Actor label recorded for a manifest import.")
    parser.add_argument("--empty", action="store_true",
                        help="Start a new data directory without synthetic demo records.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    store = ReviewStore(args.data_dir, seed_demo=not args.empty)
    if args.import_manifest is not None:
        try:
            result = store.submit(read_manifest(args.import_manifest, actor=args.actor))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (SubmissionError, APIError, OSError) as error:
            parser.exit(1, "Import failed: {}\n".format(error))
        finally:
            store.close()
        return
    try:
        server = ReviewHTTPServer(("127.0.0.1", args.port), store)
    except OSError as error:
        store.close()
        parser.exit(1, "Could not start local ReviewOps: {}\n".format(error))
    print("ReviewOps · local experiment review", flush=True)
    print("Open http://127.0.0.1:{}/".format(server.server_address[1]), flush=True)
    print("Data persists in {}".format(store.data_dir), flush=True)
    print("Stop with Ctrl+C. No cloud service or model API is used.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReviewOps stopped.")
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
