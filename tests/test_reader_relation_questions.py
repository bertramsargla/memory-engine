from dataclasses import replace
import unittest
from memory_engine.reader_relation_query import parse
from memory_engine.evidence_reader import read
from memory_engine.query import parse_query
from memory_engine.reader_contracts import EvidenceUnit,Identity


def unit(text,eid='e',source='s'):
    return EvidenceUnit(eid,source,'r-'+eid,1,0,len(text.encode()),text,'memory-engine://evidence/r-'+eid+'#0:'+str(len(text.encode())),
        's', 'document',Identity(),None,None,None,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',None,
        '2026-01-01T00:00:00Z',None,None,None,None,'complete',(),'untrusted','third_party','original','hash-'+eid)


class SourceRelationQuestions(unittest.TestCase):
    def answer(self,q,*texts):
        p=parse_query(q,scope='s',now='2026-02-01T00:00:00Z')
        return read(p,[unit(t,str(i),'s'+str(i)) for i,t in enumerate(texts)])

    def test_roles_generalize_over_active_passive_and_inversion(self):
        for text,q in [('Felix approved the schedule.','What did Felix approve?'),
                       ('The schedule was approved by Felix.','Who approved the schedule?'),
                       ('The schedule, Felix approved.','What did Felix approve?')]:
            r=self.answer(q,text);self.assertEqual(r['state'],'SUFFICIENT')
            self.assertEqual((r['claims'][0]['subject'],r['claims'][0]['value']),('Felix','schedule'))
            self.assertEqual(r['claims'][0]['assertion_kind'],'source_statement')

    def test_suggestion_is_not_approval(self):
        self.assertEqual(self.answer('What did Felix approve?','Felix suggested the schedule.')['state'],'INSUFFICIENT')
        self.assertEqual(self.answer('What did Felix suggest?','Felix suggested the schedule.')['state'],'SUFFICIENT')

    def test_decision_is_not_successful_execution(self):
        r=self.answer('What did the group decide?','The group decided to defer the release.')
        self.assertEqual(r['claims'][0]['value'],'defer the release')
        self.assertFalse(any(c['assertion_kind']=='adapter_outcome' for c in r['claims']))

    def test_verification_still_needs_adapter_outcome(self):
        self.assertIsNone(parse('Did Felix actually approve the schedule?'))
        self.assertEqual(self.answer('Did Felix approve the schedule?','Felix approved the schedule.')['state'],'INSUFFICIENT')

    def test_negation_uncertainty_and_reported_speech_do_not_become_positive(self):
        for text in ['Felix did not approve the schedule.','Felix might approve the schedule.',
                     'Rita said Felix approved the schedule.','"Felix approved the schedule."']:
            self.assertEqual(self.answer('What did Felix approve?',text)['state'],'INSUFFICIENT')

    def test_names_across_sources_do_not_merge(self):
        r=self.answer('What did Felix approve?','Felix approved the schedule.','Felix approved the schedule.')
        self.assertEqual(r['state'],'AMBIGUOUS')

    def test_ambiguous_estonian_argument_order_is_not_guessed(self):
        from memory_engine.reader_argument_roles import frames
        self.assertEqual(list(frames('Aino kinnitas Rene.')),[])
        self.assertEqual(list(frames('Rene kinnitas Aino.')),[])

    def test_who_question_keeps_unknown_author(self):
        r=self.answer('Who approved the schedule?','The schedule was approved by Yara.')
        self.assertEqual(r['claims'][0]['subject'],'Yara');self.assertIsNone(r['claims'][0]['speaker'])


if __name__=='__main__':unittest.main()
