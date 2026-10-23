"""Tests for nua/ports.py RepoFS: repo files are never read or written through a symlink or outside the root.

A committed symlink such as transcripts/x.md -> /proc/self/environ must not be listed as a transcript,
read, parsed or sent to a model, and a symlinked directory must not get past a check on the leaf. Offline.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nua.ports import RepoFS, UnsafePathError

SECRET = "GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\nANTHROPIC_API_KEY=sk-ant-xyz\n"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory next to the repo holding a secret file, like the service's environment."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "environ").write_text(SECRET, encoding="utf-8")
    (d / "secret.md").write_text(SECRET, encoding="utf-8")
    return d


@pytest.fixture
def repo(tmp_path: Path) -> RepoFS:
    root = tmp_path / "repo"
    (root / "transcripts").mkdir(parents=True)
    (root / "digests").mkdir()
    (root / "transcripts" / "real.md").write_text("a real transcript\n", encoding="utf-8")
    return RepoFS(root)


def test_regular_files_still_work(repo):
    assert repo.list("transcripts/*") == ["transcripts/real.md"]
    assert repo.exists("transcripts/real.md")
    assert repo.read("transcripts/real.md") == "a real transcript\n"
    assert repo.read_or("transcripts/real.md", "x") == "a real transcript\n"
    assert repo.read_or("transcripts/none.md", "x") == "x"
    assert repo.read_bytes("transcripts/real.md") == b"a real transcript\n"
    repo.write("digests/a.md", "digest\n")
    assert repo.read("./digests/a.md") == "digest\n"
    repo.delete("digests/a.md")
    assert not repo.exists("digests/a.md")


def test_symlinked_transcript_to_a_file_outside_is_never_listed_or_read(repo, outside):
    os.symlink(outside / "environ", repo.path("transcripts/evil.md"))
    assert repo.list("transcripts/*") == ["transcripts/real.md"]
    assert repo.list("transcripts/*.md") == ["transcripts/real.md"]
    assert not repo.exists("transcripts/evil.md")
    with pytest.raises(UnsafePathError, match="transcripts/evil.md"):
        repo.read("transcripts/evil.md")
    with pytest.raises(UnsafePathError):
        repo.read_or("transcripts/evil.md", "")
    with pytest.raises(UnsafePathError):
        repo.read_bytes("transcripts/evil.md")


def test_a_symlink_pointing_inside_the_repo_is_refused_too(repo):
    os.symlink(repo.path("transcripts/real.md"), repo.path("transcripts/alias.md"))
    assert repo.list("transcripts/*") == ["transcripts/real.md"]
    assert not repo.exists("transcripts/alias.md")
    with pytest.raises(UnsafePathError):
        repo.read("transcripts/alias.md")


def test_symlinked_directory_is_refused_below_the_leaf(repo, outside):
    os.symlink(outside, repo.path("transcripts/sub"))
    assert repo.list("transcripts/*/*") == []
    assert repo.list("transcripts/**/*.md") == ["transcripts/real.md"]
    assert not repo.exists("transcripts/sub/secret.md")
    with pytest.raises(UnsafePathError):
        repo.read("transcripts/sub/secret.md")
    with pytest.raises(UnsafePathError):
        repo.read_or("transcripts/sub/secret.md", "")


def test_a_whole_symlinked_folder_lists_nothing(tmp_path, outside):
    root = tmp_path / "repo"
    root.mkdir()
    os.symlink(outside, root / "transcripts")
    repo = RepoFS(root)
    assert repo.list("transcripts/*") == []
    assert repo.list("transcripts/*.md") == []
    assert not repo.exists("transcripts/secret.md")


def test_paths_outside_the_root_are_refused(repo, outside):
    for rel in (str(outside / "environ"), "../outside/environ", "transcripts/../../outside/environ"):
        assert not repo.exists(rel)
        with pytest.raises(UnsafePathError):
            repo.read(rel)
        with pytest.raises(UnsafePathError):
            repo.read_or(rel, "")


def test_an_absolute_path_inside_the_root_still_reads(repo):
    assert repo.read(str(repo.path("transcripts/real.md"))) == "a real transcript\n"


def test_a_root_reached_through_a_symlink_still_works(tmp_path, repo):
    link = tmp_path / "link-to-repo"
    os.symlink(repo.root, link)
    via = RepoFS(link)
    assert via.list("transcripts/*") == ["transcripts/real.md"]
    assert via.read("transcripts/real.md") == "a real transcript\n"


def test_write_never_follows_a_symlink(repo, outside):
    os.symlink(outside / "environ", repo.path("digests/x.md"))
    repo.write("digests/x.md", "digest\n")
    assert (outside / "environ").read_text(encoding="utf-8") == SECRET      # the target is untouched
    assert not repo.path("digests/x.md").is_symlink()
    assert repo.read("digests/x.md") == "digest\n"

    os.symlink(outside, repo.path("digests/sub"))
    with pytest.raises(UnsafePathError):
        repo.write("digests/sub/secret.md", "overwritten")
    with pytest.raises(UnsafePathError):
        repo.write(str(outside / "new.md"), "outside")
    assert (outside / "secret.md").read_text(encoding="utf-8") == SECRET
    assert not (outside / "new.md").exists()


def test_delete_never_reaches_outside(repo, outside):
    os.symlink(outside, repo.path("digests/sub"))
    with pytest.raises(UnsafePathError):
        repo.delete("digests/sub/secret.md")
    assert (outside / "secret.md").exists()
    # a symlink itself is removed, never its target
    os.symlink(outside / "environ", repo.path("digests/link.md"))
    repo.delete("digests/link.md")
    assert not repo.path("digests/link.md").is_symlink()
    assert (outside / "environ").exists()
