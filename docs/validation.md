# Validation record — October 20, 2026

This record covers the v0.2 submission and retained-evidence workflow.
The bundled examples and browser walkthrough records are synthetic.
These are software checks, not pilot results.

## Automated checks

The current local Python 3.9.6 test suite passes **59 tests**, rerun on
October 20, 2026 after removing a separate audit example and its fixture test.
It exercises:

- Approval refusal for missing, modified, or empty evidence.
- New submissions, exact UTF-8 hashing, and canonical manifest hashes.
- Approval reset after a revision and retained earlier evidence/decisions.
- Stale-version rejection without partial changes.
- Rollback after partial file writes or failed audit insertion.
- Migration from older schemas, retaining unknown historical values honestly.
- Persistence after restart and append-only event/snapshot controls.
- Manifest path confinement, symbolic links, sizes, UTF-8, and JSON boundaries.
- Host/Origin/token checks, bounded previews, and export labeling.
- Consistent database reads while a second CLI connection commits a revision.
- Preview text and integrity labels derived from the same bounded file read.

Fifteen dependency-free Node regressions also pass in that rerun. The first four demonstrated
the earlier preview and draft defects: previews stay bound to the displayed
evidence snapshot, preview failure never falls back to newer bytes, and assigning
or clearing a reviewer preserves an unfinished decision note. The additional
checks cover keyboard position during queue selection, delayed responses and
file removal, plus visible submission errors with draft/file retention. These
tests use a small DOM model; layout and native focus behavior require browser
checks too.

Three additional database regressions failed before the read-consistency fix:
a detail could pair old metadata with a new filename, a queue could combine
different record/history versions, and an export could label a newly submitted
user revision as synthetic. Each now holds one database read snapshot.
The tests commit a revision through a separate connection at exact read
boundaries, without timing-dependent sleeps. A fourth test checks exception
cleanup and preserves an existing caller's transaction and rollback.

Four preview regressions cover both current and retained-snapshot routes.
Atomic replacement before or after opening a file cannot combine one file's
verified label with another file's displayed bytes. A changed tail beyond the
32 KiB preview still fails the full-file hash check, and an oversized replacement
is refused. This protects the relationship between the returned text and its
integrity label; it does not prevent later edits by the filesystem owner.

The GitHub workflow checks Linux/macOS on Python 3.9/3.13. Consult
[the actual runs](https://github.com/nealyx/reviewops/actions/workflows/checks.yml)
for hosted results rather than assuming a configured job has passed.

The first hosted pass exposed a Python-version difference: Python 3.13 accepted
nesting deep enough to trigger the older interpreter's recursion error.
The parser now enforces an explicit 32-container limit; the regression checks
both sides of that boundary rather than relying on interpreter behavior.

## Browser walkthrough

Observed in the local app:

1. Submitted two synthetic files using **Add experiment**.
2. Approved the package with a note; the event referenced its snapshot.
3. Opened a revision draft, then changed the reviewer in a second browser tab.
4. The stale submission failed without losing the draft or selected files.
5. Refreshed the latest record, saw the changed reviewer, and submitted the
   revision using the current version.
6. The new package returned to **Needs review**. The previous approval remained
   attached to its earlier snapshot.
7. Read the earlier methods file and confirmed the revision text was absent.
8. Attached an invalid UTF-8 file; the UI displayed a clear error and disabled
   submission.
9. Downloaded the audit export. No browser console errors were observed.

The observed 475-pixel and 1,280-pixel viewports had no horizontal page overflow.
The narrower view stacked the queue and detail; the desktop view displayed them
side by side. These widths were read from the actual rendered pages.

[Captured synthetic workflow](assets/reviewops-workflow.jpg).

An additional browser regression used an isolated workspace. Saving and then
clearing an assignee preserved the draft note. A separate CLI process submitted
revision B while the page still displayed A: opening the old preview returned
A's retained bytes. Trying to approve from that stale page was rejected.
Refreshing showed B's filename, hash, and bytes, kept the note, and left the
record awaiting review. No browser warnings or errors were observed.

The final keyboard pass reproduced three defects in the previous frontend:
queue selection and file removal dropped focus to the page body, while a server
validation error appeared above the visible part of the scrolled intake dialog.
After the corrections, pressing Enter on a queue item focused its detail heading;
Tab then reached **Submit revision**. Removing a file focused the next file's
Remove button, and removing the last file focused the chooser. Submitting a
whitespace-only title from the bottom of the dialog focused the fully visible
error message, keeping the draft and both selected files. The invalid record was
not saved. Delayed-response and decoding focus preservation are additionally
covered by the deterministic Node checks.

## Limits

No researcher pilot, multiuser load test, network deployment, authenticated
identity check, or scientific validation was performed. File/database owners
remain able to bypass application controls. Test counts describe this revision
and can change as meaningful regression coverage grows.
