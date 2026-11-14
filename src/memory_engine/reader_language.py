"""Versioned, bounded clause parsing. This is a formal subset, not general NLI.

Every extracted role points into the original string. Reordering syntax is allowed;
inventing a speaker, entity alias, time or missing value is not. No query text enters
this parser. Unsupported constructions remain visible as coverage failures.
"""
from dataclasses import dataclass
import re

RECIPE = 'literal-clause-roles-v3-explicit-owner-correction'
PREDICATES = {'deadline':'deadline','tähtaeg':'deadline','budget':'budget','eelarve':'budget',
              'prototype':'prototype','contract':'contract','preference':'preference'}
P = '(?:' + '|'.join(PREDICATES) + ')'
S = r"[\w][\w -]{0,100}?"
V = r"[^\n!?]+?"
PASSIVE = r'(?:(?:has|had)\s+(?:not\s+)?been|was|is)\s+(?:not\s+)?(?:set|moved|changed)\s+to'
COPULA = rf'(?:{PASSIVE}|is not|ei ole|will be|is|on|oli|was|lükati|:)'
# Order is intentional: named roles disambiguate syntax before broad copula forms.
FRAMES = [
    ('owner_topic', rf"For\s+(?:the\s+)?(?P<subject>{S}),\s*(?P<value>{S})\s+(?P<verb>is|was)\s+the\s+(?P<predicate>owner)"),
    ('owner_et', rf"(?P<subject>{S})\s+eest\s+(?P<predicate>(?P<verb>vastutab))\s+(?P<value>{S})"),
    ('possessive', rf"(?P<subject>{S})['’]s\s+(?P<predicate>{P})\s+(?P<verb>is not|is|was|will be)\s+(?P<value>{V})"),
    ('relation_of', rf"(?:The\s+)?(?P<predicate>{P})\s+(?:for|of)\s+(?P<subject>{S})\s+(?P<verb>has been moved to|was moved to|is not|will be|is|was)\s+(?P<value>{V})"),
    ('inverted', rf"(?P<value>{V})\s+(?P<verb>is not|is|was)\s+(?:the\s+)?(?P<predicate>{P})\s+(?:for|of)\s+(?P<subject>{S})"),
    ('topic', rf"(?:For\s+)(?P<subject>{S}),\s*(?:the\s+)?(?P<predicate>{P})\s+(?P<verb>is not|is|was|will be)\s+(?P<value>{V})"),
    ('direct', rf"(?:The\s+)?(?P<subject>{S})\s+(?P<predicate>{P})\s*(?P<verb>{COPULA})\s+(?P<value>{V})"),
    ('inverted_et', rf"(?P<value>{V})\s+(?P<verb>ei ole|on|oli)\s+(?P<subject>{S})\s+(?P<predicate>{P})"),
    ('allocated_value', rf"(?:A\s+)?(?P<predicate>budget)\s+of\s+(?P<value>{V})\s+(?P<verb>was allocated to|has been allocated to)\s+(?P<subject>{S})"),
    ('selected_value', rf"For\s+(?P<subject>{S}),\s*(?P<value>{V})\s+(?P<verb>was selected as)\s+(?:the\s+)?(?P<predicate>{P})"),
    ('deadline_requirement_et', rf"(?P<subject>{S})\s+(?P<predicate>(?P<verb>peab|pidi)\s+valmis\s+saama)\s+(?P<value>{V})"),
]
FRAMES = [(name,re.compile(pattern,re.I)) for name,pattern in FRAMES]
QUALIFIED = re.compile(r'\b(?:if|unless|maybe|might|could|should|possibly|perhaps|unknown|unverified|or|või|kui|proposed|proposal|hypothetical|assume)\b',re.I)


@dataclass(frozen=True)
class Relation:
    subject: str
    predicate: str
    value: str
    negative: bool
    uncertain: bool
    quoted: bool
    roles: tuple  # (role, absolute source byte start, end, exact substring)
    frame: str
    structured_record_id: str | None = None


def relations(text, *, source_start=0):
    for boundary in re.finditer(r'[^\n]+',text):
        # A question mark is not a declarative sentence terminator.
        line=boundary.group()
        for segment in re.finditer(r'.+?(?:[.!?](?=\s|$)|$)',line):
            raw=segment.group(); stripped=raw.strip()
            if not stripped or stripped.endswith('?'):
                continue
            left=len(raw)-len(raw.lstrip())
            quoted=stripped.startswith(('>','"','“'))
            prefix=re.match(r'[>"“\s]*',stripped).end()
            body=stripped[prefix:].rstrip('.!"” ')
            correction=re.match(r'(?:Correction:\s*|Parandus:\s*|Actually,?\s*)',body,re.I)
            offset=boundary.start()+segment.start()+left+prefix
            if correction:
                offset+=correction.end();body=body[correction.end():]
            for frame,pattern in FRAMES:
                match=pattern.fullmatch(body)
                if not match:
                    continue
                roles=[]
                for role in ('subject','predicate','value'):
                    a,b=match.span(role)
                    start=source_start+len(text[:offset+a].encode('utf-8'))
                    roles.append((role,start,start+len(match[role].encode('utf-8')),match[role]))
                predicate=('owner' if frame.startswith('owner_') else 'deadline' if frame=='deadline_requirement_et'
                           else PREDICATES[match['predicate'].casefold()])
                negative=bool(re.search(r'\bnot\b|\bei ole\b',match['verb'],re.I))
                yield Relation(match['subject'],predicate,match['value'],negative,
                    bool(QUALIFIED.search(body)),quoted,tuple(roles),frame)
                break
