import os
import tempfile

from server.app import models


def test_sqlite_uses_wal_and_normal_sync():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{os.path.join(d, 'wal_probe.db')}"
    engine = models.init_db(url)
    with engine.connect() as conn:
        jm = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        sync = conn.exec_driver_sql("PRAGMA synchronous").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert str(jm).lower() == "wal"
    # synchronous=NORMAL is 1
    assert int(sync) == 1
    assert int(foreign_keys) == 1
