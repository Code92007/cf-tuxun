import base64
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cfshot import search
from cfshot.search_crawler import parse_statement, sync_catalog, crawl_contest


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(search, 'db_path', return_value=str(Path(self.folder.name)/'main.db'))
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.folder.cleanup()

    def test_fragment_search_and_idempotent_reindex(self):
        with search.connect() as db:
            search.upsert(db, 10, 'A', 'Example', 'Round 1', 100, 'Find the lexicographically smallest permutation of these elements.')
            search.upsert(db, 11, 'B', 'Other', 'Round 2', 200, 'A tree has vertices and edges.')
        self.assertEqual(search.search('lexicographically smallest perm')['results'][0]['contest'], 10)
        # FTS metacharacters must be treated as input, never as an executable expression.
        self.assertEqual(search.search('" OR NEAR lexicographically*')['results'][0]['contest'], 10)
        with search.connect() as db:
            search.upsert(db, 10, 'A', 'Example', 'Round 1', 100, 'An entirely different sequence.')
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0], 2)
        self.assertEqual(search.search('lexicographically')['results'], [])
        self.assertEqual(search.search('different sequence')['results'][0]['contest'], 10)

    def test_local_image_similarity_without_ocr(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (240,160),'white')
        draw = ImageDraw.Draw(image)
        draw.line([(10,140),(80,25),(120,90),(220,15)],fill='black',width=8)
        raw = io.BytesIO(); image.save(raw,format='PNG'); raw = raw.getvalue()
        with search.connect() as db:
            search.upsert(db, 20, 'C', 'Diagram', 'Round', 100, 'Diagram problem',search.visual_vectors(raw))
        upload = 'data:image/png;base64,'+base64.b64encode(raw).decode()
        with patch.object(search,'ocr',side_effect=ValueError('OCR unavailable')):
            result = search.search(image=upload)
        self.assertEqual(result['results'][0]['contest'],20)
        self.assertGreater(result['results'][0]['imageSimilarity'],0.99)
        self.assertEqual(result['warnings'],['OCR unavailable'])
        with self.assertRaises(ValueError):
            search.decode_image('data:image/png;base64,broken')

    def test_statement_parsing_and_block_page_rejected(self):
        page = '<div class="problem-statement"><div class="title">A. Example</div><p>Find a permutation of the integers satisfying all the constraints.</p><img src="/diagram.png"><script>bad()</script></div>'
        body, images = parse_statement(page)
        self.assertIn('permutation',body)
        self.assertNotIn('bad()',body)
        self.assertEqual(images,['/diagram.png'])
        with self.assertRaises(ValueError):
            parse_statement('<p>Checking your browser</p>')

    def test_long_ocr_fragment_outweighs_generic_visual_layout(self):
        words = 'uniquely identifying banking statement compound interest deposits double every single day'
        with search.connect() as db:
            search.upsert(db,30,'A','Original','Round',100,words)
            search.upsert(db,31,'B','Distractor','Round',100,'every single day',[[1.0]+[0.0]*63])
        with patch.object(search,'decode_image',return_value=b'image'), patch.object(search,'visual_vectors',return_value=[[1.0]+[0.0]*63]), patch.object(search,'ocr',return_value=words), patch('cfshot.formula_search.match',return_value=[]), patch('cfshot.illustration_search.match',return_value=[]):
            self.assertEqual(search.search(image='image')['results'][0]['contest'],30)

    def test_ocr_bounds_ignore_variable_noise_but_keep_magnitude(self):
        from cfshot.text_search import constraint_match
        with search.connect() as db:
            search.upsert(db,10,'A','Original','Round',10,r'Length $$$1 \le |a_i| \le 4000$$$.')
            search.upsert(db,11,'B','Different bound','Round',11,r'Length $$$1 \le |a_i| \le 400$$$.')
            search.upsert(db,12,'C','Coincidental sample','Round',12,'Example Output 1 4000')
            ids=constraint_match(db,'@1,@2 (1 < |a;| < 4000)')
            self.assertEqual([db.execute('SELECT contest FROM documents WHERE id=?',(i,)).fetchone()[0] for i in ids],[10])

    def test_worker_lock_and_pause_are_shared_across_connections(self):
        from cfshot.search_crawler import Fetcher
        self.assertFalse(search.worker_running())
        with search.worker_lock() as acquired:
            self.assertTrue(acquired)
            self.assertTrue(search.worker_running())
            with search.worker_lock() as second:
                self.assertFalse(second)
        self.assertFalse(search.worker_running())
        search.pause()
        stop = threading.Event()
        with self.assertRaises(InterruptedError):
            Fetcher(stop, controlled=True).fetch('https://codeforces.com/api/contest.list')
        self.assertTrue(stop.is_set())
        search._STOP.clear()

    def test_public_snapshot_round_trip_and_existing_data_protection(self):
        import gzip
        from cfshot.search_snapshot import export_snapshot, restore_snapshot_if_empty
        snapshot = Path(self.folder.name) / 'corpus.jsonl.gz'
        with search.connect() as db:
            db.execute("INSERT INTO jobs(contest,name,started,state) VALUES(2200,'Round',100,'done')")
            db.execute("INSERT INTO settings VALUES('private_runtime_value','do-not-export')")
            search.upsert(db,2200,'A','Title','Round',100,'A special permutation with unique constraints.',[[1.0]+[0.0]*63])
        self.assertEqual(export_snapshot(snapshot),1)
        first = snapshot.read_bytes()
        self.assertEqual(export_snapshot(snapshot),1)
        self.assertEqual(first,snapshot.read_bytes())
        with gzip.open(snapshot,'rt') as archive:
            self.assertNotIn('do-not-export',archive.read())
        self.assertEqual(restore_snapshot_if_empty(snapshot),0)
        with search.connect() as db:
            db.execute('DELETE FROM documents')
            db.execute('DELETE FROM fragments')
            db.execute('DELETE FROM visuals')
            db.execute('DELETE FROM jobs')
        self.assertEqual(restore_snapshot_if_empty(snapshot),1)
        self.assertEqual(search.search('special permutation')['results'][0]['contest'],2200)
        with search.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visuals').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE contest=2200').fetchone()[0],'done')

    def test_sharded_snapshot_is_stable_and_restores(self):
        from cfshot.search_snapshot import export_snapshot, restore_snapshot_if_empty, records
        snapshot = Path(self.folder.name) / 'shards' / 'manifest.json'
        with search.connect() as db:
            search.upsert(db,2200,'A','Title','Round',100,'Distinct public text',[])
            search.upsert(db,2250,'B','Title','Round',100,'Other public text',[])
        self.assertEqual(export_snapshot(snapshot),2)
        first = snapshot.read_bytes()
        self.assertEqual(len(json.loads(first)['shards']),2)
        self.assertEqual(export_snapshot(snapshot),2)
        self.assertEqual(first,snapshot.read_bytes())
        self.assertEqual(len(list(records(snapshot))),2)
        with search.connect() as db:
            db.execute('DELETE FROM documents')
            db.execute('DELETE FROM fragments')
        self.assertEqual(restore_snapshot_if_empty(snapshot),2)

    def test_catalog_orders_by_time_and_resumes_every_problem(self):
        class Fetcher:
            stop = threading.Event()
            def api(self, method):
                if method.startswith('contest.list'):
                    return [
                        {'id':12,'name':'New','phase':'FINISHED','startTimeSeconds':300},
                        {'id':99,'name':'Old','phase':'FINISHED','startTimeSeconds':100},
                        {'id':100001,'name':'Gym','phase':'FINISHED','startTimeSeconds':400},
                        {'id':13,'name':'Upcoming','phase':'BEFORE','startTimeSeconds':500}]
                return {'problems':[{'index':'A','name':'First'},{'index':'B','name':'Second'}]}
            def fetch(self,url,binary=False):
                if '/B?' in url:
                    raise ValueError('temporary block')
                return '<div class="problem-statement">An example statement long enough to be accepted and indexed.</div>'
        fetcher = Fetcher()
        with search.connect() as db:
            sync_catalog(fetcher,db)
            job = db.execute('SELECT * FROM jobs ORDER BY started DESC LIMIT 1').fetchone()
            self.assertEqual(job['contest'],12)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
            with self.assertRaises(ValueError):
                crawl_contest(fetcher,db,job)
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0],1)
            fetcher.fetch = lambda *a,**k: '<div class="problem-statement">A second example statement that is now available for indexing.</div>'
            crawl_contest(fetcher,db,job)
            self.assertEqual(db.execute('SELECT count(*) FROM documents').fetchone()[0],2)
