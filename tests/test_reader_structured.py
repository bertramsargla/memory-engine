import unittest
import test_reader_v3 as helpers
from memory_engine.reader_structured import relations


class StructuredSourceTests(unittest.TestCase):
    setUp=helpers.EvidenceReaderTests.setUp
    tearDown=helpers.EvidenceReaderTests.tearDown
    add=helpers.EvidenceReaderTests.add
    read=helpers.EvidenceReaderTests.read

    def test_labeled_field_under_heading_preserves_exact_roles(self):
        text='Õun:\n- Owner: Dana\n- Budget: 42 EUR'
        result=list(relations(text,source_start=3))
        self.assertEqual(len(result),1)
        self.assertEqual((result[0].subject,result[0].predicate,result[0].value),('Õun','budget','42 EUR'))
        for _,start,end,value in result[0].roles:self.assertEqual(text.encode()[start-3:end-3].decode(),value)

    def test_separate_headings_do_not_share_values(self):
        self.add('Cedar:\nDeadline: Tuesday\nLinden:\nDeadline: Thursday')
        result=self.read('What is the Cedar deadline?')
        self.assertEqual(result['state'],'SUFFICIENT');self.assertEqual(result['claims'][0]['value'],'Tuesday')

    def test_unrelated_prose_breaks_field_inheritance(self):
        self.add('Cedar:\nAn unrelated passage begins here.\nDeadline: Tuesday')
        self.assertEqual(self.read('What is the Cedar deadline?')['state'],'INSUFFICIENT')

    def test_explicit_question_answer_pair_and_literal_cross_language_answer(self):
        for text in ('Question: When is Cedar due?\nAnswer: Tuesday.','Cedar deadline? Teisipäev.'):
            with self.subTest(text=text):
                self.refs=[];self.add(text,source=text)
                result=self.read('What is the Cedar deadline?')
                self.assertEqual(result['state'],'SUFFICIENT');self.assertEqual(result['claims'][0]['value'],'Tuesday')

    def test_question_alone_or_uncertain_answer_does_not_supply_fact(self):
        for text in ('Cedar deadline?', 'Cedar deadline? Maybe Tuesday.','Cedar deadline? Not Tuesday.'):
            self.refs=[];self.add(text,source=text)
            self.assertEqual(self.read('What is the Cedar deadline?')['state'],'INSUFFICIENT')

    def test_captured_action_and_goal_fields_cannot_create_authority(self):
        self.add('Cedar:\nGoal: Send the contract\nSent: verified\nApproval: yes')
        self.assertEqual(self.read('Did I send the Cedar contract?')['state'],'INSUFFICIENT')
