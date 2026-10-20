"""Offline, bounded workspace backups. Uses only the Python standard library."""

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import unicodedata
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from submissions import parse_json


DATABASE = "reviewops.sqlite3"
MANIFEST = "WORKSPACE_MANIFEST.json"
MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 16 * 1024 * 1024
MAX_FILES = 10000
SIDECARS = tuple(DATABASE + suffix for suffix in ("-wal", "-journal"))
SHARED_MEMORY = DATABASE + "-shm"
MAX_SHARED_MEMORY_BYTES = 1024 * 1024
REQUIRED_COLUMNS = {
    "metadata": {"key", "value"},
    "runs": {"id", "title", "summary", "owner", "status", "version", "updated_at",
             "is_demo", "snapshot_id"},
    "evidence": {"id", "run_id", "name", "expected_sha256", "display_name"},
    "events": {"id", "run_id", "actor", "action", "note", "run_version", "created_at",
               "snapshot_id"},
    "snapshots": {"id", "run_id", "run_version", "manifest_sha256", "manifest_json",
                  "created_at"},
}


class ArchiveError(ValueError):
    """The requested operation cannot safely preserve this workspace."""


def _path_name(name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise ArchiveError("Archive names must be relative forward-slash paths.")
    try:
        encoded = name.encode("utf-8")
    except UnicodeError as error:
        raise ArchiveError("Archive names must be valid Unicode.") from error
    parts = name.split("/")
    if len(encoded) > 1024 or len(parts) > 32:
        raise ArchiveError("Archive path is too long or too deeply nested.")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or any(ord(char) < 32 or char in ':<>|"*?' for char in part)
                or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?",
                                part, re.IGNORECASE)):
            raise ArchiveError("Unsafe or nonportable archive path: {!r}".format(name))
    return name


def _payload_limit(name):
    _path_name(name)
    if name == DATABASE:
        return MAX_DATABASE_BYTES
    if name.startswith("evidence/"):
        return MAX_EVIDENCE_BYTES
    raise ArchiveError("Unexpected workspace payload: {!r}".format(name))


def _collision_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _check_names(names):
    seen = set()
    directories = {}
    for name in names:
        _path_name(name)
        key = _collision_key(name)
        if key in seen:
            raise ArchiveError("Duplicate or case/Unicode-colliding archive names.")
        seen.add(key)
        parts = name.split("/")
        for i in range(1, len(parts)):
            directory = "/".join(parts[:i])
            previous = directories.setdefault(_collision_key(directory), directory)
            if previous != directory:
                raise ArchiveError("Directory names collide across supported filesystems.")
    for key in seen:
        parts = key.split("/")
        if any("/".join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ArchiveError("An archive file is also used as a directory.")


def _regular_stream(path):
    # NONBLOCK avoids hanging on a FIFO substituted for a file; NOFOLLOW rejects
    # a final-component symlink. The offline requirement remains essential.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if path.is_symlink():
        raise ArchiveError("Symbolic links are not supported: {}".format(path.name))
    descriptor = os.open(str(path), flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ArchiveError("Only regular files can be backed up.")
    return os.fdopen(descriptor, "rb")


def _copy_bytes(source, destination, limit):
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = source.read(min(65536, limit + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ArchiveError("A file exceeds the workspace backup size limit.")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _offline(root):
    if any(os.path.lexists(str(root / name)) for name in SIDECARS):
        raise ArchiveError(
            "SQLite sidecar files are present. Stop ReviewOps and all import commands "
            "before backup, then run `python3 workspace_archive.py prepare --data-dir "
            "<workspace>` and retry. Do not delete sidecars manually.")
    shared = root / SHARED_MEMORY
    if os.path.lexists(str(shared)):
        info = shared.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SHARED_MEMORY_BYTES:
            raise ArchiveError("Standalone SQLite shared memory must be a regular file "
                               "no larger than 1 MiB.")


def _inventory(root):
    _offline(root)
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("Choose an existing workspace directory, not a symlink.")
    found, entries = {}, 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for item in sorted(directory.iterdir()):
            entries += 1
            if entries > MAX_FILES * 2:
                raise ArchiveError("Workspace contains too many filesystem entries.")
            info = item.lstat()
            relative = item.relative_to(root).as_posix()
            _path_name(relative)
            if stat.S_ISLNK(info.st_mode):
                raise ArchiveError("Workspace contains a symbolic link: " + relative)
            if stat.S_ISDIR(info.st_mode):
                if relative != "evidence" and not relative.startswith("evidence/"):
                    raise ArchiveError("Unexpected workspace directory: " + relative)
                pending.append(item)
            elif stat.S_ISREG(info.st_mode):
                if relative == SHARED_MEMORY:
                    continue  # SQLite coordination state, never database/evidence content.
                limit = _payload_limit(relative)
                if info.st_size > limit:
                    raise ArchiveError("Workspace file exceeds its limit: " + relative)
                found[relative] = (info.st_dev, info.st_ino, info.st_size,
                                   info.st_mtime_ns, info.st_ctime_ns)
            else:
                raise ArchiveError("Workspace contains a non-regular file: " + relative)
            if len(found) > MAX_FILES:
                raise ArchiveError("Workspace contains too many files.")
    _check_names(found)
    if DATABASE not in found:
        raise ArchiveError("Workspace has no reviewops.sqlite3 database.")
    if sum(value[2] for value in found.values()) > MAX_TOTAL_BYTES:
        raise ArchiveError("Workspace exceeds the 256 MiB total payload limit.")
    return found


def _database_summary(path):
    """Read only; never instantiate ReviewStore or migrate archived schema."""
    database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1",
                               uri=True, timeout=0)
    try:
        database.execute("PRAGMA query_only=ON")
        database.execute("PRAGMA trusted_schema=OFF")
        if hasattr(database, "enable_load_extension"):
            database.enable_load_extension(False)
        deadline = time.monotonic() + 5
        database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

        def authorize(action, first, second, unused_database, unused_trigger):
            if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and second == "count":
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first in {
                    "table_info", "integrity_check", "foreign_key_check"}:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        database.set_authorizer(authorize)
        schema = list(database.execute("SELECT type,name,sql FROM sqlite_master"))
        tables = {}
        for kind, name, sql in schema:
            if kind == "table":
                if name not in {*REQUIRED_COLUMNS, "sqlite_sequence"}:
                    raise ArchiveError("Unexpected database table: " + name)
                if not sql or not re.match(r"CREATE\s+TABLE\b", sql, re.IGNORECASE):
                    raise ArchiveError("Only ordinary database tables are supported.")
                tables[name] = sql
            elif kind == "view":
                raise ArchiveError("Workspace databases cannot contain views.")
            elif kind not in ("index", "trigger"):
                raise ArchiveError("Unsupported database schema object.")
        for name, expected in REQUIRED_COLUMNS.items():
            if name not in tables:
                raise ArchiveError("Unsupported workspace schema; open and cleanly close "
                                   "it with the current ReviewOps version first.")
            columns = {row[1] for row in database.execute("PRAGMA table_info(" + name + ")")}
            if not expected.issubset(columns):
                raise ArchiveError("Unsupported workspace columns in " + name)
        if database.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
            raise ArchiveError("SQLite integrity check failed.")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ArchiveError("SQLite foreign-key consistency check failed.")
        return {name: database.execute("SELECT count(*) FROM " + name).fetchone()[0]
                for name in ("runs", "events", "snapshots")}
    except sqlite3.Error as error:
        raise ArchiveError("Database read-only validation failed: " + str(error)) from error
    finally:
        database.close()


def prepare_workspace(data_dir):
    """Explicitly checkpoint a stopped workspace, without changing review records."""
    root = Path(data_dir).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ArchiveError("Choose an existing workspace directory, not a symlink.")
    root = root.resolve()
    for name in (DATABASE, SHARED_MEMORY, *SIDECARS):
        path = root / name
        if os.path.lexists(str(path)):
            info = path.lstat()
            limit = MAX_SHARED_MEMORY_BYTES if name == SHARED_MEMORY else MAX_TOTAL_BYTES
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ArchiveError("Database and sidecars must be bounded regular files.")
    if not (root / DATABASE).is_file():
        raise ArchiveError("Workspace has no reviewops.sqlite3 database.")
    database = sqlite3.connect((root / DATABASE).as_uri() + "?mode=rw", uri=True, timeout=0)
    try:
        database.execute("PRAGMA trusted_schema=OFF")
        if hasattr(database, "enable_load_extension"):
            database.enable_load_extension(False)
        database.execute("PRAGMA locking_mode=EXCLUSIVE")
        database.execute("BEGIN EXCLUSIVE")
        database.execute("COMMIT")
        checkpoint = database.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if not checkpoint or checkpoint[0] != 0:
            raise ArchiveError("SQLite checkpoint is busy. Stop all app/import processes and retry.")
        mode = database.execute("PRAGMA journal_mode=DELETE").fetchone()
        if not mode or mode[0].lower() != "delete":
            raise ArchiveError("SQLite could not leave WAL mode. Stop all app/import processes.")
    except sqlite3.Error as error:
        raise ArchiveError("Could not prepare this workspace exclusively. Stop ReviewOps "
                           "and every import/SQLite connection, then retry: " + str(error)) from error
    finally:
        database.close()
    _offline(root)
    return {"status": "prepared", "journal_mode": "delete",
            "database": _database_summary(root / DATABASE),
            "notice": "Keep the app and imports stopped until export finishes. "
                      "No review records or evidence were changed."}


def _manifest_rows(manifest):
    if (not isinstance(manifest, dict)
            or set(manifest) != {"format", "version", "created_at", "files"}
            or manifest["format"] != "reviewops-workspace"
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["created_at"], str)
            or len(manifest["created_at"]) > 100
            or not isinstance(manifest["files"], list)
            or not 1 <= len(manifest["files"]) <= MAX_FILES):
        raise ArchiveError("Unsupported or invalid workspace archive manifest.")
    rows, total = {}, 0
    for row in manifest["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}:
            raise ArchiveError("Invalid workspace manifest file record.")
        limit = _payload_limit(row["path"])
        if (type(row["bytes"]) is not int or not 0 <= row["bytes"] <= limit
                or not isinstance(row["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
            raise ArchiveError("Invalid file hash or size in workspace manifest.")
        if row["path"] in rows:
            raise ArchiveError("Duplicate workspace manifest path.")
        rows[row["path"]] = row
        total += row["bytes"]
    _check_names(rows)
    if DATABASE not in rows or total > MAX_TOTAL_BYTES:
        raise ArchiveError("Missing database or excessive workspace payload size.")
    return rows


def _zip_directory_bound(stream):
    # Our bounded format needs neither ZIP64 nor comments. Check central-directory
    # bounds before ZipFile allocates a Python object for every attacker-supplied entry.
    size = os.fstat(stream.fileno()).st_size
    if not 22 <= size <= MAX_ARCHIVE_BYTES:
        raise ArchiveError("Archive is empty or exceeds its compressed-size limit.")
    stream.seek(-22, os.SEEK_END)
    magic, disk, start_disk, on_disk, count, central_size, offset, comment = struct.unpack(
        "<4s4H2LH", stream.read(22))
    if (magic != b"PK\x05\x06" or disk or start_disk or comment
            or count != on_disk or not 2 <= count <= MAX_FILES + 1
            or central_size > (MAX_FILES + 1) * 1200
            or offset + central_size != size - 22):
        raise ArchiveError("Unsupported or excessive ZIP directory; ZIP64, split "
                           "archives and ZIP comments are not supported.")
    stream.seek(offset)
    central = stream.read(central_size)
    position, observed = 0, 0
    while position < len(central):
        if central[position:position + 4] != b"PK\x01\x02" or len(central) - position < 46:
            raise ArchiveError("Invalid ZIP central directory.")
        name_size, extra_size, comment_size = struct.unpack_from("<HHH", central, position + 28)
        position += 46 + name_size + extra_size + comment_size
        observed += 1
        if observed > count or position > len(central):
            raise ArchiveError("ZIP central directory exceeds its declared member count.")
    if observed != count:
        raise ArchiveError("ZIP central directory member count is inconsistent.")
    stream.seek(0)
    return count


def _reject_zip64_extra(extra):
    position = 0
    while position < len(extra):
        if len(extra) - position < 4:
            raise ArchiveError("Invalid ZIP extra field.")
        kind, length = struct.unpack_from("<HH", extra, position)
        position += 4
        if kind == 1:
            raise ArchiveError("ZIP64 member metadata is not supported.")
        if position + length > len(extra):
            raise ArchiveError("Invalid ZIP extra field.")
        position += length


def _check_zip_member_format(stream, entry):
    # Small ZIP64 members can use an ordinary central directory and end record.
    # Inspect local metadata too, which ZipInfo does not expose.
    if entry.extract_version >= 45:
        raise ArchiveError("ZIP64 and advanced ZIP member versions are not supported.")
    _reject_zip64_extra(entry.extra)
    stream.seek(entry.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ArchiveError("Invalid ZIP local file header.")
    compressed, uncompressed = struct.unpack_from("<II", header, 18)
    if header[4] >= 45 or 0xffffffff in (compressed, uncompressed):
        raise ArchiveError("ZIP64 member metadata is not supported.")
    name_size, extra_size = struct.unpack_from("<HH", header, 26)
    stream.seek(name_size, os.SEEK_CUR)
    extra = stream.read(extra_size)
    if len(extra) != extra_size:
        raise ArchiveError("Invalid ZIP local extra field.")
    _reject_zip64_extra(extra)


def _unpack_validated(archive, staging):
    with _regular_stream(archive) as stream:
        count = _zip_directory_bound(stream)
        with zipfile.ZipFile(stream) as package:
            entries = package.infolist()
            if len(entries) != count:
                raise ArchiveError("ZIP member count does not match its directory.")
            _check_names([entry.filename for entry in entries])
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.orig_filename != entry.filename or entry.is_dir()
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or entry.flag_bits & 1
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ArchiveError("ZIP entries must be ordinary, unencrypted files.")
                limit = MAX_MANIFEST_BYTES if entry.filename == MANIFEST else _payload_limit(entry.filename)
                if entry.file_size > limit:
                    raise ArchiveError("ZIP entry exceeds its uncompressed-size limit.")
                _check_zip_member_format(stream, entry)
            if sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
                raise ArchiveError("ZIP exceeds the uncompressed workspace limit.")
            if MANIFEST not in package.namelist():
                raise ArchiveError("Archive has no workspace manifest.")
            with package.open(MANIFEST) as manifest_stream:
                content = manifest_stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ArchiveError("Workspace manifest exceeds its size limit.")
            manifest = parse_json(content)
            rows = _manifest_rows(manifest)
            if {entry.filename for entry in entries} != {*rows, MANIFEST}:
                raise ArchiveError("ZIP contents do not exactly match the manifest.")
            for entry in entries:
                if entry.filename == MANIFEST:
                    continue
                row = rows[entry.filename]
                if entry.file_size != row["bytes"]:
                    raise ArchiveError("ZIP entry size disagrees with its manifest.")
                destination = staging / entry.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, destination.open("xb") as output:
                    observed = _copy_bytes(source, output, row["bytes"])
                if observed != {"sha256": row["sha256"], "bytes": row["bytes"]}:
                    raise ArchiveError("Workspace file hash or size mismatch: " + entry.filename)
    (staging / "evidence").mkdir(exist_ok=True)
    return {
        "format": manifest["format"], "version": manifest["version"],
        "created_at": manifest["created_at"], "files": len(rows),
        "payload_bytes": sum(row["bytes"] for row in rows.values()),
        "database": _database_summary(staging / DATABASE),
    }


def _new_output(path):
    path = Path(path).expanduser()
    if os.path.lexists(str(path)):
        raise ArchiveError("Destination already exists; choose a new path.")
    if not path.parent.is_dir():
        raise ArchiveError("The destination parent directory must already exist.")
    return path.parent.resolve() / path.name


def _publish_directory(staging, destination):
    """Atomic no-clobber publication, including a concurrently created empty dir."""
    if os.name == "nt":
        os.rename(staging, destination)  # Windows rename refuses an existing target.
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(library, "renamex_np"):
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (os.fsencode(staging), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint]
        arguments = (-100, os.fsencode(staging), -100, os.fsencode(destination), 1)
    else:
        raise ArchiveError("Atomic no-overwrite restore is unavailable on this platform.")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise ArchiveError("Destination already exists; it was not replaced.")
        raise ArchiveError("Atomic no-overwrite restore failed: " + os.strerror(code))


def export_workspace(data_dir, output):
    source = Path(data_dir).expanduser()
    if source.is_symlink():
        raise ArchiveError("Workspace directory cannot be a symbolic link.")
    source = source.resolve()
    output = _new_output(output)
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ArchiveError("Write the backup outside the workspace directory.")
    before = _inventory(source)
    descriptor, temporary = tempfile.mkstemp(prefix=".reviewops-backup-", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        rows = []
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=False) as package:
            for name in sorted(before):
                with _regular_stream(source / name) as stream, package.open(name, "w") as target:
                    observed = _copy_bytes(stream, target, _payload_limit(name))
                rows.append({"path": name, **observed})
            manifest = {
                "format": "reviewops-workspace", "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": rows,
            }
            encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ArchiveError("Workspace manifest exceeds its size limit.")
            _manifest_rows(manifest)
            package.writestr(MANIFEST, encoded)
        if _inventory(source) != before:
            raise ArchiveError("Workspace changed during backup; stop the app and retry.")
        for row in rows:
            with _regular_stream(source / row["path"]) as stream:
                observed = _copy_bytes(stream, None, _payload_limit(row["path"]))
            if observed != {"sha256": row["sha256"], "bytes": row["bytes"]}:
                raise ArchiveError("Workspace bytes changed during backup; stop the app and retry.")
        if _inventory(source) != before:
            raise ArchiveError("Workspace changed during backup; stop the app and retry.")
        with tempfile.TemporaryDirectory(prefix="reviewops-backup-check-") as checking:
            summary = _unpack_validated(temporary, Path(checking))
        # A hard link publishes this completed file only if the name is absent.
        os.link(str(temporary), str(output))
        return {"status": "exported", "archive": str(output), **summary}
    finally:
        temporary.unlink(missing_ok=True)


def inspect_archive(archive):
    with tempfile.TemporaryDirectory(prefix="reviewops-inspect-") as checking:
        summary = _unpack_validated(Path(archive).expanduser(), Path(checking))
    return {"status": "verified", **summary}


def restore_workspace(archive, destination):
    destination = _new_output(destination)
    staging = Path(tempfile.mkdtemp(prefix=".reviewops-restore-", dir=destination.parent))
    try:
        summary = _unpack_validated(Path(archive).expanduser(), staging)
        _publish_directory(staging, destination)
        return {"status": "restored", "destination": str(destination), **summary}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare", help="Checkpoint a stopped workspace for backup.")
    preparing.add_argument("--data-dir", required=True)
    exporting = commands.add_parser("export", help="Back up a stopped workspace.")
    exporting.add_argument("--data-dir", required=True)
    exporting.add_argument("--output", required=True)
    inspecting = commands.add_parser("inspect", help="Verify a backup without restoring it.")
    inspecting.add_argument("archive")
    restoring = commands.add_parser("restore", help="Restore into a new directory only.")
    restoring.add_argument("archive")
    restoring.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_workspace(args.data_dir)
        elif args.command == "export":
            result = export_workspace(args.data_dir, args.output)
        elif args.command == "inspect":
            result = inspect_archive(args.archive)
        else:
            result = restore_workspace(args.archive, args.destination)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError,
            struct.error, zlib.error) as error:
        print("Backup error: {}".format(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
