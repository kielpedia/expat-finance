import json
import sqlite3

import pytest

from finapp import db


@pytest.fixture
def backup(con):
    """A backup holding one account and one transaction, after which the live database is changed."""
    aid = db.upsert_account(con, "From backup")
    con.execute("INSERT INTO transactions(account_id, date, amount) VALUES(?, '2024-03-01', -12.5)", (aid,))
    con.commit()
    data = db.backup_bytes(con)
    con.execute("DELETE FROM accounts")
    db.upsert_account(con, "Current")
    return data


def names(con):
    return [r["name"] for r in con.execute("SELECT name FROM accounts")]


def make_db(tmp_path, sql):
    path = tmp_path / "upload.db"
    c = sqlite3.connect(path)
    c.executescript(sql)
    c.close()
    return path.read_bytes()


def test_restore_replaces_data_and_connection_keeps_working(con, backup):
    db.restore_bytes(con, backup)
    assert names(con) == ["From backup"]
    assert db.summary(con) == {"accounts": 1, "transactions": 1, "first": "2024-03-01", "last": "2024-03-01"}
    db.upsert_account(con, "Added after")
    assert "Added after" in names(con)
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_restore_keeps_a_copy_of_the_previous_data(con, backup):
    safety = db.restore_bytes(con, backup)
    old = sqlite3.connect(safety)
    assert [r[0] for r in old.execute("SELECT name FROM accounts")] == ["Current"]
    old.close()


def test_inspect_changes_nothing(con, backup):
    assert db.inspect_backup(backup)["accounts"] == 1
    assert names(con) == ["Current"]


@pytest.mark.parametrize("bad", [b"", b"not a database", b"SQLite format 3\x00" + b"\xff" * 2000])
def test_rejects_files_that_are_not_databases(con, backup, bad):
    with pytest.raises(db.BackupError):
        db.restore_bytes(con, bad)
    assert names(con) == ["Current"]


def test_rejects_a_database_from_another_app(con, tmp_path):
    with pytest.raises(db.BackupError, match="not a backup"):
        db.inspect_backup(make_db(tmp_path, "CREATE TABLE photos(id INTEGER);"))


def test_rejects_embedded_triggers(con, backup, tmp_path):
    path = tmp_path / "evil.db"
    path.write_bytes(backup)
    c = sqlite3.connect(path)
    c.execute("CREATE TRIGGER t AFTER INSERT ON accounts BEGIN DELETE FROM transactions; END")
    c.commit()
    c.close()
    with pytest.raises(db.BackupError, match="database code"):
        db.restore_bytes(con, path.read_bytes())
    assert names(con) == ["Current"]


def test_restored_data_never_keeps_an_api_key(con, backup, tmp_path):
    path = tmp_path / "old.db"
    path.write_bytes(backup)
    c = sqlite3.connect(path)
    c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('ai', ?)", (json.dumps({"api_key": "not-a-real-key", "model": "m"}),))
    c.commit()
    c.close()
    db.restore_bytes(con, path.read_bytes())
    assert db.get_setting(con, "ai") == {"model": "m"}


def test_older_backups_are_upgraded(con, tmp_path):
    old = make_db(tmp_path, """
        CREATE TABLE accounts(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, country TEXT DEFAULT 'NL', currency TEXT DEFAULT 'EUR');
        CREATE TABLE transactions(id INTEGER PRIMARY KEY, account_id INTEGER, date TEXT, amount REAL, description TEXT,
                                  category TEXT, category_locked INTEGER DEFAULT 0, counterparty TEXT, hash TEXT UNIQUE);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO accounts(id, name) VALUES(1, 'Old');
    """)
    db.restore_bytes(con, old)
    assert names(con) == ["Old"]
    assert "spread_months" in {r["name"] for r in con.execute("PRAGMA table_info(transactions)")}
