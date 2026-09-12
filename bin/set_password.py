#!/usr/bin/env python
"""Set the app's shared password.

    venv/bin/python bin/set_password.py

Prompts twice (input is hidden), then writes a PBKDF2 hash and a random salt to
.streamlit/secrets.toml, which .gitignore already excludes. The password itself
is never written anywhere, printed, or passed as an argument — a command-line
argument would land in your shell history.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui import auth  # noqa: E402

MIN_LENGTH = 8


def main() -> int:
    print("Civil Estimator — set the shared app password")
    print(f"Stored as a PBKDF2 hash in {auth.SECRETS_PATH.name} (never in git).\n")

    first = getpass.getpass("New password: ")
    if len(first) < MIN_LENGTH:
        print(f"\nToo short — use at least {MIN_LENGTH} characters.")
        return 1
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("\nThey do not match. Nothing was changed.")
        return 1

    hint = input("Optional hint shown on the login screen (Enter to skip): ").strip()
    path = auth.save_credential(first, hint=hint)
    print(f"\nSaved → {path}")
    print("File permissions set to owner-only (600).")
    print("Restart the app for it to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
