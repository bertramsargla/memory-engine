import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from sqlcipher3.dbapi2 import DatabaseError

from memory_engine.crypto import protect
from memory_engine.knowledge import Knowledge, route_activity
from memory_helpers import Memory, MemoryDirectory, logical_contents
from memory_engine.mcp import Server
from memory_engine.privacy import redact


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = MemoryDirectory()
        self.home = Path(self.temp.name)
        self.key = bytes(range(32))
        self.memory = Memory(self.home, test_key=self.key)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, text="Project ALPHA-724 starts on Tuesday.", **kwargs):
        return self.memory.ingest(text, **kwargs)['id']

    def test_encrypted_restart_and_index(self):
        self.add()
        for path in self.home.iterdir():
            self.assertNotIn(b"ALPHA", path.read_bytes())
            self.assertNotIn(b"SQLite format", path.read_bytes())
        reopened = Memory(self.home, test_key=self.key)
        result = reopened.search("ALPHA-724")['results']
        self.assertEqual(len(result), 1)
        self.assertIn('Tuesday', reopened.fetch(result[0]['id'])['content'])

    def test_tampering_and_wrong_key_fail_without_replacement(self):
        self.add()
        self.memory.close()  # authenticate fresh disk pages, not a live page cache
        original = self.memory.path.read_bytes()
        with self.assertRaises(DatabaseError):
            Memory(self.home, test_key=b'X'*32)
        self.assertEqual(self.memory.path.read_bytes(), original)
        tampered = original[:100] + bytes([original[100] ^ 1]) + original[101:]
        self.memory.path.write_bytes(tampered)
        with self.assertRaises(DatabaseError):
            Memory(self.home, test_key=self.key)
        self.assertEqual(self.memory.path.read_bytes(), tampered)

    def test_dedup_revision_scope_and_original_observation(self):
        first = self.memory.ingest('same', source='source', observed='2026-09-01T12:00:00+03:00')
        repeat = self.memory.ingest('same', source='source')
        other = self.memory.ingest('same', source='source', scope='project:other')
        revision = self.memory.ingest('changed', source='source')
        self.assertEqual(first['id'], repeat['id'])
        self.assertTrue(repeat['duplicate'])
        self.assertNotEqual(first['id'], other['id'])
        self.assertNotEqual(first['id'], revision['id'])
        self.assertEqual(self.memory.fetch(first['id'])['observed'], '2026-09-01T09:00:00+00:00')

    def test_scope_is_enforced_on_search_fetch_recent_and_status(self):
        self.add('public alpha')
        secret = self.add('other private secret', scope='project:other')
        token = self.memory.grant('test', ['project:memory_engine'])['token']
        self.assertEqual(self.memory.search('secret', token=token)['results'], [])
        with self.assertRaises(PermissionError):
            self.memory.search('secret', token=token, scope='project:other')
        with self.assertRaises(KeyError):
            self.memory.fetch(secret, token=token)
        with self.assertRaises(PermissionError):
            self.memory.recent(scope='project:other', token=token)
        self.assertEqual(self.memory.status(token=token)['events'], 1)

    def test_revocation_applies_to_existing_connection(self):
        grant = self.memory.grant('test', ['project:memory_engine'])
        server = Server(self.memory, grant['token'], 'project:memory_engine')
        server.dispatch({'method': 'tools/list'})
        self.memory.revoke(grant['id'])
        for method in ('tools/list', 'resources/list', 'ping'):
            with self.assertRaises(PermissionError):
                server.dispatch({'method': method})

    def test_citations_preserve_multibyte_text_and_pagination(self):
        text = ('Tere õhtust 🦋! ' * 6000) + 'ZXV-9837'
        event = self.add(text)
        found = self.memory.search('ZXV-9837')['results'][0]
        self.assertEqual(text.encode()[found['start_byte']:found['end_byte']].decode(), found['text'])
        parts, offset = [], 0
        while offset is not None:
            page = self.memory.fetch(event, start_byte=offset)
            parts.append(page['content'])
            offset = page['next_start_byte']
        self.assertEqual(''.join(parts), text)
        with self.assertRaises(ValueError):
            self.memory.fetch(event, start_byte=6, end_byte=9)

    def test_delete_removes_all_revisions_claims_and_search_and_blocks_reimport(self):
        first = self.add('first unique-token', source='same-source')
        self.add('second unique-token', source='same-source')
        claim = self.memory.propose('Unverified fact', [first])
        self.memory.confirm(claim, 'Corrected fact')
        self.assertEqual(self.memory.delete_source(first), 2)
        self.assertEqual(self.memory.claims(), [])
        self.assertEqual(self.memory.search('unique-token')['results'], [])
        self.assertNotIn(b'unique-token', logical_contents(self.memory))
        self.assertNotIn(b'Corrected fact', logical_contents(self.memory))
        with self.assertRaises(KeyError):
            self.memory.fetch(first)
        with self.assertRaises(ValueError):
            self.add('new revision', source='same-source')

    def test_claim_cannot_reference_other_project(self):
        other = self.add('hidden', scope='project:other')
        with self.assertRaises(ValueError):
            self.memory.propose('This is true', [other])

    def test_claim_correction_keeps_history(self):
        event = self.add()
        original = self.memory.propose('Original interpretation', [event])
        self.assertEqual(self.memory.claims()[0]['status'], 'pending')
        corrected = self.memory.confirm(original, 'Corrected interpretation')
        claims = {r['id']: r for r in self.memory.claims()}
        self.assertEqual(claims[original]['status'], 'superseded')
        self.assertEqual(claims[corrected]['supersedes'], original)

    def test_retention_erases_old_evidence_and_claims(self):
        event = self.add('expired', observed='2020-01-01T00:00:00Z')
        self.memory.propose('Expired interpretation', [event])
        self.add('recent')
        self.assertEqual(self.memory.purge_expired(), 1)
        self.assertEqual(self.memory.claims(), [])
        self.assertEqual(self.memory.status()['events'], 1)

    def test_concurrent_instances_do_not_lose_writes(self):
        second = Memory(self.home, test_key=self.key)
        errors = []
        def write(store, prefix):
            try:
                for i in range(8):
                    store.ingest(f'{prefix} {i}')
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(self.memory, 'a')), threading.Thread(target=write, args=(second, 'b'))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.memory.status()['events'], 16)

    def test_literal_absence_does_not_generate_false_matches(self):
        self.add('MemoryEngine builds a Windows memory app.')
        self.assertEqual(self.memory.search('nonexistent elephant')['results'], [])
        self.assertEqual(self.memory.search('" OR * --')['results'], [])
        with self.assertRaises(ValueError):
            self.memory.search('test', limit=True)

    def test_knowledge_paths_and_resource_scopes(self):
        knowledge = Knowledge(self.memory)
        knowledge.seed('project:memory_engine')
        knowledge.save('project:other', 'private.md', 'private marker')
        with self.assertRaises(PermissionError):
            knowledge.read('project:memory_engine', '../project_other/private.md')
        grant = self.memory.grant('reader', ['project:memory_engine'])
        server = Server(self.memory, grant['token'], 'project:memory_engine')
        listed = server.dispatch({'method': 'resources/list'})
        self.assertNotIn('private.md', json.dumps(listed))
        result = server.call_tool('knowledge_read', {'path': 'skills/write-tweet/SKILL.md'})
        self.assertIn('write-tweet', result['text'])
        with self.assertRaises(ValueError):
            server.call_tool('knowledge_read', {'path': 'private.md', 'scope': 'project:other'})

    def test_knowledge_scope_names_cannot_collide(self):
        knowledge = Knowledge(self.memory)
        knowledge.save('a_b:c', 'note.md', 'first scope')
        knowledge.save('a:b_c', 'note.md', 'second scope')
        self.assertEqual(knowledge.read('a_b:c', 'note.md'), 'first scope')
        self.assertEqual(knowledge.read('a:b_c', 'note.md'), 'second scope')


class WindowsPolicyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'DPAPI key custody is Windows-only')
    def test_dpapi_roundtrip(self):
        data = b'synthetic DPAPI test, not a credential'
        encrypted = protect(data)
        self.assertNotIn(data, encrypted)
        self.assertEqual(protect(encrypted, decrypt=True), data)

    def test_sensitive_patterns_redacted(self):
        result = redact('password=hunter2 token: abc123 4111 1111 1111 1111 123-45-6789')
        for secret in ('hunter2', 'abc123', '4111', '6789'):
            self.assertNotIn(secret, result)

    def test_raw_tokens_in_terminal_text_are_redacted(self):
        text = ("export GH=ghp_" + "a" * 36 + " key sk-proj-" + "b" * 40 + " AIza" + "c" * 35 + " GOCSPX-" + "d" * 20
                + " -----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----")
        result = redact(text)
        for secret in ("a" * 36, "b" * 40, "c" * 35, "d" * 20, "abc"):
            self.assertNotIn(secret, result)
        self.assertIn("[REDACTED TOKEN]", result)

    def test_activity_is_overridable_and_unknown_stays_general(self):
        self.assertEqual(route_activity('chrome.exe', 'Inbox - Gmail'), 'Email reply')
        self.assertEqual(route_activity('chrome.exe', 'Home / X', 'Message reply'), 'Message reply')
        self.assertEqual(route_activity('unknown.exe', 'Unclassified'), 'General')


if __name__ == '__main__':
    unittest.main()
