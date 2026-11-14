"""Versioned evidence packets; no language interpreter, provider or permission broker.

The encrypted store and caller authorization own the inputs. Every original span
is loaded through reader_contracts.normalize, never copied from search snippets.
"""
from dataclasses import dataclass
import hashlib
import json
from .reader_contracts import normalize
from .query import parse_query

VERSION='memory-engine-context-packet-v1'
OPERATIONS=('memory.evidence','memory.explain_context')

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def token_bound(text):
    # Conservative UTF-8 byte bound for the supported byte/subword providers.
    # This is NOT an exact tokenizer measurement; provider usage is recorded too.
    return len(text.encode('utf-8'))

@dataclass(frozen=True)
class ContextPacket:
    payload_json: str
    packet_id: str
    token_budget: int

    @property
    def data(self):return json.loads(self.payload_json)

    def validate(self):
        data=self.data
        if data.get('version')!=VERSION or digest(data)!=self.packet_id:
            raise ValueError('Invalid context packet identity/version')
        if token_bound(self.payload_json)>self.token_budget:
            raise ValueError('Context packet budget exceeded')
        if data.get('allowed_operations')!=list(OPERATIONS):raise ValueError('Invalid packet authority')
        return data

@dataclass(frozen=True)
class PacketLease:
    packet: ContextPacket
    references: tuple
    snapshot_hash: str
    omitted: tuple
    scope: str


def evidence_rows(db,references,scope):
    units=normalize(db,references,scope);rows=[]
    for u in units:
        source=dict(db.execute('SELECT platform,document_id,native_id,canonical_locator,identity_confidence FROM sources WHERE id=? AND scope=?',
                               (u.source_id,scope)).fetchone())
        occurrence=db.execute('SELECT metadata,sensitivity FROM occurrences WHERE id=? AND revision_id=?',(u.occurrence_id,u.revision_id)).fetchone() if u.occurrence_id else None
        metadata=json.loads(occurrence['metadata']) if occurrence else {}
        rows.append(dict(evidence_id=u.evidence_id,source_id=u.source_id,revision_id=u.revision_id,occurrence_id=u.occurrence_id,
            exact_text=u.text,exact_span=dict(start_byte=u.start_byte,end_byte=u.end_byte),content_hash=u.content_hash,
            source_type=u.source_type,app=metadata.get('app') or source['platform'],document=source['document_id'],
            source_native_id=source['native_id'],title=metadata.get('title'),locator=metadata.get('locator') or source['canonical_locator'],
            thread=u.thread,account=u.account,author=dict(native_id=u.author_identity.native_id,display_name=u.author_identity.display_name,
                platform=u.author_identity.platform,workspace=u.author_identity.workspace,account=u.author_identity.account),
            recipient=list(u.recipient_identity) if u.recipient_identity is not None else None,
            observed_at=u.observed_at,recorded_at=u.recorded_at,authored_at=u.authored_at,known_at=u.known_at,
            valid_time=dict(from_time=u.valid_from,to_time=u.valid_to),action_state=u.action_state,action_basis=u.action_basis,
            trust_class='UNTRUSTED_DATA',origin_kind=u.origin_kind,sensitivity_class=occurrence['sensitivity'] if occurrence else 'unknown',
            completeness=u.completeness,exact_citation=u.citation,identity_confidence=u.author_identity.confidence,
            source_identity_confidence=source['identity_confidence'],
            provenance_confidence=dict(u.provenance_confidence),derived_or_original='original'))
    return units,rows

def reviewed_memories(units,rows):
    reviews={}
    for u in units:
        for encoded in u.reviewed_json:
            r=json.loads(encoded)
            supports=r.get('supports',[])
            if not supports:continue
            ids=[]
            for support in supports:
                match=[e['evidence_id'] for e in rows if e['revision_id']==support['revision_id'] and
                       e['exact_span']['start_byte']<=support['start_byte'] and e['exact_span']['end_byte']>=support['end_byte']]
                if not match:break
                ids.extend(match)
            else:
                reviews[r['id']]={k:r.get(k) for k in ('id','type','subject_id','predicate','value','owner','review_state','confidence','known_from','known_to','valid_from','valid_to','supersedes','recipe')}
                reviews[r['id']].update(support_ids=sorted(set(ids)),derived_or_original='reviewed_interpretation',trust_class='UNTRUSTED_DATA')
    return list(reviews.values())

def payload(query,plan,units,rows,scope):
    people={};missing=[];ambiguous=[]
    for row in rows:
        author=row['author']
        key=digest(author)
        if any(author.values()):people.setdefault(key,dict(identity=author,support_ids=[]))['support_ids'].append(row['evidence_id'])
        absent=[k for k in ('authored_at','known_at','action_state','recipient') if row[k] in (None,'unknown')]
        if not author['native_id']:absent.append('author.native_id')
        if not author['display_name']:absent.append('author.display_name')
        if not row['valid_time']['from_time']:absent.append('valid_time')
        if absent:missing.append(dict(evidence_id=row['evidence_id'],fields=absent))
    by_name={}
    for key,p in people.items():
        name=p['identity']['display_name']
        if name:by_name.setdefault(name.casefold(),[]).append(key)
    for keys in by_name.values():
        if len(keys)>1:ambiguous.append(dict(reason='same_display_name_distinct_metadata',identity_candidates=keys))
    return dict(version=VERSION,query=query,query_intent=plan.intent,scope=scope,
        query_time=plan.as_of,timezone=plan.timezone_name,temporal_request=dict(mode=plan.temporal_mode,interval=plan.temporal_interval),
        evidence_units=rows,reviewed_memories=reviewed_memories(units,rows),current_project_context=dict(scope=scope,interpretation='not_inferred'),
        relevant_people=[dict(id=k,**p) for k,p in people.items()],relevant_entities=[],
        unresolved_ambiguities=ambiguous,conflicting_evidence=[],conflict_assessment='not_exhaustive; model may compare source assertions',
        missing_provenance=missing,allowed_operations=list(OPERATIONS),evidence_authority='UNTRUSTED_DATA',
        coverage=dict(capture='UNKNOWN',selection='bounded_selected_spans',no_result_means='no_matching_selected_evidence; not proof an event did not occur'),
        budget_contract='utf8_bytes_upper_bound; exact provider token usage reported separately')

class ContextLayer:
    """Connection scope is configured outside the model; methods are synchronous.

    UI callers must use the existing memory worker. No UI integration occurs here.
    """
    def __init__(self,memory,*,scope,token=None,owner=False):
        if not isinstance(scope,str) or not scope or (token is None and owner is not True):
            raise PermissionError('Explicit owner or scoped client credential required')
        self.memory,self.scope,self.token=memory,scope,token

    def packet(self,query,references,*,token_budget=8192,now=None,timezone='Europe/Tallinn'):
        if type(token_budget) is not int or not 1024<=token_budget<=64000:raise ValueError('Invalid packet budget')
        if not isinstance(query,str) or not query.strip() or len(query)>2000:raise ValueError('Invalid task')
        if not isinstance(references,(list,tuple)) or len(references)>64:raise ValueError('Invalid reference budget')
        plan=parse_query(query,scope=self.scope,now=now,tz=timezone)
        selected=[];omitted=[];units=();rows=[]
        with self.memory.transaction() as db:
            self.memory._authorize(db,self.scope,self.token)
            # Never let truncation of an arbitrary string drop a negation or citation.
            for ref in references:
                candidate=selected+[dict(ref)]
                proposed_units,proposed_rows=evidence_rows(db,candidate,self.scope)
                if token_bound(canonical(payload(query,plan,proposed_units,proposed_rows,self.scope)))>token_budget:
                    omitted.append(dict(reference_hash=digest(ref),reason='whole_evidence_packet_budget'));continue
                selected=candidate;units,rows=proposed_units,proposed_rows
            body=payload(query,plan,units,rows,self.scope)
            encoded=canonical(body)
            if token_bound(encoded)>token_budget:raise ValueError('Task and metadata exceed packet budget')
            snapshot=digest(dict(units=[u.input_hash for u in units],rows=rows))
        packet=ContextPacket(encoded,digest(body),token_budget);packet.validate()
        return PacketLease(packet,tuple(canonical(r) for r in selected),snapshot,tuple(omitted),self.scope)

    def context_for_task(self,task,*,token_budget=8192,now=None,timezone='Europe/Tallinn',strategy='lexical',embedder=None,reranker=None):
        result=self.memory.search(task,scope=self.scope,token=self.token,limit=20,now=now,timezone=timezone,
                                  strategy=strategy,embedder=embedder,reranker=reranker)
        refs=[dict(revision_id=r['id'],occurrence_id=(r.get('selected_occurrence') or {}).get('id'),
                   start_byte=r['start_byte'],end_byte=r['end_byte']) for r in result['results']]
        # V3 sufficiency does not prune the candidate set. The foundation model
        # receives eligible evidence even when a closed grammar cannot read it.
        return self.packet(task,refs,token_budget=token_budget,now=now,timezone=timezone),result

    def recheck(self,lease):
        with self.memory.transaction() as db:return self._recheck(db,lease)

    def _recheck(self,db,lease):
        """Internal transaction-scoped preflight, shared with the egress receipt."""
        if not isinstance(lease,PacketLease) or lease.scope!=self.scope:raise PermissionError('Wrong packet scope')
        data=lease.packet.validate()
        if data['scope']!=self.scope:raise PermissionError('Wrong serialized scope')
        self.memory._authorize(db,self.scope,self.token)
        units,rows=evidence_rows(db,[json.loads(r) for r in lease.references],self.scope)
        if digest(dict(units=[u.input_hash for u in units],rows=rows))!=lease.snapshot_hash:
            raise PermissionError('Evidence or provenance changed; rebuild packet')
        plan=parse_query(data['query'],scope=self.scope,now=data['query_time'],tz=data['timezone'])
        if canonical(payload(data['query'],plan,units,rows,self.scope))!=canonical(data):
            raise PermissionError('Packet content differs from authorized evidence')
        return units

    def explain_context(self,lease):
        self.recheck(lease)
        return dict(packet_id=lease.packet.packet_id,selected_ids=[r['evidence_id'] for r in lease.packet.data['evidence_units']],
                    omitted=list(lease.omitted),token_upper_bound=token_bound(lease.packet.payload_json),token_budget=lease.packet.token_budget,
                    authorization='connection_scope_rechecked',raw_diagnostics=False)
