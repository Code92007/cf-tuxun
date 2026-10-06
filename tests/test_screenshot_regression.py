"""Real user images and real Tesseract transcripts against the public corpus."""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cfshot import search
from cfshot.search_snapshot import restore_snapshot_if_empty
from cfshot.reviewed_search_clues import SEARCH_CLUES

ROOT=Path(__file__).resolve().parents[1]


class ScreenshotRegressionTest(unittest.TestCase):
    def test_user_screenshots_retrieve_expected_problem(self):
        # The recorded OCR comes from the production Tesseract runtime. Real
        # end-to-end OCR is also checked on deployment, without this mock.
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,DATA_DIR=folder):
            self.assertGreater(restore_snapshot_if_empty(),1000)
            cases=json.loads((ROOT/'tests/fixtures/search-cases.json').read_text())
            self.assertEqual(len(cases),len(SEARCH_CLUES))
            for case in cases:
                with self.subTest(answer=case['answer']):
                    raw=(ROOT/case['image']).read_bytes()
                    mime='jpeg' if case['image'].endswith('.jpg') else 'png'
                    with patch.object(search,'ocr',return_value=case['ocr']):
                        result=search.search(image=f'data:image/{mime};base64,'+base64.b64encode(raw).decode())
                    keys=[str(row['contest'])+row['problem'] for row in result['results']]
                    self.assertTrue(set(keys[:5]) & set(case['accepted']),keys)
                    if case['answer']=='2146D2':
                        self.assertEqual(keys[0],'2146D2')

    def test_brain_clues_and_version_aliases(self):
        from cfshot.db import init_db,get_db,matching_question_ids
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,DATA_DIR=folder):
            init_db()
            db=get_db()
            eligible=set(matching_question_ids({'difficulty':'brain'},limit=10000))
            for clue in SEARCH_CLUES:
                row=db.execute('SELECT id,brain,open_mode FROM questions WHERE canonical_key=?',(clue['key'],)).fetchone()
                self.assertTrue(row['brain'])
                self.assertIn(row['id'],eligible)
            for key,indices in [('2169D2@search',{'D1','D2'}),('2146D2@search',{'D2'})]:
                actual={r[0] for r in db.execute('SELECT problem_index FROM aliases WHERE question_id=(SELECT id FROM questions WHERE canonical_key=?)',(key,))}
                self.assertEqual(actual,indices)
            db.close()
