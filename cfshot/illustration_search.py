"""Local multiscale template matching for cropped statement illustrations."""
import io
from .search import connect, open_image


def colored_ink(raw):
    import numpy as np
    pixels=np.asarray(open_image(raw)).astype('int16')
    return bool(((pixels[:,:,0]>pixels[:,:,1]+60)&(pixels[:,:,0]>pixels[:,:,2]+60)).sum()>max(4,pixels.shape[0]*pixels.shape[1]*0.001))


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
    query_bgr=cv2.cvtColor(query,cv2.COLOR_RGB2BGR)
    def similarity(source,max_size,scales):
        factor=min(1,max_size/max(source.shape[:2]))
        if factor<1:
            source=cv2.resize(source,(round(source.shape[1]*factor),round(source.shape[0]*factor)),interpolation=cv2.INTER_AREA)
        source=cv2.copyMakeBorder(source,12,12,12,12,cv2.BORDER_CONSTANT,value=(255,255,255))
        best,best_scale=0,1
        for scale in scales:
            width,height=round(query.shape[1]*scale*factor),round(query.shape[0]*scale*factor)
            if min(width,height)<4 or width>source.shape[1] or height>source.shape[0]:
                continue
            template=cv2.resize(query_bgr,(width,height),interpolation=cv2.INTER_AREA)
            if float(template.std())<5:
                continue
            score=float(cv2.minMaxLoc(cv2.matchTemplate(source,template,cv2.TM_CCOEFF_NORMED))[1])
            if score>best:
                best,best_scale=score,scale
        return best,best_scale
    with connect() as db:
        rows=db.execute('SELECT document,image FROM illustration_images').fetchall()
    candidates=[]
    for row in rows:
        source=cv2.imdecode(np.frombuffer(row['image'],dtype=np.uint8),cv2.IMREAD_COLOR)
        if source is not None:
            score,scale=similarity(source,180,(0.5,0.75,1,1.25,1.5,2,2.5))
            candidates.append((score,scale,row['document'],row['image']))
    scores={}
    for _,scale,document,encoded in sorted(candidates,key=lambda r:-r[0])[:60]:
        source=cv2.imdecode(np.frombuffer(encoded,dtype=np.uint8),cv2.IMREAD_COLOR)
        best,_=similarity(source,400,np.arange(max(0.5,scale-0.25),min(2.51,scale+0.26),0.05))
        if best>=0.72:
            scores[document]=max(scores.get(document,0),best)
    return sorted(scores.items(),key=lambda p:-p[1])[:limit]
