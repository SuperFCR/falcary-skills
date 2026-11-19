# overleaf_sync/auth.py
"""Store the Overleaf git token via the platform's git credential helper.

Security invariant: this module NEVER writes the token to disk, the registry,
git config, process arguments, or any log. It is only handed to `git credential
approve` over stdin, which persists it through the credential helper that applies
to the Overleaf host: one the user already configured, else the platform default
(macOS Keychain via osxkeychain; Git Credential Manager on Windows; in-memory
cache on Linux), configured for that host only.
"""
from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_HOST = "git.overleaf.com"

# git's stock `cache` timeout is 15 minutes, which silently drops the login in
# the middle of a writing session. The cache stays in memory either way and is
# gone once the cache daemon exits (logout, reboot, WSL shutdown).
LINUX_CACHE_TIMEOUT_SECONDS = 30 * 24 * 60 * 60


def default_credential_helper() -> str:
    """The platform's native git credential helper.

    Hardcoding ``osxkeychain`` everywhere poisons non-mac machines: git would
    warn "credential-osxkeychain is not a git command" on every operation and
    never persist the token.
    """
    if sys.platform == "darwin":
        return "osxkeychain"
    if sys.platform == "win32":
        return "manager"  # Git Credential Manager, bundled with Git for Windows
    return f"cache --timeout={LINUX_CACHE_TIMEOUT_SECONDS}"


def _credential_request(host: str, username: str) -> str:
    return f"protocol=https\nhost={host}\nusername={username}\n\n"


def ensure_credential_helper(host: str = DEFAULT_HOST) -> str:
    """Ensure a credential helper applies to ``host``; return the effective one.

    A helper that already applies (a generic ``credential.helper`` or one scoped
    to this host, at any config level) is left untouched. Otherwise the platform
    default is configured for this host only
    (``credential.https://<host>.helper``), so credentials for other hosts such
    as GitHub keep their current behavior.
    """
    url = f"https://{host}"
    result = subprocess.run(
        ["git", "config", "--get-urlmatch", "credential.helper", url],
        capture_output=True,
        text=True,
    )
    current = result.stdout.strip()
    if result.returncode == 0 and current:
        return current
    helper = default_credential_helper()
    subprocess.run(
        ["git", "config", "--global", f"credential.{url}.helper", helper],
        check=True,
    )
    return helper


def store_token(token: str, host: str = DEFAULT_HOST, username: str = "git") -> None:
    """Persist ``token`` into the credential store via ``git credential approve``.

    The credential is supplied only on stdin in git-credential format; the token
    never appears in argv, and on failure the raised error never echoes it.
    """
    payload = (
        f"protocol=https\n"
        f"host={host}\n"
        f"username={username}\n"
        f"password={token}\n"
        f"\n"
    )
    result = subprocess.run(
        ["git", "credential", "approve"],
        input=payload,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git credential approve failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )


def get_credential(host: str = DEFAULT_HOST, username: str = "git") -> str | None:
    """Return the stored token for ``host`` or None — never prompts.

    Uses ``git credential fill`` with terminal prompting disabled, so an
    absent credential returns None instead of blocking on user input.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_ASKPASS", None)  # don't let a GUI askpass pop up either
    env["GIT_ASKPASS"] = "true"   # askpass that returns nothing
    result = subprocess.run(
        ["git", "credential", "fill"],
        input=_credential_request(host, username),
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            password = line[len("password="):]
            return password or None
    return None


def erase_credential(host: str = DEFAULT_HOST, username: str = "git") -> None:
    """Remove the stored credential for ``host`` via ``git credential reject``."""
    result = subprocess.run(
        ["git", "credential", "reject"],
        input=_credential_request(host, username),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git credential reject failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
