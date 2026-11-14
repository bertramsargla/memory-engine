import unittest
from memory_engine.reader_language import relations
from memory_engine.reader_morphology import tokens,weekday


class FunctionalRoleGrammarTests(unittest.TestCase):
    def test_passive_auxiliaries_recover_the_same_subject_slot(self):
        for auxiliary in ('has been','had been','was','is'):
            for verb in ('set','moved','changed'):
                text=f'The Verbena deadline {auxiliary} {verb} to Wednesday.'
                r=list(relations(text))[0]
                self.assertEqual((r.subject,r.predicate,r.value),('Verbena','deadline','Wednesday'))
                for _,a,b,value in r.roles:self.assertEqual(text.encode()[a:b].decode(),value)

    def test_negated_passive_retains_polarity(self):
        for text in ('The Verbena deadline has not been set to Wednesday.',
                     'The Verbena deadline was not moved to Wednesday.'):
            self.assertTrue(list(relations(text))[0].negative)

    def test_inverted_estonian_roles_remain_original(self):
        for text,subject,value in [('Kolmapäev on Õisiku tähtaeg.','Õisiku','Kolmapäev'),
                                   ('180 eurot on Rukkilille eelarve.','Rukkilille','180 eurot')]:
            r=list(relations(text))[0]
            self.assertEqual((r.subject,r.value),(subject,value))
            for _,a,b,s in r.roles:self.assertEqual(text.encode()[a:b].decode(),s)

    def test_value_first_allocation_and_selection(self):
        for text,subject,predicate,value in [('A budget of 180 EUR was allocated to Verbena.','Verbena','budget','180 EUR'),
            ('For Verbena, Wednesday was selected as the deadline.','Verbena','deadline','Wednesday')]:
            r=list(relations(text))[0];self.assertEqual((r.subject,r.predicate,r.value),(subject,predicate,value))

    def test_deontic_deadline_does_not_create_completion(self):
        r=list(relations('Verbena peab valmis saama neljapäevaks.'))[0]
        self.assertEqual(r.predicate,'deadline');self.assertEqual(weekday(r.value),'Thursday')
        self.assertEqual(r.frame,'deadline_requirement_et')

    def test_morphology_does_not_strip_names(self):
        text='Katile reedeks Juhanilt';analyses=list(tokens(text,source_start=5))
        self.assertEqual(analyses[0].state,'UNKNOWN');self.assertEqual(analyses[2].state,'UNKNOWN')
        self.assertEqual(analyses[1].lemma_candidates,('reede',))
        self.assertIn('translative',analyses[1].grammatical_cases)
        for token in analyses:self.assertEqual(text.encode()[token.start_byte-5:token.end_byte-5].decode(),token.surface)
