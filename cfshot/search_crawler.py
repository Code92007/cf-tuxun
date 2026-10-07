"""Resumable newest-first corpus backfill; never imports unreviewed game clues."""
import io
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse

from .search import connect, upsert, visual_vectors, worker_lock


class Fetcher:
    def __init__(self, stop, controlled=False):
        self.stop = stop
        self.last = 0
        self.controlled = controlled

    def fetch(self, url, binary=False):
        if self.controlled:
            with connect() as db:
                enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
            if enabled and enabled[0]=='0':
                self.stop.set()
                raise InterruptedError()
        if self.stop.wait(max(0,2.2-(time.monotonic()-self.last))):
            raise InterruptedError()
        self.last = time.monotonic()
        request = urllib.request.Request(url,headers={'User-Agent':'CF-Snap-Search/1.0','Accept-Language':'en'})
        limit = 64*1024*1024 if '/api/' in url else 8*1024*1024
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                raw = response.read(limit+1)
        except urllib.error.HTTPError as exc:
            try:
                comment = json.loads(exc.read(8192)).get('comment', '')
            except (ValueError, UnicodeError):
                comment = ''
            raise ValueError(f'HTTP {exc.code}: {comment or exc.reason}') from exc
        if len(raw)>limit:
            raise ValueError('远端内容过大')
        if binary:
            return normalize_asset(raw)
        # Some ICPC/IOI mirror statements redirect directly to a PDF.
        return raw if raw.startswith(b'%PDF-') else raw.decode('utf-8')

    def api(self, method):
        result = json.loads(self.fetch('https://codeforces.com/api/'+method))
        if result.get('status')!='OK':
            raise ValueError(result.get('comment','API failed'))
        return result['result']


def normalize_asset(raw):
    """Bound trusted public CF assets without relaxing screenshot upload limits."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 48_000_000
    with Image.open(io.BytesIO(raw)) as image:
        if image.width*image.height>48_000_000:
            raise ValueError('CF asset exceeds 48 million pixels')
        if image.width*image.height<=12_000_000:
            return raw
        image.thumbnail((1600,1600))
        buffer=io.BytesIO()
        image.save(buffer,format='PNG')
        return buffer.getvalue()


def parse_statement(page):
    if isinstance(page,bytes) and page.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(page))
        text=' '.join(p.extract_text() or '' for p in reader.pages)
        if len(text.strip())<40:
            raise ValueError('PDF statement has no extractable text')
        images=[normalize_asset(image.data) for p in reader.pages for image in p.images]
        return text,images
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page,'html.parser')
    statement = soup.select_one('.problem-statement')
    if statement is None:
        raise ValueError('未找到题面（可能被 Cloudflare 拦截），稍后重试')
    for item in statement.select('script,style'):
        item.decompose()
    images = [i.get('src','') for i in statement.select('img[src]')]
    text = statement.get_text(' ',strip=True)
    if len(text)<40:
        raise ValueError('题面不完整')
    return text,images


def sync_catalog(fetcher, db):
    contests = fetcher.api('contest.list?gym=false')
    for contest in contests:
        if contest.get('phase')!='FINISHED' or contest['id']>=100000:
            continue
        db.execute('''INSERT INTO jobs(contest,name,started) VALUES(?,?,?)
            ON CONFLICT(contest) DO UPDATE SET name=excluded.name,started=excluded.started''',
            (contest['id'],contest['name'],contest.get('startTimeSeconds',0)))
    db.commit()


def crawl_contest(fetcher, db, job):
    contest = job['contest']
    # Standings includes all contest problems, including ones absent from problemset.
    result = fetcher.api(f'contest.standings?contestId={contest}')
    for problem in result['problems']:
        if fetcher.stop.is_set():
            raise InterruptedError()
        existing = db.execute('SELECT id FROM documents WHERE contest=? AND problem=?',
                              (contest,problem['index'])).fetchone()
        if existing:
            continue
        url = f"https://codeforces.com/contest/{contest}/problem/{problem['index']}?locale=en"
        body,images = parse_statement(fetcher.fetch(url))
        vectors = []
        originals = []
        for source in images:
            if isinstance(source,bytes):
                vectors.extend(visual_vectors(source))
                originals.append(source)
                continue
            image_url = urljoin(url,source)
            parsed = urlparse(image_url)
            if parsed.scheme!='https' or parsed.hostname not in {'codeforces.com','codeforces.org','espresso.codeforces.com','sta.codeforces.com','sta.codeforces.org'}:
                continue
            # Asset failures keep the contest pending so it is retried, not silently lost.
            raw = fetcher.fetch(image_url,binary=True)
            vectors.extend(visual_vectors(raw))
            originals.append(raw)
        with db:
            upsert(db,contest,problem['index'],problem['name'],job['name'],job['started'],body,vectors)
            from .illustration_search import store
            ident = db.execute('SELECT id FROM documents WHERE contest=? AND problem=?',(contest,problem['index'])).fetchone()[0]
            store(db,ident,originals)


def backfill_illustration(fetcher, db, row=None):
    row = row or db.execute('''SELECT d.id,d.url FROM documents d
        WHERE EXISTS(SELECT 1 FROM visuals v WHERE v.document=d.id)
        AND NOT EXISTS(SELECT 1 FROM illustration_jobs j WHERE j.document=d.id)
        AND NOT EXISTS(SELECT 1 FROM illustration_retries r WHERE r.document=d.id AND r.retry_at>?)
        ORDER BY d.started DESC,d.contest DESC LIMIT 1''',(time.time(),)).fetchone()
    if row is None:
        return
    originals=[]
    try:
        _,images = parse_statement(fetcher.fetch(row['url']+'?locale=en'))
        for source in images:
            if isinstance(source,bytes):
                originals.append(source)
                continue
            url=urljoin(row['url'],source)
            parsed=urlparse(url)
            if parsed.scheme=='https' and parsed.hostname in {'codeforces.com','codeforces.org','espresso.codeforces.com','sta.codeforces.com','sta.codeforces.org'}:
                originals.append(fetcher.fetch(url,binary=True))
    except InterruptedError:
        raise
    except Exception as exc:
        with db:
            db.execute('INSERT OR REPLACE INTO illustration_retries VALUES(?,?,?)',(row['id'],time.time()+3600,str(exc)[:500]))
        raise
    from .illustration_search import store
    with db:
        store(db,row['id'],originals)


def run(stop):
    with worker_lock() as acquired:
        if acquired:
            _run(stop)


def _run(stop):
    fetcher = Fetcher(stop, controlled=True)
    catalog_at = 0
    illustration_retry = 0
    while not stop.is_set():
        try:
            from .formula_search import backfill
            try:
                backfill(stop)
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES('formula_error','')")
            except (RuntimeError, ImportError, OSError) as exc:
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES('formula_error',?)",(str(exc)[:500],))
            with connect() as db:
                enabled = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
                if enabled and enabled[0]=='0':
                    return
                if time.time() >= illustration_retry:
                    try:
                        backfill_illustration(fetcher,db)
                        db.execute("INSERT OR REPLACE INTO settings VALUES('illustration_error','')")
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        illustration_retry = time.time()+300
                        db.execute("INSERT OR REPLACE INTO settings VALUES('illustration_error',?)",(str(exc)[:500],))
                    db.commit()
                if time.time()-catalog_at>3600:
                    sync_catalog(fetcher,db)
                    catalog_at = time.time()
                    db.execute("INSERT OR REPLACE INTO settings VALUES('last_error','')")
                    db.commit()
                job = db.execute("SELECT * FROM jobs WHERE state!='done' AND retry_at<=? ORDER BY started DESC,contest DESC LIMIT 1",(time.time(),)).fetchone()
                if job is None:
                    stop.wait(60)
                    continue
                db.execute("UPDATE jobs SET state='running' WHERE contest=?",(job['contest'],))
                db.commit()
                try:
                    crawl_contest(fetcher,db,job)
                    db.execute("UPDATE jobs SET state='done',error='',retry_at=0 WHERE contest=?",(job['contest'],))
                except InterruptedError:
                    db.execute("UPDATE jobs SET state='pending' WHERE contest=?",(job['contest'],))
                    db.commit()
                    return
                except Exception as exc:
                    delay = min(86400,60*2**min(job['attempts'],10))
                    db.execute("UPDATE jobs SET state='failed',attempts=attempts+1,error=?,retry_at=? WHERE contest=?",(str(exc)[:500],time.time()+delay,job['contest']))
                db.commit()
        except InterruptedError:
            return
        except Exception as exc:
            with connect() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES('last_error',?)",(str(exc)[:500],))
            stop.wait(60)
