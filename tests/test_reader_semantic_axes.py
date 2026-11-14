import unittest
from memory_engine.reader_attribution import witnesses
from memory_engine.reader_time_axes import resolve_axis,event_witnesses
from memory_engine.reader_linguistic_time import resolve_source_axis


class SemanticAxisTests(unittest.TestCase):
    def unit(self,text,**extra):
        return dict(evidence_id='e',text=text,start_byte=0,completeness='complete',observed_at='2026-09-09T10:00:00Z',
                    authored_at=None,known_at='2026-09-09T10:00:00Z',**extra)

    def test_quote_reports_a_name_without_resolving_identity(self):
        for text,role in [('Nele said: "Friday works."','speaker_inside_quote'),
                          ('"Reede sobib," ütles Nele.','speaker_inside_quote'),
                          ('According to Nele, Friday works.','reported_speaker'),
                          ('Nele sõnul reede sobib.','reported_speaker')]:
            with self.subTest(text=text):
                result=witnesses(text,evidence_id='e')
                self.assertEqual(len(result),1)
                self.assertEqual((result[0].display_name,result[0].role),('Nele',role))
                self.assertEqual(result[0].identity_state,'UNKNOWN')
                self.assertEqual(result[0].assertion_kind,'source_report')
                self.assertEqual(result[0].authority,'none')

    def test_attribution_spans_preserve_unicode(self):
        text='  Õie ütles: "Reedel."'
        w=witnesses(text,evidence_id='e',source_start=21)[0]
        self.assertEqual(text.encode()[w.start_byte-21:w.end_byte-21].decode(),'Õie')

    def test_mentions_negated_reports_and_nested_quotes_do_not_acquire_speakers(self):
        for text in ('Nele was mentioned.','Nele did not say: "Friday."',
                     'Nele DID NOT said: "Friday."','Nele NEVER said: "Friday."',
                     'Perhaps Nele said: "Friday."','If Nele said: "Friday."',
                     '> Nele said: "Friday."','Rando said that Nele said: "Friday."',
                     'Nele said: "Rando said: \'Friday\'."'):
            # The final example has a definite outer speaker; it must never
            # produce an inner speaker or resolve a native person.
            result=witnesses(text,evidence_id='e')
            if text.startswith('Nele said:'):
                self.assertTrue(all(w.display_name=='Nele' and w.identity_state=='UNKNOWN' for w in result))
            else:self.assertEqual(result,())

    def test_partial_capture_does_not_establish_quote_boundary(self):
        self.assertEqual(witnesses('Nele said: "Friday."',evidence_id='e',completeness='partial'),())

    def test_negation_inside_quote_does_not_negate_the_reporting_event(self):
        result=witnesses('Nele said: "Friday is not approved."',evidence_id='e')
        self.assertEqual(result[0].display_name,'Nele')
        self.assertEqual(result[0].identity_state,'UNKNOWN')

    def test_names_remain_separate_occurrence_witnesses(self):
        a=witnesses('Nele said: "Friday."',evidence_id='account-a')[0]
        b=witnesses('Nele said: "Monday."',evidence_id='account-b')[0]
        self.assertNotEqual(a.evidence_id,b.evidence_id)
        self.assertEqual(a.identity_state,b.identity_state)
        self.assertEqual(a.identity_state,'UNKNOWN')

    def test_weekday_is_not_observation_date(self):
        for text in ('Meeting happened Monday.','Koosolek toimus esmaspäeval.'):
            unit=self.unit(text);r=resolve_axis([unit],'event_time')
            self.assertEqual(r['value'],'Monday')
            self.assertEqual(r['witnesses'][0]['granularity'],'weekday')
            a,b=r['witnesses'][0]['exact_span']
            self.assertIn(text.encode()[a:b].decode(),('Monday','esmaspäeval'))
            self.assertIsNone(resolve_axis([unit],'authored_at')['value'])
            self.assertNotEqual(resolve_axis([unit],'known_at')['value'],'Monday')

    def test_by_and_on_are_not_interchangeable(self):
        for text in ('Meeting happened by Monday.','Koosolek toimus esmaspäevaks.','Meeting may have happened Monday.'):
            self.assertEqual(event_witnesses(self.unit(text)),())

    def test_source_validity_never_overwrites_known_or_adapter_validity(self):
        unit=self.unit('The decision is valid starting 2026-08-11.',valid_from=None)
        out=resolve_source_axis([unit],'source_valid_from')
        self.assertEqual(out['value'],'2026-08-11')
        self.assertEqual(out['witnesses'][0]['assertion_kind'],'source_statement')
        self.assertIsNone(resolve_axis([unit],'valid_from')['value'])
        self.assertEqual(resolve_axis([unit],'known_at')['value'],'2026-09-09T10:00:00+00:00')

    def test_invalid_source_dates_and_scopes_abstain(self):
        for text in ('The decision is valid starting 2026-02-31.',
                     'The decision is not valid starting 2026-08-11.',
                     'If the decision is valid starting 2026-08-11.',
                     'Nele said the decision is valid starting 2026-08-11.'):
            self.assertEqual(resolve_source_axis([self.unit(text)],'source_valid_from')['state'],'UNKNOWN')

    def test_multiple_source_as_of_dates_remain_ambiguous(self):
        units=[self.unit('As of 2026-08-11, the plan was preliminary.'),self.unit('As of 2026-08-12, the plan was final.')]
        out=resolve_source_axis(units,'source_as_of')
        self.assertEqual(out['state'],'AMBIGUOUS')
        self.assertIsNone(out['value'])


if __name__=='__main__':unittest.main()
