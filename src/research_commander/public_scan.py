"""Fail-closed checks for a sanitized public root."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from research_commander.errors import PublicSafetyError

ROOT_MARKER = ".public-root.json"
MARKER_SCHEMA = "PublicRootMarkerV1"
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".pyright",
}
PROHIBITED_COMPONENTS = {
    ".local",
    "credentials",
    "secrets",
    "raw",
    "user-data-dir",
}
PROHIBITED_FILENAMES = {
    ".env",
    ".gitmodules",
    "auth.json",
    "cookies.json",
}
BINARY_SUFFIXES = {
    ".7z",
    ".bin",
    ".db",
    ".dll",
    ".exe",
    ".gif",
    ".jpg",
    ".jpeg",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".pdf",
    ".png",
    ".sqlite",
    ".zip",
}
CONTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key material",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    ("AWS access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Alpaca-style key", re.compile(r"\bPK[A-Z0-9]{18,}\b")),
    (
        "GitHub-style token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    ),
    (
        "Slack-style token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    ),
    (
        "JWT token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?im)^\s*(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
            r"(?!<|example|synthetic|replace|none|null)[\"']?[A-Za-z0-9_./+=-]{12,}"
        ),
    ),
    (
        "Windows user path",
        re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\r\n]+"),
    ),
    (
        "Unix user path",
        re.compile(r"(?i)(?:/home|/Users)/[^/\s]+/"),
    ),
    (
        "real account identifier",
        re.compile(
            r"(?im)^\s*[\"']?(?:account_id|broker_account|real_account)"
            r"[\"']?\s*[:=]\s*[\"']?(?!synthetic|example|none|null)[A-Za-z0-9-]{6,}"
        ),
    ),
    (
        "Git LFS configuration",
        re.compile(r"(?i)\bfilter\s*=\s*lfs\b"),
    ),
)
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.net",
        "example.org",
        "example.invalid",
        "users.noreply.github.com",
    }
)
SENSITIVE_PATH_RULES = (
    *(pattern for _, pattern in CONTENT_RULES),
    EMAIL_PATTERN,
)


@dataclass(frozen=True)
class PublicScanFinding:
    rule: str
    path: str


def _safe_display_path(path: str) -> str:
    if any(pattern.search(path) is not None for pattern in SENSITIVE_PATH_RULES):
        fingerprint = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        return f"<redacted-path:{fingerprint}>"
    return path


def _finding(rule: str, path: str) -> PublicScanFinding:
    return PublicScanFinding(rule, _safe_display_path(path))


def _marker_error(data: str, expected_repository: str | None) -> str | None:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return "invalid public-root marker"
    if not isinstance(payload, dict):
        return "invalid public-root marker"
    marker = cast(dict[str, object], payload)
    required_values: dict[str, object] = {
        "schema_version": MARKER_SCHEMA,
        "sanitized_working_tree": True,
        "private_git_history_included": False,
        "synthetic_examples_only": True,
        "credentials_permitted": False,
    }
    if any(marker.get(key) != value for key, value in required_values.items()):
        return "invalid public-root marker"
    repository_name = marker.get("repository_name")
    if not isinstance(repository_name, str) or not repository_name:
        return "invalid public-root marker"
    if expected_repository is not None:
        expected_name = expected_repository.rsplit("/", maxsplit=1)[-1]
        if repository_name != expected_name:
            return "public-root repository mismatch"
    return None


def _text_findings(
    text: str,
    path: str,
    *,
    prefix: str = "",
) -> list[PublicScanFinding]:
    findings = [
        _finding(f"{prefix}{rule}", path) for rule, pattern in CONTENT_RULES if pattern.search(text)
    ]
    for match in EMAIL_PATTERN.finditer(text):
        if match.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS:
            findings.append(_finding(f"{prefix}email address", path))
            break
    return findings


def _path_findings(path: str, *, prefix: str = "") -> list[PublicScanFinding]:
    return _text_findings(path, path, prefix=prefix)


def _deduplicate(findings: list[PublicScanFinding]) -> tuple[PublicScanFinding, ...]:
    return tuple(sorted(set(findings), key=lambda item: (item.rule, item.path)))


def _git_text(
    git: str,
    root: Path,
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        (git, "-C", str(root), *arguments),
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result


def _git_bytes(
    git: str,
    root: Path,
    arguments: tuple[str, ...],
    *,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        (git, "-C", str(root), *arguments),
        input=input_data,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _git_history_findings(
    root: Path,
    expected_repository: str | None,
) -> tuple[PublicScanFinding, ...] | None:
    git = shutil.which("git")
    if git is None:
        return None
    inside = _git_text(git, root, ("rev-parse", "--is-inside-work-tree"))
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    roots = _git_text(git, root, ("rev-list", "--max-parents=0", "--all"))
    if roots.returncode != 0:
        return (_finding("cannot enumerate Git roots", ".git"),)
    root_commits = [line for line in roots.stdout.splitlines() if line]
    findings: list[PublicScanFinding] = []
    if len(root_commits) != 1:
        findings.append(
            _finding(
                f"expected one independent clean root, found {len(root_commits)}",
                ".git",
            )
        )
    else:
        root_commit = root_commits[0]
        marker = _git_text(git, root, ("show", f"{root_commit}:{ROOT_MARKER}"))
        if marker.returncode != 0:
            findings.append(_finding("public-root marker absent from root commit", ROOT_MARKER))
        elif marker_error := _marker_error(marker.stdout, expected_repository):
            findings.append(_finding(f"root-commit {marker_error}", ROOT_MARKER))

    commits = _git_text(git, root, ("rev-list", "--all"))
    if commits.returncode != 0:
        findings.append(_finding("cannot enumerate complete Git history", ".git"))
        return _deduplicate(findings)
    commit_ids = [line for line in commits.stdout.splitlines() if line]
    for commit_id in commit_ids:
        commit = _git_bytes(git, root, ("cat-file", "commit", commit_id))
        commit_path = f".git/commits/{commit_id}"
        if commit.returncode != 0:
            findings.append(_finding("cannot read historical commit", commit_path))
            continue
        try:
            commit_text = commit.stdout.decode("utf-8")
        except UnicodeError:
            findings.append(_finding("historical non-UTF-8 commit", commit_path))
            continue
        findings.extend(_text_findings(commit_text, commit_path, prefix="historical "))

        tree = _git_bytes(git, root, ("ls-tree", "-r", "-z", commit_id))
        if tree.returncode != 0:
            findings.append(_finding("cannot enumerate historical tree", commit_path))
            continue
        for raw_entry in tree.stdout.split(b"\0"):
            if not raw_entry:
                continue
            try:
                metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
                mode, object_type, _object_id = metadata.decode("ascii").split()
                historical_path = raw_path.decode("utf-8")
            except (UnicodeError, ValueError):
                findings.append(_finding("invalid historical tree entry", ".git"))
                continue
            findings.extend(_path_findings(historical_path, prefix="historical "))
            if mode == "120000":
                findings.append(_finding("historical symlink", historical_path))
            elif mode == "160000" or object_type == "commit":
                findings.append(_finding("historical submodule", historical_path))

    objects = _git_text(git, root, ("rev-list", "--objects", "--all"))
    if objects.returncode != 0:
        findings.append(_finding("cannot enumerate complete Git history", ".git"))
        return _deduplicate(findings)
    object_paths: dict[str, set[str]] = {}
    for line in objects.stdout.splitlines():
        object_id, separator, historical_path = line.partition(" ")
        if not object_id:
            continue
        object_paths.setdefault(object_id, set())
        if separator:
            object_paths[object_id].add(historical_path)
            findings.extend(_path_findings(historical_path, prefix="historical "))
            path = Path(historical_path)
            filename = path.name.casefold()
            if any(part.casefold() in PROHIBITED_COMPONENTS for part in path.parts):
                findings.append(_finding("historical prohibited directory", historical_path))
            if filename in PROHIBITED_FILENAMES or filename.startswith(".env."):
                findings.append(_finding("historical prohibited file", historical_path))
            if path.suffix.casefold() in BINARY_SUFFIXES:
                findings.append(_finding("historical binary artifact", historical_path))
    if not object_paths:
        return _deduplicate(findings)
    check = _git_bytes(
        git,
        root,
        ("cat-file", "--batch-check"),
        input_data=("\n".join(object_paths) + "\n").encode("ascii"),
    )
    if check.returncode != 0:
        findings.append(_finding("cannot classify complete Git history", ".git"))
        return _deduplicate(findings)
    try:
        check_text = check.stdout.decode("ascii")
    except UnicodeError:
        findings.append(_finding("cannot classify complete Git history", ".git"))
        return _deduplicate(findings)
    blob_ids = [
        fields[0]
        for line in check_text.splitlines()
        if len(fields := line.split(" ")) >= 2 and fields[1] == "blob"
    ]
    if not blob_ids:
        return _deduplicate(findings)
    batch = _git_bytes(
        git,
        root,
        ("cat-file", "--batch"),
        input_data=("\n".join(blob_ids) + "\n").encode("ascii"),
    )
    if batch.returncode != 0:
        findings.append(_finding("cannot scan complete Git history", ".git"))
        return _deduplicate(findings)
    data = batch.stdout
    offset = 0
    for expected_id in blob_ids:
        header_end = data.find(b"\n", offset)
        if header_end < 0:
            findings.append(_finding("malformed Git object stream", ".git"))
            break
        header = data[offset:header_end].decode("ascii", errors="replace").split(" ")
        if len(header) != 3 or header[0] != expected_id:
            findings.append(_finding("unexpected Git object stream", ".git"))
            break
        try:
            size = int(header[2])
        except ValueError:
            findings.append(_finding("malformed Git object stream", ".git"))
            break
        content_start = header_end + 1
        content = data[content_start : content_start + size]
        offset = content_start + size + 1
        paths = object_paths.get(expected_id) or {"<historical-blob>"}
        if b"\0" in content:
            for path in paths:
                findings.append(_finding("historical hidden binary", path))
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            for path in paths:
                findings.append(_finding("historical non-UTF-8 text", path))
            continue
        for path in paths:
            findings.extend(_text_findings(text, path, prefix="historical "))
        if text.startswith("version https://git-lfs.github.com/spec/v1"):
            for path in paths:
                findings.append(_finding("historical Git LFS pointer", path))
    return _deduplicate(findings)


def scan_public_tree(
    root: Path,
    *,
    require_clean_root: bool = True,
    expected_repository: str | None = None,
) -> tuple[PublicScanFinding, ...]:
    resolved = root.resolve(strict=True)
    findings: list[PublicScanFinding] = []
    marker_path = resolved / ROOT_MARKER
    try:
        marker_text = marker_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        findings.append(_finding("public-root marker missing or unreadable", ROOT_MARKER))
    else:
        if marker_error := _marker_error(marker_text, expected_repository):
            findings.append(_finding(marker_error, ROOT_MARKER))

    for path in sorted(resolved.rglob("*")):
        relative_path = path.relative_to(resolved)
        relative = relative_path.as_posix()
        if any(part in SKIP_DIRECTORIES for part in relative_path.parts):
            continue
        findings.extend(_path_findings(relative))
        if path.is_symlink():
            findings.append(_finding("symlink", relative))
            continue
        if path.is_dir():
            if any(part.casefold() in PROHIBITED_COMPONENTS for part in relative_path.parts):
                findings.append(_finding("prohibited directory", relative))
            continue
        filename = path.name.casefold()
        if filename in PROHIBITED_FILENAMES or filename.startswith(".env."):
            findings.append(_finding("prohibited file", relative))
            continue
        if path.suffix.casefold() in BINARY_SUFFIXES:
            findings.append(_finding("binary artifact", relative))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append(_finding("unreadable file", relative))
            continue
        if b"\0" in data:
            findings.append(_finding("hidden binary content", relative))
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeError:
            findings.append(_finding("non-UTF-8 text", relative))
            continue
        if content.startswith("version https://git-lfs.github.com/spec/v1"):
            findings.append(_finding("Git LFS pointer", relative))
        findings.extend(_text_findings(content, relative))
    if require_clean_root:
        history_findings = _git_history_findings(resolved, expected_repository)
        if history_findings is None:
            findings.append(_finding("Git root is not initialized", ".git"))
        else:
            findings.extend(history_findings)
    return _deduplicate(findings)


def require_public_safe(
    root: Path,
    *,
    require_clean_root: bool = True,
    expected_repository: str | None = None,
) -> None:
    findings = scan_public_tree(
        root,
        require_clean_root=require_clean_root,
        expected_repository=expected_repository,
    )
    if findings:
        summary = "; ".join(f"{item.rule}: {item.path}" for item in findings[:10])
        raise PublicSafetyError(summary)
