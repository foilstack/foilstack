"""The version bump hook.

Each of these is a case that behaved wrongly at some point while it was being
written: the merge guard was reachable by --force, and the retry after another
hook fails used to burn a second version number.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump_version.py"


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo laid out like this one."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)

    pkg = tmp_path / "src" / "foilstack"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (pkg / "thing.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("# readme\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "bump_version.py").write_text(SCRIPT.read_text())

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repo, *args):
    return subprocess.run(
        [sys.executable, "scripts/bump_version.py", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _version(repo) -> str:
    return (repo / "src" / "foilstack" / "__init__.py").read_text().strip()


def test_bumps_the_patch_when_application_code_is_staged(repo):
    (repo / "src" / "foilstack" / "thing.py").write_text("x = 2\n")
    subprocess.run(["git", "add", "src/foilstack/thing.py"], cwd=repo, check=True)

    assert _run(repo).returncode == 0
    assert _version(repo) == '__version__ = "0.1.1"'


def test_a_readme_commit_does_not_move_the_version(repo):
    """A number that moves for everything means nothing."""
    (repo / "README.md").write_text("# readme, edited\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)

    assert _run(repo).returncode == 0
    assert _version(repo) == '__version__ = "0.1.0"'


def test_it_stages_its_own_edit(repo):
    """A hook that edits a file and leaves it unstaged makes every commit fail
    once and need repeating."""
    (repo / "src" / "foilstack" / "thing.py").write_text("x = 3\n")
    subprocess.run(["git", "add", "src/foilstack/thing.py"], cwd=repo, check=True)
    _run(repo)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "src/foilstack/__init__.py" in staged


def test_running_twice_does_not_burn_a_second_version(repo):
    """Another hook failing means the commit is retried, and this runs again."""
    (repo / "src" / "foilstack" / "thing.py").write_text("x = 4\n")
    subprocess.run(["git", "add", "src/foilstack/thing.py"], cwd=repo, check=True)

    _run(repo)
    assert _version(repo) == '__version__ = "0.1.1"'
    _run(repo)
    assert _version(repo) == '__version__ = "0.1.1"'


def test_an_explicit_version_edit_wins(repo):
    """Someone cutting 0.2.0 by hand means it, and a hook that quietly turns
    that into 0.2.1 is one they have to fight at every release."""
    (repo / "src" / "foilstack" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    _run(repo)
    assert _version(repo) == '__version__ = "0.2.0"'


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"])
def test_it_refuses_mid_operation_even_with_force(repo, marker):
    """Those replay commits that carry their own version. --force means "no
    src/ file is staged", not "bump on top of somebody else's history"."""
    (repo / ".git" / marker).write_text("deadbeef\n")

    result = _run(repo, "--force")
    assert result.returncode == 0
    assert marker in result.stdout
    assert _version(repo) == '__version__ = "0.1.0"'


def test_it_refuses_mid_rebase(repo):
    (repo / ".git" / "rebase-merge").mkdir()

    result = _run(repo, "--force")
    assert "rebase-merge" in result.stdout
    assert _version(repo) == '__version__ = "0.1.0"'


def test_dry_run_changes_nothing(repo):
    result = _run(repo, "--dry-run", "--force")
    assert "0.1.0 -> 0.1.1" in result.stdout
    assert _version(repo) == '__version__ = "0.1.0"'


def test_only_the_version_line_is_rewritten(repo):
    """A version-like string elsewhere in the file must survive untouched."""
    target = repo / "src" / "foilstack" / "__init__.py"
    target.write_text('__version__ = "0.1.0"\nMIN_SUPPORTED = "1.2.3"\n')
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add constant"], cwd=repo, check=True)

    (repo / "src" / "foilstack" / "thing.py").write_text("x = 5\n")
    subprocess.run(["git", "add", "src/foilstack/thing.py"], cwd=repo, check=True)
    _run(repo)

    assert '__version__ = "0.1.1"' in target.read_text()
    assert 'MIN_SUPPORTED = "1.2.3"' in target.read_text()
