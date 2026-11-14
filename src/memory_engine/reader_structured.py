"""Literal fields and explicitly adjacent Q/A as source assertions, not authority.

These rules attach values to text inside the same selected original span. They do
not infer authors, sent status, stable source identity or an answer across chunks.
"""
import re
from .reader_language import Relation,PREDICATES,QUALIFIED,S,P
from .structured_evidence import records

RECIPE='literal-source-fields-tables-and-qa-v2'
HEAD=re.compile(r'^(?:Project\s*:\s*)?(?P<subject>[\w][\w -]{0,100}?)\s*:$',re.I)
FIELD=re.compile(r'^\s*(?:[-*]\s+)?(?P<predicate>[\w -]+?)\s*:\s*(?P<value>.+?)\s*$')
QA=[re.compile(rf'(?:Question:\s*)?(?P<subject>{S})\s+(?P<predicate>{P})\?\s*(?:Answer:\s*)?(?P<value>[^!?\n]+?)\s*\.?$',re.I),
    re.compile(rf'Question:\s*When\s+is\s+(?P<subject>{S})\s+(?P<predicate>due)\?\s*Answer:\s*(?P<value>[^!?\n]+?)\s*\.?$',re.I)]


def _role(text,kind,a,b,source_start):
    return kind,source_start+len(text[:a].encode()),source_start+len(text[:b].encode()),text[a:b]


def relations(text,*,source_start=0,source_id=None,revision_id=None,occurrence_id=None):
    for record in records(text,source_start=source_start,source_id=source_id,revision_id=revision_id,occurrence_id=occurrence_id):
        subject=next(f.value for f in record.fields if f.name=='subject')
        for field in record.fields:
            if field.name=='subject':continue
            value=field.value.text
            negative=bool(re.match(r'^(?:not|mitte)\s+',value,re.I))
            if negative:value=re.sub(r'^(?:not|mitte)\s+','',value,flags=re.I)
            roles=tuple((name,span.start_byte,span.end_byte,span.text) for name,span in
                [('subject',subject),('predicate',field.label),('value',field.value)])
            yield Relation(subject.text,field.name,value,negative,bool(QUALIFIED.search(field.value.text)),
                False,roles,record.record_type,record.record_id)
    offset=len(text)-len(text.lstrip());body=text.strip()
    for pattern in QA:
        match=pattern.fullmatch(body)
        if match:
            roles=tuple(_role(text,k,offset+match.start(k),offset+match.end(k),source_start) for k in ('subject','predicate','value'))
            key=match['predicate'].casefold();predicate='deadline' if key=='due' else PREDICATES[key]
            negative=bool(re.match(r'^(?:not|mitte)\s+',match['value'],re.I))
            value=re.sub(r'^(?:not|mitte)\s+','',match['value'],flags=re.I) if negative else match['value']
            yield Relation(match['subject'],predicate,value,negative,bool(QUALIFIED.search(body)),False,roles,'explicit_qa')
            return
    subject=None;subject_span=None
    for line in re.finditer(r'[^\n]+',text):
        raw=line[0];trimmed=raw.strip();leading=len(raw)-len(raw.lstrip())
        heading=HEAD.fullmatch(trimmed)
        if heading:
            subject=heading['subject']
            subject_span=_role(text,'subject',line.start()+leading+heading.start('subject'),line.start()+leading+heading.end('subject'),source_start)
            continue
        field=FIELD.fullmatch(raw)
        if subject and field:
            predicate=PREDICATES.get(field['predicate'].strip().casefold())
            if predicate:
                value=field['value'].rstrip('.')
                roles=(subject_span,_role(text,'predicate',line.start()+field.start('predicate'),line.start()+field.end('predicate'),source_start),
                    _role(text,'value',line.start()+field.start('value'),line.start()+field.start('value')+len(value),source_start))
                negative=bool(re.match(r'^(?:not|mitte)\s+',value,re.I))
                if negative:value=re.sub(r'^(?:not|mitte)\s+','',value,flags=re.I)
                yield Relation(subject,predicate,value,negative,bool(QUALIFIED.search(field['value'])),False,roles,'labeled_field')
            continue
        if trimmed:
            subject=None;subject_span=None  # No inheritance across unrelated prose.
