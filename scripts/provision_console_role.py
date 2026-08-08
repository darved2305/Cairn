#!/usr/bin/env python3
"""Create the console's read-only SQL login user and prove it cannot write.

`db/migrations/0008_console_readonly_role.sql` creates the `cairn_console_ro`
*group* role and its SELECT grants. That file is committed, so it cannot
contain a password, and it is static, so it cannot know which database it was
applied to. This script does the two things that need those: it creates the
login user, and it grants CONNECT on `current_database()`.

Then it does the part that actually matters — it **verifies**. It reconnects
as the new user and asserts that a SELECT succeeds and an INSERT is rejected
by the server. A read-only role that was never tested is a claim; a read-only
role that has been observed refusing a write is a fact, and this script only
exits 0 after observing it.

Usage:
  uv run python scripts/provision_console_role.py                  # generate a password
  uv run python scripts/provision_console_role.py --password '...' # supply one
  uv run python scripts/provision_console_role.py --verify-only    # re-check an existing role

The printed connection URL is what goes into the console's *own* Secrets
Manager secret (see infra/ecs.tf). It is printed to stdout and never written
to a file — piping it somewhere is a deliberate act.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg

LOGIN_USER = "cairn_console"
GROUP_ROLE = "cairn_console_ro"


def _admin_url() -> str:
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        raise SystemExit(
            "CAIRN_DATABASE_URL is not set. This script needs the *admin* connection "
            "string (the one scripts/provision_cluster.sh wrote), because creating a "
            "role is a privileged operation."
        )
    return url


def _console_url(admin_url: str, password: str) -> str:
    """Swap the user and password out of the admin URL, keep everything else
    (host, port, database, sslmode, and the sslrootcert path the CockroachDB
    Cloud connection string carries)."""

    parts = urlsplit(admin_url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(LOGIN_USER)}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def provision(admin_url: str, password: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
        assert row is not None
        database = row[0]

        cur.execute("SELECT 1 FROM [SHOW ROLES] WHERE username = %s", (GROUP_ROLE,))
        if cur.fetchone() is None:
            raise SystemExit(
                f"role {GROUP_ROLE!r} does not exist — apply "
                "db/migrations/0008_console_readonly_role.sql first (`make migrate`)."
            )

        # CREATE USER IF NOT EXISTS won't reset a password, so set it separately;
        # re-running this script with a new password rotates the credential.
        cur.execute(f"CREATE USER IF NOT EXISTS {LOGIN_USER}")
        cur.execute(f"ALTER USER {LOGIN_USER} WITH PASSWORD %s", (password,))
        cur.execute(f'GRANT CONNECT ON DATABASE "{database}" TO {LOGIN_USER}')
        cur.execute(f"GRANT {GROUP_ROLE} TO {LOGIN_USER}")
        print(f"provisioned {LOGIN_USER} on database {database!r}, member of {GROUP_ROLE}")


def verify(console_url: str) -> None:
    """Connect as the console user and observe both halves of the contract."""

    with psycopg.connect(console_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM reuse_decisions")
        row = cur.fetchone()
        assert row is not None
        print(f"  SELECT ok: reuse_decisions has {row[0]} row(s)")

        try:
            cur.execute(
                "INSERT INTO cost_rates (resource_kind, usd, source_note) "
                "VALUES ('__console_write_probe__', 0, 'must not be inserted')"
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            print(f"  INSERT correctly rejected: {str(exc).splitlines()[0]}")
        else:
            # Undo it with the same connection if it somehow succeeded, then fail
            # loudly — a console role that can write is the exact bug this exists
            # to prevent, and a green exit here would be a lie.
            cur.execute("DELETE FROM cost_rates WHERE resource_kind = '__console_write_probe__'")
            raise SystemExit(
                "FAIL: the console role was able to INSERT. The read-only grant is not "
                "in effect — do not wire this credential into the console task."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", help="Password for the login user; generated if omitted.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip creation; verify an existing role using --password.",
    )
    args = parser.parse_args()

    admin_url = _admin_url()
    password = args.password or secrets.token_urlsafe(24)
    if args.verify_only and not args.password:
        raise SystemExit("--verify-only needs --password to connect as the console user")

    if not args.verify_only:
        provision(admin_url, password)

    console_url = _console_url(admin_url, password)
    print("verifying the role actually is read-only ...")
    verify(console_url)

    print()
    print("Read-only console connection URL (put this in its OWN Secrets Manager secret,")
    print("wired only to the console task definition — see infra/ecs.tf):")
    print()
    print(f"  {console_url}")
    print()
    print("Not written to any file. Nothing else in this repo will print it again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
