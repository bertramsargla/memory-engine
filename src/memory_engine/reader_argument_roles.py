"""Bounded role grammar. An actor mentioned by a source is not its author.

These explicit active/passive constructions yield linguistic source assertions,
never adapter-verified outcomes. Unsupported argument order remains unresolved.
"""
from dataclasses import dataclass
import re
from .reader_epistemics import strength

RECIPE='explicit-argument-roles-v1'
VERBS={'approved':'approved','approve':'approved','suggested':'suggested','suggest':'suggested',
       'proposed':'suggested','propose':'suggested','decided':'decided','decide':'decided',
       'prefers':'prefers','prefer':'prefers','preferred':'prefers',
       'kinnitas':'approved','kinnitati':'approved','kinnitatud':'approved',
       'kiitis heaks':'approved','kiitnud heaks':'approved',
       'pakkus':'suggested','pakuti':'suggested','otsustas':'decided',
       'otsustati':'decided','eelistab':'prefers','eelistas':'prefers'}
VERB='(?:'+'|'.join(re.escape(v) for v in sorted(VERBS,key=len,reverse=True))+')'
NP=r'[\w][\w\s€$.,%\-]{0,180}?'
EN_AUX=r'(?:has been|had been|have been|may have been|might have been|could have been|was|were|is|are)'
FRAMES=[
 ('passive',rf'(?P<object>{NP})\s+{EN_AUX}\s+(?P<negative>not\s+)?(?P<verb>{VERB})\s+by\s+(?P<actor>{NP})'),
 ('passive_et',rf'(?P<actor>{NP})\s+poolt\s+(?P<negative>ei\s+)?(?P<verb>{VERB})\s+(?P<object>{NP})'),
 ('object_first',rf'(?P<object>{NP}),\s*(?P<actor>{NP})\s+(?P<negative>did not\s+)?(?P<verb>{VERB})'),
 ('active',rf'(?P<actor>{NP})\s+(?:(?P<negative>did not|does not|do not|ei)\s+|(?:may|might|could|probably|võib-olla|tõenäoliselt)\s+)?(?P<verb>{VERB})\s+(?P<object>{NP})'),
]
FRAMES=[(name,re.compile(pattern,re.I)) for name,pattern in FRAMES]
REPORTED=re.compile(r'\b(?:said|says|wrote|writes|reported|forwarded|ütles|sõnul|kirjutas|tsiteeris|edastatud)\b',re.I)
ET_ACCUSATIVE=re.compile(r'\b(?:hinna|päeva|lepingu|otsuse|eelarve|pakkumise)\s*$',re.I)
ET_OBJECT_FIRST=re.compile(rf'(?P<object>{NP})\s+(?P<verb>kinnitas|kiitis heaks)\s+(?P<actor>{NP})',re.I)


@dataclass(frozen=True)
class ArgumentFrame:
    actor: str | None
    relation: str
    object: str
    polarity: str
    epistemic_strength: str
    temporal_modifier: str | None
    voice: str
    roles: tuple
    speaker_of_source: str | None = None
    speaker_inside_quote: str | None = None
    reported_speaker: str | None = None
    attribution: str = 'source_mentions_actor_identity_unresolved'
    recipe: str = RECIPE


def frames(text,*,source_start=0):
    segments=[(line.start()+segment.start(),segment[0]) for line in re.finditer(r'[^\n]+',text)
        for segment in re.finditer(r'.+?(?:[.!?](?=\s|$)|$)',line[0])]
    for base,raw in segments:
        body=raw.strip().rstrip('.!');offset=base+len(raw)-len(raw.lstrip())
        if not body or raw.strip().endswith('?') or body.startswith(('>','"','“','„')) or REPORTED.search(body):
            continue
        temporal=None;temporal_span=None
        leading=re.match(r'(?P<time>On\s+[^,]+),\s*',body,re.I)
        if leading:
            temporal=leading['time'];temporal_span=(offset+leading.start('time'),offset+leading.end('time'))
            offset+=leading.end();body=body[leading.end():]
        options=list(FRAMES)
        # In object-first Estonian, require an overt supported object-case marker.
        # Do not reverse two ambiguous person names by guessing a word order.
        special=ET_OBJECT_FIRST.fullmatch(body)
        if special and ET_ACCUSATIVE.search(special['object']):options.insert(0,('object_first_et',ET_OBJECT_FIRST))
        for voice,pattern in options:
            match=pattern.fullmatch(body)
            if not match:continue
            # Beneficiary postpositions do not identify the actor of an impersonal
            # verb. The source can say "for X" without saying "by X".
            if re.search(r'\b(?:jaoks|heaks)\s*$',match['actor'],re.I):continue
            # Estonian active transitives can place either argument first. Without
            # an overt supported object case, two names are not ordered by guess.
            if voice=='active' and match['verb'].casefold() in ('kinnitas','kiitis heaks','pakkus') and not ET_ACCUSATIVE.search(match['object']):
                continue
            # A nominative/oblique recipient before an unsupported verb does not
            # become an actor. Only the declared transitive grammar is recognized.
            roles=[];values={}
            for name in ('actor','verb','object'):
                a,b=match.span(name);value=match[name]
                article=re.match(r'(?i)(?:the|a|an)\s+',value) if name!='verb' else None
                if article:a+=article.end();value=value[article.end():]
                start=source_start+len(text[:offset+a].encode());end=source_start+len(text[:offset+b].encode())
                roles.append((name,start,end,value));values[name]=value
            if temporal_span:
                a,b=temporal_span;roles.append(('temporal_modifier',source_start+len(text[:a].encode()),source_start+len(text[:b].encode()),text[a:b]))
            negative=bool(match.groupdict().get('negative'))
            yield ArgumentFrame(values['actor'],VERBS[values['verb'].casefold()],values['object'],
                'negative' if negative else 'positive',strength(body),temporal,voice,tuple(roles))
            break
