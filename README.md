# ReviewOps

A local prototype for coordinating research experiment reviews: inspect bundled
runs in a queue, assign a reviewer, inspect evidence, record a decision, and
retain the audit history. New-run creation and import are not included in v0.1.

**Every bundled run, researcher name, evidence file, and result is synthetic.**
This is an independent demonstration built with AI-assisted engineering. It
contains no Millennium internal research, unpublished results, credentials, or
copied internal code.

The intended next step is a small pilot with researchers who currently coordinate
experiment reviews manually. No user adoption, time savings, or research outcome
has been measured yet.

## Run locally

Requires Python 3.9 or later. There are no third-party packages, model APIs,
accounts, or cloud services.

```sh
python3 app.py --port 8787
```

Open **http://127.0.0.1:8787/**. Stop the server with Ctrl+C.

Data persists in `runtime/reviewops.sqlite3` and files are copied into
`runtime/evidence/` on the first launch. Restarting with the same data directory
preserves assignments, decisions, audit events, and changed evidence files.
Bundled files in `demo/` are not changed by normal use.

For a separate, fresh demonstration without deleting existing work:

```sh
python3 app.py --port 8788 --data-dir ./runtime/fresh-demo
```

## Five demonstration cases

| Run | Evidence | What to try |
| --- | --- | --- |
| SYN-001 | Two files with matching SHA-256 hashes | Assign an owner and approve with a review note. |
| SYN-002 | A missing file | Request the missing evidence; approval is refused. |
| SYN-003 | A deliberately changed file | Inspect expected and actual hashes; approval is refused. |
| SYN-004 | No files | Request an evidence package; an empty package cannot be approved. |
| SYN-005 | One file with a matching hash | Judge the synthetic methods description and approve or request changes. |

After approving SYN-001 or SYN-005, edit or remove its copied file in
`runtime/evidence/`, then refresh the page. Its effective status becomes
**Blocked** while the previous approval event remains in the audit trail.
The prior approval is historical evidence of a decision, not a claim that a
modified file is still approved. Restoring the exact original bytes makes the
previous approval effective again; this prototype checks content identity rather
than recording a continuous file-monitoring history.

## Review rules

- Approval requires at least one file and freshly verified matching hashes for
  every file in that run's manifest.
- Missing, changed, or empty evidence makes any run's effective status **Blocked**,
  including runs that have not been reviewed. The recorded review status remains
  available separately.
- Hash matching establishes file integrity only. It does not validate a result,
  detect a flawed method, execute an experiment, or certify scientific correctness.
- Decisions require an actor name and a nonempty note. Actor names are self-reported.
- Changes to assignments and decisions increment the run's version. A stale
  version returns HTTP 409, requiring the reviewer to refresh before retrying.
- Successful mutations and seeded records append audit events. Application
  code never rewrites them; SQLite triggers also reject ordinary event updates
  and deletes. Each event records the resulting run version, so exported history
  identifies which revision an action refers to.
- Evidence is read only under the designated evidence directory. Absolute paths,
  parent-directory traversal, symbolic links, and files over 8 MiB cannot satisfy
  the evidence gate.
- Export downloads current run details, current evidence verification, and the
  full recorded audit history as JSON. It excludes the local request token.
- Evidence previews show at most the first 32 KiB as plain text, with a truncation
  indicator. Invalid UTF-8 bytes are replaced. Preview requests select an entry
  from that run's manifest by index; they cannot request an arbitrary file.
  Changed files remain readable for review; missing or invalid files are refused.

## Scope and limitations

This is a **local, single-user prototype**, not a production multiuser service.
It binds only to `127.0.0.1`, validates Host and Origin headers, requires a
per-process token for writes, and caps request bodies at 16 KiB. These controls
reduce accidental browser-origin access; they do not authenticate researchers or
protect against other processes running as the same operating-system user.

SQLite and the evidence folder can still be changed by their owner. The audit is
append-only through this application, not a cryptographically tamper-proof or
independently witnessed ledger. Evidence is rechecked when requested and before
approval; there is no background file watcher. A separate process can change
files at any time, including between verification and a response.

The server processes requests sequentially. The version check demonstrates
conflict handling, but no load testing or multiuser deployment is claimed.
The prototype does not import private projects, run untrusted artifacts, execute
shell commands, upload files, provide user authentication, or connect to GitHub.
The data model can be generalized after a pilot establishes the actual workflow.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

The tests use temporary data directories. They cover approval gates, effective
approval invalidation after evidence changes, optimistic conflicts, restart
persistence, audit immutability through ordinary SQL mutations, path confinement,
request token and origin/host checks, body limits, bounded evidence previews,
and token-free export.

## API

All responses are JSON except static frontend assets. `GET /api/state` returns a
local `csrf_token`; write requests send it in the `X-ReviewOps-Token` header and use
`Content-Type: application/json`.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/state` | Runs, audit events, synthetic-data indicator, and request token |
| GET | `/api/runs/<id>` | Run details, current file hashes, and run-specific events |
| GET | `/api/runs/<id>/evidence/<index>` | Plain-text preview of a manifest entry, using its zero-based index |
| POST | `/api/runs/<id>/assign` | `{owner, actor, expected_version}` |
| POST | `/api/runs/<id>/decision` | `{decision, actor, note, expected_version}` |
| GET | `/api/export` | Current details and complete recorded audit history |

`decision` is `approved` or `changes_requested`. The effective run status is
`needs_review`, `approved`, `changes_requested`, or `blocked`.
`recorded_status` preserves the stored review status when a run is blocked.
Evidence status is `verified`, `missing`, `changed`, or `empty`; if multiple file
failures exist, the run summary prioritizes `changed` over `missing`.

HTTP 400 indicates invalid input, 403 a request-boundary failure, 404 an unknown
run or route, 409 a stale version, 413 an oversized body, and 422 a failed approval
evidence gate. Assignment and decision endpoints return the updated run detail.

## Pilot questions

Use only approved, non-sensitive examples when testing a real workflow.
Measure time from “ready for review” to a recorded decision, the proportion of
packages missing required evidence, and how often reviewers need clarification.
Compare against the existing coordination method on equivalent tasks. Keep the
results as observations, including failures, rather than treating this demo as
evidence of an improvement.
