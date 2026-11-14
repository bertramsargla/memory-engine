import unittest
from memory_engine.reader_time_axes import resolve_axis


class TimeAxisTests(unittest.TestCase):
    def test_no_axis_substitution(self):
        u=dict(evidence_id='e', observed_at='2026-01-09T12:00:00+02:00', text='Workshop happened on 2026-01-02.')
        self.assertEqual(resolve_axis([u], 'authored_at')['state'], 'UNKNOWN')
        self.assertEqual(resolve_axis([u], 'known_at')['state'], 'UNKNOWN')
        self.assertEqual(resolve_axis([u], 'event_time')['value'], '2026-01-02')
        self.assertEqual(resolve_axis([u], 'observed_at')['value'], '2026-01-09T10:00:00+00:00')

    def test_date_granularity_and_original_unicode_span(self):
        text='  Üldkoosolek leidis aset 2026-02-03.'
        r=resolve_axis([dict(evidence_id='e',text=text,start_byte=7)], 'event_time')
        w=r['witnesses'][0];a,b=w['exact_span']
        self.assertEqual(text.encode()[a-7:b-7].decode(), '2026-02-03')
        self.assertEqual(w['granularity'], 'date')
        self.assertEqual(w['assertion_kind'], 'source_statement')

    def test_uncertain_quoted_negated_and_partial_are_not_event_proof(self):
        for text in ['Meeting did not happen on 2026-02-03.', 'If meeting happened on 2026-02-03.',
                     'Mira said meeting happened on 2026-02-03.', '"Meeting happened on 2026-02-03."',
                     'Koosolek ei toimunud 2026-02-03.', 'Meeting happened on 2026-02-30.',
                     'No meeting happened on 2026-02-03.', 'Meeting never happened on 2026-02-03.',
                     'Meeting allegedly happened on 2026-02-03.']:
            self.assertEqual(resolve_axis([dict(evidence_id='e',text=text)], 'event_time')['state'], 'UNKNOWN')
        self.assertEqual(resolve_axis([dict(evidence_id='e',text='Meeting occurred on 2026-02-03.',completeness='partial')], 'event_time')['state'], 'UNKNOWN')

    def test_multiple_event_times_need_an_event_target(self):
        units=[dict(evidence_id=str(i),text=f'Workshop occurred on 2026-02-0{i}.') for i in (1,2)]
        self.assertEqual(resolve_axis(units, 'event_time')['state'], 'AMBIGUOUS')

    def test_invalid_or_naive_metadata_is_unknown(self):
        for value in ['invalid', '2026-01-01', 12]:
            r=resolve_axis([dict(evidence_id='e',known_at=value)], 'known_at')
            self.assertEqual(r['state'], 'UNKNOWN')

    def test_repeated_same_witness_preserves_all_lineage(self):
        r=resolve_axis([dict(evidence_id=str(i),valid_from='2026-01-01T00:00:00Z') for i in (1,2)], 'valid_from')
        self.assertEqual(r['state'], 'RESOLVED');self.assertEqual(len(r['witnesses']),2)


if __name__=='__main__':unittest.main()
