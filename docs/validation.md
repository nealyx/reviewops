# Validation record — October 20, 2026

This record covers the v0.2 submission and retained-evidence workflow. All test
records and files are synthetic. These are software checks, not pilot results.

## Automated checks

The local Python 3.9.6 test suite passes **50 tests**. It exercises:

- Approval refusal for missing, modified, or empty evidence.
- New submissions, exact UTF-8 hashing, and canonical manifest hashes.
- Approval reset after a revision and retained earlier evidence/decisions.
- Stale-version rejection without partial changes.
- Rollback after partial file writes or failed audit insertion.
- Migration from older schemas, retaining unknown historical values honestly.
- Persistence after restart and append-only event/snapshot controls.
- Manifest path confinement, symbolic links, sizes, UTF-8, and JSON boundaries.
- Host/Origin/token checks, bounded previews, and export labeling.

The GitHub workflow checks Linux/macOS on Python 3.9/3.13. Consult
[the actual runs](https://github.com/nealyx/reviewops/actions/workflows/checks.yml)
for hosted results rather than assuming a configured job has passed.

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

The observed 475-pixel viewport had no horizontal page overflow. A requested
desktop viewport override did not change the observed width, so this pass does
not claim verification at that requested size.

[Captured synthetic workflow](assets/reviewops-workflow.png).

## Limits

No researcher pilot, multiuser load test, network deployment, authenticated
identity check, or scientific validation was performed. File/database owners
remain able to bypass application controls. Test counts describe this revision
and can change as meaningful regression coverage grows.
