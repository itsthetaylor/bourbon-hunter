"""Fixtures for the intake integration scripts.

The scripts under tests/scripts/ exercise the real intake path against a real
Postgres (clustering, vision stubbed, add_bottle, per-photo routing). Each one
gets its OWN fresh database so they cannot see each other's rows or the
owner/admin account.

Set BOURBON_TEST_PG to an admin connection URL on a Postgres you can create
databases on, e.g.  postgresql://postgres:PASSWORD@127.0.0.1:5433/postgres
The DB-backed scripts are skipped when it is unset.
"""
import os
import uuid

import pytest

# The scripts are run by test_intake.py as subprocesses; they are not test
# modules and must not be imported by collection (their top level does work).
collect_ignore_glob = ["scripts/*"]


def _admin_url():
    return os.environ.get("BOURBON_TEST_PG", "").strip()


@pytest.fixture
def fresh_db():
    """Create a throwaway database, yield its URL, drop it afterwards."""
    admin = _admin_url()
    if not admin:
        pytest.skip("BOURBON_TEST_PG not set — DB-backed intake scripts skipped")
    import psycopg2
    name = "bh_test_" + uuid.uuid4().hex[:10]
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()
    url = admin.rsplit("/", 1)[0] + "/" + name
    try:
        yield url
    finally:
        conn = psycopg2.connect(admin)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            conn.close()
