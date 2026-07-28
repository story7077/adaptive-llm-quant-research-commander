from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from research_commander.errors import PublicSafetyError
from research_commander.public_scan import scan_public_tree
from research_commander.snapshot import create_clean_snapshot

EXPECTED_REPOSITORY = "story7077/adaptive-llm-quant-research-commander"


def marker_text(repository_name: str = "adaptive-llm-quant-research-commander") -> str:
    return (
        '{"schema_version":"PublicRootMarkerV1",'
        f'"repository_name":"{repository_name}",'
        '"sanitized_working_tree":true,'
        '"private_git_history_included":false,'
        '"synthetic_examples_only":true,'
        '"credentials_permitted":false}\n'
    )


def git_run(
    git: str,
    root: Path,
    *arguments: str,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    environment = os.environ.copy()
    environment.update(extra_env or {})
    completed = subprocess.run(  # noqa: S603
        (git, "-C", str(root), *arguments),
        input=input_text,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    assert completed.returncode == 0
    return completed.stdout.strip()


def test_snapshot_removes_git_history_and_rejects_env(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / "src").mkdir()
    (source / "src/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    destination = tmp_path / "snapshot"
    manifest = create_clean_snapshot(source, destination, allowlist=["src/**"])
    assert manifest["history_included"] is False
    assert not (destination / ".git").exists()

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")
    with pytest.raises(PublicSafetyError, match="prohibited file"):
        create_clean_snapshot(unsafe, tmp_path / "unsafe-snapshot", allowlist=["**"])


def test_public_scan_detects_secret_and_personal_path_without_echoing_values(
    tmp_path: Path,
) -> None:
    root = tmp_path / "public"
    root.mkdir()
    secret = "sk-" + ("Z" * 24)
    (root / "bad.txt").write_text(
        secret + "\n" + "C:" + "\\Users\\private-user\\data\n",
        encoding="utf-8",
    )
    findings = scan_public_tree(root, require_clean_root=False)
    rules = {finding.rule for finding in findings}
    assert "OpenAI-style key" in rules
    assert "Windows user path" in rules
    assert all(secret not in finding.rule and secret not in finding.path for finding in findings)


def test_public_scan_rejects_uninitialized_history_when_required(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    (root / "README.md").write_text("# Synthetic\n", encoding="utf-8")
    findings = scan_public_tree(root, require_clean_root=True)
    assert any(finding.rule == "Git root is not initialized" for finding in findings)


def test_public_scan_accepts_later_commits_from_marked_clean_root(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    root = tmp_path / "public"
    root.mkdir()
    marker = root / ".public-root.json"
    marker.write_text(marker_text(), encoding="utf-8")
    readme = root / "README.md"
    readme.write_text("# Synthetic public root\n", encoding="utf-8")

    git_run(git, root, "init")
    git_run(git, root, "config", "user.name", "Synthetic Test")
    git_run(git, root, "config", "user.email", "synthetic" + "@" + "example.invalid")
    git_run(git, root, "add", ".")
    git_run(git, root, "commit", "-m", "clean public root")
    readme.write_text("# Synthetic public root\n\nSecond public commit.\n", encoding="utf-8")
    git_run(git, root, "add", "README.md")
    git_run(git, root, "commit", "-m", "public feature")

    assert (
        scan_public_tree(
            root,
            require_clean_root=True,
            expected_repository=EXPECTED_REPOSITORY,
        )
        == ()
    )


def test_public_scan_binds_marker_schema_and_repository(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    marker = root / ".public-root.json"
    marker.write_text(marker_text("different-public-repository"), encoding="utf-8")

    findings = scan_public_tree(
        root,
        require_clean_root=False,
        expected_repository=EXPECTED_REPOSITORY,
    )

    assert any(finding.rule == "public-root repository mismatch" for finding in findings)

    marker.write_text(
        marker_text().replace("PublicRootMarkerV1", "UnsupportedMarkerV0"),
        encoding="utf-8",
    )
    findings = scan_public_tree(
        root,
        require_clean_root=False,
        expected_repository=EXPECTED_REPOSITORY,
    )
    assert any(finding.rule == "invalid public-root marker" for finding in findings)


def test_public_scan_checks_all_refs_and_historical_commit_metadata(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    root = tmp_path / "public"
    root.mkdir()
    (root / ".public-root.json").write_text(marker_text(), encoding="utf-8")
    (root / "README.md").write_text("# Synthetic public root\n", encoding="utf-8")
    git_run(git, root, "init")
    git_run(git, root, "config", "user.name", "Synthetic Test")
    git_run(git, root, "config", "user.email", "synthetic" + "@" + "example.invalid")
    git_run(git, root, "add", ".")
    git_run(git, root, "commit", "-m", "clean public root")

    secret = "sk-" + ("Q" * 24)
    blob = git_run(git, root, "hash-object", "-w", "--stdin", input_text=secret + "\n")
    tree = git_run(
        git,
        root,
        "mktree",
        input_text=f"100644 blob {blob}\tarchived.txt\n",
    )
    personal_email = "private-user" + "@" + "personal.invalid"
    personal_path = "C:" + "\\Users\\private-user\\research"
    foreign_commit = git_run(
        git,
        root,
        "commit-tree",
        tree,
        "-m",
        f"imported from {personal_path} by {personal_email}",
    )
    git_run(git, root, "update-ref", "refs/heads/foreign-root", foreign_commit)

    findings = scan_public_tree(
        root,
        require_clean_root=True,
        expected_repository=EXPECTED_REPOSITORY,
    )
    rules = {finding.rule for finding in findings}

    assert "expected one independent clean root, found 2" in rules
    assert "historical OpenAI-style key" in rules
    assert "historical Windows user path" in rules
    assert "historical email address" in rules


def test_public_scan_ignores_only_github_generated_merge_identity(
    tmp_path: Path,
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    root = tmp_path / "public"
    root.mkdir()
    (root / ".public-root.json").write_text(marker_text(), encoding="utf-8")
    (root / "README.md").write_text("# Synthetic public root\n", encoding="utf-8")
    git_run(git, root, "init")
    git_run(git, root, "config", "user.name", "Synthetic Test")
    git_run(git, root, "config", "user.email", "synthetic" + "@" + "example.invalid")
    git_run(git, root, "add", ".")
    git_run(git, root, "commit", "-m", "clean public root")
    first_parent = git_run(git, root, "rev-parse", "HEAD")
    tree = git_run(git, root, "rev-parse", "HEAD^{tree}")
    second_parent = git_run(
        git,
        root,
        "commit-tree",
        tree,
        "-p",
        first_parent,
        "-m",
        "safe feature",
    )
    private_email = "private-user" + "@" + "personal.invalid"
    merge_commit = git_run(
        git,
        root,
        "commit-tree",
        tree,
        "-p",
        first_parent,
        "-p",
        second_parent,
        "-m",
        "Merge synthetic pull request",
        extra_env={
            "GIT_AUTHOR_NAME": "Public User",
            "GIT_AUTHOR_EMAIL": private_email,
            "GIT_COMMITTER_NAME": "GitHub",
            "GIT_COMMITTER_EMAIL": "noreply" + chr(64) + "github.com",
        },
    )
    git_run(git, root, "update-ref", "refs/remotes/pull/1/merge", merge_commit)

    assert (
        scan_public_tree(
            root,
            require_clean_root=True,
            expected_repository=EXPECTED_REPOSITORY,
        )
        == ()
    )

    unsafe_commit = git_run(
        git,
        root,
        "commit-tree",
        tree,
        "-p",
        first_parent,
        "-p",
        second_parent,
        "-m",
        "Merge unsafe synthetic pull request",
        extra_env={
            "GIT_AUTHOR_NAME": "Public User",
            "GIT_AUTHOR_EMAIL": private_email,
            "GIT_COMMITTER_NAME": "Synthetic Test",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        },
    )
    git_run(git, root, "update-ref", "refs/remotes/pull/2/merge", unsafe_commit)

    findings = scan_public_tree(
        root,
        require_clean_root=True,
        expected_repository=EXPECTED_REPOSITORY,
    )
    assert any(finding.rule == "historical email address" for finding in findings)


def test_public_scan_requires_marker_in_first_commit(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    root = tmp_path / "public"
    root.mkdir()
    (root / "README.md").write_text("# Synthetic public root\n", encoding="utf-8")
    git_run(git, root, "init")
    git_run(git, root, "config", "user.name", "Synthetic Test")
    git_run(git, root, "config", "user.email", "synthetic" + "@" + "example.invalid")
    git_run(git, root, "add", ".")
    git_run(git, root, "commit", "-m", "unmarked root")
    (root / ".public-root.json").write_text(marker_text(), encoding="utf-8")
    git_run(git, root, "add", ".public-root.json")
    git_run(git, root, "commit", "-m", "marker added too late")

    findings = scan_public_tree(
        root,
        require_clean_root=True,
        expected_repository=EXPECTED_REPOSITORY,
    )

    assert any(finding.rule == "public-root marker absent from root commit" for finding in findings)


def test_public_scan_redacts_sensitive_filename(tmp_path: Path) -> None:
    root = tmp_path / "public"
    root.mkdir()
    (root / ".public-root.json").write_text(marker_text(), encoding="utf-8")
    secret = "ghp_" + ("R" * 32)
    (root / f"{secret}.txt").write_text("synthetic fixture\n", encoding="utf-8")

    findings = scan_public_tree(
        root,
        require_clean_root=False,
        expected_repository=EXPECTED_REPOSITORY,
    )

    assert any(finding.rule == "GitHub-style token" for finding in findings)
    assert all(secret not in finding.path and secret not in finding.rule for finding in findings)
    assert any(finding.path.startswith("<redacted-path:") for finding in findings)
