"""OCR tolerant phrase reranking across literal text and TeX source."""
import math
import re
from collections import Counter


def normalize(text):
    text = re.sub(r'(\d+)\s*\^\s*\{?(\d+)\}?',r'\1\2',text)
    text = re.sub(r'\\(?:leq?|lt|geq?|gt)\b', ' ', text)
    text = re.sub(r'\\(?:ldots|dots|cdots)\b', ' ', text)
    text = re.sub(r'\\[A-Za-z]+', ' ', text)
    text = text.replace('$','').replace('_','').replace('{','').replace('}','')
    return re.findall(r'[a-z]+\d*|\d+',text.lower())


def grams(words):
    return Counter(tuple(words[i:i+n]) for n in (2,3,4) for i in range(len(words)-n+1))


def match(db, text, limit=100):
    query = grams(normalize(text))
    if not query:
        return []
    rows = db.execute('SELECT id,body FROM documents').fetchall()
    hits = []
    frequencies = Counter()
    first = {}
    for g in query:
        first.setdefault(g[0],[]).append(g)
    for row in rows:
        words=normalize(row['body'])
        shared=Counter()
        for i,word in enumerate(words):
            for g in first.get(word,()):
                if shared[g]<query[g] and tuple(words[i:i+len(g)])==g:
                    shared[g]+=1
        frequencies.update(shared.keys())
        if shared:
            hits.append((row['id'],shared))
    weights = {g:(len(g)-1)*math.log(1+len(rows)/(1+frequencies[g])) for g in query}
    denominator = sum(weights[g]*count for g,count in query.items()) or 1
    scores = [(ident,sum(weights[g]*count for g,count in shared.items())/denominator) for ident,shared in hits]
    return sorted(scores,key=lambda p:-p[1])[:limit]


def sample_match(db, text, limit=40):
    # OCR frequently merges "1 2 4" into "124". Preserve the ordered digit
    # stream rather than treating the merged value as an unrelated integer.
    if sum(c.isdigit() for c in text)<40:
        return []
    compact=lambda value: ''.join(re.findall(r'[a-z0-9]+',value.lower()))
    query=compact(text)
    grams=set(query[i:i+8] for i in range(len(query)-7))
    if not grams:
        return []
    scores=[]
    for row in db.execute('SELECT id,body FROM documents'):
        body=row['body']
        start=re.search(r'\bExample(?:s)?\s+Input\b',body,re.I)
        if not start:
            continue
        sample=body[start.start():]
        sample=re.split(r'\bNote\b',sample,maxsplit=1)[0]
        sample=compact(sample)
        shared=sum(g in sample for g in grams)/len(grams)
        if shared>=0.15:
            scores.append((row['id'],shared))
    return sorted(scores,key=lambda p:-p[1])[:limit]


def constraint_signature(text):
    text=re.sub(r'\d+\s*\^\s*\{?\d+\}?',lambda m: ''.join(re.findall(r'\d+',m[0])),text)
    text=re.sub(r'\\(?:leq?|lt)\b','<',text)
    text=re.sub(r'\\(?:geq?|gt)\b','>',text)
    text=text.replace('≤','<').replace('≥','>').replace('<=','<').replace('>=','>')
    text=re.sub(r'[a-zA-Z]_(?:\{[^}]*\}|[a-zA-Z0-9])','',text)
    text=re.sub(r'\\[a-zA-Z]+','',text)
    numbers=re.findall(r'\d+',text)
    if len(numbers)<2 or not any(int(n)>=100 for n in numbers) or not re.search(r'[<>]',text):
        return None
    return ''.join(re.findall(r'[0-9<>|]',text))


def constraint_match(db,text):
    # Variable OCR is unreliable (a_i may become @;). Numeric bounds and
    # absolute-value bars distinguish constraints from coincidental samples.
    snippets=re.findall(r'\([^()\n]*[<>≤≥][^()\n]*\)',text)+text.splitlines()
    signatures={s for snippet in snippets if (s:=constraint_signature(snippet))}
    if not signatures:
        return []
    matches=[]
    for row in db.execute('SELECT id,body FROM documents'):
        expressions=re.findall(r'\${3,6}(.*?)\${3,6}',row['body'],re.S)
        if any(constraint_signature(tex) in signatures for tex in expressions):
            matches.append(row['id'])
    return matches
