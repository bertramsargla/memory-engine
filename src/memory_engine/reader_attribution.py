"""Textual attribution witnesses, never native-person identity or world truth.

The source can report that a named person spoke without proving who that person
is, whether the report is true, or whether the source's author believes the quote.
"""
from dataclasses import dataclass
import re

RECIPE='explicit-textual-attribution-v1'
NAME=r'[^\W\d_]\w*(?:[ -][^\W\d_]\w*){0,3}'
PATTERNS=[
    ('speaker_inside_quote',re.compile(rf'(?P<speaker>{NAME})\s+(?:said|wrote|ütles|kirjutas)\s*:\s*["“„](?P<quote>[^"“”„\n]+)["”]\.?',re.I)),
    ('speaker_inside_quote',re.compile(rf'["“„](?P<quote>[^"“”„\n]+)["”],?\s+(?:said|wrote|ütles|kirjutas)\s+(?P<speaker>{NAME})\.?',re.I)),
    ('reported_speaker',re.compile(rf'According to\s+(?P<speaker>{NAME}),\s+[^\n]+',re.I)),
    ('reported_speaker',re.compile(rf'(?P<speaker>{NAME})\s+sõnul\s+[^\n]+',re.I)),
]
BLOCKED=re.compile(r'\b(?:if|unless|maybe|perhaps|allegedly|kui|võib-olla|väidetavalt|ignore|system|said that|reported that)\b',re.I)


@dataclass(frozen=True)
class AttributionWitness:
    role: str
    display_name: str
    start_byte: int
    end_byte: int
    evidence_id: str
    assertion_kind: str='source_report'
    identity_state: str='UNKNOWN'
    authority: str='none'
    recipe: str=RECIPE


def witnesses(text,*,evidence_id,source_start=0,completeness='complete'):
    if not isinstance(text,str) or len(text.encode())>64000 or completeness!='complete':return ()
    if source_start<0 or not evidence_id:return ()
    result=[]
    for line in re.finditer(r'[^\n]+',text):
        raw=line[0];body=raw.strip()
        # Nested/forwarded messages need adapter message boundaries; do not use
        # an inner visible name as the outer sender. Full-line matches only.
        if body.startswith('>') or BLOCKED.search(body):continue
        for role,pattern in PATTERNS:
            m=pattern.fullmatch(body)
            if not m:continue
            name=m['speaker']
            if set(name.casefold().split()) & {'not','never','did','does','perhaps','probably','reportedly','ei'}:continue
            if any(not part[0].isupper() for part in name.replace('-',' ').split()):continue
            offset=line.start()+len(raw)-len(raw.lstrip())
            start=source_start+len(text[:offset+m.start('speaker')].encode())
            end=source_start+len(text[:offset+m.end('speaker')].encode())
            result.append(AttributionWitness(role,name,start,end,evidence_id))
            break
    return tuple(result)
