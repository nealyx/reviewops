#!/usr/bin/env python3
"""ReviewOps: a local, synthetic experiment-review prototype (Python stdlib only)."""

import argparse
import hashlib
import json
import mimetypes
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


PROJECT_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 16 * 1024
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
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
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
    return value


class ReviewStore:
    def __init__(self, data_dir, fixture_dir=None):
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
        if not self.db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
            try:
                self._seed()
            except Exception:
                self.db.close()
                raise

    def close(self):
        self.db.close()

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
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, 1, ?)",
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
        version = self._row(run_id)["version"]
        self.db.execute(
            "INSERT INTO events (run_id,actor,action,note,run_version,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, actor, action, note, version, now()))

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
                "SELECT name,expected_sha256 FROM evidence WHERE run_id=? ORDER BY id",
                (run_id,)):
            entry = dict(row)
            entry["actual_sha256"] = None
            try:
                path = safe_file(self.evidence_dir, entry["name"])
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
        run = dict(self._row(run_id))
        entries = self.evidence(run_id)
        run["recorded_status"] = run["status"]
        run["evidence_status"] = self.evidence_status(entries)
        run["evidence_count"] = len(entries)
        if run["evidence_status"] != "verified":
            run["status"] = "blocked"
        return {"run": run, "evidence": entries, "events": self.events(run_id), "demo": True}

    def evidence_preview(self, run_id, index):
        self._row(run_id)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise APIError(400, "Evidence index must be a nonnegative integer.")
        entries = self.evidence(run_id)
        if index >= len(entries):
            raise APIError(404, "Evidence entry not found.")
        entry = entries[index]
        try:
            path = safe_file(self.evidence_dir, entry["name"])
        except ValueError:
            raise APIError(403, "Invalid evidence file path.")
        if entry["status"] == "missing":
            raise APIError(404, "Evidence is missing, unreadable, or exceeds the file limit.")
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_PREVIEW_BYTES + 1)
        except OSError:
            raise APIError(404, "Evidence file is no longer readable.")
        return {
            "name": entry["name"],
            "text": content[:MAX_PREVIEW_BYTES].decode("utf-8", errors="replace"),
            "truncated": len(content) > MAX_PREVIEW_BYTES,
            "status": entry["status"],
        }

    def state(self):
        run_ids = [row["id"] for row in self.db.execute("SELECT id FROM runs ORDER BY id")]
        return {"runs": [self.detail(run_id)["run"] for run_id in run_ids],
                "events": self.events(), "demo": True}

    def export(self):
        state = self.state()
        return {
            "schema_version": 1,
            "demo": True,
            "notice": "SYNTHETIC demonstration data. No real experiments or researchers.",
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
    def __init__(self, address, store, static_dir=None):
        if address[0] != "127.0.0.1":
            raise ValueError("ReviewOps binds only to 127.0.0.1.")
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.static_dir = Path(static_dir or PROJECT_DIR / "static").resolve()
        super().__init__(address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ReviewOps/0.1"

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
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.server.token):
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
        self._send(error.status, {"error": error.message, "demo": True})

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
                raise APIError(413, "Request exceeds the 16 KiB limit.")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise APIError(415, "Use application/json.")
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise APIError(400, "Invalid JSON.")
            if not isinstance(body, dict):
                raise APIError(400, "The request body must be a JSON object.")
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
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    store = ReviewStore(args.data_dir)
    try:
        server = ReviewHTTPServer(("127.0.0.1", args.port), store)
    except OSError as error:
        store.close()
        parser.exit(1, "Could not start local ReviewOps: {}\n".format(error))
    print("ReviewOps · SYNTHETIC local demo", flush=True)
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
