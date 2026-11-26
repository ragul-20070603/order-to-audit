import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as dbm  # noqa: E402
import seed  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_TODAY", "2026-11-26")
    c = dbm.connect(str(tmp_path / "test.db"))
    dbm.init_schema(c)
    seed.load(c)
    yield c
    c.close()


@pytest.fixture
def users(conn):
    return {r["username"]: r for r in conn.execute("SELECT * FROM users")}


@pytest.fixture
def ids(conn):
    return {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM items")}
