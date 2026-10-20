# Review a real audit of published results

This walkthrough connects [EGRA/FACED](https://github.com/nealyx/egra-faced)
saved-output verification to a ReviewOps evidence package. The included report
comes from an actual execution against unchanged public CSV files at
[commit `229aee43`](https://github.com/nealyx/egra-faced/commit/229aee43e0299cbcabf5813b7d312f3c1ff74955).
It is marked `is_demo: false` because the verification was performed on published
artifacts. It is not a new EEG experiment or evidence that a team has adopted
ReviewOps.

The workflow is: **published files → automated checks → retained review package
→ a human decision still to be made**. A verifier pass does not approve the
research. No human decision or named reviewer is fabricated.

## What was checked

The standard-library EGRA verifier reported:

| Check | Observed result |
|---|---|
| Public CSV file hashes | 46 matched the saved artifact manifest |
| Regression groups | 46 checked against saved predictions |
| Summary values | 774 compared using the verifier's `1e-10` relative/absolute tolerance |
| Bootstrap interval bounds | 0 recomputed; `--bootstrap` was not requested |
| EEG preprocessing or model execution | Not performed |
| Human review | Not performed |

The [example directory](../examples/published-audit/) contains four evidence
files, totaling less than 16 KiB with its import manifest:

- [Review notes](../examples/published-audit/review-notes.txt): scope and questions
  for a reviewer.
- [Verification report](../examples/published-audit/verification.json): the
  verifier's complete JSON output, including its limitations.
- [Provenance](../examples/published-audit/provenance.json): source commit,
  execution time/environment, verifier hash, report hash, and a hash of the
  ordered public CSV path/hash list.
- [Saved artifact manifest](../examples/published-audit/saved-artifacts.json):
  an exact copy of EGRA's public manifest identifying all 46 CSV files. It does
  not embed their prediction rows.

The manifest's original snapshot-source commit and the later verifier commit
are both recorded. These identify saved bytes and the checking program; they
do not establish when or how the historical models were run.

## Import the included audit record

From a ReviewOps checkout with submission support:

```sh
python3 app.py --empty --data-dir runtime/published-audit \
  --import-manifest examples/published-audit/manifest.json
python3 app.py --data-dir runtime/published-audit --port 8790
```

Open `http://127.0.0.1:8790/` and select **EGRA-SAVED-229AEE4**. Its initial state
should be:

- **Needs review**, version 1, with no assigned owner.
- Four evidence files with matching import hashes.
- One **submitted** event labeled **Automated verification import**.
- No approval or changes-requested event.

Read the notes, report, provenance, and source manifest using the evidence
preview. The actor is a self-reported automation label; ReviewOps does not
authenticate it. Importing this checked-in report does not rerun the verifier.
The verified indicator checks stored bytes against their import snapshot,
not the correctness of the evidence's scientific claims.

The import refuses a duplicate ID. To repeat the walkthrough, choose a new
runtime directory. To continue an existing record, use the normal revision
workflow and its current record version; do not delete its history.

## Recompute from the public source, then import your own report

Use sibling checkouts named `egra-faced` and `reviewops`. For fresh clones:

```sh
git clone https://github.com/nealyx/egra-faced.git
git -C egra-faced checkout --detach 229aee43e0299cbcabf5813b7d312f3c1ff74955
git clone https://github.com/nealyx/reviewops.git
cd reviewops
```

For existing clones, keep your working files and use a separate clean EGRA clone
at the pinned commit. The following recipe only reads that checkout and writes
a new, ignored package under ReviewOps `runtime/`. It uses the existing EGRA
verifier and ReviewOps import command; no model, EEG download, or third-party
Python package is required.

Run this from the ReviewOps repository root:

```sh
python3 - <<'PY'
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

source = Path("../egra-faced").resolve()
template = Path("examples/published-audit")
package = Path("runtime/published-audit-replay")
provenance = json.loads((template / "provenance.json").read_text())
commit = subprocess.check_output(
    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
assert commit == provenance["source_commit"], "Use the pinned EGRA commit."
assert not subprocess.check_output(
    ["git", "-C", str(source), "status", "--porcelain"], text=True).strip(), \
    "Use a clean EGRA checkout."

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

verifier = source / provenance["verifier_path"]
saved_manifest = source / provenance["saved_manifest_source_path"]
assert sha256(verifier) == provenance["verifier_sha256"]
assert sha256(saved_manifest) == provenance["saved_manifest_sha256"]
package.mkdir(parents=True, exist_ok=False)
report_path = package / "verification.json"
subprocess.run([sys.executable, str(verifier), "--report", str(report_path)], check=True)
report = json.loads(report_path.read_text())
assert report["status"] == "passed"
shutil.copyfile(saved_manifest, package / "saved-artifacts.json")
shutil.copyfile(template / "review-notes.txt", package / "review-notes.txt")
provenance.update(
    verified_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    python_version=report["python_version"],
    operating_system=platform.system(),
    machine=platform.machine(),
    verification_report_sha256=sha256(report_path),
    command="python3 <egra-checkout>/scripts/verify_saved_results.py "
            "--report <package-directory>/verification.json",
    command_note="Interpreter and paths normalized; executed by this replay recipe.",
)
(package / "provenance.json").write_text(
    json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
manifest = json.loads((template / "manifest.json").read_text())
manifest["id"] = "EGRA-SAVED-REPLAY"
manifest["note"] = "Fresh public saved-output verification; awaiting human review."
(package / "manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print("New audit package:", package)
PY

python3 app.py --empty --data-dir runtime/published-audit-replay-data \
  --import-manifest runtime/published-audit-replay/manifest.json
python3 app.py --data-dir runtime/published-audit-replay-data --port 8790
```

The recipe stops if the package directory already exists. Use a new directory
name for another run. It updates the execution time, environment, and report
hash instead of attaching old provenance to a new report. The CSV file-set
identity stays the same because the pinned verifier checks the unchanged public
manifest. A different Python version may change the report's environment field
and therefore its byte hash.

The new **EGRA-SAVED-REPLAY** record should again have verified evidence and
**Needs review** status. Assign a real reviewer when one is available. An actual
reviewer can then request changes or approve the stated review scope. This
walkthrough intentionally records neither decision.

## What remains outside this audit

EGRA's report identifies gaps: raw-DE nuisance summaries lack committed
row-level predictions; fingerprint predictions omit a feature-type field; and
some segment metrics can only be aggregated from saved flags because complete
rankings are absent. No raw EEG, model weights, private research files, or
participant recordings were accessed for this package.

ReviewOps retains the submitted evidence, its hashes, and later decisions. It
does not independently establish model accuracy, valid study design, or
generalization. Rerunning preprocessing/inference and assessing those scientific
questions are separate work with separate evidence.

[Manifest format](manifests.md) · [Design and limits](design.md)
