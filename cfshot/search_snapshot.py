"""Portable public corpus snapshots; never includes accounts or runtime secrets."""
import gzip
import json
import os
import tempfile
from pathlib import Path

from .search import connect, upsert

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[1] / 'catalog' / 'search-corpus.jsonl.gz'


def export_snapshot(output=DEFAULT_SNAPSHOT):
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
                write({'type':'document', **record})
                count += 1
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def restore_snapshot_if_empty(source=DEFAULT_SNAPSHOT):
    source = Path(source)
    if not source.exists():
        return 0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT count(*) FROM documents').fetchone()[0]:
            return 0
        count = 0
        with gzip.open(source, 'rt', encoding='utf-8') as archive:
            if json.loads(next(archive)) != {'type':'header','version':1}:
                raise ValueError('Unsupported search snapshot version')
            for line in archive:
                record = json.loads(line)
                kind = record.pop('type')
                if kind == 'contest':
                    db.execute('''INSERT INTO jobs(contest,name,started,state) VALUES(?,?,?,?)
                        ON CONFLICT(contest) DO UPDATE SET name=excluded.name,started=excluded.started,state=excluded.state''',
                        (record['contest'],record['name'],record['started'],record['state']))
                elif kind == 'document':
                    upsert(db,record['contest'],record['problem'],record['title'],record['contest_name'],record['started'],record['body'],record['vectors'])
                    count += 1
                else:
                    raise ValueError('Unknown search snapshot record')
        return count
