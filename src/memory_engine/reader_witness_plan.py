"""Explicit question slots for source attribution and independent time axes.

These projections answer what an authorized source/adapter establishes. They
cannot turn a quoted name into a native identity or a source date into known time.
"""
from dataclasses import dataclass,replace
import re
from .reader_attribution import witnesses
from .reader_time_axes import resolve_axis,EVENT,EVENT_DAY,MODAL_EVENT,UNSAFE_CONTEXT
from .reader_linguistic_time import resolve_source_axis,PATTERNS
from .reader_claims import candidate

RECIPE='source-witness-answer-slots-v1'


@dataclass(frozen=True)
class WitnessQuestion:
    slot: str
    subject: str='selected source'
    event: str | None=None


def request(text):
    simple={
        'who is quoted in the selected source?':'quoted_speaker',
        'who is the reported speaker in the selected source?':'reported_speaker',
        'keda valitud allikas tsiteerib?':'quoted_speaker',
        'when did the memory engine observe this?':'observed_at',
        'when did the memory engine learn this?':'known_at',
        'when was this source authored?':'authored_at',
        'millal mälu seda nägi?':'observed_at',
        'what is the source-stated validity start?':'source_valid_from',
        'what is the source-stated validity end?':'source_valid_to',
        "what is the source's as-of date?":'source_as_of',
    }
    if text.strip().casefold() in simple:return WitnessQuestion(simple[text.strip().casefold()])
    m=re.fullmatch(r'When did ([\w][\w -]{0,100}?) (?:happen|occur)\?',text.strip(),re.I)
    if not m:m=re.fullmatch(r'Millal ([\w][\w -]{0,100}?) toimus\?',text.strip(),re.I)
    if m and not re.search(r'\b(?:and|or|if|ja|või|kui)\b',m[1],re.I):
        event=re.sub(r'^the\s+','',m[1],flags=re.I)
        meeting=re.fullmatch(r'(.+) meeting',event,re.I)
        return WitnessQuestion('meeting_time',meeting[1],event) if meeting else WitnessQuestion('event_time',event,event)
    return None


def project(question,units):
    claims=[]
    for u in units:
        confidence=dict(u.provenance_confidence).get('text')
        text_ok=u.completeness=='complete' and (confidence is None or confidence==1)
        if question.slot in ('quoted_speaker','reported_speaker'):
            if not text_ok:continue
            role='speaker_inside_quote' if question.slot=='quoted_speaker' else 'reported_speaker'
            for w in witnesses(u.text,evidence_id=u.evidence_id,source_start=u.start_byte,completeness=u.completeness):
                if w.role!=role:continue
                claims.append(replace(candidate(u,question.subject,question.slot,w.display_name,
                    assertion_kind='source_attribution',epistemic_strength='CERTAIN',recipe=RECIPE,
                    extraction_support=((role,w.start_byte,w.end_byte,w.display_name),)),speaker=None,speaker_confidence=None))
        else:
            if question.event is not None:
                if not text_ok:continue
                # Full single-event match: do not select an unrelated date in a
                # multi-event paragraph using similarity or observation time.
                m=EVENT.fullmatch(u.text.strip()) or EVENT_DAY.fullmatch(u.text.strip())
                if not m or m['event'].casefold()!=question.event.casefold():continue
            if question.slot.startswith('source_') and not text_ok:continue
            found=resolve_source_axis([u],question.slot) if question.slot.startswith('source_') else resolve_axis([u],'event_time' if question.event else question.slot)
            for w in found['witnesses']:
                if found['state']!='RESOLVED':continue
                exact=w.get('exact_span');roles=()
                if exact:
                    a,b=exact;surface=u.text.encode()[a-u.start_byte:b-u.start_byte].decode()
                    roles=((question.slot,a,b,surface),)
                claims.append(candidate(u,question.subject,question.slot,w['value'],
                    assertion_kind='source_temporal' if exact else 'observation_metadata',
                    epistemic_strength='CERTAIN',recipe=RECIPE,extraction_support=roles))
    return tuple(claims)
