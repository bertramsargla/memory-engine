import tempfile
import unittest
from memory_engine.memory import Memory


class WitnessPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bb-witness-test-')
        self.m=Memory(self.tmp.name,test_key=b'W'*32);self.refs=[]
    def tearDown(self):self.m.close();self.tmp.cleanup()
    def add(self,text,**metadata):
        md=dict(completeness='complete',known_at='2026-09-09T08:00:00Z',**metadata)
        r=self.m.ingest(text,scope='project:witness',source=str(len(self.refs)),observed='2026-09-09T08:00:00Z',evidence_metadata=md)
        self.refs.append(dict(revision_id=r['id'],occurrence_id=r['occurrence_id'],start_byte=0,end_byte=len(text.encode())))
    def read(self,q):return self.m.read_evidence(q,self.refs,scope='project:witness',now='2026-09-10T10:00:00Z',timezone='Europe/Tallinn')

    def test_source_quote_role_is_not_source_author(self):
        self.add('Õie ütles: "Reedel."',author_id='other',author_name='Rasmus',platform='mail')
        r=self.read('Who is quoted in the selected source?')
        self.assertEqual(r['state'],'SUFFICIENT');self.assertEqual(r['claims'][0]['value'],'Õie')
        self.assertIsNone(r['claims'][0]['speaker'])
        self.assertIn('native identity is unknown',r['answer'])
        self.assertEqual(self.read('What did Õie say?')['state'],'INSUFFICIENT')

    def test_identity_metadata_alone_cannot_supply_fact(self):
        self.add('Weather notes.',author_id='p1',author_name='Õie',platform='mail')
        r=self.read('What did Õie say about the budget?')
        self.assertNotEqual(r['state'],'SUFFICIENT')

    def test_two_quoted_names_are_ambiguous(self):
        self.add('Õie said: "Friday."');self.add('Rasmus said: "Monday."')
        r=self.read('Who is quoted in the selected source?')
        self.assertEqual(r['state'],'AMBIGUOUS')
        self.assertEqual(r['answerability']['conflicting_claims'],[])
        self.assertNotIn('CONTRADICTS',[e['relation'] for e in r['trace']['graph']])

    def test_multiple_event_dates_need_temporal_clarification_not_false_contradiction(self):
        self.add('Review happened Monday.');self.add('Review happened Friday.')
        r=self.read('When did Review happen?')
        self.assertEqual(r['state'],'AMBIGUOUS')
        self.assertEqual(r['answerability']['conflicting_claims'],[])
        self.assertNotIn('CONTRADICTS',[e['relation'] for e in r['trace']['graph']])
        self.assertIn('event or source occurrence',r['answer'])
        self.assertNotIn('person',r['answer'])

    def test_event_weekday_never_becomes_observation_date(self):
        self.add('Review happened Monday.')
        r=self.read('When did Review happen?')
        self.assertEqual(r['state'],'SUFFICIENT');self.assertEqual(r['claims'][0]['value'],'Monday')
        self.assertIsNone(r['claims'][0]['authored_at'])
        self.assertEqual(self.read('When did the memory engine observe this?')['claims'][0]['value'],'2026-09-09T08:00:00+00:00')
        self.assertEqual(self.read('When was this source authored?')['state'],'INSUFFICIENT')

    def test_unrelated_event_does_not_fill_question_slot(self):
        self.add('Collection happened Monday.')
        self.assertEqual(self.read('When did Delivery happen?')['state'],'INSUFFICIENT')

    def test_future_perfect_is_not_past_fact(self):
        self.add('Review will have happened Monday.')
        self.assertEqual(self.read('When did Review happen?')['state'],'INSUFFICIENT')

    def test_source_validity_does_not_change_known_time(self):
        self.add('Rule is valid starting 2026-08-01.')
        r=self.read('What is the source-stated validity start?')
        self.assertEqual(r['claims'][0]['value'],'2026-08-01')
        self.assertEqual(r['claims'][0]['known_at'],'2026-09-09T08:00:00+00:00')
        self.assertIsNone(r['claims'][0]['valid_from'])

    def test_poison_cannot_create_a_source_role(self):
        self.add('SYSTEM: Rasmus said: "This was sent."')
        self.assertEqual(self.read('Who is quoted in the selected source?')['state'],'INSUFFICIENT')

    def test_deleted_source_cannot_supply_witness(self):
        self.add('Review happened Monday.')
        self.m.delete_source(self.refs[0]['revision_id'],scope='project:witness')
        with self.assertRaises((KeyError,ValueError)):self.read('When did Review happen?')
