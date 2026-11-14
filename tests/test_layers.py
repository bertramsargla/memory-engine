import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory_engine.crypto import atomic_write
from memory_engine.layers import clean_context
from memory_engine.memory import SCHEMA
from memory_helpers import Memory, MemoryDirectory, install_legacy, logical_contents
from memory_engine.mcp import Server
from unittest.mock import patch


class ContextLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = MemoryDirectory()
        self.home = Path(self.temp.name)
        self.key = b'L'*32
        self.memory = Memory(self.home, test_key=self.key, migrate_legacy=True)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, at, text='Person: Ada Example\nTask: send the prototype tomorrow.', **kw):
        defaults = {'source': 'adapter-document:synthetic', 'kind': 'capture', 'app': 'notepad.exe',
                    'observed': at, 'context': {'stream_id': 'test-stream', 'focused_role': 'Edit'}}
        return self.memory.ingest(text, **(defaults | kw))['id']

    def test_schema_one_migration_preserves_cipher_evidence_and_grants(self):
        old = sqlite3.connect(':memory:')
        old.executescript(SCHEMA)
        old.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('old-id','project:memory_engine','old-source','old-rev','note','Old note','MemoryEngine','',
                     '2026-09-06T10:00:00+00:00','2026-09-06T10:00:00+00:00','Topic: Recovery\nGoal: preserve evidence','user_provided','complete'))
        old.execute('INSERT INTO grants(id,name,hash,scopes) VALUES (?,?,?,?)', ('grant', 'Synthetic grant', hashlib.sha256(b'synthetic-token').hexdigest(), '["project:memory_engine"]'))
        old.execute('INSERT INTO chunks VALUES (?,?,?,?,?)', (1, 'old-id', 0, 15, 'Topic: Recovery'))
        old.execute('INSERT INTO search_index(rowid,text) VALUES (1,?)', ('Topic: Recovery',))
        old.commit()
        legacy_path = install_legacy(self.memory, self.memory.cipher.encrypt(old.serialize()))
        old.close()
        migrated = Memory(self.home, test_key=self.key, migrate_legacy=True)
        self.assertEqual(migrated.status()['schema'], 10)
        self.assertTrue(migrated.search('Recovery', token='synthetic-token')['results'])
        self.assertIn('Recovery', migrated.fetch('old-id')['content'])
        self.assertEqual(migrated.organize(), {'processed': 1, 'remaining': 0})
        self.assertEqual(migrated.organize()['processed'], 0)
        self.assertEqual(len(migrated.layer_list('identity')), 1)
        self.assertNotIn(b'Recovery', migrated.path.read_bytes())

    def test_interrupted_migration_preserves_original_encrypted_vault(self):
        old = sqlite3.connect(':memory:')
        old.executescript(SCHEMA)
        legacy_path = install_legacy(self.memory, self.memory.cipher.encrypt(old.serialize()))
        old.close()
        before = (self.home / 'memory.bb').read_bytes() if (self.home / 'memory.bb').exists() else self.memory.path.read_bytes()
        def fail(db):
            db.execute('CREATE TABLE incomplete(id TEXT)')
            raise RuntimeError('synthetic migration failure')
        with patch('memory_engine.layers.migrate', side_effect=fail), self.assertRaises(RuntimeError):
            Memory(self.home, test_key=self.key, migrate_legacy=True)
        self.assertEqual(legacy_path.read_bytes(), before)
        self.assertEqual(Memory(self.home, test_key=self.key, migrate_legacy=True).status()['schema'], 10)

    def test_repeat_sightings_time_focus_and_sessions_without_invented_gap(self):
        first = self.capture('2026-09-06T10:00:00Z')
        self.assertEqual(self.capture('2026-09-06T10:00:08Z'), first)
        self.capture('2026-09-06T10:00:16Z', context={'stream_id': 'test-stream', 'focused_role': 'Document'})
        snapshot = self.memory.snapshot_details(first)
        self.assertEqual(snapshot['sightings'], 3)
        self.assertEqual(snapshot['duration_s'], 16)
        self.assertEqual(snapshot['context']['focused_role'], 'Document')
        self.assertEqual(len(self.memory.layer_list('sessions')), 1)
        self.capture('2026-09-06T11:00:00Z')
        self.assertEqual(self.memory.snapshot_details(first)['duration_s'], 16)
        self.assertEqual(len(self.memory.layer_list('sessions')), 2)
        self.capture('2026-09-06T11:00:08Z', context={'stream_id': 'after-pause'})
        self.assertEqual(self.memory.snapshot_details(first)['duration_s'], 16)
        self.assertEqual(len(self.memory.layer_list('sessions')), 3)

    def test_out_of_order_capture_never_rewinds_focus_or_last_seen(self):
        event = self.capture('2026-09-06T10:00:08Z')
        self.capture('2026-09-06T10:00:00Z', context={'stream_id': 'test-stream', 'focused_role': 'Button'})
        self.capture('2026-09-06T10:00:08Z')
        snapshot = self.memory.snapshot_details(event)
        self.assertEqual(snapshot['sightings'], 1)
        self.assertEqual(snapshot['context']['focused_role'], 'Edit')
        self.assertEqual(self.memory.fetch(event)['last_seen'], '2026-09-06T10:00:08+00:00')

    def test_time_splits_hour_midnight_and_never_counts_imports(self):
        self.capture('2026-09-06T23:59:56Z')
        self.capture('2026-09-07T00:00:04Z')
        self.memory.ingest('A large imported document', observed='2026-09-07T00:00:06Z')
        ledger = self.memory.layer_list('time')
        self.assertEqual({r['hour_utc']: r['duration_s'] for r in ledger}, {'2026-09-06T23': 4, '2026-09-07T00': 4})

    def test_chronicle_all_levels_repeats_and_iso_week_year(self):
        event = self.capture('2027-01-01T23:59:56Z')
        self.capture('2027-01-02T00:00:04Z')
        for level in ('hour', 'day', 'week', 'month', 'year'):
            rows = self.memory.chronicle(level)
            self.assertTrue(rows)
            self.assertEqual(rows[0]['evidence'][0]['event_id'], event)
            self.assertEqual(rows[0]['summary_kind'], 'extractive')
        self.assertEqual(self.memory.chronicle('week')[0]['period'], '2026-W53')
        self.assertEqual(len(self.memory.chronicle('day')), 2)
        self.assertEqual(self.memory.chronicle('year')[0]['snapshots'], 1)
        self.assertEqual(self.memory.chronicle('year')[0]['children'], [{'level': 'month', 'period': '2027-01'}])
        self.assertEqual(len(self.memory.chronicle('week')[0]['children']), 2)

    def test_wiki_updates_alias_links_and_stale_review(self):
        first = self.memory.ingest('Person: Ada Example\nCompany: Sample Works')['id']
        page = next(p for p in self.memory.layer_list() if p['kind'] == 'person')
        self.assertEqual(page['evidence_count'], 1)
        self.assertTrue(any(p['kind'] == 'company' for p in page['related']))
        self.memory.edit_wiki(page['id'], 'User-reviewed profile.', ['A. Example'])
        self.memory.ingest('A. Example proposed another prototype.')
        updated = self.memory.layer_record(page['id'])
        self.assertEqual(updated['evidence_count'], 2)
        self.assertEqual(updated['status'], 'needs_review')
        self.assertEqual(self.memory.layer_list(query='A. Example')[0]['id'], page['id'])
        self.assertFalse(any(r.get('name') == 'Ada Example' for r in self.memory.relevant_layers('Ada')))
        self.assertEqual(self.memory.link_entity('Ada Example', 'person', first), page['id'])

    def test_candidates_have_exact_utf8_spans_and_do_not_become_identity(self):
        text = 'Tere õhtust 🦋\n  Preference: use concise Estonian.  \nTask: verify the fictional partnership.\nWriting rule: no emojis.\nReminder: review tomorrow.'
        self.memory.ingest(text)
        for layer in ('identity', 'open_loops', 'writing', 'reminders'):
            row = self.memory.layer_list(layer)[0]
            self.assertEqual(row['status'], 'pending')
            self.assertEqual(text.encode()[row['start_byte']:row['end_byte']].decode(), row['text'])
            self.assertEqual(self.memory.layer_record(row['id'])['source_quote'], row['text'])
        self.assertFalse(any(r['layer'] == 'identity' for r in self.memory.relevant_layers('Estonian')))
        row = self.memory.layer_list('identity')[0]
        self.memory.review_item(row['id'], 'confirmed')
        self.assertTrue(any(r['layer'] == 'identity' for r in self.memory.relevant_layers('Estonian')))

    def test_corrections_close_and_due_date_preserve_lineage(self):
        self.memory.ingest('Task: send today.\nReminder: check release.')
        task = self.memory.layer_list('open_loops')[0]
        successor = self.memory.review_item(task['id'], 'confirmed', replacement='Send after review.')
        self.assertEqual(self.memory.layer_record(task['id'])['status'], 'superseded')
        self.assertEqual(self.memory.layer_record(successor)['supersedes'], task['id'])
        self.memory.review_item(successor, 'closed')
        self.assertEqual(self.memory.layer_record(successor)['status'], 'closed')
        reminder = self.memory.layer_list('reminders')[0]
        self.memory.review_item(reminder['id'], 'confirmed', due_at='2026-09-07T12:00:00+03:00')
        self.assertEqual(self.memory.layer_record(reminder['id'])['due_at'], '2026-09-07T09:00:00+00:00')
        with self.assertRaises(ValueError):
            self.memory.review_item(reminder['id'], 'confirmed', due_at='2026-09-07')

    def test_deletion_removes_all_layers_and_plaintext_residuals(self):
        event = self.capture('2026-09-06T10:00:00Z', text='Person: ERASEUNIQUEPERSON\nPreference: ERASEUNIQUEPREF',
                             context={'stream_id': 'x', 'focused_name': 'ERASEUNIQUEFOCUS'})
        page = next(p for p in self.memory.layer_list() if p['kind'] == 'person')
        self.memory.edit_wiki(page['id'], 'ERASEUNIQUESUMMARY', ['ERASEUNIQUEALIAS'])
        row = self.memory.layer_list('identity')[0]
        self.memory.review_item(row['id'], 'confirmed', replacement='ERASEUNIQUECORRECTION')
        self.memory.delete_source(event)
        for layer in ('wiki', 'identity', 'sessions', 'time'):
            self.assertEqual(self.memory.layer_list(layer), [])
        self.assertEqual(self.memory.chronicle(), [])
        self.assertNotIn(b'ERASEUNIQUE', logical_contents(self.memory))

    def test_partial_wiki_erasure_invalidates_summary_and_alias(self):
        first = self.memory.ingest('Person: Ada Example', source='first')['id']
        self.memory.ingest('Person: Ada Example', source='second')
        page = next(p for p in self.memory.layer_list() if p['kind'] == 'person')
        self.memory.edit_wiki(page['id'], 'ERASESUMMARYFROMFIRST', ['ERASEALIASFROMFIRST'])
        self.memory.delete_source(first)
        surviving = self.memory.layer_record(page['id'])
        self.assertEqual(surviving['evidence_count'], 1)
        self.assertEqual(surviving['status'], 'extractive')
        self.assertEqual(surviving['aliases'], [])
        raw = logical_contents(self.memory)
        self.assertNotIn(b'ERASESUMMARYFROMFIRST', raw)
        self.assertNotIn(b'ERASEALIASFROMFIRST', raw)

    def test_scope_and_live_revocation_cover_every_layer_endpoint(self):
        self.memory.ingest('Person: Allowed Example')
        hidden = self.memory.ingest('Person: Private Example', scope='project:other')['id']
        page = self.memory.layer_list(scope='project:other')[0]['id']
        grant = self.memory.grant('layers', ['project:memory_engine'])
        server = Server(self.memory, grant['token'], 'project:memory_engine')
        self.assertTrue(server.call_tool('memory_layers', {'layer': 'wiki'}))
        for name, args in [('memory_layer_record', {'ref': page}), ('memory_snapshot_context', {'event_id': hidden})]:
            with self.assertRaises(KeyError):
                server.call_tool(name, args)
        for fn in (lambda: self.memory.chronicle(scope='project:other', token=grant['token']),
                   lambda: self.memory.layer_list(scope='project:other', token=grant['token'])):
            with self.assertRaises(PermissionError):
                fn()
        self.memory.revoke(grant['id'])
        for name, args in [('memory_layers', {'layer': 'wiki'}), ('memory_chronicle', {'level': 'day'}), ('memory_context', {'query': 'Allowed'})]:
            with self.assertRaises(PermissionError):
                server.call_tool(name, args)

    def test_context_urls_strip_tokens_and_reject_credentials(self):
        context = clean_context({'url': 'https://example.test/thread/123?token=secret#private'})
        self.assertEqual(context['url'], 'https://example.test/thread/123')
        self.assertEqual(context['domain'], 'example.test')
        for value in ({'url': 'https://user:password@example.test'}, {'domain': 'fake.test'}, {'duration': 999}):
            with self.assertRaises(ValueError):
                clean_context(value)

    def test_failed_write_does_not_change_existing_ciphertext(self):
        before = self.memory.path.read_bytes()
        with self.assertRaises(ValueError):
            self.memory.ingest('test', context={'focused_role': 'x'*3000})
        self.assertEqual(self.memory.path.read_bytes(), before)

    def test_confirmed_identity_survives_retention_with_evidence_until_dismissed(self):
        event = self.memory.ingest('Preference: use Estonian.', observed='2020-01-01T00:00:00Z')['id']
        item = self.memory.layer_list('identity')[0]
        self.memory.review_item(item['id'], 'confirmed')
        self.assertEqual(self.memory.purge_expired(), 0)
        self.assertIn('Estonian', self.memory.fetch(event)['content'])
        self.memory.review_item(item['id'], 'dismissed')
        self.assertEqual(self.memory.purge_expired(), 1)
        self.assertEqual(self.memory.layer_list('identity'), [])

    def test_retention_uses_last_observation_not_first_sighting(self):
        event = self.memory.ingest('Old but revisited evidence', source='revisited', observed='2020-01-01T00:00:00Z')['id']
        self.memory.ingest('Old but revisited evidence', source='revisited')
        self.assertEqual(self.memory.purge_expired(), 0)
        self.assertEqual(self.memory.fetch(event)['observed'], '2020-01-01T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
