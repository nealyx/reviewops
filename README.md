# ReviewOps

[![Checks](https://github.com/nealyx/reviewops/actions/workflows/checks.yml/badge.svg)](https://github.com/nealyx/reviewops/actions/workflows/checks.yml)

**Know what was reviewed, which evidence it used, and what needs to happen next.**

ReviewOps is a local experiment-review workspace. Submit a small evidence package,
assign its reviewer, inspect the files, and record a decision. When the experiment
changes, submit a new snapshot: the old files and decision remain readable, and
the new evidence needs a fresh review.

It addresses a common research handoff problem: results live in one folder,
comments live somewhere else, and nobody is sure whether an approval refers to
the latest files.

**Status: working local prototype; researcher pilot pending.** The five startup
runs and import example are synthetic. This is an independent project built
with AI-assisted engineering.
No adoption, time savings, or scientific outcome has been measured.

[Manifest format](docs/manifests.md) · [Design and boundaries](docs/design.md) ·
[Workspace backups](docs/workspaces.md) ·
[Validation and preview](docs/validation.md) · [Researcher pilot](PILOT.md) ·
[Contributing](CONTRIBUTING.md) · [Download a release](https://github.com/nealyx/reviewops/releases)

![ReviewOps with a synthetic experiment awaiting review after a revision](docs/assets/reviewops-overview.jpg)

## Run locally

Python 3.9 or later; no third-party dependencies, accounts, model APIs, or cloud services.

```sh
git clone https://github.com/nealyx/reviewops.git
cd reviewops
python3 app.py --port 8787
```

Open **http://127.0.0.1:8787/**. Stop with Ctrl+C. On macOS,
`launch.command` also starts the app.

Five bundled cases cover valid evidence, a missing file, changed bytes, and an
empty package. They are copied once into `runtime/`; restarting preserves work
and never restores original files over your edits.

For a separate workspace without demonstration records:

```sh
python3 app.py --empty --data-dir ./workspaces/my-workspace --port 8788
```

`--empty` affects only an uninitialized directory. It never clears existing records.
Keep workspaces separate: the default is `runtime/`, while additional examples
use sibling directories under `workspaces/`. Do not put one workspace inside another.

## A five-minute walkthrough

1. Open **SYN-001**, read its two evidence files, assign a reviewer, and approve
   with a note explaining what you checked.
2. Open **SYN-002**. Its missing file blocks approval. Record a change request.
3. Use **Add experiment** to submit metadata and small UTF-8 text files. Mark it
   synthetic if it is a demonstration.
4. Approve it, then **Submit revision** with changed evidence. Every revision is
   a complete replacement package. Its status returns to **Needs review**.
5. Expand **Changes since previous submission** to see added, removed, changed,
   and unchanged files. Open both the previous and current versions in the
   comparison, then export the audit to inspect the recorded history.

An empty package may be submitted as an incomplete handoff; it stays blocked.
Hash verification checks content identity, **not scientific correctness**.
A reviewer still needs to assess the methods, results, and limitations.

You can also import the checked-in synthetic example:

```sh
python3 app.py --empty --data-dir ./workspaces/import-demo \
  --import-manifest examples/manifest.json
python3 app.py --data-dir ./workspaces/import-demo --port 8788
```

The importer copies only files listed by the selected manifest. Later edits to
the source files do not change the submitted snapshot.
See the [format and revision example](docs/manifests.md).

## What happens when evidence changes?

| Action | Current status | Historical record |
|---|---|---|
| Submit valid evidence | Needs review | New snapshot and submission event |
| Approve with a note | Approved | Decision references that snapshot |
| Request changes | Changes requested | Request references that snapshot |
| Submit revised evidence | Needs review | Earlier snapshots and decisions retained |
| Modify or remove a stored file outside the app | Blocked | Previous decision retained; mismatch visible |
| Restore the exact approved bytes | Previous approval becomes effective again | No continuous file-change history is claimed |

Each assignment, decision, or submission increments the **record version**.
Only submissions create a new **evidence snapshot**. Stale versions are rejected,
so two open tabs cannot silently overwrite one another's actions.
Each detail, queue, and audit export also reads a consistent database snapshot
when a separate CLI import commits a revision during the request.

The revision comparison uses the current and immediately preceding evidence
submissions, even when assignments and decisions occurred between them. It
compares filenames and expected hashes, and shows changes to submitted metadata.
Renames appear as a removal and an addition. It is a file-level comparison, not
a line-by-line text diff or a scientific assessment. Previewing either version
checks the actual stored bytes, so unchanged submitted hashes cannot hide a
later file-integrity failure.

Unfinished decision notes stay with each experiment while you switch records
in the same tab. Refreshing evidence preserves the text; a warning appears if
the draft began on an earlier snapshot. A successful decision clears the text
that was submitted, while preserving any new typing. Drafts are held only in
the tab's memory and disappear on page reload or close.

## Back up or move a workspace

Stop the server and all import commands, then prepare and export the workspace.
Keep them stopped until export finishes. These commands use the separate workspace
created above; use `--data-dir runtime` to back up the default demo instead:

```sh
python3 workspace_archive.py prepare --data-dir workspaces/my-workspace
python3 workspace_archive.py export \
  --data-dir workspaces/my-workspace --output ../workspace-backup.zip
python3 workspace_archive.py inspect ../workspace-backup.zip
python3 workspace_archive.py restore ../workspace-backup.zip \
  --destination workspaces/restored-workspace
python3 app.py --data-dir workspaces/restored-workspace --port 8788
```

The ZIP includes the database, review history, and evidence files. Inspection
checks its inventory, sizes, hashes, and database consistency. Restore requires
a new destination and refuses to overwrite existing work. Missing or modified
evidence remains missing or modified; restoring does not grant a new approval.

`prepare` checkpoints SQLite's pending storage and changes journal mode without
editing review records. Never delete SQLite sidecars manually. The
[workspace guide](docs/workspaces.md) explains the offline requirement, archive
limits, and privacy considerations. Audit JSON exports contain no file bodies
and cannot replace this backup.

## Data and operating limits

This is a **local, single-user tool**. Names are self-reported labels, not
authenticated identities. There is no team server, messaging service, experiment
runner, or GitHub integration. Nothing is uploaded to a cloud service.

- Submissions: 20 UTF-8 text files, 256 KiB per file, 1 MiB total evidence, and
  2 MiB per JSON request. Binary artifacts and large datasets are out of scope.
- Previews show at most the first 32 KiB, as plain text, never executable HTML.
  Text and its integrity label come from the same file read; a changed tail
  beyond the displayed text still fails the hash check. Files over 8 MiB are
  refused, including stored files enlarged outside the app.
- The server binds to `127.0.0.1`, checks Host/Origin, and requires a local
  request token for writes. Other processes running as your OS user remain
  outside that boundary.
- Idle connections expire after five seconds without socket activity. An
  unfinished request body is rejected without changing the workspace. This is
  an inactivity timeout, not a deadline for continuously arriving data.
- SQLite triggers reject ordinary updates/deletes to audit events and snapshot
  records. The database owner can bypass those controls; this is not an
  independently witnessed or tamper-proof ledger.
- Files are checked on reads and before approval. There is no continuous watcher;
  another local process can change a file between verification and a response.
- Audit exports include metadata, manifests, verification, and history.
  Restorable workspace backups use the separate offline command above.

`runtime/` and `workspaces/` are ignored by Git. For a custom data directory, use a location outside
the checkout or add your own ignore rule. Review exports before sharing
user-submitted records.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

For frontend development, Node.js 22 or later can also run the small behavior
regressions without installing packages:

```sh
node --test tests/test_frontend.js
```

Node is not needed to run the app.

The frontend regressions exercise evidence previews, retained decision drafts,
keyboard focus after queue selection and file removal, and recoverable
submission errors using a small DOM model. They do not
replace checking keyboard navigation and layout in a browser.

Tests use temporary directories and synthetic cases. They cover integrity gates,
retained revisions, approval reset, stale updates, failed-write rollback,
restart persistence, manifest confinement, HTTP boundaries, previews, audit
export, and backup/restore with damaged evidence and destination conflicts.
The workflow runs on Linux and macOS; its badge reports the latest hosted result.
Software tests do not establish multiuser scale or scientific validity.

## API

`GET /api/state` returns a local `csrf_token`. Writes send it in
`X-ReviewOps-Token` with `Content-Type: application/json`.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/state` | Queue, events, data indicator, token |
| GET | `/api/runs/<id>` | Details, current evidence, snapshots, history |
| POST | `/api/submissions` | Create or revise a complete evidence package |
| POST | `/api/runs/<id>/assign` | `{owner, actor, expected_version}` |
| POST | `/api/runs/<id>/decision` | `{decision, actor, note, expected_version}` |
| GET | `/api/runs/<id>/evidence/<index>` | Current evidence preview |
| GET | `/api/runs/<id>/snapshots/<snapshot>/evidence/<index>` | Retained evidence preview |
| GET | `/api/export` | Version 2 audit export without the request token |

Decisions are `approved` or `changes_requested`. Effective status may also be
`needs_review` or `blocked`; `recorded_status` preserves the stored review status.
HTTP 400 means invalid or incomplete input, 403 a request-boundary failure,
404 a missing entry, 408 a request body that stopped arriving, 409 a stale
version, 413 an oversized request, and 422 a failed approval gate.

The next milestone is an observed researcher handoff using permitted examples.
The [pilot guide](PILOT.md) records task success, time, confusion, and changes
supported by feedback. Measured usefulness remains an open question.
