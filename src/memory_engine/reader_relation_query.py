"""Bounded source-reported relation questions, separate from verified actions.

This contract asks for an argument of an explicit source assertion. It does not
establish that the reported event succeeded or resolve a named person globally.
"""
from dataclasses import dataclass
import re

VERBS={'approve':'approved','approved':'approved','suggest':'suggested','suggested':'suggested',
       'propose':'suggested','proposed':'suggested','decide':'decided','decided':'decided',
       'prefer':'prefers','prefers':'prefers','kinnitas':'approved','pakkus':'suggested',
       'otsustas':'decided','eelistab':'prefers'}
V='(?:'+'|'.join(sorted(VERBS,key=len,reverse=True))+')'
PATTERNS=[re.compile(rf'What\s+(?:did|does)\s+(?P<actor>.+?)\s+(?P<verb>{V})\??',re.I),
          re.compile(rf'(?:Who|Kes)\s+(?P<verb>{V})\s+(?P<object>.+?)\??',re.I),
          re.compile(rf'Mida\s+(?P<actor>.+?)\s+(?P<verb>{V})\??',re.I)]
QUALIFIED=[re.compile(r'What\s+does\s+(?P<actor>.+?)\s+prefer\s+for\s+(?P<qualifier>.+?)\?',re.I),
           re.compile(r'How\s+does\s+(?P<actor>.+?)\s+prefer\s+to\s+receive\s+(?P<qualifier>.+?)\?',re.I),
           re.compile(r'Kuidas\s+eelistab\s+(?P<actor>.+?)\s+(?P<qualifier>\w+)\s+saada\?',re.I)]
# These are question-domain lexical equivalents, not entity aliases or evidence.
QUALIFIER_TERMS={'invoices':('invoices','invoice','arveid','arved','arve'),
                 'küsimusi':('küsimusi','küsimused','küsimus')}


def noun(value):
    return re.sub(r'^(?:the|a|an)\s+','',value.strip().rstrip('?'),flags=re.I).casefold()


@dataclass(frozen=True)
class RelationQuestion:
    predicate: str
    actor: str | None
    object: str | None
    requested_role: str
    qualifier: str | None = None
    recipe: str = 'source-relation-question-v1'

    def matches(self,claim,normalizer=None):
        object_matches = self.object is None or noun(claim.value)==noun(self.object)
        if not object_matches and normalizer is not None:
            object_matches = normalizer.equivalent(claim.value,self.object)
        qualifier_matches=True
        if self.qualifier:
            forms=QUALIFIER_TERMS.get(self.qualifier.casefold(),(self.qualifier,))
            qualifier_matches=any(re.search(r'(?<!\w)'+re.escape(form)+r'(?!\w)',claim.value,re.I) for form in forms)
        return claim.predicate==self.predicate and (self.actor is None or noun(claim.subject)==noun(self.actor)) and (
            object_matches) and qualifier_matches


def parse(text):
    # Never route explicit verification/provenance requirements to text semantics.
    if re.search(r'\b(?:actually|verified|really|final|finally|definitely|sent|read|tegelikult|kindlalt|lõplikult)\b',text,re.I):
        return None
    for pattern in QUALIFIED:
        match=pattern.fullmatch(text.strip())
        if match and not re.search(r'\b(?:and|or|if|ja|või|kui)\b',match['actor']+' '+match['qualifier'],re.I):
            return RelationQuestion('prefers',match['actor'],None,'object',match['qualifier'])
    for pattern in PATTERNS:
        match=pattern.fullmatch(text.strip())
        if match:
            actor=match.groupdict().get('actor');obj=match.groupdict().get('object')
            if any(re.search(r'\b(?:and|or|if|ja|või|kui)\b',s,re.I) for s in (actor,obj) if s):return None
            return RelationQuestion(VERBS[match['verb'].casefold()],actor,obj,'object' if actor else 'actor')
    return None
