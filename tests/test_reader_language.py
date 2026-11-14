"""Development grammar tests; frozen reader expectations are not changed."""
import unittest
from memory_engine.reader_language import relations
import test_reader_v3 as helpers


class ClauseRoleTests(unittest.TestCase):
    def test_frames_preserve_roles_and_original_unicode_spans(self):
        for text in ("Cedar's deadline is Tuesday.", 'The deadline for Cedar is Tuesday.',
                     'Tuesday is the deadline for Cedar.', 'For Cedar, the deadline is Tuesday.',
                     'Cedar deadline was moved to Tuesday.', 'Cedar tähtaeg on Tuesday.'):
            with self.subTest(text=text):
                result=list(relations(text,source_start=11))
                self.assertEqual(len(result),1)
                r=result[0]
                self.assertEqual((r.subject,r.predicate,r.value),('Cedar','deadline','Tuesday'))
                for _,start,end,value in r.roles:
                    self.assertEqual(text.encode()[start-11:end-11].decode(),value)

    def test_explicit_negation_and_proposal_are_not_positive_facts(self):
        negative=list(relations('Cedar deadline is not Tuesday.'))[0]
        self.assertTrue(negative.negative)
        for value in ('Tuesday or Thursday','possibly Tuesday','Tuesday unless approval fails','a proposed Tuesday'):
            self.assertTrue(list(relations('Cedar deadline is '+value+'.'))[0].uncertain)

    def test_questions_and_reported_speech_are_not_copula_assertions(self):
        for text in ('Is Tuesday the deadline for Cedar?', 'The deadline for Cedar is Tuesday?',
                     'Dana said "Cedar deadline is Tuesday".', 'Assume Tuesday is the deadline for Cedar.'):
            found=list(relations(text))
            self.assertTrue(not found or all(r.uncertain for r in found),text)

    def test_decimal_and_multiline_offsets(self):
        text='Noise.\nCedar budget is 12.75 EUR.\nFor Õun, the deadline is Thursday.'
        found=list(relations(text))
        self.assertEqual([r.value for r in found],['12.75 EUR','Thursday'])
        for r in found:
            for _,a,b,value in r.roles:self.assertEqual(text.encode()[a:b].decode(),value)


class ReaderLanguageIntegration(unittest.TestCase):
    setUp=helpers.EvidenceReaderTests.setUp
    tearDown=helpers.EvidenceReaderTests.tearDown
    add=helpers.EvidenceReaderTests.add
    read=helpers.EvidenceReaderTests.read

    def test_new_forms_still_detect_conflicting_values(self):
        self.add("Cedar's deadline is Tuesday.")
        self.add('The deadline for Cedar is Thursday.')
        self.assertEqual(self.read('What is the Cedar deadline?')['state'],'CONFLICTING')

    def test_form_roles_are_carried_in_answer_claim(self):
        self.add('For Cedar, the deadline is Tuesday.')
        answer=self.read('What is the Cedar deadline?')
        self.assertEqual(answer['state'],'SUFFICIENT')
        self.assertEqual(len(answer['claims'][0]['extraction_support']),3)

    def test_quoted_reordered_form_does_not_acquire_sender(self):
        self.add('> The deadline for Cedar is Tuesday.',author_name='Dana',author_id='a',account_id='work',platform='mail')
        self.assertEqual(self.read('What did Dana say about Cedar?')['state'],'INSUFFICIENT')

    def test_temporal_sequence_unknown_anchor_returns_state_not_exception(self):
        self.add('Cedar deadline is Tuesday.')
        result=self.read('What was I researching before I opened this file?')
        self.assertEqual(result['state'],'INSUFFICIENT')
