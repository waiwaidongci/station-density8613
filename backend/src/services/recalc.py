import hashlib,io,json,zipfile
from ..utils.serialization import now
PACKAGE_TYPE='station-density-recalc'
FORMAT_VERSION=1
BATCH_SIZE=100
# 版本机制前完成的结果没有 algorithm_version 字段，全部由初版算法计算
UNSTAMPED_RESULT_VERSION='density-dbscan-v1'
def _canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def _sha(text):return hashlib.sha256(text.encode()).hexdigest()
def package_files(run):
    '''把已完成任务打成复算包文件集：manifest.json + batches/batch-NNN.json。
    批次保持原样本顺序切片，每批自带该区间的预期密度明细，可乱序到达、按SHA256验收。'''
    result=run.result or {}
    records=run.snapshot['records']
    rows=result.get('rows') or []
    if len(rows)!=len(records):raise ValueError('结果明细与冻结样本数量不一致')
    checksum=result.get('checksum') or _sha(json.dumps(records,sort_keys=True))
    version=result.get('algorithm_version') or UNSTAMPED_RESULT_VERSION
    package_id='recalc-run-%s-%s'%(run.id,checksum[:12])
    files={};batches=[]
    for index,start in enumerate(range(0,len(records),BATCH_SIZE)):
        chunk=records[start:start+BATCH_SIZE];detail=rows[start:start+BATCH_SIZE]
        for r,row in zip(chunk,detail):
            if r['reference']!=row.get('reference'):raise ValueError('结果明细与冻结样本顺序不一致')
        payload={'package_id':package_id,'batch_index':index,'start':start,'count':len(chunk),
                 'records':[{'reference':r['reference'],'values':r['values']} for r in chunk],
                 'expected':[{'reference':row['reference'],'cluster':row['cluster'],'kind':row['kind']} for row in detail]}
        data=_canonical(payload)
        name='batches/batch-%03d.json'%index
        files[name]=data
        batches.append({'file':name,'index':index,'start':start,'count':len(chunk),'sha256':_sha(data)})
    manifest={'package_type':PACKAGE_TYPE,'format_version':FORMAT_VERSION,'package_id':package_id,
              'run_id':run.id,'run_name':run.name,'created_at':now(),
              'algorithm_version':version,'parameters':run.parameters,
              'record_count':len(records),'input_checksum':checksum,'batches':batches}
    files['manifest.json']=_canonical(manifest)
    return package_id,files
def build_recalc_package(run):
    package_id,files=package_files(run)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):z.writestr(name,files[name])
    return package_id,buf.getvalue()
