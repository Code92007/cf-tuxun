#!/usr/bin/env python3
"""Grant or revoke CF Snap review permissions for an existing user."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfshot.db import get_db, init_db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    init_db()
    changed = get_db().execute(
        "UPDATE users SET is_admin=? WHERE username=? COLLATE NOCASE",
        (0 if args.revoke else 1, args.username),
    ).rowcount
    if not changed:
        raise SystemExit(f"User not found: {args.username}")
    action = "Revoked admin from" if args.revoke else "Granted admin to"
    print(f"{action} {args.username}")


if __name__ == "__main__":
    main()
