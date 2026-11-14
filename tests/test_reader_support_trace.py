import json
import unittest
from dataclasses import replace
from memory_engine.reader_contracts import Identity
from memory_engine.evidence_reader import read
from memory_engine.query import parse_query
from test_reader_relation_questions import unit


class SupportStageTraceTests(unittest.TestCase):
    def answer(self,text,question):
        return read(parse_query(question,scope='s',now='2026-02-01T00:00:00Z'),[unit(text)])

    def test_diagnostics_never_serialize_personal_surface_text(self):
        r=self.answer('ConfidentialOrchid budget is 7937 EUR.','What is the ConfidentialOrchid budget?')
        trace=r['trace']['support_stages'];serialized=json.dumps(trace)
        self.assertNotIn('ConfidentialOrchid',serialized);self.assertNotIn('7937',serialized)
        self.assertEqual(len(trace['stages']),13);self.assertFalse(trace['raw_content_included'])

    def test_unsupported_extraction_is_distinct_from_question_contract(self):
        r=self.answer('The proprietor of the parcel remains Ada.','What is the Ada budget?')
        stages={s['stage']:s for s in r['trace']['support_stages']['stages']}
        self.assertEqual(stages['question']['state'],'PRESENT')
        self.assertEqual(stages['linguistic_extraction']['state'],'MISSING')
        self.assertEqual(stages['verifier']['state'],'MISSING')

    def test_uncertainty_is_rejection_not_lost_capture(self):
        r=self.answer('Orchid deadline is possibly Friday.','What is the Orchid deadline?')
        stages={s['stage']:s for s in r['trace']['support_stages']['stages']}
        self.assertEqual(stages['candidate_evidence']['state'],'PRESENT')
        self.assertEqual(stages['certainty']['state'],'REJECTED')
        self.assertEqual(r['state'],'INSUFFICIENT')

    def test_closed_verification_does_not_claim_independent_entailment(self):
        r=self.answer('Orchid budget is 900 EUR.','What is the Orchid budget?')
        stage=next(s for s in r['trace']['support_stages']['stages'] if s['stage']=='verifier')
        self.assertEqual(stage['independent_semantic_verification'],'not_invoked')
        self.assertEqual(r['state'],'SUFFICIENT')

    def test_email_identity_keys_are_hashed(self):
        u=replace(unit('Orchid budget is 900 EUR.'),author_identity=Identity(native_id='private@secret.example',display_name='Nora',confidence=1))
        r=read(parse_query('What did Nora say about Orchid?',scope='s',now='2026-02-01T00:00:00Z'),[u])
        self.assertNotIn('private@secret.example',json.dumps(r['trace']['support_stages']))


if __name__=='__main__':unittest.main()
