"""Validate explicitly submitted text evidence without executing its contents."""

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

MAX_FILES = 20
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 32
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,119}$")
RESERVED_FILENAME = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", re.IGNORECASE)


class SubmissionError(ValueError):
    """A submission failed a documented validation rule."""


def parse_json(content):
    """Reject ambiguous/nonstandard JSON and bound parser failure responses."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SubmissionError("JSON objects cannot contain duplicate keys.")
            result[key] = value
        return result

    def finite_constant(value):
        raise SubmissionError("JSON cannot contain NaN or Infinity.")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise SubmissionError("JSON numbers must be finite.")
        return number

    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        result = json.loads(content, object_pairs_hook=unique_object,
                            parse_constant=finite_constant, parse_float=finite_float)
        # Python versions have different decoder recursion thresholds. Enforce
        # our own structural bound instead of relying on RecursionError.
        pending = [(result, 0)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, (dict, list)):
                if depth >= MAX_JSON_DEPTH:
                    raise SubmissionError("JSON nesting exceeds 32 levels.")
                children = value.values() if isinstance(value, dict) else value
                pending.extend((child, depth + 1) for child in children)
        return result
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, SubmissionError):
            raise
        raise SubmissionError("Input is not valid UTF-8 JSON.")


def text(value, field, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise SubmissionError("{} must be text.".format(field))
    value = value.strip()
    if not value and not allow_empty:
        raise SubmissionError("{} is required.".format(field))
    if len(value) > maximum:
        raise SubmissionError("{} exceeds {} characters.".format(field, maximum))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SubmissionError("{} contains invalid Unicode.".format(field))
    return value


def prepare_submission(body):
    """Return validated metadata and exact UTF-8 bytes, never interpreted code."""
    if not isinstance(body, dict):
        raise SubmissionError("Submission must be an object.")
    run_id = text(body.get("id"), "id", 64)
    if not IDENTIFIER.fullmatch(run_id):
        raise SubmissionError("id may contain letters, numbers, hyphens and underscores.")
    is_demo = body.get("is_demo")
    if not isinstance(is_demo, bool):
        raise SubmissionError("is_demo must explicitly be true or false.")
    prepared = {
        "id": run_id,
        "title": text(body.get("title"), "title", 160),
        "summary": text(body.get("summary"), "summary", 4000),
        "owner": text(body.get("owner", ""), "owner", 200, allow_empty=True),
        "actor": text(body.get("actor"), "actor", 200),
        "note": text(body.get("note"), "note", 4000),
        "is_demo": is_demo,
        "files": [],
    }
    entries = body.get("evidence")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise SubmissionError("evidence must be a list of at most {} files.".format(MAX_FILES))
    names, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise SubmissionError("Each evidence entry must be an object.")
        name = text(entry.get("name"), "evidence name", 120)
        if not FILENAME.fullmatch(name) or name in (".", "..") or name.endswith("."):
            raise SubmissionError("Evidence names must be simple filenames without directories.")
        # Keep accepted evidence compatible with the portable backup format.
        if RESERVED_FILENAME.fullmatch(name):
            raise SubmissionError("Evidence filename {!r} is a reserved device name, even with "
                                  "an extension. Rename it before submitting so the workspace "
                                  "can be backed up portably.".format(name))
        if name.casefold() in names:
            raise SubmissionError("Evidence filenames must be unique, ignoring letter case.")
        names.add(name.casefold())
        content = entry.get("content")
        if not isinstance(content, str):
            raise SubmissionError("Evidence content must be UTF-8 text.")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise SubmissionError("Evidence contains invalid Unicode.")
        if len(data) > MAX_FILE_BYTES:
            raise SubmissionError("Each evidence file is limited to 256 KiB.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SubmissionError("A submission is limited to 1 MiB of evidence.")
        prepared["files"].append({
            "name": name, "data": data, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {key: prepared[key] for key in ("id", "title", "summary", "owner", "is_demo")}
    manifest["evidence"] = [
        {key: item[key] for key in ("name", "size_bytes", "sha256")}
        for item in prepared["files"]
    ]
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    prepared["manifest"] = manifest
    prepared["manifest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return prepared


def read_manifest(path, actor=None):
    """Read a user-selected manifest and explicitly listed files within its folder."""
    try:
        str(path).encode("utf-8")
    except UnicodeEncodeError:
        raise SubmissionError("The manifest path contains invalid Unicode.")
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SubmissionError("Choose a regular manifest file, not a symbolic link.")
    path = path.resolve()
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise SubmissionError("The manifest exceeds 2 MiB.")
    with path.open("rb") as stream:
        content = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise SubmissionError("The manifest exceeds 2 MiB.")
    body = parse_json(content)
    if not isinstance(body, dict):
        raise SubmissionError("Manifest must contain an object.")
    if actor is not None:
        body["actor"] = actor
    entries = body.get("evidence")
    if not isinstance(entries, list) or len(entries) > MAX_FILES:
        raise SubmissionError("evidence must list at most 20 files.")
    resolved_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SubmissionError("Each evidence entry must be an object.")
        if "content" in entry and "path" in entry:
            raise SubmissionError("Use either inline content or a path for each file.")
        if "content" in entry:
            resolved_entries.append(entry)
            continue
        name = entry.get("path")
        if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
            raise SubmissionError("Evidence paths must be relative to the manifest.")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise SubmissionError("Evidence paths contain invalid Unicode.")
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
            raise SubmissionError("Evidence paths cannot escape the manifest directory.")
        target = path.parent
        for part in relative.parts:
            target = target / part
            if target.is_symlink():
                raise SubmissionError("Evidence paths cannot contain symbolic links.")
        if not target.is_file():
            raise SubmissionError("Evidence file is missing: {}".format(name))
        if target.stat().st_size > MAX_FILE_BYTES:
            raise SubmissionError("Each evidence file is limited to 256 KiB.")
        with target.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise SubmissionError("Each evidence file is limited to 256 KiB.")
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            raise SubmissionError("Evidence must be UTF-8 text: {}".format(name))
        resolved_entries.append({"name": entry.get("name", relative.name), "content": content})
    body["evidence"] = resolved_entries
    # Validate the complete resolved package too, including cumulative byte and
    # metadata limits. Import callers cannot accidentally bypass those rules.
    prepare_submission(body)
    return body
