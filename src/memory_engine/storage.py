"""Incremental encrypted persistence. No UI, retrieval or model policy lives here.

Connections live until close(). All writers/checkpoints (including close) share an
OS lock: the pinned wheel's SQLite 3.51.1 predates the WAL-reset fix. Read snapshots
can run concurrently in different Memory instances. Never open this DB using a
second library or an uncoordinated checkpoint tool.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3 as legacy_sqlite
import threading

from sqlcipher3 import dbapi2 as sqlite

from .crypto import Cipher, atomic_write, file_lock

FORMAT = 'memory-engine-sqlcipher-v1'
APPLICATION_ID = 0x42425554
LEGACY_FENCE = b'MEMORY-ENGINE-SQLCIPHER-ACTIVE\nUse updated clients. Migration backup details, if any, are in memory.storage.json.\n'


class StorageError(RuntimeError):
    pass


class MigrationRequired(StorageError):
    pass


def statements(db, script):
    """DDL inside the caller's transaction; executescript would commit first."""
    statement = ''
    for line in script.splitlines(keepends=True):
        statement += line
        if legacy_sqlite.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        db.execute(statement)


def connect(path, key):
    db = sqlite.connect(str(path), isolation_level=None, check_same_thread=False, timeout=5)
    try:
        if not db.execute('PRAGMA cipher_version').fetchone():
            raise StorageError('SQLCipher unavailable; plaintext storage is forbidden.')
        # Key is random binary, not a password; never install SQL trace callbacks.
        db.execute('PRAGMA key = "x\'' + key.hex() + '\'"')
        db.execute('PRAGMA cipher_compatibility=4')
        # Keep the engine's default key-memory protection. Full allocator memory
        # locking exceeds the normal Windows working-set quota on this host.
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA cache_size=-8192')
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()  # authenticate first page
        db.row_factory = sqlite.Row
        return db
    except BaseException:
        db.close()
        raise


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def copy_legacy(source, target):
    """Copy logical rows in bounded batches; plaintext exists only in memory.

FTS shadow pages are SQLite-version-specific derived state, so rebuild them from
their public content tables, preserving rowids (the citation chunk IDs).
"""
    schema = source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    virtual = {r['name'] for r in schema if r['type'] == 'table' and r['sql'].upper().startswith('CREATE VIRTUAL TABLE')}
    shadows = {name + suffix for name in virtual for suffix in ('_data', '_idx', '_content', '_docsize', '_config')}
    tables = [r for r in schema if r['type'] == 'table' and r['name'] not in shadows]
    for row in tables:
        target.execute(row['sql'])
        name = quote(row['name'])
        columns = 'rowid,*' if row['name'] in virtual else '*'
        cursor = source.execute(f'SELECT {columns} FROM {name}')
        width = len(cursor.description)
        while batch := cursor.fetchmany(256):
            target.executemany(f'INSERT INTO {name} VALUES ({",".join("?" for _ in range(width))})' if row['name'] not in virtual
                               else f'INSERT INTO {name}(rowid,{",".join(quote(c[0]) for c in cursor.description[1:])}) VALUES ({",".join("?" for _ in range(width))})',
                               [tuple(r) for r in batch])
        # Full row equality in deterministic order, independent of file layouts.
        a = source.execute(f'SELECT {columns} FROM {name} ORDER BY rowid')
        b = target.execute(f'SELECT {columns} FROM {name} ORDER BY rowid')
        while True:
            left, right = a.fetchmany(256), b.fetchmany(256)
            if [tuple(r) for r in left] != [tuple(r) for r in right]:
                raise StorageError('Migration row verification failed.')
            if not left:
                break
    for row in schema:
        if row['type'] != 'table' and row['name'] not in shadows:
            target.execute(row['sql'])
    target.execute(f'PRAGMA user_version={source.execute("PRAGMA user_version").fetchone()[0]}')


class EncryptedStore:
    def __init__(self, home: Path, *, test_key=None, initialize, migrate_legacy=False):
        self.home = home
        self.path = home / 'memory.db'
        self.legacy = home / 'memory.bb'
        self.lock_path = home / 'memory.lock'
        self.marker = home / 'memory.storage.json'
        self._lock = threading.RLock()
        self._db = None
        home.mkdir(parents=True, exist_ok=True)
        with file_lock(self.lock_path):
            self.cipher = Cipher(home, test_key)
            state = json.loads(self.marker.read_text('utf-8')) if self.marker.exists() else {}
            if state and state.get('format') != FORMAT:
                raise StorageError('Unsupported storage format; files were not replaced.')
            if self.path.exists() and not state:
                raise StorageError('Storage manifest is missing. Restore the matching manifest; database was not replaced.')
            if not self.path.exists():
                if state.get('state') == 'active':
                    raise StorageError('Active database is missing. Restore database and WAL; no legacy fallback.')
                if self.legacy.exists() and not migrate_legacy:
                    raise MigrationRequired('Legacy vault needs offline migration. Stop old app/MCP clients and run run.py migrate.')
                self._publish(initialize, state)
                state = json.loads(self.marker.read_text('utf-8'))
            db = connect(self.path, self.cipher.key)
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                    raise StorageError('Database application identity is invalid.')
                # Authenticate the published candidate before finishing activation.
                # An invalid key must not change even the legacy fence/manifest.
                if state.get('state') == 'preparing' or (not state.get('backup') and state.get('legacy_signature') is None):
                    state = self._fence_legacy(state)
                self._legacy_signature = state.get('legacy_signature')
                self._check_legacy()
                db.execute('BEGIN IMMEDIATE')
                initialize(db)
                db.commit()
                if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                    raise StorageError('Encrypted WAL unavailable.')
                db.execute('PRAGMA wal_autocheckpoint=1000')
                db.execute('PRAGMA journal_size_limit=16777216')
                self._db = db
                if state.get('state') != 'active':
                    state['state'] = 'active'
                    atomic_write(self.marker, json.dumps(state).encode())
            except BaseException:
                db.close()
                self._db = None
                raise

    def _signature(self):
        if not self.legacy.exists():
            return None
        stat = self.legacy.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def _check_legacy(self):
        if self._signature() != self._legacy_signature:
            if self.legacy.exists() and self.legacy.read_bytes() == LEGACY_FENCE:
                # A byte-identical restored fence can have a new filesystem time.
                self._legacy_signature = self._signature()
            else:
                raise StorageError('Legacy vault changed after migration. Stop old clients and recover the divergent histories explicitly.')

    def _fence_legacy(self, state):
        if not state.get('backup'):
            if self.legacy.exists() and self.legacy.read_bytes() != LEGACY_FENCE:
                raise StorageError('A retired client created a divergent vault before activation.')
            if not self.legacy.exists():
                atomic_write(self.legacy, LEGACY_FENCE)
            state['legacy_signature'] = self._signature()
            state['state'] = 'active'
            atomic_write(self.marker, json.dumps(state).encode())
            return state
        backup = self.home / state['backup']
        if backup.parent != self.home or not backup.name.startswith('memory.legacy-'):
            raise StorageError('Invalid legacy backup manifest.')
        raw = self.legacy.read_bytes()
        if raw != LEGACY_FENCE:
            if hashlib.sha256(raw).hexdigest() != state['legacy_sha256'] or raw != backup.read_bytes():
                raise StorageError('Legacy vault diverged before activation; recovery required.')
            # The original encrypted bytes were fsynced and verified in backup.
            # Retired clients now reject the original filename instead of reading
            # stale grants/evidence or resurrecting deleted content into a fork.
            atomic_write(self.legacy, LEGACY_FENCE)
        elif hashlib.sha256(backup.read_bytes()).hexdigest() != state['legacy_sha256']:
            raise StorageError('Legacy migration backup failed verification.')
        state['legacy_signature'] = self._signature()
        state['state'] = 'active'
        atomic_write(self.marker, json.dumps(state).encode())
        return state

    def _publish(self, initialize, state):
        candidate = self.home / 'memory.migrating.db'
        # Only unpublished, task-owned candidate files are disposable. Original,
        # backups, active DB and active WAL are never removed by recovery.
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate.with_name(candidate.name + suffix).unlink(missing_ok=True)
        source = None
        backup = None
        signature = self._signature()
        if self.legacy.exists():
            raw = self.legacy.read_bytes()
            clear = self.cipher.decrypt(raw)
            digest = hashlib.sha256(raw).hexdigest()
            backup = self.home / ('memory.legacy-' + digest + '.bb.bak')
            if backup.exists() and backup.read_bytes() != raw:
                raise StorageError('Legacy backup does not match; refusing replacement.')
            if not backup.exists():
                atomic_write(backup, raw)
            source = legacy_sqlite.connect(':memory:')
            source.row_factory = legacy_sqlite.Row
            source.deserialize(clear)
            del clear
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                source.close()
                raise StorageError('Legacy database failed integrity check.')
        state = {'format': FORMAT, 'state': 'preparing', 'legacy_signature': signature,
                 'backup': backup.name if backup else None,
                 'legacy_sha256': digest if backup else None}
        atomic_write(self.marker, json.dumps(state).encode())
        db = None
        try:
            db = connect(candidate, self.cipher.key)
            db.execute('BEGIN IMMEDIATE')
            if source:
                copy_legacy(source, db)
            initialize(db)
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise StorageError('Candidate database failed integrity check.')
            if db.execute('PRAGMA cipher_integrity_check').fetchall():
                raise StorageError('Candidate encryption integrity check failed.')
            db.close()
            db = None
            with candidate.open('r+b') as stream:
                os.fsync(stream.fileno())
            if self._signature() != signature:
                raise StorageError('Legacy vault changed during migration.')
            os.replace(candidate, self.path)
            # A crash before this marker update is recoverable: the published DB
            # is always fully validated and will be reopened, never overwritten.
            self._fence_legacy(state)
        finally:
            if db:
                db.close()
            if source:
                source.close()

    @contextmanager
    def transaction(self, *, write=False):
        with self._lock, (file_lock(self.lock_path) if write else nullcontext()):
            if self._db is None:
                raise StorageError('Memory store is closed.')
            self._check_legacy()
            db = self._db
            db.execute('PRAGMA query_only=' + ('OFF' if write else 'ON'))
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.execute('PRAGMA query_only=OFF')

    def close(self):
        with self._lock:
            if self._db is not None:
                with file_lock(self.lock_path):
                    self._db.close()  # final close may checkpoint: coordinate it
                    self._db = None

    def checkpoint(self):
        """Explicit maintenance uses the same interprocess gate as every writer."""
        with self._lock, file_lock(self.lock_path):
            if self._db is None:
                raise StorageError('Memory store is closed.')
            self._check_legacy()
            return tuple(self._db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())

    def check_integrity(self):
        """Full maintenance scan, never called per query or on the Qt thread."""
        with self.transaction() as db:
            cipher_errors = [tuple(r) for r in db.execute('PRAGMA cipher_integrity_check')]
            structure = [r[0] for r in db.execute('PRAGMA integrity_check')]
            foreign_keys = [tuple(r) for r in db.execute('PRAGMA foreign_key_check')]
            if cipher_errors or structure != ['ok'] or foreign_keys:
                raise StorageError('Encrypted database integrity check failed; restore/recover explicitly.')
            return True

    def __del__(self):
        if getattr(self, '_db', None) is not None:
            self.close()
