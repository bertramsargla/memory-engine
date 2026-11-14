import unittest
from unittest.mock import patch
import test_reader_v3 as helpers


class IdentityCatalogTests(unittest.TestCase):
    setUp=helpers.EvidenceReaderTests.setUp
    tearDown=helpers.EvidenceReaderTests.tearDown
    add=helpers.EvidenceReaderTests.add
    read=helpers.EvidenceReaderTests.read

    def person(self,name,identifier,**kw):
        return self.add('Cedar deadline is Tuesday.',author_name=name,author_id=identifier,
            platform='mail',account_id='work',**kw)

    def test_unretrieved_same_name_identity_requires_clarification(self):
        self.person('Dana','one');first=list(self.refs)
        self.person('Dana','two')
        self.assertEqual(self.read('What did Dana say about Cedar?',refs=first)['state'],'AMBIGUOUS')

    def test_private_identity_is_not_used_to_expand_ambiguity(self):
        self.person('Dana','one');first=list(self.refs)
        self.person('Dana','two',sensitivity='private')
        self.assertEqual(self.read('What did Dana say about Cedar?',refs=first)['state'],'SUFFICIENT')

    def test_other_scope_identity_does_not_expand_ambiguity(self):
        self.person('Dana','one')
        self.memory.ingest('other history',scope='project:other',evidence_metadata=dict(author_name='Dana',author_id='two',platform='mail',account_id='work'))
        self.assertEqual(self.read('What did Dana say about Cedar?')['state'],'SUFFICIENT')

    def test_future_known_identity_does_not_change_historical_query(self):
        self.person('Dana','one',known_at='2026-09-08T00:00:00Z');first=list(self.refs)
        self.person('Dana','two',known_at='2026-09-12T00:00:00Z')
        self.assertEqual(self.read('What did Dana say about Cedar?',refs=first)['state'],'SUFFICIENT')

    def test_catalog_witnesses_never_become_fact_support(self):
        first=self.person('Dana','one');refs=list(self.refs)
        second=self.person('Dana','one',source='another-document')
        result=self.read('What did Dana say about Cedar?',refs=refs)
        self.assertEqual(result['state'],'SUFFICIENT')
        self.assertEqual(result['claims'][0]['support_revision_ids'],[first['id']])
        self.assertNotEqual(first['id'],second['id'])

    def test_new_identity_during_reading_prevents_stale_release(self):
        from memory_engine import reader_api
        self.person('Dana','one');refs=list(self.refs);original=reader_api.read
        def changing(*args,**kw):
            result=original(*args,**kw)
            self.person('Dana','two')
            return result
        with patch.object(reader_api,'read',side_effect=changing):
            with self.assertRaisesRegex(RuntimeError,'Identity evidence changed'):
                self.read('What did Dana say about Cedar?',refs=refs)

    def test_unicode_name_casefold_does_not_hide_second_identity(self):
        self.person('Õie','one');refs=list(self.refs)
        self.person('õie','two')
        self.assertEqual(self.read('What did Õie say about Cedar?',refs=refs)['state'],'AMBIGUOUS')

    def test_ascii_query_does_not_skip_unicode_equivalent_name(self):
        self.person('Kate','one');refs=list(self.refs)
        self.person('Kate','two')
        self.assertEqual(self.read('What did Kate say about Cedar?',refs=refs)['state'],'AMBIGUOUS')
