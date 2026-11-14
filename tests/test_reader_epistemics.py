import unittest
from memory_engine.reader_epistemics import strength,precision
import test_reader_v3 as helpers


class EpistemicContractTests(unittest.TestCase):
    def test_modality_is_not_a_numeric_probability(self):
        for text,expected in [('might work','POSSIBLE'),('võiks sobida','POSSIBLE'),
            ('probably Thursday','PROBABLE'),('tõenäoliselt reedel','PROBABLE'),
            ('if approved','POSSIBLE'),('kui kinnitatakse','POSSIBLE'),('teadmata','UNKNOWN'),('Friday','CERTAIN')]:
            self.assertEqual(strength(text),expected)

    def test_approximation_and_certainty_are_separate(self):
        for text in ('approximately 530 EUR','ligikaudu 530 eurot','at least 530 EUR','vähemalt 530 eurot'):
            self.assertEqual(strength(text),'CERTAIN');self.assertEqual(precision(text),'APPROXIMATE')


class EpistemicReaderTests(unittest.TestCase):
    setUp=helpers.EvidenceReaderTests.setUp
    tearDown=helpers.EvidenceReaderTests.tearDown
    add=helpers.EvidenceReaderTests.add
    read=helpers.EvidenceReaderTests.read

    def test_exact_question_requires_exact_evidence(self):
        self.add('Thistle budget is approximately 530 EUR.')
        result=self.read('What is the exact Thistle budget?')
        self.assertEqual(result['state'],'INSUFFICIENT')
        self.assertIn('exact_value',result['answerability']['missing_slots'])

    def test_general_question_can_preserve_approximate_value(self):
        self.add('Thistle budget is approximately 530 EUR.')
        result=self.read('What is the Thistle budget?')
        self.assertEqual(result['state'],'SUFFICIENT')
        self.assertEqual(result['claims'][0]['value_precision'],'APPROXIMATE')
        self.assertIn('approximately',result['answer'])

    def test_estonian_precision_is_not_silently_promoted(self):
        self.add('Õisik eelarve on umbes 530 eurot.')
        self.assertEqual(self.read('Mis on Õisik täpne eelarve?')['state'],'INSUFFICIENT')

    def test_uncertain_value_remains_unanswered(self):
        self.add('Thistle deadline is probably Thursday.')
        self.assertEqual(self.read('What is the Thistle deadline?')['state'],'INSUFFICIENT')
