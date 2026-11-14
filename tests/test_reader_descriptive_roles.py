import unittest
from memory_engine.reader_language import relations
from memory_engine.reader_question_language import functional
from memory_engine.reader_claims import MEETING_ET


class DescriptiveRolesTests(unittest.TestCase):
    def test_explicit_owner_frames_have_original_spans(self):
        for text,subject,value in [('For the Juniper audit, Lena is the owner.','Juniper audit','Lena'),
                                   ('Nurme aruande eest vastutab Leelo.','Nurme aruande','Leelo')]:
            parsed=list(relations(text))[0]
            self.assertEqual((parsed.subject,parsed.predicate,parsed.value),(subject,'owner',value))
            for _,a,b,span in parsed.roles:self.assertEqual(text.encode()[a:b].decode(),span)
    def test_correction_prefix_is_not_part_of_entity(self):
        text='Parandus: Nurme eelarve on 720 eurot.'
        parsed=list(relations(text))[0]
        self.assertEqual(parsed.subject,'Nurme')
        for _,a,b,span in parsed.roles:self.assertEqual(text.encode()[a:b].decode(),span)
    def test_owner_question_retains_complete_object(self):
        self.assertEqual(functional('Kes vastutab Nurme aruande eest?'),(('owner',),'Nurme aruande'))
    def test_owner_is_not_approval_or_action(self):
        self.assertEqual(list(relations('Leelo may be the owner.')),[])
        self.assertIsNone(functional('Did Leelo approve the Nurme report?'))
    def test_estonian_event_date_requires_explicit_event_verb(self):
        self.assertIsNotNone(MEETING_ET.fullmatch('Nurme koosolek toimus 2026-09-04'))
        for text in ('Nurme koosolek ei toimunud 2026-09-04','Nurme koosolek salvestati 2026-09-04',
                     'Nurme koosolek võib toimuda 2026-09-04'):
            self.assertIsNone(MEETING_ET.fullmatch(text))
