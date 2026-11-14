"""Optional closed-value proposals checked by a separate entailment classifier.

Candidate values must occur in original evidence. The model can judge a bounded
textual proposition; it cannot propose permissions, action states, identities, dates
or new values. This experimental path is not selected by the default reader.
"""
from dataclasses import replace
import hashlib
import json
import math
import re
from .reader_claims import candidate,extract,VALUES
from .reader_contracts import ReaderBudgetExceeded

RECIPE='closed-values-entailment-v1'
GUARD=re.compile(r'ignore\s+(?:all|previous|other)|system\s*(?:override|:)|\b(?:quoted?|forwarded|propos\w*|maybe|might|could|should|possibly|perhaps|unknown|unverified|or|või|võib|pole\s+kinnitatud|kui|assume)\b',re.I)
DAYS=re.compile(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|esmaspäev(?:al)?|teisipäev(?:al)?|kolmapäev(?:al)?|neljapäev(?:al)?|reede(?:l)?|laupäev(?:al)?|pühapäev(?:al)?)\b',re.I)
NUMBER=re.compile(r'(?<![\w.-])\d+(?:[.,]\d+)?(?:\s*(?:EUR|USD|euros?|eurot|dollars?|€|%))?(?![\w-]|\.\d)',re.I)
NORMAL_DAYS={**VALUES,'kolmapäeval':'Wednesday','laupäev':'Saturday','laupäeval':'Saturday','pühapäev':'Sunday','pühapäeval':'Sunday'}


def augment(plan,units,model,*,threshold=.85,max_pairs=8):
    from .evidence_reader import question_slots
    slots,person,subject,desired=question_slots(plan)
    if desired or not subject or plan.intent in ('verify_action','locate_source'):
        return (),[]
    predicates=set(slots)&{'deadline','budget'}
    if not predicates:return (),[]
    original=extract(units);claims=[];trace=[];pairs=0
    contract=getattr(model,'contract',None)
    if not isinstance(contract,dict):raise ValueError('Versioned support model contract required')
    model_id=hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest()
    for unit in units:
        confidence=dict(unit.provenance_confidence).get('text')
        if unit.completeness in ('partial','truncated') or confidence is not None and confidence<1:
            continue
        if GUARD.search(unit.text) or re.search(r'(?:^|\n)\s*[>"“]',unit.text):
            continue
        for predicate in sorted(predicates):
            # Existing direct/uncertain claims keep their established semantics;
            # model scoring must not erase negation, uncertainty or a correction.
            if any(unit.evidence_id in c.support_ids and c.predicate==predicate for c in original):
                continue
            seen=set()
            for match in (DAYS if predicate=='deadline' else NUMBER).finditer(unit.text):
                raw=match[0];value=NORMAL_DAYS.get(raw.casefold(),raw)
                if value.casefold() in seen:continue
                seen.add(value.casefold());pairs+=1
                if pairs>max_pairs:raise ReaderBudgetExceeded('support_model_pair_budget_exceeded',max_pairs)
                hypothesis=f'The {predicate} for {subject} is {value}.'
                try:
                    result=model.score(unit.text,hypothesis)
                    score=result.get('probabilities',{}).get('entailment')
                except Exception as exc:
                    raise ReaderBudgetExceeded('support_model_unavailable_or_invalid',max_pairs) from exc
                if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:
                    raise ReaderBudgetExceeded('support_model_unavailable_or_invalid',max_pairs)
                trace.append(dict(evidence_id=unit.evidence_id,model_id=model_id,score=score,
                    hypothesis_hash=hashlib.sha256(hypothesis.encode()).hexdigest(),threshold=threshold,accepted=score>=threshold))
                if score<threshold:continue
                start=unit.start_byte+len(unit.text[:match.start()].encode())
                c=candidate(unit,subject,predicate,value,inference_type='derived',recipe=RECIPE+':'+model_id,
                    extraction_support=(('value',start,start+len(raw.encode()),raw),))
                claims.append(c)
    return tuple(claims),trace
