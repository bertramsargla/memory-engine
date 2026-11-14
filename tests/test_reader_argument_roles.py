import unittest
from memory_engine.reader_argument_roles import frames


class ArgumentRoleTests(unittest.TestCase):
    def test_active_passive_and_object_first_preserve_roles(self):
        for text in ('Selma approved Thursday.','Thursday was approved by Selma.',
                     'Thursday, Selma approved.','On Tuesday, Selma approved Thursday.'):
            frame=list(frames(text))[0]
            self.assertEqual((frame.actor,frame.relation,frame.object),('Selma','approved','Thursday'))
            self.assertIsNone(frame.speaker_of_source)
            for _,a,b,value in frame.roles:self.assertEqual(text.encode()[a:b].decode(),value)

    def test_estonian_overt_agent_and_object_case(self):
        for text in ('Selma poolt kinnitati uus hind.','Uue hinna kinnitas Selma.'):
            frame=list(frames(text))[0]
            self.assertEqual(frame.actor,'Selma');self.assertEqual(frame.relation,'approved')
            self.assertIn('hin',frame.object.casefold())
            self.assertIsNone(frame.speaker_of_source)

    def test_negated_passive_and_active_do_not_reverse_polarity(self):
        for text in ('Thursday was not approved by Selma.','Selma did not approve Thursday.',
                     'Selma poolt ei kinnitatud uut hinda.','Selma ei kiitnud heaks uut hinda.'):
            self.assertEqual(list(frames(text))[0].polarity,'negative',text)

    def test_modals_remain_possible(self):
        for text in ('Selma might approve Thursday.','Thursday might have been approved by Selma.'):
            self.assertEqual(list(frames(text))[0].epistemic_strength,'POSSIBLE')

    def test_quote_and_report_do_not_acquire_outer_author(self):
        for text in ('> Thursday was approved by Selma.','A newspaper said Selma approved Thursday.',
                     'Taavi sõnul kinnitas Selma uue hinna.','Forwarded: Selma approved Thursday.'):
            self.assertEqual(list(frames(text)),[],text)

    def test_promised_delivery_and_oblique_recipient_do_not_verify_action(self):
        self.assertEqual(list(frames('Katile lubati dokument saata reedel.')),[])

    def test_unknown_actor_names_are_not_merged_or_resolved(self):
        frame=list(frames('Selma approved Thursday.'))[0]
        self.assertIn('identity_unresolved',frame.attribution)

    def test_decimal_amount_is_not_a_sentence_boundary(self):
        frame=list(frames('Selma approved 42.50 EUR.'))[0]
        self.assertEqual(frame.object,'42.50 EUR')
