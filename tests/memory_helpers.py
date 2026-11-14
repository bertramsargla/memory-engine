"""Synthetic fixture ownership for persistent file handles (not a fake store)."""
from pathlib import Path
import tempfile
import weakref

from memory_engine.memory import Memory as RealMemory

_stores = weakref.WeakSet()


def Memory(*args, **kwargs):
    memory = RealMemory(*args, **kwargs)
    _stores.add(memory)
    return memory


class MemoryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        root = Path(self.name).resolve()
        for memory in list(_stores):
            if memory.home.resolve().is_relative_to(root):
                memory.close()
        super().cleanup()


def install_legacy(memory, raw):
    """Replace only this test's freshly allocated synthetic fixture."""
    root = memory.home.resolve()
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    for store in list(_stores):
        if store.home.resolve() == root:
            store.close()
    for name in ('memory.db', 'memory.db-wal', 'memory.db-shm', 'memory.storage.json'):
        (root / name).unlink(missing_ok=True)
    path = root / 'memory.bb'
    path.write_bytes(raw)
    return path


def logical_contents(memory):
    # Includes FTS shadow rows to detect retained deleted terms, without assuming
    # SQLCipher pages can be decrypted using the retired snapshot Cipher API.
    output = bytearray()
    with memory.transaction() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '"'):
                for value in row:
                    output.extend(value if isinstance(value, bytes) else str(value).encode())
    return bytes(output)


def legacy_snapshot(memory, version=3):
    """Materialize an actual historical schema from compatible evidence columns."""
    import sqlite3
    from memory_engine.memory import SCHEMA
    from memory_engine import layers
    old=sqlite3.connect(':memory:')
    old.executescript(SCHEMA)
    if version>=2: layers.migrate(old)
    if version>=3: layers.migrate_v3(old)
    virtual={'search_index','layer_index'}
    with memory.transaction() as current:
        tables=[r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not any(r[0].startswith(v+'_') for v in virtual)]
        for table in tables:
            columns=[r[1] for r in old.execute('PRAGMA table_info('+table+')')]
            projection=','.join(columns)
            if table in virtual: projection='rowid,'+projection
            rows=current.execute('SELECT '+projection+' FROM '+table).fetchall()
            old.executemany('INSERT INTO '+table+'('+projection+') VALUES ('+','.join('?' for _ in projection.split(','))+')',[tuple(r) for r in rows])
    old.commit(); raw=memory.cipher.encrypt(old.serialize()); old.close()
    return raw
