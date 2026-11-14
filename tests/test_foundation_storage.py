"""Real SQLCipher, isolated keys, actual process death and constrained disk space."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlcipher3.dbapi2 import DatabaseError, OperationalError
from cryptography.exceptions import InvalidTag

from memory_engine.crypto import Cipher, protect
from memory_engine.memory import Memory, SCHEMA
from memory_engine.storage import MigrationRequired, StorageError, LEGACY_FENCE


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.key = b'F' * 32

    def open(self, **kwargs):
        memory = Memory(self.home, test_key=self.key, **kwargs)
        self.addCleanup(memory.close)
        return memory

    def legacy(self):
        db = sqlite3.connect(':memory:')
        db.executescript(SCHEMA)
        content = 'Tere! Aster Labs CASE-7319.'
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('original', 'project:memory_engine', 'doc', 'legacy-hash', 'note', 'Synthetic',
                    'MemoryEngine', '', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z', content, 'unknown', 'complete'))
        db.execute('INSERT INTO chunks VALUES (1,?,?,?,?)', ('original', 0, len(content.encode()), content))
        db.execute('INSERT INTO search_index(rowid,text) VALUES (1,?)', (content,))
        db.execute("INSERT INTO tombstones VALUES ('project:memory_engine','deleted')")
        db.commit()
        raw = Cipher(self.home, self.key).encrypt(db.serialize())
        db.close()
        (self.home / 'memory.bb').write_bytes(raw)
        return raw

    def test_incremental_encryption_and_index_plans(self):
        memory = self.open()
        memory.ingest('SECRET_SYNTHETIC_CASE7319')
        base = memory.path.read_bytes()
        with (patch.object(memory.cipher, 'decrypt', side_effect=AssertionError('snapshot read')),
              patch.object(memory.cipher, 'encrypt', side_effect=AssertionError('snapshot write'))):
            memory.settings({'language': 'et'})
            self.assertTrue(memory.search('SECRET_SYNTHETIC_CASE7319')['results'])
        # A small committed write remains in WAL, not a rewritten database image.
        self.assertEqual(base, memory.path.read_bytes())
        for path in self.home.iterdir():
            self.assertNotIn(b'SECRET_SYNTHETIC', path.read_bytes())
            self.assertNotIn(b'SQLite format', path.read_bytes())
        with memory.transaction() as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
            plans = str([tuple(r) for r in db.execute("EXPLAIN QUERY PLAN SELECT id FROM events WHERE scope=? ORDER BY observed DESC LIMIT 20", ('project:memory_engine',))])
            self.assertIn('events_scope_observed', plans)
            self.assertNotIn('TEMP B-TREE', plans)

    def test_exception_rolls_back_evidence_and_indexes(self):
        memory = self.open()
        with patch('memory_engine.provenance.record', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                memory.ingest('ATOMICITY_MARKER')
        self.assertEqual(memory.recent(), [])
        self.assertEqual(memory.search('ATOMICITY_MARKER')['results'], [])

    def test_deletion_exception_does_not_commit_half_a_delete(self):
        memory = self.open()
        event = memory.ingest('KEEP_ME', source='source')['id']
        erase = memory._erase
        def failure(*args, **kwargs):
            erase(*args, **kwargs)
            raise OSError('fail after all deletion SQL before outer commit')
        with patch.object(memory, '_erase', side_effect=failure), self.assertRaises(OSError):
            memory.delete_source(event)
        self.assertEqual(memory.fetch(event)['content'], 'KEEP_ME')
        self.assertTrue(memory.search('KEEP_ME')['results'])
        self.assertTrue(memory.ingest('KEEP_ME', source='source')['duplicate'])

    def test_read_transaction_cannot_write(self):
        memory = self.open()
        with self.assertRaises(OperationalError), memory.transaction() as db:
            db.execute('DELETE FROM settings')
        self.assertEqual(memory.settings()['language'], 'auto')

    def test_settings_snapshots_do_not_alias_the_cache(self):
        memory = self.open()
        returned = memory.settings({'ignored_apps': ['private.exe']})
        returned['ignored_apps'].clear()
        self.assertEqual(memory.cached_settings['ignored_apps'], ['private.exe'])
        cached = memory.cached_settings
        cached['ignored_apps'].clear()
        self.assertEqual(memory.settings()['ignored_apps'], ['private.exe'])

    def test_new_store_also_refuses_retired_snapshot_clients(self):
        memory = self.open()
        self.assertEqual((self.home/'memory.bb').read_bytes(), LEGACY_FENCE)
        with self.assertRaises(ValueError):
            memory.cipher.decrypt((self.home/'memory.bb').read_bytes())
        self.assertIsNone(json.loads((self.home/'memory.storage.json').read_text())['backup'])

    def test_legacy_confirmed_claim_retains_exact_evidence_but_not_after_explicit_delete(self):
        memory = self.open()
        event = memory.ingest('Durable original evidence.', source='retain', observed='2020-01-01T00:00:00Z')['id']
        claim = memory.propose('Owner confirmed interpretation.', [event])
        memory.confirm(claim)
        self.assertEqual(memory.purge_expired(), 0)
        self.assertEqual(memory.fetch(event)['content'], 'Durable original evidence.')
        self.assertEqual(memory.delete_source(event), 1)
        self.assertEqual(memory.claims(), [])
        with self.assertRaises(ValueError):
            memory.ingest('Resurrection blocked.', source='retain')

    def test_lexical_lookup_drives_from_fts_and_remains_scoped(self):
        memory = self.open()
        wanted = memory.ingest('CASE-7319 public')['id']
        memory.ingest('CASE-7319 forbidden', scope='project:other')
        plans = []
        original = memory.store._db
        # Explain the actual executed statement via an explicit synthetic-only
        # diagnostic connection trace. Production never installs this callback.
        queries = []
        original.set_trace_callback(queries.append)
        try:
            self.assertEqual([r['id'] for r in memory.search('CASE-7319')['results']], [wanted])
        finally:
            original.set_trace_callback(None)
        statement = next(q for q in queries if 'bm25(lexical_v2)' in q)
        with memory.transaction() as db:
            plans = [r[3] for r in db.execute('EXPLAIN QUERY PLAN '+statement)]
        self.assertIn('lexical_v2 VIRTUAL TABLE', plans[0])
        self.assertTrue(any('INTEGER PRIMARY KEY' in plan for plan in plans))

    def test_missing_and_invalid_key_never_replaced(self):
        memory = self.open()
        memory.close()
        original = memory.path.read_bytes()
        with self.assertRaises(RuntimeError):
            Memory(self.home)
        self.assertFalse((self.home / 'key.dpapi').exists())
        with self.assertRaises(DatabaseError):
            Memory(self.home, test_key=b'Z' * 32)
        self.assertEqual(original, memory.path.read_bytes())

    def test_corrupted_page_rejected_on_access(self):
        memory = self.open()
        memory.ingest('INTEGRITY')
        memory.close()
        raw = bytearray(memory.path.read_bytes())
        raw[100] ^= 0x80
        memory.path.write_bytes(raw)
        with self.assertRaises(DatabaseError):
            self.open()
        self.assertEqual(bytes(raw), memory.path.read_bytes())

    def test_migration_is_explicit_backed_up_and_preserves_citations(self):
        old = self.legacy()
        with self.assertRaises(MigrationRequired):
            self.open()
        memory = self.open(migrate_legacy=True)
        self.assertEqual((self.home / 'memory.bb').read_bytes(), LEGACY_FENCE)
        self.assertEqual(next(self.home.glob('*.bb.bak')).read_bytes(), old)
        with self.assertRaises(ValueError):
            Cipher(self.home, self.key).decrypt((self.home / 'memory.bb').read_bytes())
        self.assertEqual(memory.search('CASE-7319')['results'][0]['citation'], 'memory-engine://evidence/original#0:27')
        self.assertEqual(memory.fetch('original')['provenance']['first']['action'], 'unknown')
        with self.assertRaises(ValueError):
            memory.ingest('no resurrection', source='deleted')

    def test_invalid_legacy_key_cannot_publish_or_fence_original(self):
        old = self.legacy()
        with self.assertRaises(InvalidTag):
            Memory(self.home, test_key=b'Z'*32, migrate_legacy=True)
        self.assertEqual((self.home/'memory.bb').read_bytes(), old)
        self.assertFalse((self.home/'memory.db').exists())
        self.assertFalse((self.home/'memory.storage.json').exists())

    def test_migration_interruption_and_disk_publish_failure_restart(self):
        old = self.legacy()
        def fail(db):
            db.execute('CREATE TABLE incomplete(id)')
            raise RuntimeError('interrupted')
        with patch('memory_engine.layers.migrate', side_effect=fail), self.assertRaises(RuntimeError):
            self.open(migrate_legacy=True)
        self.assertEqual(old, (self.home / 'memory.bb').read_bytes())
        self.assertFalse((self.home / 'memory.db').exists())
        replace = os.replace
        def disk_fail(src, dest):
            if Path(dest).name == 'memory.db':
                raise OSError('synthetic disk write failure at publication')
            return replace(src, dest)
        with patch('memory_engine.storage.os.replace', side_effect=disk_fail), self.assertRaises(OSError):
            self.open(migrate_legacy=True)
        self.assertEqual(old, (self.home / 'memory.bb').read_bytes())
        self.assertEqual(self.open(migrate_legacy=True).fetch('original')['id'], 'original')

    def test_changed_old_vault_is_not_silently_ignored(self):
        old = self.legacy()
        memory = self.open(migrate_legacy=True)
        (self.home / 'memory.bb').write_bytes(old + b'old writer')
        with self.assertRaises(StorageError):
            memory.recent()

    def test_crash_after_publication_before_legacy_fence_can_resume(self):
        old = self.legacy()
        with patch('memory_engine.storage.EncryptedStore._fence_legacy', side_effect=OSError('power loss')), self.assertRaises(OSError):
            self.open(migrate_legacy=True)
        self.assertTrue((self.home / 'memory.db').exists())
        self.assertEqual((self.home / 'memory.bb').read_bytes(), old)
        memory = self.open(migrate_legacy=True)
        self.assertEqual(memory.fetch('original')['id'], 'original')
        self.assertEqual((self.home / 'memory.bb').read_bytes(), LEGACY_FENCE)

    def test_process_death_mid_migration_restarts_from_verified_backup(self):
        old = self.legacy()
        script = '''
import os,sys
from pathlib import Path
from unittest.mock import patch
from memory_engine.memory import Memory
def crash(db):
    db.execute('CREATE TABLE half_migrated(id)')
    os._exit(71)
with patch('memory_engine.layers.migrate',crash):
    Memory(Path(sys.argv[1]),test_key=b'F'*32,migrate_legacy=True)
'''
        child = subprocess.run([sys.executable, '-c', script, str(self.home)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 71)
        self.assertEqual((self.home/'memory.bb').read_bytes(), old)
        self.assertEqual(next(self.home.glob('*.bb.bak')).read_bytes(), old)
        memory = self.open(migrate_legacy=True)
        self.assertEqual(memory.fetch('original')['id'], 'original')
        self.assertTrue(memory.store.check_integrity())

    @unittest.skipUnless(os.name == 'nt', 'DPAPI key custody is Windows-only')
    def test_migration_cli_on_synthetic_dpapi_vault(self):
        old = self.legacy()
        (self.home/'key.dpapi').write_bytes(protect(self.key))
        child = subprocess.run([sys.executable, 'run.py', 'migrate', '--home', str(self.home)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors='replace'))
        self.assertIn(b'Original encrypted bytes', child.stdout)
        self.assertEqual(next(self.home.glob('*.bb.bak')).read_bytes(), old)
        self.assertEqual(self.open().fetch('original')['id'], 'original')

    def test_missing_active_database_does_not_fall_back(self):
        self.legacy()
        memory = self.open(migrate_legacy=True)
        memory.close()
        memory.path.unlink()
        with self.assertRaises(StorageError):
            self.open(migrate_legacy=True)

    def test_concurrent_instances_keep_all_writes(self):
        first, second = self.open(), self.open()
        def write(index):
            memory = first if index % 2 else second
            return memory.ingest(f'CONCURRENT {index}', source=f'doc-{index}')['id']
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(write, range(40)))
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual(first.status()['events'], 40)

    def test_revocation_is_visible_to_an_already_open_second_store(self):
        owner, reader = self.open(), self.open()
        owner.ingest('AUTHORIZATION_MARKER')
        grant = owner.grant('reader', ['project:memory_engine'])
        self.assertTrue(reader.search('AUTHORIZATION_MARKER', token=grant['token'])['results'])
        owner.revoke(grant['id'])
        with self.assertRaises(PermissionError):
            reader.search('AUTHORIZATION_MARKER', token=grant['token'])

    def test_separate_processes_write_and_checkpoint_without_lost_events(self):
        memory = self.open()
        script = '''
import sys
from pathlib import Path
from memory_engine.memory import Memory
with Memory(Path(sys.argv[1]),test_key=b'F'*32) as m:
    for i in range(15):
        m.ingest('MULTIPROCESS '+sys.argv[2]+' '+str(i))
        m.store.checkpoint()
'''
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.home), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        try:
            for process in processes:
                _, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error.decode(errors='replace'))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertEqual(memory.status()['events'], 30)
        self.assertTrue(memory.store.check_integrity())

    def test_disk_full_rolls_back_real_sqlite_pages(self):
        memory = self.open()
        with memory.transaction(write=True) as db:
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            db.execute(f'PRAGMA max_page_count={pages+1}')
        with self.assertRaises(DatabaseError):
            memory.ingest('FULL_DISK_MARKER ' + 'x' * 500000)
        self.assertEqual(memory.status()['events'], 0)
        self.assertEqual(memory.search('FULL_DISK_MARKER')['results'], [])

    def test_process_death_rolls_back_and_committed_wal_recovers(self):
        self.open().close()
        script = '''
import os,sys
from pathlib import Path
from memory_engine.memory import Memory
m=Memory(Path(sys.argv[1]),test_key=b'F'*32)
m.ingest('COMMITTED_BEFORE_CRASH',source='committed')
with m.transaction(write=True) as db:
    db.execute("INSERT INTO settings VALUES ('uncommitted','true')")
    os._exit(79)
'''
        child = subprocess.run([sys.executable, '-c', script, str(self.home)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 79)
        reopened = self.open()
        self.assertTrue(reopened.search('COMMITTED_BEFORE_CRASH')['results'])
        self.assertNotIn('uncommitted', reopened.settings())


if __name__ == '__main__':
    unittest.main()
