"""Explicit time-axis witnesses. Source event statements are not verified events.

Date-only expressions keep date granularity; no timezone or midnight is invented.
This bounded resolver consumes already authorized evidence and never falls back
between observation, authoring, knowledge, validity and linguistic event time.
"""
from dataclasses import dataclass, asdict
from datetime import date
import re
from .reader_temporal import instant

RECIPE = 'explicit-time-axis-witness-v2-weekday-granularity'
AXES = ('observed_at', 'authored_at', 'known_at', 'valid_from', 'valid_to')
EVENT = re.compile(r'(?P<event>[\w][\w -]{0,100}?)\s+'
    r'(?:happened|occurred|was held|took place|toimus|leidis aset)\s+'
    r'(?:on\s+)?(?P<date>\d{4}-\d{2}-\d{2})[.!]?', re.I)
WEEKDAYS={'monday':'Monday','tuesday':'Tuesday','wednesday':'Wednesday','thursday':'Thursday',
          'friday':'Friday','saturday':'Saturday','sunday':'Sunday',
          'esmaspäeval':'Monday','teisipäeval':'Tuesday','kolmapäeval':'Wednesday','neljapäeval':'Thursday',
          'reedel':'Friday','laupäeval':'Saturday','pühapäeval':'Sunday'}
EVENT_DAY=re.compile(r'(?P<event>[\w][\w -]{0,100}?)\s+'
    r'(?:happened|occurred|was held|took place|toimus|leidis aset)\s+(?:on\s+)?(?P<date>'+ '|'.join(WEEKDAYS)+r')[.!]?',re.I)
UNSAFE_CONTEXT = re.compile(r'["“”>]|\b(?:if|unless|might|maybe|allegedly|apparently|not|never|no|said|reported|kui|võib|väidetavalt|ei|mitte|ütles|väitis|ignore|system)\b', re.I)
MODAL_EVENT=re.compile(r'\b(?:may|could|will|would|should|must)\s+(?:have|be|happen|occur)|\b(?:probably|possibly|perhaps|tõenäoliselt|võiks)\b',re.I)


def field(unit, name):
    return unit.get(name) if isinstance(unit, dict) else getattr(unit, name, None)


@dataclass(frozen=True)
class TimeWitness:
    evidence_id: str
    value: str
    basis: str
    granularity: str
    exact_span: tuple[int, int] | None = None
    assertion_kind: str = 'adapter_metadata'


def event_witnesses(unit):
    text = field(unit, 'text') or ''
    if len(text.encode('utf-8')) > 64000 or UNSAFE_CONTEXT.search(text) or MODAL_EVENT.search(text):
        return ()
    if field(unit, 'completeness') in ('partial', 'truncated'):
        return ()
    result = []
    for line in re.finditer(r'[^\n]+', text):
        stripped = line.group().strip()
        match = EVENT.fullmatch(stripped) or EVENT_DAY.fullmatch(stripped)
        if not match:
            continue
        try:
            raw_value=match['date']
            value = WEEKDAYS.get(raw_value.casefold()) or date.fromisoformat(raw_value).isoformat()
        except ValueError:
            continue
        offset = line.start() + len(line.group()) - len(line.group().lstrip()) + match.start('date')
        start = (field(unit, 'start_byte') or 0) + len(text[:offset].encode('utf-8'))
        result.append(TimeWitness(field(unit, 'evidence_id'), value,
            'original_event_expression', 'weekday' if raw_value.casefold() in WEEKDAYS else 'date',
            (start, start + len(raw_value.encode())), 'source_statement'))
    return tuple(result)


def resolve_axis(units, axis):
    if axis not in (*AXES, 'event_time'):
        raise ValueError('An explicit supported temporal axis is required')
    witnesses = []
    invalid = False
    for unit in units:
        if axis == 'event_time':
            witnesses.extend(event_witnesses(unit))
        elif field(unit, axis) is not None:
            try:
                value = instant(field(unit, axis)).isoformat()
                witnesses.append(TimeWitness(field(unit, 'evidence_id'), value, axis, 'instant'))
            except (TypeError, ValueError):
                invalid = True
    values = {w.value for w in witnesses}
    # Multiple timestamps may be perfectly valid observations of different events.
    # Do not label them a factual contradiction or silently pick the latest.
    state = 'UNKNOWN' if invalid or not values else 'AMBIGUOUS' if len(values) > 1 else 'RESOLVED'
    return dict(state=state, value=next(iter(values)) if state == 'RESOLVED' else None,
        basis='original_event_expression' if axis == 'event_time' else axis,
        witnesses=[asdict(w) for w in witnesses], recipe=RECIPE, authority='none',
        reason='invalid_time' if invalid else 'multiple_time_witnesses' if len(values)>1 else
        'explicit_axis_witness' if values else 'requested_axis_unknown')
