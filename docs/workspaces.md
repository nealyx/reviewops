# Back up and restore a workspace

The JSON audit export describes reviews but contains no evidence file bodies.
Use the separate offline backup command to preserve the workspace database and
all regular files under its `evidence/` directory.

The default workspace is `runtime/`. Keep additional workspaces in separate
locations, such as `workspaces/my-workspace`; never nest them inside a directory
that already contains a `reviewops.sqlite3` database. Each workspace must contain
only its database, SQLite sidecars, and evidence. `runtime/` and `workspaces/`
are both ignored by Git.

**Stop ReviewOps and all CLI imports before exporting. Keep them stopped until
the command finishes.** Export refuses SQLite WAL or journal sidecars, including
an empty WAL. Some SQLite builds retain them after a clean process exit.
Use the explicit preparation command below to checkpoint the database. Do not
delete sidecars manually: they may contain committed data that has not reached
the main database.

## Export and inspect

From the ReviewOps checkout, with an existing workspace at `workspaces/my-workspace`:

```sh
python3 workspace_archive.py prepare --data-dir workspaces/my-workspace
python3 workspace_archive.py export \
  --data-dir workspaces/my-workspace --output ../workspace-backup.zip
python3 workspace_archive.py inspect ../workspace-backup.zip
```

For the default demo, substitute `--data-dir runtime`. If an older setup has
nested workspaces, stop all app/import processes and move each child workspace
as a complete directory to a separate location before exporting either one.

Choose an output outside the workspace, in an existing parent directory.
The command refuses to overwrite an existing file. It checks the staged archive
before publishing it. `inspect` verifies the archive's inventory, file hashes and
sizes, and SQLite integrity and foreign-key consistency. It uses a temporary
directory that is removed afterward; it does not open the application or migrate
the database.

`prepare` obtains an exclusive SQLite lock, checkpoints committed WAL data, and
switches the database to DELETE journal mode. It fails if another connection
prevents the exclusive transition. It writes SQLite's storage and journal
configuration, but does not change review records, events, or evidence. Starting
ReviewOps again returns the database to WAL mode. Keep the app stopped between
preparation and export.

A standalone regular `reviewops.sqlite3-shm` file of at most 1 MiB may remain
after preparation on some platforms. With no WAL or journal present, it is
coordination metadata rather than database content. Export leaves it untouched
and does not include it in the archive. Symlinked, non-regular, or oversized
shared-memory files are refused.

The backup includes:

- The complete `reviewops.sqlite3` file, with metadata, assignments, decisions,
  record versions, snapshot manifests, and audit events.
- Current, historical, and unreferenced regular evidence files, with exact bytes.
- An archive manifest containing actual file hashes and sizes.

Missing evidence stays absent. Modified evidence keeps its modified bytes and
the database's original expected hash. Restoring therefore preserves a blocked
record rather than converting damaged evidence into verified evidence. No
review decision or audit event is invented by backup, inspection, or restore.
Empty evidence folders need not be retained; files and their relative paths are.

## Restore to a new directory

```sh
python3 workspace_archive.py restore ../workspace-backup.zip \
  --destination workspaces/restored-workspace
python3 app.py --data-dir workspaces/restored-workspace --port 8788
```

The destination parent must exist, and the destination itself must not exist,
even as an empty directory or symbolic link. Restore validates in a temporary
sibling directory, then publishes the complete workspace atomically without
replacing an existing destination. Handled failures remove the temporary copy.
A process or power failure may leave a hidden `.reviewops-restore-*` temporary
directory; no automatic crash cleanup is claimed.

Atomic publication uses macOS or Linux no-replace rename facilities, or Windows
rename behavior. If the platform or filesystem cannot provide that operation,
restore fails instead of using a replacing fallback. Cross-platform support in
code is not evidence that every filesystem or operating system has been tested.

## Bounds and limits

This format supports the current submission/snapshot database schema. For an
older workspace, first open it with the current ReviewOps version, then stop it
cleanly. The backup tool never upgrades a database schema; only the explicitly
requested preparation step changes SQLite storage/journal configuration.

Limits are 64 MiB for the database, 8 MiB per evidence file, 256 MiB total
uncompressed payload, 10,000 payload files, 20,000 filesystem entries during
export, and a 4 MiB manifest. The compressed archive is limited to 272 MiB.
Oversized or unreadable files cause an error; they are never silently skipped.
Unexpected top-level workspace files also cause an error. Keep unrelated
documents and backups outside the workspace.

ZIP members must exactly match the manifest. Traversal, absolute paths, symbolic
links, non-regular files, duplicate or colliding paths, encrypted entries, ZIP64,
split archives, and ZIP comments are refused. The supported compression methods
are stored and deflated. Portable names exclude Windows device names, alternate
data streams, and trailing spaces or periods.

The database check uses a read-only, immutable SQLite connection, disables
extension loading, restricts SQL authorization, and enables untrusted-schema
protections where SQLite supports them. Views and virtual tables are refused.
Inspection does not execute evidence files or instantiate the application.

This is an **offline backup**, not a live snapshot service. Sidecar detection and
repeat inventory/hash checks catch common concurrent changes; they cannot prove
that every other local process is stopped or freeze files against later edits.
File permission bits, ownership, timestamps, and unrelated workspace files are
not an operating-system backup. Keep the archive private if its workspace is
private: the ZIP is not encrypted.

Hashes establish content consistency, not the identity of the archive's author
or scientific validity. Someone who can replace both the payload and manifest
can create a different internally consistent archive. Restore only backups whose
origin you trust. The backup is additional evidence preservation, not a new
approval of any experiment.
