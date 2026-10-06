"""Render statement TeX offline and match screenshots against normalized ink."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .search import connect, open_image

_PATTERN = re.compile(r'\${6}(.*?)\${6}|\${3}(.*?)\${3}', re.S)
_RENDER_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE = None
_ROOT = Path(__file__).resolve().parents[1]


def formulas(body):
    seen = set()
    for match in _PATTERN.finditer(body):
        tex = (match.group(1) if match.group(1) is not None else match.group(2)).strip()
        # Single letters/numbers are too generic to identify a question visually.
        minimum = 6 if '_' in tex else 10
        if len(tex) < minimum or len(tex) > 8000 or tex in seen:
            continue
        seen.add(tex)
        yield tex, match.group(1) is not None


class Renderer:
    def __init__(self):
        node = os.environ.get('SEARCH_NODE') or shutil.which('node')
        if not node:
            raise RuntimeError('Offline formula indexing requires Node.js and MathJax')
        self.process = subprocess.Popen([node, str(_ROOT/'scripts/render_formulas.cjs')],
            cwd=_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def render(self, tex, display=True):
        import resvg_py
        import select
        self.process.stdin.write(json.dumps({'tex':tex,'display':display})+'\n')
        self.process.stdin.flush()
        if not select.select([self.process.stdout],[],[],15)[0]:
            self.close()
            raise RuntimeError('Formula renderer timed out')
        result = json.loads(self.process.stdout.readline())
        if 'error' in result:
            raise ValueError(result['error'])
        return resvg_py.svg_to_bytes(svg_string=result['svg'],zoom=2,skip_system_fonts=True)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin,self.process.stdout):
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()


def thumbnail(raw):
    import numpy as np
    from PIL import Image, ImageOps
    image = ImageOps.grayscale(open_image(raw))
    pixels = np.asarray(image)
    if float(pixels.mean()) < 128:
        image = ImageOps.invert(image)
        pixels = np.asarray(image)
    ink = np.argwhere(pixels < 180)
    if len(ink) < 8:
        return None
    y0,x0 = ink.min(axis=0)
    y1,x1 = ink.max(axis=0)+1
    image = image.crop((int(x0),int(y0),int(x1),int(y1)))
    image.thumbnail((126,62), Image.Resampling.LANCZOS)
    canvas = Image.new('L',(128,64),255)
    canvas.paste(image,((128-image.width)//2,(64-image.height)//2))
    buffer=io.BytesIO();canvas.save(buffer,format='PNG')
    return buffer.getvalue()


def descriptor(raw):
    import numpy as np
    from PIL import Image
    # A light spatial blur tolerates antialiasing/font differences without losing
    # multi-line formula structure, unlike the old 8x8 general image descriptor.
    from PIL import ImageFilter
    with Image.open(io.BytesIO(raw)) as image:
        pixels = np.asarray(image.filter(ImageFilter.GaussianBlur(0.7)), dtype=np.float32)
    vector = (1-pixels/255).ravel()
    return vector/max(float(np.linalg.norm(vector)),1e-9)


def coarse_descriptor(raw):
    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as image:
        pixels = np.asarray(image.resize((16,8),Image.Resampling.LANCZOS),dtype=np.float32)
    vector = (1-pixels/255).ravel()
    return vector/max(float(np.linalg.norm(vector)),1e-9)


def index_document(db, document, body, renderer):
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    errors = []
    images = []
    for tex, display in formulas(body):
        try:
            raw = renderer.render(tex,display)
            thumb = thumbnail(raw)
            if thumb:
                images.append((document,tex,thumb))
        except ValueError as exc:
            errors.append(str(exc))
    with db:
        db.execute('DELETE FROM formula_images WHERE document=?',(document,))
        db.executemany('INSERT INTO formula_images(document,tex,image) VALUES(?,?,?)',images)
        db.execute('INSERT OR REPLACE INTO formula_jobs(document,body_hash,error) VALUES(?,?,?)',
                   (document,body_hash,'; '.join(errors)[:500]))
    return len(images)


def backfill(stop=None):
    from .search import _STOP
    stop = stop or _STOP
    count = 0
    with _RENDER_LOCK, Renderer() as renderer, connect() as db:
        rows = db.execute('''SELECT d.id,d.body FROM documents d LEFT JOIN formula_jobs j ON j.document=d.id
            WHERE j.document IS NULL ORDER BY d.started DESC,d.contest DESC''').fetchall()
        for row in rows:
            if stop.is_set():
                break
            count += index_document(db,row['id'],row['body'],renderer)
    return count


def match(raw, limit=100):
    import numpy as np
    from PIL import Image
    global _CACHE
    thumbs = [thumbnail(raw)]
    with Image.open(io.BytesIO(raw)) as image:
        # A long prose line can end in a small constraint. Compare suffix
        # regions independently so the bold heading does not drown out math.
        if image.height<80 and image.width/image.height>10:
            for fraction in (0.65,0.7,0.75):
                cropped=image.crop((round(image.width*fraction),0,image.width,image.height))
                buffer=io.BytesIO();cropped.save(buffer,format='PNG')
                thumbs.append(thumbnail(buffer.getvalue()))
    thumbs = [thumb for thumb in thumbs if thumb is not None]
    if not thumbs:
        return []
    queries = np.asarray([descriptor(thumb) for thumb in thumbs],dtype=np.float32)
    with connect() as db:
        from . import search
        generation = (search.db_path(), *db.execute('SELECT count(*),coalesce(max(id),0) FROM formula_images').fetchone())
        with _CACHE_LOCK:
            if _CACHE is None or _CACHE[0]!=generation:
                rows = db.execute('SELECT document,image FROM formula_images ORDER BY id').fetchall()
                matrix = np.asarray([coarse_descriptor(r['image']) for r in rows],dtype=np.float32) if rows else np.empty((0,128),dtype=np.float32)
                _CACHE = (generation,[r['document'] for r in rows],[r['image'] for r in rows],matrix)
            _, documents,images,matrix = _CACHE
    if not len(documents):
        return []
    coarse = (matrix @ np.asarray([coarse_descriptor(thumb) for thumb in thumbs],dtype=np.float32).T).max(axis=1)
    indices = np.argsort(coarse)[-800:]
    fine = np.asarray([descriptor(images[i]) for i in indices],dtype=np.float32)
    similarities = (fine @ queries.T).max(axis=1)
    scores = {}
    for index, similarity in zip(indices, similarities):
        document = documents[index]
        if similarity>=0.40:
            scores[document] = max(scores.get(document,0),float(similarity))
    return sorted(scores.items(),key=lambda p:-p[1])[:limit]
