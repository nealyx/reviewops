"""Render a captured experiment record as an inert, portable HTML summary."""

from html import escape


SUMMARY_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
               "img-src 'none'; connect-src 'none'; object-src 'none'; "
               "base-uri 'none'; form-action 'none'")

LABELS = {
    "needs_review": "Needs review", "approved": "Approved",
    "changes_requested": "Changes requested", "blocked": "Blocked",
    "verified": "Verified", "changed": "Changed", "missing": "Unavailable",
    "empty": "No evidence",
}


def _text(value, absent="Unknown (not recorded)"):
    return escape(absent if value is None else str(value), quote=True)


def _field(label, value):
    return "<dt>{}</dt><dd>{}</dd>".format(_text(label), _text(value))


def _status(value):
    return LABELS.get(value, value)


def render_review_summary(detail, generated_at):
    """Use only captured metadata and hash observations; never open evidence files."""
    run = detail["run"]
    snapshots = detail["snapshots"]
    events = detail["events"]
    all_synthetic = (run["is_demo"] is True and bool(snapshots)
                     and all(item["manifest"].get("is_demo") is True for item in snapshots))
    scope = ("Synthetic experiment records, including retained snapshots."
             if all_synthetic else
             "Contains user-submitted or unclassified experiment history.")
    parts = ["""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="%s">
<title>ReviewOps review summary</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#192b33;background:#f4f7f7;line-height:1.55}
*{box-sizing:border-box}body{margin:0}main{max-width:900px;margin:auto;padding:40px 24px 64px}
h1,h2,h3{line-height:1.2;overflow-wrap:anywhere}h1{font-size:2.2rem;margin:0 0 8px}h2{margin:32px 0 16px}h3{margin:0 0 12px;font-size:1.1rem}
p{margin:12px 0}.eyebrow{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:#4c666e}.muted{color:#4c666e;font-size:.92rem}
.notice,article{background:#fff;border:1px solid #c9d5d9;border-radius:8px;padding:18px;margin:14px 0}.notice{border-left:4px solid #486e79}
dl{display:grid;grid-template-columns:minmax(130px,1fr) minmax(0,3fr);gap:8px 16px;margin:0}dt{font-weight:600}dd{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}
.text{white-space:pre-wrap;overflow-wrap:anywhere}.files{list-style:none;padding:0}.files li{margin:12px 0;padding:12px 0;border-top:1px solid #dce4e6}.filename{font-weight:600;overflow-wrap:anywhere}section{margin-top:30px}footer{border-top:1px solid #c9d5d9;margin-top:32px;padding-top:16px}
@media(max-width:560px){main{padding:24px 16px 40px}dl{grid-template-columns:1fr;gap:3px}dd{margin-bottom:10px}article,.notice{padding:14px}}
@media print{:root{background:#fff;color:#000}main{max-width:none;padding:0}article{border-radius:0}h2,h3{break-after:avoid}dl,.files li{break-inside:avoid}.notice{border-color:#777}}
</style></head><body><main>
<header><p class="eyebrow">ReviewOps · portable record</p><h1>Review summary</h1>
<p class="text">%s</p><p class="muted">Generated %s</p></header>
<aside class="notice"><p>%s</p><p>Review titles, names, and notes before sharing. User-entered text can be sensitive even when experiment records are synthetic.</p></aside>
""" % (_text(SUMMARY_CSP), _text(run["title"]), _text(generated_at), _text(scope))]
    parts.append('<section aria-labelledby="current"><h2 id="current">Current record</h2><dl>')
    for label, value in (
            ("Experiment ID", run["id"]), ("Record version", run["version"]),
            ("Effective status", _status(run["status"])),
            ("Recorded review status", _status(run["recorded_status"])),
            ("Current evidence check", _status(run["evidence_status"])),
            ("Current assigned reviewer", run["owner"] or "Unassigned"),
            ("Current record label", "Synthetic example" if run["is_demo"] else "User submission"),
            ("Current evidence snapshot", run.get("snapshot_id")),
            ("Record last updated", run["updated_at"])):
        parts.append(_field(label, value))
    parts.append('</dl><h3>Experiment description</h3><p class="text">{}</p>'.format(_text(run["summary"])))
    parts.append('<p class="muted">The assigned reviewer is a current assignment, not necessarily the actor who recorded a decision. A blocked effective status does not erase the recorded review status.</p></section>')
    parts.append('<section aria-labelledby="evidence"><h2 id="evidence">Current evidence observations</h2>')
    parts.append('<p>These hashes describe files read while this summary was generated. Unavailable includes missing, unreadable, oversized, or unsafe files. The check does not freeze file contents or establish scientific correctness.</p>')
    if not detail["evidence"]:
        parts.append('<p>No current evidence files are recorded.</p>')
    for item in detail["evidence"]:
        parts.append('<article><h3>{}</h3><dl>'.format(_text(item["name"])))
        parts.append(_field("Observed status", _status(item["status"])))
        parts.append(_field("Expected SHA-256", item["expected_sha256"]))
        parts.append(_field("Observed SHA-256", item.get("actual_sha256") or "Not available"))
        parts.append('</dl></article>')
    parts.append('</section><section aria-labelledby="snapshots"><h2 id="snapshots">Retained snapshot manifests</h2>')
    parts.append('<p>These are recorded manifests and expected hashes. Earlier file bytes were not rechecked for this summary. A retained manifest does not establish that its files remain present or unchanged.</p>')
    if not snapshots:
        parts.append('<p>No snapshot manifests are retained in this record.</p>')
    for snapshot in snapshots:
        manifest = snapshot["manifest"]
        current = snapshot["id"] == run.get("snapshot_id")
        parts.append('<article><h3>{}</h3><dl>'.format("Current snapshot" if current else "Earlier snapshot"))
        for label, value in (
                ("Snapshot ID", snapshot["id"]),
                ("Captured at record version" if manifest.get("legacy_capture") else "Submitted at record version", snapshot["run_version"]),
                ("Recorded at", snapshot["created_at"]),
                ("Recorded manifest SHA-256", snapshot["manifest_sha256"]),
                ("Title at capture" if manifest.get("legacy_capture") else "Title at submission", manifest.get("title")),
                ("Assigned reviewer at capture" if manifest.get("legacy_capture") else "Assigned reviewer at submission", "Unassigned" if manifest.get("owner") == "" else manifest.get("owner")),
                ("Snapshot label", "Synthetic example" if manifest.get("is_demo") is True else
                 "User submission" if manifest.get("is_demo") is False else "Unknown (not recorded)")):
            parts.append(_field(label, value))
        parts.append('</dl>')
        parts.append('<p class="text">{}</p>'.format(_text(manifest.get("summary"))))
        if manifest.get("legacy_capture"):
            parts.append('<p class="notice">Legacy capture: these files were captured at upgrade. This does not establish their earlier contents or retroactively connect old decisions to this snapshot.</p>')
        parts.append('<ul class="files">')
        for item in manifest.get("evidence", []):
            parts.append('<li><p class="filename">{}</p><dl>{}{}</dl></li>'.format(
                _text(item["name"]), _field("Expected SHA-256", item["sha256"]),
                _field("Recorded bytes", item.get("size_bytes"))))
        parts.append('</ul>' if manifest.get("evidence") else '</ul><p>No files listed in this manifest.</p>')
        parts.append('</article>')
    parts.append('</section><section aria-labelledby="history"><h2 id="history">Recorded actions</h2>')
    parts.append('<p>Newest recorded event first. Actor names are unauthenticated labels entered locally. Record versions include assignments and decisions; they are not submission numbers.</p>')
    if not events:
        parts.append('<p>No actions are retained. No reviewer or decision attribution can be inferred from this absence.</p>')
    for event in events:
        parts.append('<article><h3>Recorded action: {}</h3><dl>'.format(_text(_status(event["action"]))))
        for label, value in (("Event ID", event["id"]), ("Actor label", event["actor"]),
                             ("Recorded at", event["created_at"]),
                             ("Record version", event.get("run_version")),
                             ("Evidence snapshot reference", event.get("snapshot_id"))):
            parts.append(_field(label, value))
        parts.append('</dl><p class="text">{}</p></article>'.format(_text(event["note"])))
    parts.append('</section><footer><h2>Scope and limits</h2><p>This document contains one experiment\'s retained metadata and actions from one consistent database read. Files are observed separately; other local processes can change them during or after the read. Restoring exact approved bytes can make an earlier recorded approval effective again. No continuous file-change history is claimed.</p><p>Unknown references remain unknown. Retained events do not prove complete prior history, authenticated authorship, scientific validity, or tamper-proof storage. The database owner can alter local records.</p><p>This summary does not attach evidence file bodies or add internal storage paths or request credentials. It is not a workspace backup; use the separate backup workflow to preserve the database and evidence files.</p></footer></main></body></html>')
    return "\n".join(parts)
