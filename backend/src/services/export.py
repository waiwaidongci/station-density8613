import csv,io
from fastapi import HTTPException
from fastapi.responses import Response
from ..models.note import Note
from .runs import get_run
from .recalc import build_recalc_package
from .audit import log
def csv_response(result,filename):
    buf=io.StringIO();fields=result['columns'];w=csv.DictWriter(buf,fieldnames=fields);w.writeheader()
    for r in result['rows']:w.writerow({k:r.get(k,'') for k in fields})
    return Response('﻿'+buf.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="'+filename+'"'})
def run_csv(db,id):
    r=get_run(db,id)
    if r.state!='completed':raise HTTPException(409,'结果尚未完成')
    return csv_response(r.result,'run-'+str(id)+'.csv')
def note_csv(db,id):
    n=db.get(Note,id)
    if not n:raise HTTPException(404,'未找到说明')
    if n.state!='approved':raise HTTPException(403,'仅批准后的业务说明可导出')
    return csv_response(n.frozen_result,'approved-'+str(id)+'.csv')
def recalc_package(db,id,user):
    r=get_run(db,id)
    if r.state!='completed':raise HTTPException(409,'结果尚未完成')
    package_id,data=build_recalc_package(r)
    log(db,user,'run.recalc_package',id,package_id);db.commit()
    return Response(data,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="'+package_id+'.zip"'})
