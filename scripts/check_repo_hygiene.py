#!/usr/bin/env python3
"""Fail the build if something that must never be published has been committed.

Scope, stated honestly
----------------------
This is a **backstop, not a secret scanner**. It reads only the files Git is tracking and looks
for two classes of mistake:

1. **Artefacts that should never be in a repository** -- a real ``.env``, a database, a virtual
   environment, a build directory, a cache, a generated report.
2. **Credential-shaped literals** matching a small set of well-known provider formats.

It will not catch a novel credential format, a secret already buried in history, or a password
that looks like an ordinary word. For those, use a dedicated tool such as ``gitleaks`` or
``trufflehog``. What this *does* guarantee is that the specific errors that would embarrass this
repository cannot land silently, using nothing but the standard library.

Values are never printed. Findings report the path, the line number, and the category only, so a
failing build log cannot itself leak the thing it just caught.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Paths that must never be tracked. Checked against the tracked-file list, not the disk, because
#: an ignored file merely sitting in the working tree is fine.
FORBIDDEN_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(^|/)\.env$", "a real .env file"),
    (r"(^|/)\.env\.(?!example$)[^/]+$", "an environment file other than .env.example"),
    (r"(^|/)\.venv/|(^|/)venv/|(^|/)env/", "a virtual environment"),
    (r"\.sqlite3(-wal|-shm)?$|\.db$", "a database file"),
    (r"(^|/)__pycache__/|\.py[co]$", "compiled Python"),
    (r"\.egg-info/", "build metadata"),
    (r"(^|/)\.(pytest|ruff|mypy)_cache/", "a tool cache"),
    (r"(^|/)reports/", "generated reports"),
    (r"\.(log|jsonl)$", "a runtime log"),
    (r"(^|/)data/", "runtime data"),
    (r"\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519)$", "a key or certificate"),
)

#: Credential formats worth failing a build over.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY")),
)

#: Fragments that mark a match as a documented placeholder rather than a credential. Kept
#: deliberately narrow: each one is a string this repository actually ships on purpose.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "replace-me",
    "change-me",
    "your-token",
    "placeholder",
    "PLACEHOLDER",
    "example.com",
    "ci-only",
    "demo-only",
    "123456789:replace",
)

#: Binary and vendored paths that are not worth reading.
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".woff", ".woff2")


def tracked_files() -> list[str]:
    """Return the files Git is tracking.

    S607 (partial executable path) is suppressed deliberately. Resolving an absolute path to git
    would make the script non-portable across CI images and developer machines for no security
    gain: the argument list is a fixed literal and contains no caller-supplied input.
    """
    result = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Outside a repository there is nothing to check. Say so plainly rather than surfacing a
        # CalledProcessError traceback, which reads like a bug in this script.
        raise SystemExit(
            "not inside a Git repository, so there are no tracked files to check; "
            "run this from a checkout"
        )
    return [line for line in result.stdout.splitlines() if line]


def check_paths(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        for pattern, description in FORBIDDEN_PATH_PATTERNS:
            if re.search(pattern, path):
                problems.append(f"{path}: tracked but is {description}")
                break
    return problems


def check_contents(paths: list[str]) -> list[str]:
    problems = []
    for path in paths:
        if path.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                fragment = match.group(0)
                if any(marker in fragment for marker in PLACEHOLDER_MARKERS):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                # The value itself is deliberately not included.
                problems.append(f"{path}:{line}: looks like a {label}")
    return problems


def main() -> int:
    paths = tracked_files()
    problems = check_paths(paths) + check_contents(paths)

    if problems:
        print("Repository hygiene check FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nIf a finding is a deliberate placeholder, add a marker from PLACEHOLDER_MARKERS "
            "to it. If it is a real credential, rotate it before doing anything else.",
            file=sys.stderr,
        )
        return 1

    print(f"Repository hygiene check passed ({len(paths)} tracked files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
