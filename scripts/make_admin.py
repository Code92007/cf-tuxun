#!/usr/bin/env python3
"""Grant or revoke CF Snap admin permissions for an existing user."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cfshot.db import get_db, init_db  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--super", action="store_true", help="manage the super-admin role")
    args = parser.parse_args()
    init_db()
    if args.super:
        sql = "UPDATE users SET is_super_admin=?,is_admin=1 WHERE username=? COLLATE NOCASE"
        values = (0 if args.revoke else 1, args.username)
    else:
        sql = "UPDATE users SET is_admin=? WHERE username=? COLLATE NOCASE AND is_super_admin=0"
        values = (0 if args.revoke else 1, args.username)
    changed = get_db().execute(sql, values).rowcount
    if not changed:
        raise SystemExit(f"User not found: {args.username}")
    role = "super admin" if args.super else "admin"
    action = f"Revoked {role} from" if args.revoke else f"Granted {role} to"
    print(f"{action} {args.username}")


if __name__ == "__main__":
    main()
