"""Private search corpus, FTS inverted index and local visual vectors."""
import base64
import io
import fcntl
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from .db import db_path

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD = None


@contextmanager
def worker_lock():
    path = Path(db_path()).with_name('search-worker.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def worker_running():
    with worker_lock() as acquired:
        return not acquired


@contextmanager
def connect():
    db = sqlite3.connect(str(Path(db_path()).with_name('search.db')), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS documents(
      id INTEGER PRIMARY KEY, contest INTEGER NOT NULL, problem TEXT NOT NULL,
      title TEXT NOT NULL, contest_name TEXT NOT NULL, started INTEGER NOT NULL,
      body TEXT NOT NULL, url TEXT NOT NULL, updated REAL NOT NULL,
      UNIQUE(contest,problem));
    CREATE VIRTUAL TABLE IF NOT EXISTS fragments USING fts5(title,body, tokenize='unicode61');
    CREATE TABLE IF NOT EXISTS visuals(document INTEGER NOT NULL, vector TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS visuals_document ON visuals(document);
    CREATE TABLE IF NOT EXISTS jobs(contest INTEGER PRIMARY KEY, name TEXT NOT NULL,
      started INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS formula_images(id INTEGER PRIMARY KEY AUTOINCREMENT,
      document INTEGER NOT NULL,tex TEXT NOT NULL,image BLOB NOT NULL,coarse BLOB);
    CREATE INDEX IF NOT EXISTS formula_images_document ON formula_images(document);
    CREATE TABLE IF NOT EXISTS formula_jobs(document INTEGER PRIMARY KEY,
      body_hash TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS illustration_images(id INTEGER PRIMARY KEY AUTOINCREMENT,
      document INTEGER NOT NULL,image BLOB NOT NULL);
    CREATE INDEX IF NOT EXISTS illustration_images_document ON illustration_images(document);
    CREATE TABLE IF NOT EXISTS illustration_jobs(document INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS illustration_retries(document INTEGER PRIMARY KEY,
      retry_at REAL NOT NULL,error TEXT NOT NULL);
    ''')
    if 'coarse' not in {r[1] for r in db.execute('PRAGMA table_info(formula_images)')}:
        try:
            db.execute('ALTER TABLE formula_images ADD COLUMN coarse BLOB')
            db.commit()
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc):
                db.close()
                raise
    db.execute('CREATE INDEX IF NOT EXISTS formula_images_unprepared ON formula_images(id) WHERE coarse IS NULL')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def upsert(db, contest, problem, title, name, started, body, vectors=()):
    db.execute('''INSERT INTO documents(contest,problem,title,contest_name,started,body,url,updated)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(contest,problem) DO UPDATE SET
        title=excluded.title,body=excluded.body,updated=excluded.updated''',
        (contest,problem,title,name,started,body,
         f'https://codeforces.com/contest/{contest}/problem/{problem}',time.time()))
    ident = db.execute('SELECT id FROM documents WHERE contest=? AND problem=?',(contest,problem)).fetchone()[0]
    db.execute('DELETE FROM fragments WHERE rowid=?',(ident,))
    db.execute('INSERT INTO fragments(rowid,title,body) VALUES(?,?,?)',(ident,title,body))
    db.execute('DELETE FROM visuals WHERE document=?',(ident,))
    db.execute('DELETE FROM formula_images WHERE document=?',(ident,))
    db.execute('DELETE FROM formula_jobs WHERE document=?',(ident,))
    db.execute('DELETE FROM illustration_images WHERE document=?',(ident,))
    db.execute('DELETE FROM illustration_jobs WHERE document=?',(ident,))
    db.executemany('INSERT INTO visuals VALUES(?,?)',[(ident,json.dumps(v)) for v in vectors])


def open_image(raw):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 12_000_000
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 12_000_000:
            raise ValueError('图片像素过大')
        rgba = ImageOps.exif_transpose(source).convert('RGBA')
        image = Image.new('RGBA', rgba.size, 'white')
        image.alpha_composite(rgba)
        image = image.convert('RGB')
    image.thumbnail((1600,1600))
    return image


def visual_vectors(raw):
    """Contrast-normalized local descriptors; deterministic and no model download."""
    from PIL import ImageOps, Image
    import numpy as np
    image = ImageOps.grayscale(open_image(raw))
    if float(np.asarray(image).mean()) < 128:
        image = ImageOps.invert(image)
    vectors = []
    # Whole illustration plus overlapping crops tolerates partial screenshots.
    boxes = [(0,0,image.width,image.height)]
    for scale in (0.75,0.5):
        w,h = max(1,int(image.width*scale)),max(1,int(image.height*scale))
        for x in (0,(image.width-w)//2,image.width-w):
            for y in (0,(image.height-h)//2,image.height-h):
                boxes.append((x,y,x+w,y+h))
    for box in boxes:
        pixels = np.asarray(image.crop(box).resize((8,8),Image.Resampling.LANCZOS)).ravel().astype(float).tolist()
        mean = sum(pixels)/len(pixels)
        norm = math.sqrt(sum((p-mean)**2 for p in pixels))
        if norm > 10:
            vectors.append([(p-mean)/norm for p in pixels])
    return vectors


def ocr(raw):
    if not shutil.which('tesseract'):
        raise ValueError('截图识别需要安装 Tesseract（Docker 镜像已包含）；可先使用文字检索')
    image = open_image(raw)
    if image.height < 80:
        from PIL import Image
        scale = min(3,2400/image.width)
        if scale>1:
            image=image.resize((round(image.width*scale),round(image.height*scale)),Image.Resampling.LANCZOS)
    with tempfile.TemporaryDirectory(prefix='cfsnap-ocr-') as folder:
        path = Path(folder)/'query.png'
        image.save(path)
        try:
            result = subprocess.run(['tesseract',str(path),'stdout','-l','eng','--psm','6'],
                                    capture_output=True,text=True,timeout=25,check=True)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError('截图识别失败，请换一张清晰截图或填写文字') from exc
    return result.stdout[:5000].strip()


def decode_image(value):
    if not isinstance(value,str) or not re.match(r'^data:image/(png|jpeg|webp);base64,',value):
        raise ValueError('仅支持 PNG、JPEG、WebP 图片')
    try:
        raw = base64.b64decode(value.split(',',1)[1],validate=True)
    except (ValueError,TypeError) as exc:
        raise ValueError('图片编码无效') from exc
    if not raw or len(raw)>3*1024*1024:
        raise ValueError('图片不能超过 3 MB')
    try:
        open_image(raw)
    except ImportError as exc:
        raise ValueError('图片检索需要安装 requirements.txt 中的依赖') from exc
    except Exception as exc:
        raise ValueError('图片无效或像素过大') from exc
    return raw


def search(text='', image=None):
    recognized = ''
    query_vectors = []
    warnings = []
    raw = None
    numeric_sample = False
    if image:
        raw = decode_image(image)
        query_vectors = visual_vectors(raw)
        try:
            recognized = ocr(raw)
        except ValueError as exc:
            warnings.append(str(exc))
    text = (str(text or '')+' '+recognized).strip()[:5000]
    tokens = list(dict.fromkeys(re.findall(r'[^\W_]+',text.lower())))[:60]
    tokens = [t for t in tokens if len(t)>1]
    if not tokens and not query_vectors:
        raise ValueError('请提供可辨认的文字或图片')
    with connect() as db:
        ranks = {}
        if text:
            from .text_search import match as phrase_match
            numeric = sum(c.isdigit() for c in text)
            numeric_sample = numeric>=40 and numeric/max(1,sum(c.isalnum() for c in text))>0.6
            for rank,(ident,similarity) in enumerate([] if numeric_sample else phrase_match(db,text)):
                if similarity > 0.025:
                    ranks[ident] = {'score':8*similarity/(60+rank+1),'phraseSimilarity':round(similarity,3)}
            from .text_search import sample_match
            for rank,(ident,similarity) in enumerate(sample_match(db,text)):
                item = ranks.setdefault(ident,{'score':0})
                item['score'] += 12*similarity/(60+rank+1)
                item['sampleSimilarity'] = round(similarity,3)
            from .text_search import constraint_match
            for ident in constraint_match(db,text):
                item = ranks.setdefault(ident,{'score':0})
                item['score'] += 12/61
                item['constraintMatch'] = True
        if tokens:
            expression = ' OR '.join('"'+t+'"*' for t in tokens)
            rows = db.execute('''SELECT rowid,bm25(fragments) rank FROM fragments
                WHERE fragments MATCH ? ORDER BY rank LIMIT 100''',(expression,)).fetchall()
            best_strength = max(1e-12, -rows[0]['rank']) if rows else 1
            text_weight = 3 if len(tokens) >= 8 else 1
            for rank,row in enumerate(rows):
                strength = max(0, -row['rank']) / best_strength
                item = ranks.setdefault(row['rowid'],{'score':0})
                item['textRank'] = rank+1
                item['score'] += text_weight*strength/(60+rank+1)
        if query_vectors:
            import numpy as np
            query_matrix = np.asarray(query_vectors, dtype=np.float32)
            scores = {}
            cursor = db.execute('SELECT document,vector FROM visuals')
            while True:
                batch = cursor.fetchmany(1024)
                if not batch:
                    break
                matrix = np.asarray([json.loads(r['vector']) for r in batch], dtype=np.float32)
                similarities = (matrix @ query_matrix.T).max(axis=1)
                for row, similarity in zip(batch, similarities):
                    if similarity>=0.65:
                        scores[row['document']] = max(scores.get(row['document'],0),float(similarity))
            for rank,(ident,similarity) in enumerate(sorted(scores.items(),key=lambda v:-v[1])[:100]):
                item = ranks.setdefault(ident,{'score':0})
                item['score'] += 1/(60+rank+1)
                item['imageSimilarity'] = round(similarity,3)
        if raw:
            from .formula_search import match
            strong_prose = len(tokens)>=12 and any(r.get('phraseSimilarity',0)>0.45 for r in ranks.values())
            formula_hits = [] if numeric_sample or strong_prose else match(raw)
            for rank,(ident,similarity) in enumerate(formula_hits):
                item = ranks.setdefault(ident,{'score':0})
                weight = 4 if len(tokens)<8 else 0.5
                item['score'] += weight*similarity**4/(60+rank+1)
                item['formulaSimilarity'] = round(similarity,3)
            from .illustration_search import match as illustration_match, colored_ink
            strong_text = any(r.get('phraseSimilarity',0)>0.3 or r.get('constraintMatch') for r in ranks.values())
            strong_formula = bool(formula_hits and formula_hits[0][1]>=0.8)
            need_illustration = len(tokens)<4 and not strong_text and (colored_ink(raw) or not strong_formula)
            for rank,(ident,similarity) in enumerate(illustration_match(raw) if need_illustration else []):
                item = ranks.setdefault(ident,{'score':0})
                item['score'] += 12*similarity**6/(60+rank+1)
                item['imageSimilarity'] = round(similarity,3)
        results = []
        for ident,score in sorted(ranks.items(),key=lambda v:-v[1]['score'])[:20]:
            row = dict(db.execute('SELECT * FROM documents WHERE id=?',(ident,)).fetchone())
            body = row.pop('body')
            position = next((body.lower().find(t) for t in tokens if t in body.lower()),0)
            row['snippet'] = body[max(0,position-80):max(0,position-80)+450]
            row.update(score)
            results.append(row)
    return {'results':results,'recognizedText':recognized,'warnings':warnings}


def status():
    with connect() as db:
        return {'running': worker_running(),
                'enabled': db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0]=='1'
                    if db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone() else False,
                'documents':db.execute('SELECT count(*) FROM documents').fetchone()[0],
                'visuals':db.execute('SELECT count(*) FROM visuals').fetchone()[0],
                'formulas':db.execute('SELECT count(*) FROM formula_images').fetchone()[0],
                'formulaPending':db.execute('SELECT count(*) FROM documents d LEFT JOIN formula_jobs j ON j.document=d.id WHERE j.document IS NULL').fetchone()[0],
                'formulaError':(db.execute("SELECT value FROM settings WHERE key='formula_error'").fetchone() or [''])[0],
                'illustrations':db.execute('SELECT count(*) FROM illustration_images').fetchone()[0],
                'illustrationPending':db.execute('''SELECT count(*) FROM documents d WHERE EXISTS(SELECT 1 FROM visuals v WHERE v.document=d.id)
                    AND NOT EXISTS(SELECT 1 FROM illustration_jobs j WHERE j.document=d.id)''').fetchone()[0],
                'illustrationError':(db.execute("SELECT value FROM settings WHERE key='illustration_error'").fetchone() or [''])[0],
                'jobs':{r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM jobs GROUP BY state')},
                'errors':[dict(r) for r in db.execute("SELECT contest,name,error,retry_at FROM jobs WHERE error!='' ORDER BY started DESC LIMIT 10")],
                'lastError': (db.execute("SELECT value FROM settings WHERE key='last_error'").fetchone() or [''])[0],
                'ocrAvailable': bool(shutil.which('tesseract'))}


def start():
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            if _STOP.is_set():
                raise ValueError('回刷正在暂停，请稍后再继续')
            return
        with connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('enabled','1')")
        _STOP.clear()
        from .search_crawler import run
        _THREAD = threading.Thread(target=run,args=(_STOP,),name='cf-search-backfill',daemon=True)
        _THREAD.start()


def pause():
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('enabled','0')")
    _STOP.set()


def resume():
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
    if row is None or row[0]=='1':
        start()
