"""Portable public corpus snapshots; never includes accounts or runtime secrets."""
import gzip
import io
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .search import connect, upsert

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[1] / 'catalog' / 'search-corpus' / 'manifest.json'
LEGACY_SNAPSHOT = DEFAULT_SNAPSHOT.parent.parent / 'search-corpus.jsonl.gz'


def _export_legacy(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=output.parent, delete=False)
    temporary = Path(handle.name)
    count = 0
    try:
        with handle, gzip.GzipFile(fileobj=handle, mode='wb', mtime=0, filename='') as archive, connect() as db:
            db.execute('BEGIN')
            def write(record):
                archive.write((json.dumps(record, ensure_ascii=False, separators=(',', ':'))+'\n').encode())
            write({'type':'header', 'version':1})
            for job in db.execute('SELECT contest,name,started,state FROM jobs ORDER BY contest'):
                record = dict(job)
                record['state'] = 'done' if record['state']=='done' else 'pending'
                write({'type':'contest', **record})
            for row in db.execute('SELECT id,contest,problem,title,contest_name,started,body FROM documents ORDER BY contest,problem'):
                record = dict(row)
                ident = record.pop('id')
                record['vectors'] = [json.loads(v[0]) for v in db.execute('SELECT vector FROM visuals WHERE document=? ORDER BY rowid', (ident,))]
                record['formulas'] = [{'tex':r['tex'],'image':base64.b64encode(r['image']).decode()} for r in db.execute('SELECT tex,image FROM formula_images WHERE document=? ORDER BY id',(ident,))]
                record['formulaIndexed'] = db.execute('SELECT 1 FROM formula_jobs WHERE document=?',(ident,)).fetchone() is not None
                record['illustrations'] = [base64.b64encode(r[0]).decode() for r in db.execute('SELECT image FROM illustration_images WHERE document=? ORDER BY id',(ident,))]
                record['illustrationIndexed'] = db.execute('SELECT 1 FROM illustration_jobs WHERE document=?',(ident,)).fetchone() is not None
                write({'type':'document', **record})
                count += 1
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def records(source):
    source = Path(source)
    if source.suffix == '.json':
        manifest = json.loads(source.read_text())
        if manifest.get('version') != 2:
            raise ValueError('Unsupported search snapshot manifest')
        for shard in manifest['shards']:
            path = source.parent / shard['file']
            if path.parent != source.parent or hashlib.sha256(path.read_bytes()).hexdigest() != shard['sha256']:
                raise ValueError('Invalid search snapshot shard')
            yield from records(path)
        return
    with gzip.open(source, 'rt', encoding='utf-8') as archive:
        if json.loads(next(archive)) != {'type':'header','version':1}:
            raise ValueError('Unsupported search snapshot version')
        for line in archive:
            yield json.loads(line)


def export_snapshot(output=DEFAULT_SNAPSHOT):
    output = Path(output)
    if output.suffix != '.json':
        return _export_legacy(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        staging = Path(staging)
        count = _export_legacy(staging / 'all.gz')
        handles = {}
        raw_handles = []
        try:
            for record in records(staging / 'all.gz'):
                # Stable 25-contest ranges keep files comfortably below GitHub's limit.
                lower = int(record['contest']) // 25 * 25
                name = f'{lower:04d}-{lower+24:04d}.jsonl.gz'
                if name not in handles:
                    raw_handle = open(staging / name,'wb')
                    raw_handles.append(raw_handle)
                    handles[name] = io.TextIOWrapper(gzip.GzipFile(fileobj=raw_handle,mode='wb',mtime=0,filename=''),encoding='utf-8')
                    handles[name].write(json.dumps({'type':'header','version':1})+'\n')
                handles[name].write(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n')
        finally:
            for handle in handles.values():
                handle.close()
            for handle in raw_handles:
                handle.close()
        shards = []
        for name in sorted(handles):
            path = staging / name
            data = path.read_bytes()
            if len(data) >= 95_000_000:
                raise ValueError('Search shard exceeds safe GitHub file size')
            shards.append({'file':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
            os.replace(path,output.parent / name)
        manifest = staging / 'manifest.json'
        manifest.write_text(json.dumps({'version':2,'documents':count,'shards':shards},indent=2)+'\n')
        os.replace(manifest,output)
    return count


def restore_snapshot_if_empty(source=DEFAULT_SNAPSHOT):
    source = Path(source)
    if not source.exists():
        if source == DEFAULT_SNAPSHOT and LEGACY_SNAPSHOT.exists():
            source = LEGACY_SNAPSHOT
        else:
            return 0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT count(*) FROM documents').fetchone()[0]:
            return 0
        count = 0
        for record in records(source):
            kind = record.pop('type')
            if kind == 'contest':
                db.execute('''INSERT INTO jobs(contest,name,started,state) VALUES(?,?,?,?)
                    ON CONFLICT(contest) DO UPDATE SET name=excluded.name,started=excluded.started,state=excluded.state''',
                    (record['contest'],record['name'],record['started'],record['state']))
            elif kind == 'document':
                upsert(db,record['contest'],record['problem'],record['title'],record['contest_name'],record['started'],record['body'],record['vectors'])
                ident = db.execute('SELECT id FROM documents WHERE contest=? AND problem=?',(record['contest'],record['problem'])).fetchone()[0]
                db.executemany('INSERT INTO formula_images(document,tex,image) VALUES(?,?,?)',[(ident,r['tex'],base64.b64decode(r['image'],validate=True)) for r in record.get('formulas',[])])
                if record.get('formulaIndexed'):
                    db.execute('INSERT OR REPLACE INTO formula_jobs(document,body_hash,error) VALUES(?,?,?)',(ident,hashlib.sha256(record['body'].encode()).hexdigest(),''))
                db.executemany('INSERT INTO illustration_images(document,image) VALUES(?,?)',[(ident,base64.b64decode(raw,validate=True)) for raw in record.get('illustrations',[])])
                if record.get('illustrationIndexed'):
                    db.execute('INSERT OR REPLACE INTO illustration_jobs(document) VALUES(?)',(ident,))
                count += 1
            else:
                raise ValueError('Unknown search snapshot record')
        return count
