import unittest
from types import SimpleNamespace
from memory_engine.reader_question_language import functional,person
from memory_engine.reader_relation_query import parse


class ReaderQuestionLanguageTests(unittest.TestCase):
    def test_full_possessive_subject_not_only_capitalized_token(self):
        self.assertEqual(functional('Mis on Nurmenuku projekti eelarve?'),(('budget',),'Nurmenuku projekti'))
    def test_question_contract_does_not_ignore_other_requests(self):
        self.assertIsNone(functional('Mis on Nurmenuku eelarve ja omanik?'))
    def test_estonian_event_question_and_speaker_question(self):
        self.assertEqual(functional('Millal toimus Lepatriinu koosolek?'),(('meeting_time',),'Lepatriinu'))
        self.assertEqual(person('Mida Siiri ütles?'),'Siiri')
        self.assertIsNone(person('Kas Siiri ütles midagi?'))
    def test_preference_target_must_be_supported(self):
        question=parse('What does Cora prefer for invoices?')
        self.assertTrue(question.matches(SimpleNamespace(subject='Cora',predicate='prefers',value='email for invoices')))
        self.assertFalse(question.matches(SimpleNamespace(subject='Cora',predicate='prefers',value='telephone for support')))
        self.assertFalse(question.matches(SimpleNamespace(subject='Dora',predicate='prefers',value='email for invoices')))
    def test_qualified_preference_preserves_verification_guard(self):
        self.assertIsNone(parse('What does Cora actually prefer for invoices?'))
    def test_supported_cross_language_target_vocabulary(self):
        question=parse('How does Cora prefer to receive invoices?')
        self.assertTrue(question.matches(SimpleNamespace(subject='Cora',predicate='prefers',value='arveid posti teel saada')))
        self.assertFalse(question.matches(SimpleNamespace(subject='Cora',predicate='prefers',value='teateid posti teel saada')))
