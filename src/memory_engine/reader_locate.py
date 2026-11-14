"""Exact observed-mention reading. Existence is weaker than factual truth."""
import re
from .query import IDENTIFIERS
from .reader_claims import candidate
from .reader_contracts import MAX_CLAIMS,ReaderBudgetExceeded


def requested(plan):
    if plan.intent=='verify_action':
        return False
    return plan.intent in ('locate_source','retrieve_exact_identifier') or (
        bool(plan.phrases) and plan.raw_text.strip().startswith(('"','“')))


def claims(plan,units):
    if not requested(plan):
        return ()
    needles=tuple(plan.exact_identifiers or plan.phrases or plan.entity_mentions)
    if not plan.exact_identifiers and not plan.phrases:
        # Adjacent capitalized mentions in the actual query form one requested
        # name; unrelated mentions separated by question grammar do not.
        spans=[]
        for name in needles:
            match=re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',plan.raw_text,re.I)
            if match:spans.append((match.start(),match.end()))
        merged=[]
        for start,end in sorted(spans):
            if merged and plan.raw_text[merged[-1][1]:start].isspace():
                merged[-1]=(merged[-1][0],end)
            else:merged.append((start,end))
        needles=tuple(plan.raw_text[a:b] for a,b in merged)
    if not needles:
        # A lexical match alone cannot interpret a paraphrased source description.
        return ()
    if len(needles)>32:
        raise ReaderBudgetExceeded('requested_mention_budget_exceeded',32)
    result=[]
    for needle in needles:
        for unit in units:
            if plan.exact_identifiers:
                found=needle.casefold() in {x.casefold() for x in IDENTIFIERS.findall(unit.text)}
            else:
                found=bool(re.search(r'(?<!\w)'+re.escape(needle)+r'(?!\w)',unit.text,re.I))
            if found:
                if len(result)>=MAX_CLAIMS:
                    raise ReaderBudgetExceeded('candidate_claim_budget_exceeded',MAX_CLAIMS)
                result.append(candidate(unit,needle,'observed_mention',needle,assertion_kind='observed_mention'))
    if {c.value.casefold() for c in result}!={n.casefold() for n in needles}:
        return ()
    return tuple(result)
