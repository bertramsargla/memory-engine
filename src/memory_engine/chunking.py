"""Versioned UTF-8 byte projections; no rewriting of original evidence."""
from bisect import bisect_left,bisect_right
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Recipe:
    maximum:int=1200
    overlap:int=160
    minimum:int=160

    @property
    def identity(self):return f'natural-v1:b{self.maximum}:o{self.overlap}:m{self.minimum}'

    def __post_init__(self):
        if not 128<=self.maximum<=4096 or not 0<=self.overlap<self.maximum//2 or not 32<=self.minimum<=self.maximum//2:
            raise ValueError('Invalid bounded chunk recipe')


CONFIGS={r.identity:r for r in [Recipe(720,120,120),Recipe(1200,160,160),Recipe(1800,240,200)]}


def project(text,recipe=Recipe(),message_boundaries=()):
    offsets=[0]
    for c in text:offsets.append(offsets[-1]+len(c.encode('utf-8')))
    valid=set(offsets); total=offsets[-1]
    if any(x not in valid for x in message_boundaries):raise ValueError('Message boundary splits UTF-8 or lies outside original')
    levels=[sorted(set(message_boundaries)),
            [offsets[m.end()] for m in re.finditer(r'\n[ \t]*\n',text)],
            [offsets[m.end()] for m in re.finditer(r'\n',text)],
            [offsets[m.end()] for m in re.finditer(r'[.!?](?:["”\']?)[ \t]+',text)],
            [offsets[m.end()] for m in re.finditer(r'\s+',text)]]
    start=0
    while start<total:
        end=offsets[bisect_right(offsets,min(total,start+recipe.maximum))-1]
        if end<total:
            floor=start+recipe.minimum
            for boundaries in levels:
                i=bisect_right(boundaries,end)-1
                if i>=0 and boundaries[i]>=floor:
                    end=boundaries[i];break
        left,right=bisect_left(offsets,start),bisect_left(offsets,end)
        yield start,end,text[left:right]
        if end==total:break
        next_start=offsets[bisect_left(offsets,max(start+1,end-recipe.overlap))]
        # Align the overlap's beginning to a word/line boundary where possible.
        boundaries=levels[-1];i=bisect_left(boundaries,next_start)
        if i<len(boundaries) and boundaries[i]<end:next_start=boundaries[i]
        start=next_start if next_start>start else end


def parse_recipe(identity):
    if identity in CONFIGS:return CONFIGS[identity]
    m=re.fullmatch(r'natural-v1:b(\d+):o(\d+):m(\d+)',identity)
    if not m:raise ValueError('Unknown chunking recipe')
    return Recipe(*map(int,m.groups()))
