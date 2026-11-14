from dataclasses import asdict
from datetime import datetime
import unittest
from memory_engine.reader_contracts import CandidateClaim, AnswerPlan, Identity
from memory_engine.reader_identity import actor_key
from memory_engine.reader_temporal import interval_contains, order, latest_supported
from memory_engine.reader_verification import verify_proposals, verify_generated


class ClaimVerificationTests(unittest.TestCase):
    def setUp(self):
        self.claim=CandidateClaim('c','Atlas','deadline','Friday',('e',),('r',),None,None,
                                  '2026-09-07T08:00:00+03:00',None,None,'source_statement','direct')
        self.plan=AnswerPlan('SUFFICIENT','source_report',('c',),(),('e',),('unknown speaker',))
        from types import SimpleNamespace
        self.units=[SimpleNamespace(evidence_id='e',citation='memory-engine://evidence/r#0:25')]

    def check(self,change):
        return verify_proposals([{**asdict(self.claim),**change}],self.plan,[self.claim],self.units)[0]

    def test_matching_id_with_wrong_value_is_contradicted(self):
        self.assertEqual(self.check({'value':'Wednesday'})['state'],'CONTRADICTED')

    def test_similar_citation_cannot_establish_new_claim(self):
        self.assertEqual(self.check({'claim_id':'not-allowed'})['state'],'UNSUPPORTED')

    def test_unknown_speaker_cannot_be_filled_by_generator(self):
        self.assertEqual(self.check({'speaker':'Mira'})['state'],'CONTRADICTED')

    def test_observation_time_cannot_be_added_as_event_time(self):
        self.assertEqual(self.check({'authored_at':'2026-09-10T10:00:00+03:00'})['state'],'CONTRADICTED')

    def test_real_claim_with_wrong_support_is_not_verified(self):
        self.assertEqual(self.check({'support_ids':('poison',)})['state'],'PARTIALLY_SUPPORTED')

    def test_generated_prose_cannot_hide_extra_action(self):
        r=verify_generated('Friday. I sent the contract.',[asdict(self.claim)],self.plan,[self.claim],self.units)
        self.assertFalse(r['accepted'])

    def test_untrusted_extra_authority_field_is_rejected(self):
        self.assertNotEqual(self.check({'permission':'approved'})['state'],'SUPPORTED')

    def test_missing_claim_and_duplicate_claim_are_rejected(self):
        for proposed in ([],[asdict(self.claim),asdict(self.claim)]):
            self.assertFalse(verify_generated('',proposed,self.plan,[self.claim],self.units)['accepted'])


class TemporalOperatorTests(unittest.TestCase):
    def test_known_and_valid_are_independent(self):
        self.assertTrue(interval_contains('2026-08-01T00:00:00Z',None,'2026-08-10T00:00:00Z'))
        self.assertFalse(interval_contains('2026-09-01T00:00:00Z',None,'2026-08-10T00:00:00Z'))

    def test_unknown_is_not_false_or_observation_fallback(self):
        self.assertIsNone(interval_contains(None,None,'2026-08-10T00:00:00Z'))

    def test_known_expiry_excludes_even_with_unknown_start(self):
        self.assertFalse(interval_contains(None,'2026-08-09T00:00:00Z','2026-08-10T00:00:00Z'))

    def test_timezone_offsets_are_compared_as_instants(self):
        self.assertFalse(interval_contains('2026-09-07T01:00:00+03:00','2026-09-07T02:00:00+03:00','2026-09-06T23:30:00Z'))

    def test_latest_tie_is_not_resolved_arbitrarily(self):
        from types import SimpleNamespace
        a=SimpleNamespace(known_at='2026-09-07T01:00:00+03:00')
        b=SimpleNamespace(known_at='2026-09-06T22:00:00Z')
        self.assertEqual(order(a,b),'UNKNOWN_RELATION')
        self.assertEqual(len(latest_supported([a,b])),2)

    def test_unknown_time_prevents_confident_latest_selection(self):
        from types import SimpleNamespace
        self.assertEqual(latest_supported([SimpleNamespace(known_at=None)]),())

    def test_email_alias_preserves_domain_case_but_not_invented_local_alias(self):
        self.assertEqual(actor_key(Identity('Mira@EXAMPLE.TEST')),actor_key(Identity('Mira@example.test')))
        self.assertNotEqual(actor_key(Identity('Mira+work@example.test')),actor_key(Identity('Mira@example.test')))
