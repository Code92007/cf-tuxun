import base64
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from cfshot import search
from cfshot.formula_search import formulas, Renderer, index_document, match

FORMULA = r'o(v,x) = \begin{cases}v\,\&\,x & \mathrm{ty} = \& \\ v\,|\,x & \mathrm{ty} = |.\end{cases}'
FIXTURE = Path(__file__).with_name('fixtures')/'2246E-formula.png'


class FormulaTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patch = patch.object(search,'db_path',return_value=str(Path(self.folder.name)/'main.db'))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def test_math_delimiters_and_generic_expressions(self):
        extracted = list(formulas('One $$$x$$$, an inline $$$0 \\le x \\le 1000000000$$$, and $$$$$$'+FORMULA+'$$$$$$.'))
        self.assertEqual(extracted[-1],(FORMULA,True))
        self.assertFalse(any(tex=='x' for tex,_ in extracted))

    @unittest.skipUnless(os.environ.get('SEARCH_NODE') or shutil.which('node'),'requires offline MathJax runtime')
    def test_real_2246e_screenshot_padding_resize_and_snapshot(self):
        from cfshot.search_snapshot import export_snapshot,restore_snapshot_if_empty
        with search.connect() as db, Renderer() as renderer:
            search.upsert(db,2246,'E','Security Game','Round 1108',100,'The operation is $$$$$$'+FORMULA+'$$$$$$.')
            original = db.execute('SELECT id,body FROM documents WHERE contest=2246').fetchone()
            self.assertEqual(index_document(db,original['id'],original['body'],renderer),1)
            search.upsert(db,1,'A','Unrelated','Round',50,r'Consider $$$$$$f(x)=\begin{cases}x^2 & x>0 \\ -x & x<0\end{cases}$$$$$$.')
            other = db.execute('SELECT id,body FROM documents WHERE contest=1').fetchone()
            index_document(db,other['id'],other['body'],renderer)
        raw = FIXTURE.read_bytes()
        variants = [raw]
        with Image.open(io.BytesIO(raw)) as image:
            image = image.resize((int(image.width*0.7),int(image.height*0.7)))
            padded = Image.new('RGB',(image.width+100,image.height+80),'white')
            padded.paste(image,(50,40)); buffer=io.BytesIO();padded.save(buffer,format='PNG');variants.append(buffer.getvalue())
        for image in variants:
            self.assertEqual(match(image)[0][0],original['id'])
            with patch.object(search,'ocr',return_value='o(v,x) ty = ty x'):
                result=search.search(image='data:image/png;base64,'+base64.b64encode(image).decode())
                self.assertEqual((result['results'][0]['contest'],result['results'][0]['problem']),(2246,'E'))
        snapshot=Path(self.folder.name)/'snapshot.gz';export_snapshot(snapshot)
        with search.connect() as db:
            for table in ('documents','fragments','formula_images','formula_jobs','visuals'):
                db.execute('DELETE FROM '+table)
        self.assertEqual(restore_snapshot_if_empty(snapshot),2)
        with search.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM formula_images').fetchone()[0],2)
        self.assertEqual(search.search('operation')['results'][0]['contest'],2246)
