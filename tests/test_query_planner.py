from datetime import datetime,timezone
from pathlib import Path
import unittest
from memory_engine.query import parse_query
from memory_helpers import Memory,MemoryDirectory


class QueryPlannerTests(unittest.TestCase):
    def test_natural_lookup_removes_grammar(self):
        q=parse_query('Where did I see Aster Labs?')
        self.assertEqual(q.lexical_terms,('Aster','Labs'))
        self.assertEqual(q.intent,'locate_source')

    def test_yesterday_uses_tallinn_local_day(self):
        q=parse_query('What did Mira tell me yesterday?',now='2026-09-08T00:30:00+03:00',tz='Europe/Tallinn')
        self.assertEqual(q.lexical_terms,('Mira',))
        self.assertEqual(q.temporal_interval,('2026-09-06T21:00:00+00:00','2026-09-07T21:00:00+00:00'))
        self.assertEqual(q.requested_provenance,('speaker',))

    def test_historical_known_at_is_not_latest(self):
        q=parse_query('What deadline did I believe on Tuesday?',now='2026-09-10T12:00:00Z')
        self.assertEqual(q.intent,'historical_state'); self.assertEqual(q.temporal_mode,'known_at_time')
        self.assertEqual(q.lexical_terms,('deadline',))

    def test_current_candidate_values_remain_content(self):
        q=parse_query('Which deadline is current, Monday or Friday?')
        self.assertEqual(q.temporal_mode,'current');self.assertIn('Monday',q.lexical_terms);self.assertIn('Friday',q.lexical_terms)

    def test_action_verification_does_not_infer_outcome(self):
        q=parse_query('Did I actually send the contract?')
        self.assertEqual(q.intent,'verify_action');self.assertIn('action_state',q.requested_provenance)

    def test_identifiers_and_quotes_are_literal_data(self):
        q=parse_query('Find CASE-7319 and "source of truth"')
        self.assertEqual(q.exact_identifiers,('CASE-7319',));self.assertEqual(q.phrases,('source of truth',))
        self.assertIn('source of truth',q.semantic_query)

    def test_estonian_and_negation_preserve_semantic_query(self):
        q=parse_query('Kus ma nägin Aster Labs?')
        self.assertEqual(q.language,'et');self.assertEqual(q.lexical_terms,('Aster','Labs'))
        q=parse_query('Why did we not approve the contract?')
        self.assertIn('not',q.lexical_terms);self.assertEqual(q.semantic_query,q.raw_text)

    def test_activity_sequence_has_unresolved_anchor(self):
        q=parse_query('What was I researching before I opened the roadmap?')
        self.assertEqual(q.temporal_mode,'sequence');self.assertEqual(q.sequence_anchor,'roadmap')

    def test_invalid_clock_and_query(self):
        with self.assertRaises(ValueError):parse_query('hello',now=datetime(2026,9,1))
        with self.assertRaises(ValueError):parse_query('x'*2001)

    def test_real_api_uses_typed_terms_and_checks_scope(self):
        with MemoryDirectory() as d:
            m=Memory(Path(d),test_key=b'Q'*32)
            r=m.ingest('Aster Labs makes navigation tools.')
            self.assertEqual(m.search('Where did I see Aster Labs?')['results'][0]['id'],r['id'])
            g=m.grant('reader',['project:other'])
            with self.assertRaises(PermissionError):m.search('Aster Labs',token=g['token'])
