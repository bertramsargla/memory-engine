"""Per-query evidence reader: explicit projections, support, slots, plan, verification.

This intentionally bounded reader never certifies arbitrary free prose. Unsupported
linguistic structures abstain and remain visible in evaluation. There is no persistence
or tool/permission authority. An optional bounded support classifier must be supplied
explicitly; the default path is deterministic.
"""
from dataclasses import asdict, replace
import json
import re
import time
from .reader_contracts import AnswerabilityResult, AnswerPlan, Edge, identity_hash,MAX_CLAIMS,ReaderBudgetExceeded
from .reader_claims import extract, support_graph, PREDICATES, incompatible
from .reader_identity import resolve, actor_key
from .reader_temporal import eligible
from .reader_verification import render, verify_generated
from . import reader_locate
from .reader_relation_query import parse as relation_question
from .reader_support_trace import build as support_stage_trace
from . import reader_question_language
from . import reader_witness_plan

RECIPE='bounded-evidence-reader-v3.14-source-witness-role-separation'


def question_slots(plan):
    text=plan.raw_text
    witness_question=reader_witness_plan.request(text)
    if witness_question:return (witness_question.slot,),None,witness_question.subject,None
    relation=relation_question(text)
    if relation:
        return (relation.predicate,'unambiguous_reported_actor'),None,relation.actor,None
    if reader_locate.requested(plan):
        return ('observed_mention',),None,None,None
    functional=reader_question_language.functional(text)
    if functional:
        return functional[0],None,functional[1],None
    person=re.search(r'\bwhat did\s+(.+?)\s+(?:say|tell|write)\b',text,re.I)
    person=person[1].strip() if person else reader_question_language.person(text)
    if person:
        person=re.sub(r'\s+in\s+my\s+\w+\s+account$','',person,flags=re.I)
    slots=tuple(dict.fromkeys(v for k,v in PREDICATES.items() if re.search(r'\b'+re.escape(k)+r'\b',text,re.I)))
    # Contract as an object is not a requested linguistic relation for action checks.
    desired=None
    if plan.intent=='verify_action':
        desired=next((state for pattern,state in [('send|sent','sent'),('read','read'),('approve|approved','approved')]
                      if re.search(r'\b('+pattern+r')\b',text,re.I)),None)
        slots=('resolved_action_target','verified_delivery_state')
    elif re.search(r'\bwhen\b',text,re.I) and re.search(r'\bmeeting\b',text,re.I):
        slots=('meeting_time',)
    if person:
        slots=('resolved_speaker_identity',*slots) if slots else ('resolved_speaker_identity','person_statement')
    if slots and re.search(r'\b(?:exactly|exact|täpselt|täpne)\b',text,re.I):
        slots=(*slots,'exact_value')
    suffix=re.search(r'\b(?:and|ja|ning)\s+([^?]+)',text,re.I)
    if suffix and not any(re.search(r'\b'+re.escape(k)+r'\b',suffix[1],re.I) for k in PREDICATES):
        slots=(*slots,'unparsed_requirement')
    ignored={'what','when','where','which','mis','kus','i',person.casefold() if person else ''}
    subjects=[e for e in plan.entity_mentions if e.casefold() not in ignored]
    subject=subjects[0] if len(subjects)==1 else None
    return slots,person,subject,desired


def read(plan,units,*,debug=False,identity_witnesses=(),support_model=None,semantic_normalizer=None):
    started=time.perf_counter()
    role_resolver=None
    if semantic_normalizer is not None and hasattr(semantic_normalizer,'backend'):
        from .reader_role_bindings import RoleResolver
        role_resolver=RoleResolver(semantic_normalizer.backend,dependency=getattr(semantic_normalizer,'role_dependency',None))
    witness_question=reader_witness_plan.request(plan.raw_text)
    claims=reader_witness_plan.project(witness_question,units) if witness_question else extract(units,role_resolver=role_resolver)+reader_locate.claims(plan,units)
    if semantic_normalizer is not None:
        projected=[]
        for claim in claims:
            # Only common object noun phrases, never actor identity or event time.
            if claim.predicate in ('approved','suggested') and claim.assertion_kind=='source_statement':
                relation_context=relation_question(plan.raw_text)
                if hasattr(semantic_normalizer,'resolve_value'):
                    object_span=next((s for s in claim.extraction_support if s[0]=='object'),None)
                    value=semantic_normalizer.resolve_value(claim.value,role='object',
                        question=relation_context.object if relation_context and relation_context.predicate==claim.predicate else None,
                        source_start=object_span[1] if object_span else 0)
                else:
                    value=semantic_normalizer.value(claim.value)
                encoded=json.dumps(asdict(value),ensure_ascii=False,sort_keys=True)
                claim=replace(claim,claim_id=identity_hash([claim.claim_id,encoded,semantic_normalizer.contract]),
                    value=value.normalized_phrase if value.state=='RESOLVED' else claim.value,
                    semantic_value_json=encoded)
                if claim.recipe=='case-constrained-role-bindings-v1' and value.state=='AMBIGUOUS':
                    claim=replace(claim,uncertain=True,inference_type='uncertain')
                if claim.recipe=='case-constrained-role-bindings-v1' and hasattr(semantic_normalizer,'display_name'):
                    actor_span=next((s for s in claim.extraction_support if s[0]=='actor'),None)
                    verb_span=next((s for s in claim.extraction_support if s[0]=='verb'),None)
                    if actor_span and verb_span:
                        passive=verb_span[3].casefold() in ('kinnitati','pakuti')
                        name=semantic_normalizer.display_name(actor_span[3],grammatical_case='sg g' if passive else 'sg n',
                            role='passive_agent' if passive else 'actor',source_start=actor_span[1])
                        encoded_name=json.dumps(asdict(name),ensure_ascii=False,sort_keys=True)
                        claim=replace(claim,claim_id=identity_hash([claim.claim_id,encoded_name]),
                            subject=name.display_name if name.display_name_resolution=='RESOLVED' else claim.subject,
                            semantic_subject_json=encoded_name)
                        if name.display_name_resolution=='AMBIGUOUS' or passive and name.display_name_resolution!='RESOLVED':
                            claim=replace(claim,uncertain=True,inference_type='uncertain')
            if claim.predicate=='owner' and claim.assertion_kind=='source_statement':
                value=semantic_normalizer.common_head(claim.subject)
                encoded=json.dumps(asdict(value),ensure_ascii=False,sort_keys=True)
                claim=replace(claim,claim_id=identity_hash([claim.claim_id,encoded,semantic_normalizer.contract]),
                    subject=value.normalized_phrase if value.state=='RESOLVED' else claim.subject,
                    semantic_subject_json=encoded)
            projected.append(claim)
        claims=tuple(projected)
    support_trace=[]
    if support_model is not None:
        from .reader_semantic import augment
        extra,support_trace=augment(plan,units,support_model)
        claims+=extra
    if len(claims)>MAX_CLAIMS:
        raise ReaderBudgetExceeded('candidate_claim_budget_exceeded',MAX_CLAIMS)
    edges=list(support_graph(claims))
    slots,person,subject,desired=question_slots(plan)
    if semantic_normalizer is not None and subject and 'owner' in slots:
        normalized=semantic_normalizer.common_head(subject)
        if normalized.state=='RESOLVED':subject=normalized.normalized_phrase
    relation=relation_question(plan.raw_text)
    if slots and plan.temporal_mode in ('current','latest') and 'observed_mention' not in slots and not witness_question:
        slots=(*slots,'temporal_currentness')
    if plan.temporal_mode=='sequence':
        slots=(*slots,'anchor_occurrence')
    lookup={u.evidence_id:u for u in units}; temporal_decisions=[]
    candidates=[]
    for c in claims:
        ok,reason=eligible(c,plan)
        temporal_decisions.append(dict(claim_id=c.claim_id,eligible=ok,reason=reason))
        if ok:
            candidates.append(c)
    identity_units=tuple(u for u in units if not plan.account_hints or u.account in plan.account_hints)
    identity=resolve(person,identity_units+tuple(identity_witnesses)) if person else None
    identity_ids=set(identity.evidence_ids) if identity else set(lookup)
    if identity:
        for a in units:
            for b in units:
                if a.evidence_id>=b.evidence_id:
                    continue
                ak,bk=actor_key(a.author_identity),actor_key(b.author_identity)
                same_name=a.author_identity.display_name and a.author_identity.display_name==b.author_identity.display_name
                if ak and ak==bk:
                    edges.append(Edge(a.evidence_id,'SAME_ENTITY_AS',b.evidence_id,'strong_actor_identifier'))
                elif same_name:
                    edges.append(Edge(a.evidence_id,'POSSIBLY_SAME_ENTITY_AS',b.evidence_id,'name_is_not_identity'))
        candidates=[c for c in candidates if set(c.support_ids)&identity_ids
                    and c.assertion_kind not in ('quotation','reviewed_interpretation')]
    if relation:
        candidates=[c for c in candidates if relation.matches(c,semantic_normalizer)]
    elif subject:
        candidates=[c for c in candidates if c.subject.casefold()==subject.casefold() or c.predicate=='action_state']
    else:
        # A question with multiple unresolved subject mentions must not be answered
        # by choosing whichever retrieved assertion happened to appear first.
        if not person and 'observed_mention' not in slots:
            candidates=[]
    relevant=[]
    action_target_units=[]
    if desired and len(plan.exact_identifiers)==1:
        identifier=plan.exact_identifiers[0].casefold()
        action_target_units=[u for u in identity_units if u.source_identity_confidence=='adapter_asserted' and identifier in
            {x.casefold() for x in (u.source_native_id,u.source_document_id) if x}]
    action_target_ids={u.evidence_id for u in action_target_units}
    target_ambiguous=len({u.source_id for u in action_target_units})>1
    for c in candidates:
        if desired:
            if c.predicate!='action_state' or c.value!=desired or not set(c.support_ids)&action_target_ids:
                continue
            c=replace(c,subject=plan.exact_identifiers[0])
        elif c.predicate not in slots and 'person_statement' not in slots:
            continue
        relevant.append(c)
    # Reviewed interpretations may resolve the requested memory state. They remain
    # reviewed assertions in the answer, not upgraded objective truth. Contradicting
    # unreviewed assertions stay visible in the graph and trace.
    reviews={(c.subject.casefold(),c.predicate) for c in relevant if c.assertion_kind=='reviewed_interpretation'}
    selected_pool=[c for c in relevant if c.assertion_kind=='reviewed_interpretation'
        or (c.subject.casefold(),c.predicate) not in reviews]
    conflicts=[]
    for i,a in enumerate(selected_pool):
        for b in selected_pool[i+1:]:
            if (a.subject.casefold(),a.predicate)!=(b.subject.casefold(),b.predicate):
                continue
            if incompatible(a,b):
                conflicts.extend([a.claim_id,b.claim_id])
    positive=[c for c in selected_pool if not c.uncertain and c.polarity=='positive' and c.assertion_kind!='quotation']
    dedup={}
    for c in positive:
        key=(c.subject.casefold(),c.predicate,c.value.casefold(),c.speaker,c.assertion_kind)
        if key not in dedup:
            dedup[key]=c
    chosen=list(dedup.values())
    satisfied=set(c.predicate for c in chosen)
    reported_actor_ambiguous=False
    if relation and chosen:
        sources={lookup[e].source_id for c in positive for e in c.support_ids}
        actors={c.subject.casefold() for c in positive}
        # No adapter identity edge binds an actor mentioned inside arbitrary text.
        # Distinct source reports must not be merged into one person by display name.
        reported_actor_ambiguous=len(actors)>1 or len(sources)>1
        if not reported_actor_ambiguous:satisfied.add('unambiguous_reported_actor')
    if chosen and all(c.value_precision=='EXACT' for c in chosen):
        satisfied.add('exact_value')
    if reviews and any(c.assertion_kind=='reviewed_interpretation' for c in chosen):
        satisfied.add('temporal_currentness')
    if desired and chosen:
        satisfied.add('verified_delivery_state')
        if not target_ambiguous:satisfied.add('resolved_action_target')
    if identity and identity.state=='RESOLVED':
        satisfied.add('resolved_speaker_identity')
    if person and chosen:
        satisfied.add('person_statement')
    missing=set(slots)-satisfied
    provenance=[]; ambiguous=[]; reason='bounded_claims_cover_question'; state='SUFFICIENT'
    if not units:
        state,reason='INSUFFICIENT','no_selected_evidence_capture_coverage_unknown'
    elif not slots:
        state,reason='INSUFFICIENT','question_contract_not_supported'
    elif re.search(r'\bbelieve(?:d)?\b',plan.raw_text,re.I):
        state,reason='INSUFFICIENT','observation_does_not_establish_user_belief'
        provenance.append('explicit_user_belief')
    elif identity and identity.state=='AMBIGUOUS':
        state,reason='AMBIGUOUS','multiple_unmerged_speaker_identities';ambiguous.append('resolved_speaker_identity')
    elif identity and identity.state=='UNKNOWN':
        state,reason='INSUFFICIENT',identity.reason;provenance.append('resolved_speaker_identity')
    elif reported_actor_ambiguous:
        state,reason='AMBIGUOUS','reported_actor_not_identity_resolved';ambiguous.append('unambiguous_reported_actor')
    elif target_ambiguous:
        state,reason='AMBIGUOUS','same_identifier_multiple_action_objects';ambiguous.append('resolved_action_target')
    elif witness_question and len({c.value for c in chosen})>1:
        state,reason='AMBIGUOUS','multiple_explicit_source_witnesses';ambiguous.append(witness_question.slot)
    elif missing:
        state,reason='INSUFFICIENT','required_slots_missing'
    elif conflicts:
        state,reason='CONFLICTING','incompatible_supported_or_uncertain_assertions'
    elif plan.temporal_mode in ('current','latest') and not reviews and 'observed_mention' not in slots and not witness_question:
        state,reason='INSUFFICIENT','temporal_currentness_not_established';missing.add('temporal_currentness')
    elif plan.temporal_mode=='sequence':
        state,reason='INSUFFICIENT','sequence_anchor_not_established';missing.add('anchor_occurrence')
    if desired and not chosen:
        provenance.append('verified_delivery_state')
    if desired and not action_target_ids:
        provenance.append('resolved_action_target')
    if plan.temporal_mode=='known_at_time' and not chosen:
        provenance.append('known_at')
    answerability=AnswerabilityResult(state,tuple(slots),tuple(sorted(satisfied & set(slots))),tuple(sorted(missing)),
        tuple(ambiguous),tuple(dict.fromkeys(conflicts)),tuple(c.claim_id for c in chosen) if state=='SUFFICIENT' else (),
        tuple(provenance),(reason,))
    allowed=tuple(chosen) if state=='SUFFICIENT' else ()
    answer_plan=AnswerPlan(state,'supported_source_report' if allowed else 'clarification' if state=='AMBIGUOUS' else 'abstention',
        tuple(c.claim_id for c in allowed),tuple(c.claim_id for c in claims if c.claim_id not in {x.claim_id for x in allowed}),
        tuple(dict.fromkeys(e for c in allowed for e in c.support_ids)),
        ('Source statements are observed assertions; unknown author/event time stays unknown.',) if allowed else (reason,))
    prose=render(allowed,units)
    verification=verify_generated(prose,[asdict(c) for c in allowed],answer_plan,allowed,units)
    if not verification['accepted']:
        raise RuntimeError('Internal bounded renderer violated its answer plan')
    if not allowed:
        prose={'AMBIGUOUS':'Which person do you mean? The evidence contains unresolved identities.',
               'CONFLICTING':'The selected evidence contains conflicting assertions. A supported resolution is missing.',
               'INSUFFICIENT':'The selected evidence does not establish the requested answer.'}[state]
        if witness_question and state=='AMBIGUOUS':
            prose=('The selected source attributes statements to multiple names. Specify which quotation or report you mean.'
                if witness_question.slot in ('quoted_speaker','reported_speaker') else
                'The selected evidence contains multiple time witnesses. Specify which event or source occurrence you mean.')
    trace=dict(recipe=RECIPE,query_hash=identity_hash(plan.raw_text),scope_hash=identity_hash(plan.scope),
        evidence=[dict(evidence_id=u.evidence_id,revision_id=u.revision_id,occurrence_id=u.occurrence_id,
            span=[u.start_byte,u.end_byte],text_hash=u.content_hash,input_hash=u.input_hash,trust_class=u.trust_class) for u in units],
        candidates=[dict(claim_id=c.claim_id,support_ids=c.support_ids,kind=c.assertion_kind,uncertain=c.uncertain) for c in claims],
        identity=dict(state=identity.state,reason=identity.reason) if identity else None,
        identity_witness_ids=[w.evidence_id for w in identity_witnesses],
        explicit_witness_slot=witness_question.slot if witness_question else None,
        support_model_judgments=support_trace,
        temporal=temporal_decisions,graph=[asdict(e) for e in edges],answerability=asdict(answerability),
        selected_claim_ids=answer_plan.claims_allowed,bytes=sum(len(u.text.encode()) for u in units),
        byte_budget=64000,milliseconds=(time.perf_counter()-started)*1000,authority='none')
    trace['support_stages']=support_stage_trace(plan,units,claims,slots,candidates,relevant,chosen,identity,
        temporal_decisions,edges,allowed,verification,answerability)
    trace['semantic_normalization']=dict(contract=semantic_normalizer.contract if semantic_normalizer else None,
        claims=[dict(claim_id=c.claim_id,state=json.loads(c.semantic_value_json)['state'],
            reason=json.loads(c.semantic_value_json)['reason']) for c in claims if c.semantic_value_json],
        identity_authority='none')
    if debug:
        trace['developer_raw_claims']=[asdict(c) for c in claims]
    result=dict(state=state,answer=prose,claims=[asdict(c) for c in allowed],reason_codes=[reason],
        answerability=asdict(answerability),answer_plan=asdict(answer_plan),verification=verification,trace=trace,
        recipe=RECIPE,capture_coverage='unknown',long_term_memory_written=False)
    return json.loads(json.dumps(result,ensure_ascii=False,allow_nan=False))
