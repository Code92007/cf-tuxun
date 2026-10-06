#!/usr/bin/env python3
"""Merge public derived image indexes into an existing matching corpus."""
import argparse
import base64
import gzip
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cfshot.search import connect,worker_lock
from cfshot.search_snapshot import DEFAULT_SNAPSHOT, records


def merge(source):
    formulas=illustrations=0
    with worker_lock() as acquired:
        if not acquired:
            raise RuntimeError('Stop the crawler before importing derived caches')
        with connect() as db:
            for record in records(source):
                if record.get('type')!='document':
                    continue
                row=db.execute('SELECT id,body FROM documents WHERE contest=? AND problem=?',(record['contest'],record['problem'])).fetchone()
                if row is None or row['body']!=record['body']:
                    continue
                ident=row['id']
                if record.get('formulaIndexed') and not db.execute('SELECT 1 FROM formula_jobs WHERE document=?',(ident,)).fetchone():
                    db.execute('DELETE FROM formula_images WHERE document=?',(ident,))
                    images=[(ident,r['tex'],base64.b64decode(r['image'],validate=True)) for r in record.get('formulas',[])]
                    db.executemany('INSERT INTO formula_images(document,tex,image) VALUES(?,?,?)',images)
                    db.execute('INSERT INTO formula_jobs VALUES(?,?,?)',(ident,hashlib.sha256(row['body'].encode()).hexdigest(),''))
                    formulas+=len(images)
                if record.get('illustrationIndexed') and not db.execute('SELECT 1 FROM illustration_jobs WHERE document=?',(ident,)).fetchone():
                    from cfshot.illustration_search import store
                    images=[base64.b64decode(raw,validate=True) for raw in record.get('illustrations',[])]
                    store(db,ident,images)
                    illustrations+=len(images)
    return formulas,illustrations


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,default=DEFAULT_SNAPSHOT)
    args=parser.parse_args()
    print('Imported formula/illustration images:',*merge(args.snapshot))
