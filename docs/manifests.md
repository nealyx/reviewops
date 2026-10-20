# Submitting an evidence package

The browser and CLI use the same validation and snapshot logic. An experiment
has a stable ID. Every submission retains a complete evidence package.

## Manifest example

See the runnable [`examples/manifest.json`](../examples/manifest.json):

```json
{
  "id": "DEMO-REPLICATION",
  "title": "Synthetic replication handoff",
  "summary": "Compare two scripted measurements and review their limits.",
  "owner": "Demo reviewer",
  "actor": "Demo submitter",
  "note": "Ready for a methods review.",
  "is_demo": true,
  "evidence": [
    {"name": "methods.txt", "path": "methods.txt"},
    {"name": "results.csv", "path": "results.csv"}
  ]
}
```

The CLI resolves each `path` relative to the manifest folder. A file may instead
have a `content` string, but not both. The HTTP route accepts inline `content`;
it never resolves browser-supplied paths on your computer.

```sh
python3 app.py --empty --data-dir ./workspaces/example \
  --import-manifest examples/manifest.json --actor "Demo operator"
```

`--actor` overrides the manifest's self-reported actor label. The command prints
the saved detail and exits. A duplicate ID without a valid `expected_version`
fails rather than overwriting the experiment.

## A revision

Read the experiment's current **record version** from its detail view or API.
Add that number as `expected_version`, retain the same ID, update the note,
and provide the entire new evidence list:

```json
{
  "id": "DEMO-REPLICATION",
  "title": "Synthetic replication handoff",
  "summary": "Updated explanation for the two scripted values.",
  "owner": "Demo reviewer",
  "actor": "Demo submitter",
  "note": "Added the requested baseline explanation.",
  "is_demo": true,
  "expected_version": 2,
  "evidence": [
    {"name": "methods.txt", "content": "SYNTHETIC revised methods.\n"}
  ]
}
```

`2` is illustrative; use the actual version. Assignments and decisions also
increment it. New submissions always reset review status. Empty evidence is
allowed for an incomplete handoff, but that package stays blocked.

## Validation

- IDs: 1–64 ASCII letters, digits, hyphens, or underscores; first character
  must be a letter or digit.
- Titles: 160 characters; summaries/notes: 4,000 each; actor/owner: 200 each.
  Owner may be empty.
- `is_demo` must explicitly be a JSON boolean.
- JSON objects cannot repeat keys, numbers must be finite, and container nesting
  is limited to 32 levels independently of Python's decoder recursion threshold.
- At most 20 files, valid UTF-8, 256 KiB each, 1 MiB total.
- Display names: simple filenames, at most 120 characters, unique ignoring
  letter case. Directories, traversal, and unsafe filename forms are refused.
  Reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and
  `LPT1`–`LPT9`) are refused even with extensions or different letter case,
  because they cannot be stored in portable workspace backups. Rename the
  source file or choose another manifest display name, such as `auxiliary.txt`.
- CLI paths stay within the manifest folder and cannot use symbolic links.
  A manifest is limited to 2 MiB.

UTF-8 bytes are preserved when hashing; line endings matter. Strict browser
decoding rejects invalid binary attachments rather than silently changing them.

## Stored form

Evidence lives under `evidence/snapshots/<random snapshot ID>/`. A canonical
manifest hashes experiment metadata, initial owner, and each file's name, size,
and content hash. Later assignments and decisions are recorded separately.

The audit export includes manifests and decision references but no file bodies.
Retain the complete runtime directory for a backup.
