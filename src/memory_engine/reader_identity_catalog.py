"""Scope-eligible identity witnesses, distinct from selected factual evidence.

A name collision elsewhere in captured history can require clarification even if
only one person's paragraph ranked highly. This query reads metadata, never text.
"""
from dataclasses import dataclass
import json
from .reader_contracts import Identity,ReaderBudgetExceeded,identity_hash


@dataclass(frozen=True)
class IdentityWitness:
    evidence_id: str
    source_id: str
    author_identity: Identity
    account: str | None


def witnesses(db,plan,name):
    if not name:return ()
    # Unicode casefold is performed in Python, because SQLite NOCASE is ASCII-only.
    # The per-scope scan is bounded; do not silently drop additional identities.
    args=[plan.scope]
    condition=''
    if name.isascii():
        # Include non-ASCII aliases too: Unicode casefold can turn e.g. Kelvin K
        # into ASCII k. The final comparison is Python's explicit normalization.
        condition+=" AND (o.author_name=? COLLATE NOCASE OR o.author_id=? OR o.author_name GLOB '*[^ -~]*')"
        args.extend([name,name])
    if plan.account_hints:
        condition+=' AND o.account_id IN ('+','.join('?' for _ in plan.account_hints)+')'
        args.extend(plan.account_hints)
    cutoff=plan.temporal_interval[1] if plan.temporal_mode=='known_at_time' and plan.temporal_interval else plan.as_of
    operator='<' if plan.temporal_mode=='known_at_time' else '<='
    condition+=' AND (o.known_at IS NULL OR julianday(o.known_at)'+operator+'julianday(?))'
    args.append(cutoff)
    rows=db.execute('''SELECT DISTINCT o.author_id,o.author_name,s.platform,o.workspace_id,o.account_id,o.confidence,
        min(r.source_id) AS source_id,max(r.source_id) AS other_source FROM occurrences o JOIN eligible_events e ON e.id=o.revision_id JOIN revisions r ON r.id=e.id JOIN sources s ON s.id=r.source_id
        WHERE e.scope=? AND (o.author_name IS NOT NULL OR o.author_id IS NOT NULL)'''+condition+' GROUP BY o.author_id,o.author_name,s.platform,o.workspace_id,o.account_id,o.confidence LIMIT 513',args).fetchall()
    if len(rows)>512:
        raise ReaderBudgetExceeded('identity_catalog_budget_exceeded',512)
    found=[]
    for row in rows:
        if (row['author_name'] or '').casefold()!=name.casefold() and row['author_id']!=name:
            continue
        confidence_fields=json.loads(row['confidence'] or '{}')
        confidence=confidence_fields.get('author_id',confidence_fields.get('author'))
        identity=Identity(row['author_id'],row['author_name'],row['platform'],row['workspace_id'],row['account_id'],confidence)
        for source_id in sorted({row['source_id'],row['other_source']}):
            found.append(IdentityWitness('identity-witness:'+identity_hash([list(row),source_id]),source_id,identity,row['account_id']))
    return tuple(sorted(found,key=lambda x:x.evidence_id))
