# Contributing to ReviewOps

ReviewOps is a local prototype for inspecting evidence and recording review
decisions. Useful contributions make that workflow clearer or protect its
records. Researcher usefulness remains unmeasured; the [pilot protocol](PILOT.md)
describes the next validation step.

## Find the relevant code

| Location | Responsibility |
|---|---|
| [`app.py`](app.py) | Loopback HTTP server, SQLite records, snapshots, hash checks, decisions, exports, and startup |
| [`submissions.py`](submissions.py) | Submission validation and explicitly selected local manifest/file imports |
| [`static/`](static/) | Browser interface; plain HTML, CSS, and JavaScript |
| [`demo/`](demo/) | Synthetic startup fixtures |
| [`examples/`](examples/) | Explicitly imported packages, including the public saved-output audit |
| [`tests/`](tests/) | Store/HTTP behavior, migration, input boundaries, public fixture provenance, and frontend regressions |

Read the [design and boundaries](docs/design.md), [manifest format](docs/manifests.md),
or [public audit walkthrough](docs/public-audit-walkthrough.md) when changing
those paths.

## Work in an isolated local workspace

Use Python 3.9 or later. From the repository root, choose a new data directory:

```sh
python3 app.py --empty --data-dir runtime/contributor \
  --import-manifest examples/manifest.json
python3 app.py --data-dir runtime/contributor --port 8791
```

Open `http://127.0.0.1:8791/`; stop the server with Ctrl+C. This imports the
synthetic example into a separate workspace. `--empty` only skips automatic
seeding for a new directory; it does not reset an existing database. Use a
different directory name if `runtime/contributor` already contains work.

`runtime/` is ignored by Git. Keep development experiments separate from any
workspace used for actual reviews. Existing databases and retained evidence are
not disposable test fixtures.

## Run the checks

The Python suite uses only the standard library and temporary workspaces:

```sh
python3 -m unittest discover -s tests -v
```

Frontend development checks use Node.js 22 or later. Node is needed only for
these checks, not to run ReviewOps; no npm dependencies or `npm install` step
are required:

```sh
node --check static/app.js
node --test tests/test_frontend.js
```

The Node tests execute the actual frontend script with a small DOM/API harness.
They complement browser checks; they do not verify layout or every browser
interaction. For UI changes, also exercise the affected flow in your isolated
workspace. The [CI workflow](.github/workflows/checks.yml) runs the automated
checks on Linux and macOS.

## Write a regression that protects a decision

For example, a revision must not inherit approval of earlier evidence:

1. Submit a complete package: record version 1, snapshot A, `needs_review`.
2. Approve it using `expected_version: 1`: version 2; the event references A.
3. Submit new evidence using `expected_version: 2`: version 3, new snapshot B,
   `needs_review`; A and its approval remain in history.
4. Attempt a stale decision using version 2: expect HTTP 409 and no additional
   event or changed evidence.

[`tests/test_submissions.py`](tests/test_submissions.py) contains patterns for
these transitions and file-write failures. When fixing a bug, first demonstrate
the failing behavior, then assert the meaningful outcome: preserved bytes,
correct snapshot references, unchanged history on failure, or recoverable user
input. Prefer these checks to tests that merely repeat implementation details.

## Keep the scope grounded

Preserve the distinction between matching file hashes, a reviewer's recorded
decision, and scientific validity. Keep synthetic labels and public provenance
accurate. Evidence stays text and is never executed.

The current operating model is one local operator, small evidence packages,
and self-reported actor names. Shared hosting, authentication, larger artifact
storage, or automated scientific judgments need requirements from actual
workflow observations. A reproducible bug fix or a small improvement supported
by pilot feedback is a useful contribution without expanding that scope.

## Report a reproducible issue

Open a [GitHub issue](https://github.com/nealyx/reviewops/issues) with the commit,
operating system, Python version, browser or Node version when relevant, exact
steps, expected behavior, and observed behavior. A small synthetic manifest or
public example makes the report easier to reproduce.

Share only the minimal relevant error text and permitted example files. Do not
attach a real workspace database, private evidence, participant records, or an
unreviewed audit export. Describe the structure or substitute synthetic content
when the original evidence cannot be shared.
