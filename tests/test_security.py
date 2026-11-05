import json
import sqlite3

from finapp import benchmarks, db, keystore


def test_backup_never_contains_api_key(con, tmp_path):
    db.set_setting(con, "ai", {"api_key": "not-a-real-key", "model": "m"})
    out = tmp_path / "b.db"
    out.write_bytes(db.backup_bytes(con))
    cfg = json.loads(sqlite3.connect(out).execute("SELECT value FROM settings WHERE key='ai'").fetchone()[0])
    assert "api_key" not in cfg and cfg["model"] == "m"


def test_old_key_is_removed_from_database(con, monkeypatch):
    monkeypatch.setattr(keystore, "store_available", lambda: False)
    db.set_setting(con, "ai", {"api_key": "not-a-real-key"})
    keystore.migrate_from_db(con)
    assert "api_key" not in db.get_setting(con, "ai")


def test_municipal_charges():
    p = {"municipality": "Utrecht", "residents": 4, "home_owner": True, "woz": 450_000}
    c = benchmarks.municipal_charges(p)
    assert c["afval"] == 600.05 and c["riool"] == 247.27 and round(c["ozb"], 2) == 362.70
    own = {"municipality": benchmarks.OTHER_MUNICIPALITY, "residents": 2, "home_owner": False,
           "local_rates": {"name": "Testdorp", "afval": 400, "riool": 200, "ozb_pct": 0.1}}
    c = benchmarks.municipal_charges(own)
    assert c["municipality"] == "Testdorp" and c["total"] == 400        # renters pay only the waste charge
    assert benchmarks.municipal_charges({}) is None
