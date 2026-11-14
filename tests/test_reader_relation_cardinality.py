import unittest
from memory_engine.evidence_reader import read
from memory_engine.reader_claims import extract,support_graph
from memory_engine.query import parse_query
from test_reader_relation_questions import unit


class RelationCardinalityTests(unittest.TestCase):
    def test_person_can_approve_more_than_one_object(self):
        units=[unit('Nils approved the schedule. Nils approved the revised budget.')]
        result=read(parse_query('What did Nils approve?',scope='s',now='2026-02-01T00:00:00Z'),units)
        self.assertEqual(result['state'],'SUFFICIENT')
        self.assertEqual({c['value'] for c in result['claims']},{'schedule','revised budget'})
        self.assertFalse(any(e.relation=='CONTRADICTS' for e in support_graph(extract(units))))

    def test_opposite_polarity_for_same_relation_still_conflicts(self):
        units=[unit('Nils approved the schedule. Nils did not approve the schedule.')]
        result=read(parse_query('What did Nils approve?',scope='s',now='2026-02-01T00:00:00Z'),units)
        self.assertEqual(result['state'],'CONFLICTING')

    def test_functional_deadline_values_still_conflict(self):
        units=[unit('Sorrel deadline is Tuesday. Sorrel deadline is Saturday.')]
        result=read(parse_query('What is the Sorrel deadline?',scope='s',now='2026-02-01T00:00:00Z'),units)
        self.assertEqual(result['state'],'CONFLICTING')


if __name__=='__main__':unittest.main()
