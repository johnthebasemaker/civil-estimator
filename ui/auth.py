"""Password gate for the app.

**What this is:** a shared-password door so a colleague cannot open the tab and
start editing an estimate. It is a deterrent.

**What this is not:** protection for commercially sensitive tender data against
anyone who can reach the machine. Streamlit runs the whole app locally with the
user's own file access; anybody with a shell can read `output/`, the project
JSONs and this module. Treat it as a lock on an office door, not a safe.

The password itself never enters the repository. `bin/set_password.py` writes a
PBKDF2-HMAC-SHA256 hash and a random salt into `.streamlit/secrets.toml`, which
`.gitignore` already excludes. PBKDF2 rather than a bare salted digest because a
single fast hash of a short human password is brute-forced in seconds.

If no password has been configured the app **refuses entry** and says how to set
one. Failing open would be the worst of both worlds: the appearance of a gate
with none of the effect.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"

# A credential does not belong inside a container image or a git checkout. This
# lets the password file live wherever the deployment keeps its secrets — a
# mounted file under /etc, a Docker secret — without moving the app. Read at
# call time so the web app and the worker can be told separately.
SECRETS_ENV = "CIVIL_ESTIMATOR_SECRETS"


def secrets_path() -> Path:
    return Path(os.environ.get(SECRETS_ENV) or SECRETS_PATH)


ITERATIONS = 240_000
SALT_BYTES = 16
MAX_ATTEMPTS = 5           # before a cool-off
COOLOFF_SECONDS = 30


# ---------- Hashing ----------
def hash_password(plain: str, salt_hex: str | None = None,
                  iterations: int = ITERATIONS) -> tuple[str, str, int]:
    """Return (salt_hex, hash_hex, iterations)."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return salt.hex(), digest.hex(), iterations


def verify_password(plain: str, salt_hex: str, hash_hex: str,
                    iterations: int = ITERATIONS) -> bool:
    """Constant-time comparison, so a wrong guess leaks nothing by timing."""
    try:
        _, candidate, _ = hash_password(plain, salt_hex, iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, hash_hex)


# ---------- Stored credential ----------
def load_credential(path: Path | None = None) -> dict | None:
    """The configured credential, or None when the app has no password yet."""
    path = Path(path) if path else secrets_path()
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    auth = data.get("auth")
    if not isinstance(auth, dict):
        return None
    if not (auth.get("salt") and auth.get("hash")):
        return None
    return {
        "salt": str(auth["salt"]),
        "hash": str(auth["hash"]),
        "iterations": int(auth.get("iterations", ITERATIONS)),
        "hint": str(auth.get("hint", "")),
    }


def save_credential(plain: str, path: Path | None = None, hint: str = "") -> Path:
    """Write a new password hash, preserving anything else in the file."""
    path = Path(path) if path else secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # Keep unrelated sections; drop only the [auth] block we own.
        keep, skipping = [], False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                skipping = stripped == "[auth]"
            if not skipping:
                keep.append(line)
        existing = "\n".join(keep).rstrip()

    salt_hex, hash_hex, iterations = hash_password(plain)
    block = (f"[auth]\n"
             f"salt = \"{salt_hex}\"\n"
             f"hash = \"{hash_hex}\"\n"
             f"iterations = {iterations}\n")
    if hint:
        block += f"hint = \"{hint}\"\n"

    path.write_text((existing + "\n\n" if existing else "") + block, encoding="utf-8")
    os.chmod(path, 0o600)          # owner-only; it is a credential file
    return path


def is_configured(path: Path | None = None) -> bool:
    return load_credential(path) is not None


# ---------- Streamlit gate ----------
SESSION_KEY = "_authenticated"


def is_authenticated(session) -> bool:
    return bool(session.get(SESSION_KEY))


def attempt_login(session, plain: str, path: Path | None = None) -> tuple[bool, str]:
    """Check a submitted password. Returns (ok, message)."""
    cred = load_credential(path)
    if cred is None:
        return False, "No password is configured for this app."

    blocked_until = session.get("_login_blocked_until", 0)
    if blocked_until and time.time() < blocked_until:
        wait = int(blocked_until - time.time()) + 1
        return False, f"Too many attempts. Try again in {wait} s."

    if verify_password(plain, cred["salt"], cred["hash"], cred["iterations"]):
        session[SESSION_KEY] = True
        session["_login_attempts"] = 0
        session.pop("_login_blocked_until", None)
        return True, ""

    attempts = int(session.get("_login_attempts", 0)) + 1
    session["_login_attempts"] = attempts
    if attempts >= MAX_ATTEMPTS:
        session["_login_blocked_until"] = time.time() + COOLOFF_SECONDS
        session["_login_attempts"] = 0
        return False, (f"Too many attempts. Locked for {COOLOFF_SECONDS} s.")
    remaining = MAX_ATTEMPTS - attempts
    return False, f"Incorrect password. {remaining} attempt(s) before a pause."


def logout(session) -> None:
    session[SESSION_KEY] = False
