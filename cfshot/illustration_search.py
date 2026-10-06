"""Local multiscale template matching for cropped statement illustrations."""
import io
from .search import connect, open_image


def colored_ink(raw):
    import numpy as np
    pixels=np.asarray(open_image(raw)).astype('int16')
    return bool(((pixels.max(axis=2)-pixels.min(axis=2))>40).sum()>max(4,pixels.shape[0]*pixels.shape[1]*0.001))


def store(db, document, images):
    rows=[]
    for raw in images:
        image=open_image(raw)
        image.thumbnail((1200,1200))
        buffer=io.BytesIO();image.save(buffer,format='PNG')
        rows.append((document,buffer.getvalue()))
    db.execute('DELETE FROM illustration_images WHERE document=?',(document,))
    db.executemany('INSERT INTO illustration_images(document,image) VALUES(?,?)',rows)
    db.execute('INSERT OR REPLACE INTO illustration_jobs(document) VALUES(?)',(document,))
    db.execute('DELETE FROM illustration_retries WHERE document=?',(document,))


def match(raw, limit=40):
    import cv2
    import numpy as np
    query=np.asarray(open_image(raw))
    # Large text screenshots are handled by OCR; bound expensive template search.
    if query.shape[0]>400 or query.shape[1]>700:
        return []
    ys,xs=np.where(query.min(axis=2)<210)
    if not len(xs):
        return []
    query=query[max(0,ys.min()-2):ys.max()+3,max(0,xs.min()-2):xs.max()+3]
    scores={}
    with connect() as db:
        for row in db.execute('SELECT document,image FROM illustration_images'):
            source=cv2.imdecode(np.frombuffer(row['image'],dtype=np.uint8),cv2.IMREAD_COLOR)
            if source is None:
                continue
            factor=min(1,600/max(source.shape[:2]))
            if factor<1:
                source=cv2.resize(source,(round(source.shape[1]*factor),round(source.shape[0]*factor)),interpolation=cv2.INTER_AREA)
            source=cv2.copyMakeBorder(source,32,32,32,32,cv2.BORDER_CONSTANT,value=(255,255,255))
            query_bgr=cv2.cvtColor(query,cv2.COLOR_RGB2BGR)
            best=0
            for scale in np.arange(0.5,2.51,0.05):
                width,height=round(query.shape[1]*scale*factor),round(query.shape[0]*scale*factor)
                if min(width,height)<8 or width>source.shape[1] or height>source.shape[0]:
                    continue
                template=cv2.resize(query_bgr,(width,height),interpolation=cv2.INTER_AREA)
                if float(template.std())<5:
                    continue
                similarity=float(cv2.minMaxLoc(cv2.matchTemplate(source,template,cv2.TM_CCOEFF_NORMED))[1])
                best=max(best,similarity)
            if best>=0.72:
                scores[row['document']]=max(scores.get(row['document'],0),best)
    return sorted(scores.items(),key=lambda p:-p[1])[:limit]
