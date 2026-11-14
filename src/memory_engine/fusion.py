"""Versioned rank fusion. Exact identifiers are prioritized by the caller."""
import math

RECIPE='equal-weights-rrf60-or-minmax-v1'


def scores(groups,method):
    if method not in ('rrf','normalized'):raise ValueError('Invalid fusion method')
    fused={}
    for name,rows in groups.items():
        if name=='exact':continue
        if method=='rrf':values=[1/(60+i) for i in range(1,len(rows)+1)]
        else:
            raw=[-r['bm25'] if name=='lexical' else r['dense_score'] if name=='dense' else 1/(i+1)
                 for i,r in enumerate(rows)]
            if any(not math.isfinite(v) for v in raw):raise ValueError('Non-finite candidate score')
            low,high=(min(raw),max(raw)) if raw else (0,0)
            values=[(v-low)/(high-low) if high>low else 1.0 for v in raw]
        seen=set()
        for row,value in zip(rows,values):
            cid=row['chunk_id']
            if cid in seen:continue
            seen.add(cid);fused[cid]=fused.get(cid,0)+value
    return fused
