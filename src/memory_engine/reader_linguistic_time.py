"""Source-described validity/as-of dates, distinct from observed/known time."""
from dataclasses import asdict
from datetime import date
import re
from .reader_time_axes import TimeWitness,field,UNSAFE_CONTEXT,MODAL_EVENT

RECIPE='source-linguistic-time-v1'
PREFIX=r'[\w][\w -]{0,100}?'
PATTERNS={
    'source_valid_from':re.compile(rf'{PREFIX}\s+(?:is valid (?:starting|from)|kehtib alates)\s+(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\.?',re.I),
    'source_valid_to':re.compile(rf'{PREFIX}\s+(?:is valid until|kehtib kuni)\s+(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\.?',re.I),
    'source_as_of':re.compile(r'(?:As of|Seisuga)\s+(?P<date>\d{4}-\d{2}-\d{2}),\s+[^\n]+',re.I),
}


def resolve_source_axis(units,axis):
    if axis not in PATTERNS:raise ValueError('Explicit source-language axis required')
    witnesses=[]
    for unit in units:
        text=field(unit,'text') or ''
        if len(text.encode())>64000 or field(unit,'completeness') in ('partial','truncated') or UNSAFE_CONTEXT.search(text) or MODAL_EVENT.search(text):continue
        for line in re.finditer(r'[^\n]+',text):
            raw=line[0];body=raw.strip();match=PATTERNS[axis].fullmatch(body)
            if not match:continue
            try:value=date.fromisoformat(match['date']).isoformat()
            except ValueError:continue
            offset=line.start()+len(raw)-len(raw.lstrip())+match.start('date')
            start=(field(unit,'start_byte') or 0)+len(text[:offset].encode())
            witnesses.append(TimeWitness(field(unit,'evidence_id'),value,axis,'date',(start,start+10),'source_statement'))
    values={w.value for w in witnesses}
    return dict(state='RESOLVED' if len(values)==1 else 'AMBIGUOUS' if values else 'UNKNOWN',
                value=next(iter(values)) if len(values)==1 else None,witnesses=[asdict(w) for w in witnesses],
                basis=axis,recipe=RECIPE,authority='none',
                epistemic_limit='Source assertion only; does not establish MemoryEngine known time or verified validity.')
