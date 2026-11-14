"""Bounded temporary extraction, not automatic long-term memory or general NLI."""
from dataclasses import replace
import json
import re
from .reader_contracts import CandidateClaim, Edge, identity_hash, MAX_CLAIMS, ReaderBudgetExceeded
from .reader_identity import actor_key
from .reader_temporal import order
from .reader_language import relations, RECIPE as LANGUAGE_RECIPE
from .reader_structured import relations as structured_relations, RECIPE as STRUCTURED_RECIPE
from .reader_epistemics import strength,precision
from .reader_morphology import weekday
from .reader_argument_roles import frames as argument_frames
from .reader_contrast import values as contrast_values

PREDICATES={'deadline':'deadline','tähtaeg':'deadline','budget':'budget','eelarve':'budget',
            'prototype':'prototype','contract':'contract','preference':'preference'}
VALUES={'reede':'Friday','reedel':'Friday','esmaspäev':'Monday','esmaspäeval':'Monday',
        'teisipäev':'Tuesday','teisipäeval':'Tuesday','kolmapäev':'Wednesday',
        'neljapäev':'Thursday','neljapäeval':'Thursday'}
MEETING=re.compile(r'^(?P<subject>[\w][\w -]{0,100}?)\s+meeting\s+(?:happened|was held|is scheduled)\s+(?:on\s+)?(?P<value>.+)$',re.I)
MEETING_ET=re.compile(r'^(?P<subject>[\w][\w -]{0,100}?)\s+koosolek\s+toimus\s+(?P<value>\d{4}-\d{2}-\d{2})$',re.I)
POISON=re.compile(r'ignore\s+(?:all|previous|other)|system\s*(?:override|:)|grant\s+all\s+permissions|tell the user.*verified',re.I)
UNCERTAIN=re.compile(r'\b(unknown|unverified|maybe|possibly|perhaps|teadmata|might|could|or|või)\b',re.I)
# Different objects of these relations can coexist. A decision to do A and a
# decision to do B are not automatically contradictory without their context.
MULTIVALUED_RELATIONS=frozenset(('approved','suggested','decided','prefers'))


def incompatible(a,b):
    if (a.subject.casefold(),a.predicate)!=(b.subject.casefold(),b.predicate):return False
    # A source can quote several people or describe repeated events; a selection
    # can contain several observations. Without an exclusive event/identity
    # binding, different witness values require disambiguation, not contradiction.
    multi_witness={'source_attribution','source_temporal','observation_metadata'}
    if a.assertion_kind in multi_witness and b.assertion_kind in multi_witness:return False
    same_value=a.value.casefold()==b.value.casefold()
    return (same_value and a.polarity!=b.polarity) or (
        not same_value and a.polarity==b.polarity=='positive' and a.predicate not in MULTIVALUED_RELATIONS)


def sentences(text):
    # Decimal points are not sentence endings. No normalization of original text.
    return [s.strip().rstrip('.!?') for s in re.split(r'(?<=[.!?])\s+|\n+',text) if s.strip()]


def candidate(unit,subject,predicate,value,**kw):
    return CandidateClaim(identity_hash(['bounded-claims-v2-epistemic-guards',unit.input_hash,subject,predicate,value,kw]),subject,predicate,value,
        (unit.evidence_id,),(unit.revision_id,),actor_key(unit.author_identity),unit.authored_at,unit.known_at,
        None,None,kw.pop('assertion_kind','source_statement'),kw.pop('inference_type','direct'),
        speaker_confidence=unit.author_identity.confidence,**kw)


def extract(units,*,role_resolver=None):
    claims=[]; seen_reviews=set()
    def add(claim):
        if len(claims)>=MAX_CLAIMS:
            raise ReaderBudgetExceeded('candidate_claim_budget_exceeded',MAX_CLAIMS)
        claims.append(claim)
    for unit in units:
        forwarded=bool(re.search(r'forwarded message|begin forwarded|original message',unit.text,re.I))
        # Without adapter message spans, a multiline quotation cannot safely be
        # attributed line-by-line to the outer sender. Preserve uncertainty for it.
        multiline_quote=bool(re.search(r'(?:^|\n)\s*["“][\s\S]*\n',unit.text))
        instruction_bearing=bool(POISON.search(unit.text))
        text_confidence=dict(unit.provenance_confidence).get('text')
        partial=unit.completeness in ('partial','truncated') or (text_confidence is not None and text_confidence<1)
        parsed_relations=[(r,LANGUAGE_RECIPE) for r in relations(unit.text,source_start=unit.start_byte)]
        parsed_relations.extend((r,STRUCTURED_RECIPE) for r in structured_relations(unit.text,source_start=unit.start_byte,
            source_id=unit.source_id,revision_id=unit.revision_id,occurrence_id=unit.occurrence_id))
        for parsed,recipe in parsed_relations:
            quoted=forwarded or multiline_quote or parsed.quoted
            contrast=contrast_values(parsed.value)
            corrections=list(contrast) if contrast else re.split(r',\s*(?:not|mitte)\s+',parsed.value,maxsplit=1,flags=re.I)
            value=weekday(corrections[0]) or VALUES.get(corrections[0].casefold(),corrections[0])
            c=candidate(unit,parsed.subject,parsed.predicate,value,
                polarity='negative' if parsed.negative else 'positive',
                assertion_kind='quotation' if quoted else 'source_statement',
                uncertain=parsed.uncertain or instruction_bearing or partial or strength(parsed.value)!='CERTAIN',
                epistemic_strength=strength(parsed.value),value_precision=precision(value),
                extraction_support=parsed.roles,recipe=recipe,structured_record_id=parsed.structured_record_id)
            if quoted:
                c=replace(c,speaker=None,speaker_confidence=None,inference_type='uncertain')
            add(c)
            if len(corrections)>1 and not parsed.negative:
                add(replace(c,claim_id=identity_hash([c.claim_id,'negated',corrections[1]]),
                    polarity='negative',value=weekday(corrections[1]) or VALUES.get(corrections[1].casefold(),corrections[1])))
        for sentence in sentences(unit.text):
            quoted=forwarded or multiline_quote or sentence.startswith(('>','"','“'))
            meeting=MEETING.fullmatch(sentence) or MEETING_ET.fullmatch(sentence)
            if meeting:
                add(candidate(unit,meeting['subject'],'meeting_time',meeting['value'],
                    assertion_kind='quotation' if quoted else 'source_statement', uncertain=quoted or instruction_bearing or partial))
        frames=list(argument_frames(unit.text,source_start=unit.start_byte))
        if role_resolver is not None:
            new=list(role_resolver.frames(unit.text,source_start=unit.start_byte))
            keys={(f.actor,f.relation,f.object,f.polarity) for f in frames}
            frames.extend(f for f in new if (f.actor,f.relation,f.object,f.polarity) not in keys)
        for frame in frames:
            quoted=forwarded or multiline_quote
            # Infinitival "decided to X" reports a decision, never completion of X.
            value=re.sub(r'^to\s+','',frame.object,flags=re.I) if frame.relation=='decided' else frame.object
            c=candidate(unit,frame.actor,frame.relation,value,
                polarity=frame.polarity,epistemic_strength=frame.epistemic_strength,
                value_precision=precision(value),extraction_support=frame.roles,recipe=frame.recipe,
                assertion_kind='quotation' if quoted else 'source_statement',
                uncertain=quoted or instruction_bearing or partial or frame.epistemic_strength!='CERTAIN')
            if quoted:c=replace(c,speaker=None,speaker_confidence=None,inference_type='uncertain')
            add(c)
        if unit.action_state in ('sent','read','approved','failed') and unit.action_basis:
            # Action metadata covers this observed object; the question still has
            # to establish which object it asks about before selecting this claim.
            add(candidate(unit,'observed_object','action_state',unit.action_state,
                assertion_kind='adapter_outcome'))
        for encoded in unit.reviewed_json:
            review=json.loads(encoded)
            if review['id'] in seen_reviews:
                continue
            supports=[]
            for edge in review['supports']:
                selected=[u for u in units if u.revision_id==edge['revision_id']
                    and u.start_byte<=edge['start_byte'] and u.end_byte>=edge['end_byte']]
                if not selected:
                    supports=[];break
                supports.append(selected[0])
            if not supports or not all(review.get(k) for k in ('subject_id','predicate','value')):
                continue
            seen_reviews.add(review['id'])
            add(CandidateClaim(identity_hash(['review-v2',encoded,[u.input_hash for u in supports]]),
                review['subject_id'],review['predicate'],review['value'],tuple(u.evidence_id for u in supports),
                tuple(dict.fromkeys(u.revision_id for u in supports)),None,None,review['known_from'],
                review['valid_from'],review['valid_to'],'reviewed_interpretation','derived',
                review_id=review['id'],supersedes=review['supersedes'],known_to=review['known_to']))
    return tuple(claims)


def support_graph(claims):
    edges=[]
    for claim in claims:
        edges.extend(Edge(eid,'SUPPORTS',claim.claim_id,claim.assertion_kind) for eid in claim.support_ids)
    for i,a in enumerate(claims):
        for b in claims[i+1:]:
            if (a.subject.casefold(),a.predicate)!=(b.subject.casefold(),b.predicate):
                continue
            if a.supersedes and a.supersedes==b.review_id:
                edges.append(Edge(a.claim_id,'SUPERSEDES',b.claim_id,'explicit_reviewed_supersession'))
            if b.supersedes and b.supersedes==a.review_id:
                edges.append(Edge(b.claim_id,'SUPERSEDES',a.claim_id,'explicit_reviewed_supersession'))
            if incompatible(a,b) and a.value.casefold()!=b.value.casefold():
                edges.append(Edge(a.claim_id,'CONTRADICTS',b.claim_id,'incompatible_slot_values'))
            elif a.value.casefold()==b.value.casefold() and a.polarity!=b.polarity:
                edges.append(Edge(a.claim_id,'CONTRADICTS',b.claim_id,'explicit_negation'))
            edges.append(Edge(a.claim_id,order(a,b),b.claim_id,'known_time_only'))
    return tuple(edges)
