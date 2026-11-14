"""Versioned structural projections. Cells keep source bytes, not invented prose."""
from dataclasses import dataclass
import hashlib
import re
from .reader_contracts import ReaderBudgetExceeded

RECIPE = 'explicit-records-v1'
NAMES = {'project':'subject','projekt':'subject','subject':'subject',
         'deadline':'deadline','tähtaeg':'deadline','budget':'budget','eelarve':'budget',
         'owner':'owner','vastutaja':'owner','field':'field','väli':'field','value':'value','väärtus':'value'}


@dataclass(frozen=True)
class Span:
    text: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class Field:
    name: str
    value: Span
    label: Span
    role: str


@dataclass(frozen=True)
class StructuredRecord:
    record_id: str
    source_id: str | None
    revision_id: str | None
    occurrence_id: int | None
    record_type: str
    fields: tuple[Field, ...]
    row_identity: str
    heading_context: Span | None
    recipe: str = RECIPE
    provenance: str = 'original_structure_only_author_action_finality_unknown'


def records(text, *, source_start=0, source_id=None, revision_id=None, occurrence_id=None):
    if len(text.encode()) > 64000:
        raise ReaderBudgetExceeded('structured_projection_byte_budget',64000)
    lines = list(re.finditer(r'[^\n]+', text))
    if len(lines) > 512:
        raise ReaderBudgetExceeded('structured_projection_line_budget',512)

    def span(a,b):
        while a < b and text[a].isspace():a += 1
        while b > a and text[b-1].isspace():b -= 1
        return Span(text[a:b],source_start+len(text[:a].encode()),source_start+len(text[:b].encode()))

    def cells(line):
        raw=line[0]; separator='|' if '|' in raw else '\t' if '\t' in raw else None
        if separator is None:return None
        a,b=line.start(),line.end()
        if separator=='|':
            while a<b and text[a].isspace():a+=1
            while b>a and text[b-1].isspace():b-=1
            if a<b and text[a]=='|':a+=1
            if b>a and text[b-1]=='|':b-=1
        # finditer coordinates above are relative; explicit split spans avoid guessing.
        out=[];cursor=a
        for m in re.finditer(re.escape(separator),text[a:b]):
            out.append(span(cursor,a+m.start()));cursor=a+m.end()
        out.append(span(cursor,b))
        return out

    def record(kind, fields, heading=None):
        identity=hashlib.sha256(repr((source_id,revision_id,occurrence_id,kind,fields)).encode()).hexdigest()
        return StructuredRecord(identity,source_id,revision_id,occurrence_id,kind,tuple(fields),identity,heading)

    def field(name,value,label):
        return Field(name,value,label,'subject' if name=='subject' else 'value')

    heading=None;i=0
    while i<len(lines):
        line=lines[i];raw=line[0];table=cells(line)
        if table:
            block=[table];j=i+1
            while j<len(lines) and cells(lines[j]) is not None:
                block.append(cells(lines[j]));j+=1
            keys=[NAMES.get(c.text.casefold()) for c in table]
            data=[row for row in block[1:] if not all(re.fullmatch(r':?-{3,}:?',c.text) for c in row)]
            # Never repair ragged rows or choose one of duplicate headers silently.
            valid=all(len(row)==len(keys) and all(c.text for c in row) for row in data)
            valid=valid and all(keys) and len(set(keys))==len(keys)
            if valid and 'subject' in keys and len(keys)>=2:
                for row in data:
                    yield record('header_row',[field(k,v,h) for k,v,h in zip(keys,row,table)],heading)
            elif valid and keys==['field','value'] and heading:
                fields=[field('subject',heading,heading)]
                seen=set()
                for row in data:
                    name=NAMES.get(row[0].text.casefold())
                    if name in (None,'subject','field','value') or name in seen:
                        fields=[];break
                    seen.add(name);fields.append(field(name,row[1],row[0]))
                if len(fields)>1:yield record('key_value_table',fields,heading)
            heading=None;i=j;continue
        # A subject heading is structure only. It is not a global source identity.
        match=re.fullmatch(r'\s*(?:(?:Project|Projekt)\s*:\s*)?(?P<subject>[\w][\w -]{0,100}?)\s*:\s*',raw,re.I)
        explicit=re.fullmatch(r'\s*(?:Project|Projekt)\s*:\s*(?P<subject>[^:]+?)\s*',raw,re.I)
        head=explicit or match
        if head:
            heading=span(line.start()+head.start('subject'),line.start()+head.end('subject'));i+=1;continue
        # Adjacent known label/value forms: every label must be explicit and unique.
        if NAMES.get(raw.strip().casefold())=='subject':
            fields=[];seen=set();j=i
            while j+1<len(lines):
                label=span(lines[j].start(),lines[j].end());key=NAMES.get(label.text.casefold())
                if key in (None,'field','value') or key in seen:break
                value=span(lines[j+1].start(),lines[j+1].end())
                if NAMES.get(value.text.casefold()) or not value.text:break
                seen.add(key);fields.append(field(key,value,label));j+=2
            if len(fields)>1:yield record('adjacent_labels',fields)
            i=max(i+1,j);heading=None;continue
        if raw.strip():heading=None
        i+=1
