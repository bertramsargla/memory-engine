import unittest
from memory_engine.reader_contrast import values
from memory_engine.reader_language import relations


class ReaderContrastTests(unittest.TestCase):
    def test_contrast_is_not_a_disjunction(self):
        self.assertEqual(values('Mitte neljapäev, vaid teisipäev'),('teisipäev','neljapäev'))
        self.assertEqual(values('Not Thursday but Tuesday'),('Tuesday','Thursday'))
        self.assertIsNone(values('Thursday or Tuesday'))
        self.assertIsNone(values('Not Thursday but maybe Tuesday'))
    def test_negation_scope_cannot_be_dropped(self):
        self.assertIsNone(values('It is not true that Thursday is wrong'))
    def test_estonian_shift_keeps_original_value_and_spans(self):
        text='Nurme tähtaeg lükati teisipäevale.'
        claim=list(relations(text))[0]
        self.assertEqual((claim.subject,claim.predicate,claim.value),('Nurme','deadline','teisipäevale'))
        for _,a,b,value in claim.roles:self.assertEqual(text.encode()[a:b].decode(),value)
