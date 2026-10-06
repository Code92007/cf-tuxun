#!/usr/bin/env python3
"""Run/resume the full search backfill in the foreground; Ctrl+C checkpoints."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cfshot.search import start, pause, status, _STOP
import time
import argparse

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Refresh catalog and attempt the newest unfinished contest, then exit')
    args = parser.parse_args()
    if args.once:
        from cfshot.search import connect
        from cfshot.search_crawler import Fetcher, sync_catalog, crawl_contest
        fetcher = Fetcher(_STOP)
        with connect() as db:
            sync_catalog(fetcher, db)
            db.execute("INSERT OR REPLACE INTO settings VALUES('enabled','1')")
            db.commit()
            job = db.execute("SELECT * FROM jobs WHERE state!='done' ORDER BY started DESC,contest DESC LIMIT 1").fetchone()
            if job:
                try:
                    crawl_contest(fetcher, db, job)
                    db.execute("UPDATE jobs SET state='done',error='' WHERE contest=?", (job['contest'],))
                except Exception as exc:
                    db.execute("UPDATE jobs SET state='failed',attempts=attempts+1,error=?,retry_at=? WHERE contest=?", (str(exc)[:500],time.time()+60,job['contest']))
                db.commit()
        print(status())
        sys.exit(0)
    start()
    try:
        while not _STOP.wait(30):
            print(status(), flush=True)
    except KeyboardInterrupt:
        pause()
