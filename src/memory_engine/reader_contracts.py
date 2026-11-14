"""Immutable, per-query evidence projections, loaded only through authorized SQL."""
from dataclasses import dataclass, field, asdict
import hashlib
import json

RECIPE = 'original-span-ir-v1'
MAX_CLAIMS = 128


class ReaderBudgetExceeded(ValueError):
    def __init__(self,code,limit):
        super().__init__(code)
        self.code,self.limit=code,limit


def budget_result(error):
    message=('The support model could not complete an assessment of the selected evidence.'
        if error.code=='support_model_unavailable_or_invalid' else 'The selected evidence exceeds the bounded reader budget. Narrow the selection.')
    return dict(state='INSUFFICIENT',answer=message,
        claims=[],reason_codes=[error.code],
        answerability=dict(state='INSUFFICIENT',required_question_slots=['complete_bounded_candidate_set'],
            satisfied_slots=[],missing_slots=['complete_bounded_candidate_set'],ambiguous_slots=[],
            conflicting_claims=[],supporting_claim_ids=[],missing_provenance=[],reason_codes=[error.code]),
        answer_plan=dict(answerability='INSUFFICIENT',answer_type='abstention',claims_allowed=[],claims_forbidden=[],
            required_support=[],uncertainty_to_expose=[error.code],citations_required=True,authority='none'),
        verification=dict(accepted=False,claims=[],reason='extraction_incomplete'),
        trace=dict(reason=error.code,limit=error.limit,authority='none'),
        recipe='reader-budget-failure-v1',capture_coverage='unknown',long_term_memory_written=False)


@dataclass(frozen=True)
class Identity:
    native_id: str | None = None
    display_name: str | None = None
    platform: str | None = None
    workspace: str | None = None
    account: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class EvidenceUnit:
    evidence_id: str
    source_id: str
    revision_id: str
    occurrence_id: int | None
    start_byte: int
    end_byte: int
    text: str
    citation: str
    scope: str
    source_type: str | None
    author_identity: Identity
    recipient_identity: tuple[str, ...] | None
    account: str | None
    thread: str | None
    observed_at: str | None
    recorded_at: str | None
    authored_at: str | None
    known_at: str | None
    valid_from: str | None
    valid_to: str | None
    action_state: str | None
    action_basis: str | None
    completeness: str | None
    provenance_confidence: tuple[tuple[str, float], ...]
    trust_class: str
    origin_kind: str | None
    derived_or_original: str
    content_hash: str
    # Canonical JSON prevents mutation of nested reviewed metadata after loading.
    reviewed_json: tuple[str, ...] = ()
    source_native_id: str | None = None
    source_document_id: str | None = None
    source_identity_confidence: str | None = None

    @property
    def input_hash(self):
        return identity_hash(asdict(self))


@dataclass(frozen=True)
class CandidateClaim:
    claim_id: str
    subject: str
    predicate: str
    value: str
    support_ids: tuple[str, ...]
    support_revision_ids: tuple[str, ...]
    speaker: str | None
    authored_at: str | None
    known_at: str | None
    valid_from: str | None
    valid_to: str | None
    assertion_kind: str
    inference_type: str
    polarity: str = 'positive'
    review_id: str | None = None
    supersedes: str | None = None
    known_to: str | None = None
    uncertain: bool = False
    recipe: str = 'bounded-claims-v2-epistemic-guards'
    speaker_confidence: float | None = None
    extraction_support: tuple = ()
    structured_record_id: str | None = None
    epistemic_strength: str = 'UNKNOWN'
    value_precision: str = 'UNKNOWN'
    semantic_value_json: str | None = None
    semantic_subject_json: str | None = None


@dataclass(frozen=True)
class Edge:
    source: str
    relation: str
    target: str
    basis: str


@dataclass(frozen=True)
class AnswerabilityResult:
    state: str
    required_question_slots: tuple[str, ...]
    satisfied_slots: tuple[str, ...]
    missing_slots: tuple[str, ...]
    ambiguous_slots: tuple[str, ...]
    conflicting_claims: tuple[str, ...]
    supporting_claim_ids: tuple[str, ...]
    missing_provenance: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self):
        required=set(self.required_question_slots)
        satisfied,missing=set(self.satisfied_slots),set(self.missing_slots)
        if not (satisfied|missing|set(self.ambiguous_slots))<=required or satisfied&missing:
            raise ValueError('Answerability slots violate the question contract')
        if self.state=='SUFFICIENT' and (required-satisfied or self.conflicting_claims or self.ambiguous_slots):
            raise ValueError('Sufficient requires all slots without unresolved conflict')


@dataclass(frozen=True)
class AnswerPlan:
    answerability: str
    answer_type: str
    claims_allowed: tuple[str, ...]
    claims_forbidden: tuple[str, ...]
    required_support: tuple[str, ...]
    uncertainty_to_expose: tuple[str, ...]
    citations_required: bool = True
    authority: str = 'none'


def identity_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalize(db, references, scope):
    """Caller MUST authorize scope in the same transaction. No caller text accepted.

    Selected occurrences must belong to the revision. Absent occurrence means unknown
    metadata, not an automatic choice of the first/latest sighting. Every requested
    span is re-read from immutable original bytes; a citation string alone is no proof.
    """
    if not isinstance(references, (list, tuple)) or len(references)>64:
        raise ValueError('Select at most 64 bounded evidence spans')
    units=[]; seen=set(); byte_count=0; review_ids=set(); review_cache={}; review_bytes=0
    for ref in references:
        if not isinstance(ref,dict) or set(ref)-{'revision_id','occurrence_id','start_byte','end_byte'}:
            raise ValueError('Only original evidence references are accepted')
        revision=ref.get('revision_id')
        if not isinstance(revision,str):
            raise ValueError('Revision identity required')
        row=db.execute('''SELECT r.*,s.source_type,s.platform,s.account_id AS source_account,
            s.workspace_id AS source_workspace,s.thread_id AS source_thread,
            s.native_id AS source_native_id,s.document_id AS source_document_id,
            s.identity_confidence AS source_identity_confidence FROM revisions r
            JOIN sources s ON s.id=r.source_id JOIN eligible_events e ON e.id=r.id
            WHERE r.id=? AND r.scope=?''',(revision,scope)).fetchone()
        if row is None:
            raise KeyError('Evidence deleted, private or outside scope')
        raw=row['content'].encode('utf-8')
        start,end=ref.get('start_byte'),ref.get('end_byte')
        if type(start) is not int or type(end) is not int or not 0<=start<end<=len(raw) or end-start>64000:
            raise ValueError('Invalid original span')
        try:
            text=raw[start:end].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Span splits a UTF-8 character') from exc
        oid=ref.get('occurrence_id'); origin={}
        if oid is not None:
            if type(oid) is not int or oid<1:
                raise ValueError('Invalid occurrence identity')
            occurrence=db.execute('SELECT * FROM occurrences WHERE id=? AND revision_id=?',(oid,revision)).fetchone()
            if occurrence is None:
                raise ValueError('Occurrence does not belong to selected revision')
            origin=dict(occurrence)
        key=(revision,oid,start,end)
        if key in seen:
            continue
        seen.add(key);byte_count+=end-start
        if byte_count>64000:
            raise ValueError('Evidence reader input exceeds 64KB')
        reviewed=[]
        # A reviewed interpretation is usable only if ALL normalized support spans
        # will be present. Graph construction performs that cross-unit check later.
        for r in db.execute('''SELECT DISTINCT i.* FROM eligible_interpretations i JOIN interpretation_evidence e
            ON e.interpretation_id=i.id WHERE e.revision_id=? AND i.scope=?
            AND i.review_state IN ('confirmed','superseded') LIMIT ?''',(revision,scope,MAX_CLAIMS+1)):
            item=dict(r)
            if r['id'] in review_cache:
                reviewed.append(review_cache[r['id']]);continue
            review_ids.add(r['id'])
            if len(review_ids)>MAX_CLAIMS:
                raise ReaderBudgetExceeded('reviewed_interpretation_budget_exceeded',MAX_CLAIMS)
            item['supports']=[dict(s) for s in db.execute('SELECT revision_id,start_byte,end_byte FROM interpretation_evidence WHERE interpretation_id=? LIMIT 65',(r['id'],))]
            if len(item['supports'])>64:
                raise ReaderBudgetExceeded('interpretation_support_budget_exceeded',64)
            encoded=json.dumps(item,sort_keys=True)
            review_bytes+=len(encoded.encode())
            if review_bytes>64000:
                raise ReaderBudgetExceeded('reviewed_metadata_budget_exceeded',64000)
            review_cache[r['id']]=encoded;reviewed.append(encoded)
        recipients=json.loads(origin['recipients']) if origin.get('recipients') is not None else None
        account=origin.get('account_id')
        # Source fields describe object identity; occurrence fields describe the actor
        # observed at that sighting. Do not fill an unknown actor account from the source.
        confidence=json.loads(origin.get('confidence') or '{}')
        author=Identity(origin.get('author_id'),origin.get('author_name'),row['platform'],origin.get('workspace_id'),account,
            confidence.get('author_id',confidence.get('author')))
        units.append(EvidenceUnit(identity_hash(key),row['source_id'],revision,oid,start,end,text,
            f'memory-engine://evidence/{revision}#{start}:{end}',scope,row['source_type'],author,
            tuple(recipients) if recipients is not None else None,account,origin.get('thread_id'),
            origin.get('observed_at'),origin.get('recorded_at'),origin.get('authored_at'),origin.get('known_at'),
            None,None,origin.get('action_state'),origin.get('action_basis'),origin.get('completeness') or row['completeness'],
            tuple(sorted(json.loads(origin.get('confidence') or '{}').items())),
            'untrusted',origin.get('origin_kind'),'original',hashlib.sha256(raw[start:end]).hexdigest(),tuple(reviewed),
            row['source_native_id'],row['source_document_id'],row['source_identity_confidence']))
    return tuple(units)
