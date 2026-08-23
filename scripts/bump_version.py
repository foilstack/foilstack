"""Bump the patch version when application code is committed.

Runs as a pre-commit hook. `src/foilstack/__init__.py` holds the one copy of
the version — pyproject reads it from there — so bumping that line bumps the
package, the page footer and the TCGCSV User-Agent together.

Three deliberate restraints:

* **Only when `src/` is staged.** A README fix or a CI tweak is not a new
  version of the software, and a version that moves for those stops meaning
  anything.
* **Never during a merge, rebase, cherry-pick or revert.** Those replay
  commits that already carry their own version, and bumping on top produces a
  number nobody chose and a conflict on the line every time.
* **It stages its own change.** A hook that edits a file and leaves it
  unstaged makes every commit fail once and need repeating.

Run it by hand to see what it would do:

    uv run python scripts/bump_version.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "foilstack" / "__init__.py"

# Matches `__version__ = "1.2.3"` and nothing else. Anchored so a version-like
# string elsewhere in the file cannot be rewritten by accident.
PATTERN = re.compile(r'^(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")$', re.MULTILINE)

# Git writes one of these while a multi-commit operation is in flight.
IN_PROGRESS = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def _operation_in_progress() -> str | None:
    git_dir = Path(_git("rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = ROOT / git_dir
    for name in IN_PROGRESS:
        if (git_dir / name).exists():
            return name
    return None


def _staged_paths() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bump the patch version.")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument(
        "--force",
        action="store_true",
        help="bump even when no src/ file is staged (still refuses mid-merge)",
    )
    args = ap.parse_args(argv)

    # Not bypassable by --force. That flag means "bump even though no src/ file
    # is staged"; it does not mean "bump in the middle of replaying somebody
    # else's commits", and conflating the two makes the dangerous case reachable
    # by the flag people reach for when they are in a hurry.
    operation = _operation_in_progress()
    if operation:
        print(f"bump-version: {operation} in progress, leaving the version alone")
        return 0

    staged = _staged_paths()
    if not args.force and not any(p.startswith("src/foilstack/") for p in staged):
        return 0

    # An explicit edit to the version wins. Someone cutting 0.2.0 by hand means
    # it, and a hook that quietly turns it into 0.2.1 is a hook that has to be
    # fought with every release.
    if not args.force and VERSION_FILE.relative_to(ROOT).as_posix() in staged:
        current = _git("show", f":{VERSION_FILE.relative_to(ROOT).as_posix()}")
        committed = _git("show", f"HEAD:{VERSION_FILE.relative_to(ROOT).as_posix()}")
        if current != committed:
            print("bump-version: version already changed in this commit, leaving it")
            return 0

    text = VERSION_FILE.read_text()
    match = PATTERN.search(text)
    if match is None:
        print(f'bump-version: no `__version__ = "x.y.z"` in {VERSION_FILE}', file=sys.stderr)
        return 1

    major, minor, patch = (int(match.group(i)) for i in (2, 3, 4))
    old = f"{major}.{minor}.{patch}"
    new = f"{major}.{minor}.{patch + 1}"

    if args.dry_run:
        print(f"bump-version: would bump {old} -> {new}")
        return 0

    VERSION_FILE.write_text(PATTERN.sub(rf"\g<1>{major}.{minor}.{patch + 1}\g<5>", text, count=1))
    subprocess.run(["git", "add", "--", str(VERSION_FILE)], cwd=ROOT, check=True)
    print(f"bump-version: {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
