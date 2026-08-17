import pytest

from coding_tools import RepoPathError, resolve_repo_path


def test_resolve_repo_path_accepts_relative_path(tmp_path):
    assert resolve_repo_path(str(tmp_path), "src/app.py") == tmp_path / "src" / "app.py"


def test_resolve_repo_path_rejects_parent_escape(tmp_path):
    with pytest.raises(RepoPathError, match="escapes repository"):
        resolve_repo_path(str(tmp_path), "../secret")


def test_resolve_repo_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepoPathError, match="escapes repository"):
        resolve_repo_path(str(tmp_path), "link/secret")
