"""Certainty and numeric precision are separate, versioned linguistic projections."""
import re

RECIPE='epistemic-strength-and-precision-v1'
POSSIBLE=re.compile(r'\b(?:may|might|could|possibly|perhaps|maybe|possible|võib|võiks|võimalik|võib-olla|ehk)\b',re.I)
PROBABLE=re.compile(r'\b(?:probably|likely|tõenäoliselt|arvatavasti|ilmselt)\b',re.I)
CONDITIONAL=re.compile(r'\b(?:if|unless|kui|tingimusel)\b',re.I)
UNKNOWN=re.compile(r'\b(?:unknown|uncertain|unverified|teadmata|ebakindel|kinnitamata)\b',re.I)
APPROXIMATE=re.compile(r'\b(?:about|approximately|roughly|around|nearly|at least|at most|less than|more than|ligikaudu|umbes|vähemalt|kuni|alla|üle)\b',re.I)


def strength(text):
    if UNKNOWN.search(text):return 'UNKNOWN'
    if CONDITIONAL.search(text) or POSSIBLE.search(text):return 'POSSIBLE'
    if PROBABLE.search(text):return 'PROBABLE'
    return 'CERTAIN'  # Unqualified assertion, not independent proof of world truth.


def precision(value):
    if APPROXIMATE.search(value):return 'APPROXIMATE'
    if re.fullmatch(r'(?:[€$]\s*)?\d+(?:[.,]\d+)?(?:\s*(?:EUR|USD|euros?|eurot|dollars?|%))?',value,re.I):return 'EXACT'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|esmaspäev|teisipäev|kolmapäev|neljapäev|reede|laupäev|pühapäev',value,re.I):return 'EXACT'
    return 'UNKNOWN'
