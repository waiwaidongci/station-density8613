#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''复算包与离线核验器测试（纯标准库，沙箱可直接运行）。

运行：python3 test-artifacts/gb-0042/test_recalc.py
覆盖：打包结构、已知答案oracle（核心/边界/噪声）、乱序分批到达与续验、中断跳过、
换包与批次改动拒用旧进度、簇改号容忍、噪声/成员差异拒绝、算法版本锁定。
环境装有 numpy/sklearn 时自动追加与线上 calculate() 的真实对照。
'''
import hashlib,importlib.util,io,json,os,random,shutil,subprocess,sys,tempfile,types,zipfile
ROOT=os.path.dirname(os.path.abspath(__file__))
REPO=os.path.abspath(os.path.join(ROOT,'..','..'))
sys.path.insert(0,os.path.join(REPO,'backend'))
from src.services.recalc import build_recalc_package,package_files,BATCH_SIZE
spec=importlib.util.spec_from_file_location('recalc_verify',os.path.join(REPO,'recalc','verify.py'))
verify=importlib.util.module_from_spec(spec);spec.loader.exec_module(verify)
VERIFIER=os.path.join(REPO,'recalc','verify.py')
PARAMS={'eps':0.55,'min_samples':5}
FAILED=[]
def test(fn):
    try:fn();print('PASS',fn.__name__)
    except Exception as e:
        FAILED.append(fn.__name__);print('FAIL',fn.__name__,'->',e)
        import traceback;traceback.print_exc()
def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def sha(text):return hashlib.sha256(text.encode()).hexdigest()
def make_records(n=240,seed=42):
    rng=random.Random(seed);out=[]
    for i in range(n):
        cls=i%3
        if i%19==0:vals={'temperature':round(rng.uniform(12,42),2),'humidity':round(rng.uniform(12,92),2),'particles':round(rng.uniform(4,140),2)}
        else:vals={'temperature':round(rng.gauss([19,26,32][cls],0.8),2),'humidity':round(rng.gauss([72,45,28][cls],2.5),2),'particles':round(max(.1,rng.gauss([22,48,85][cls],3)),2)}
        out.append({'id':i+1,'source_id':1,'reference':'HL-T-%04d'%(i+1),'values':vals})
    return out
def v1_rows(records,params=PARAMS):
    return verify.ALGORITHMS['density-dbscan-v1']([{'reference':r['reference'],'values':r['values']} for r in records],params)
def make_run(records,rows,run_id=3,name='复算测试研究',params=None,stamped=True):
    result={'rows':rows,'checksum':sha(json.dumps(records,sort_keys=True))}
    if stamped:result['algorithm_version']='density-dbscan-v1'
    return types.SimpleNamespace(id=run_id,name=name,parameters=dict(params or PARAMS),snapshot={'records':records},result=result)
def unpack(pkg_dir,data,only=None):
    os.makedirs(pkg_dir,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if only is None or name in only:z.extract(name,pkg_dir)
def cli(pkg_dir,*args):
    return subprocess.run([sys.executable,'-I',VERIFIER,pkg_dir,*args],capture_output=True,text=True)
def read(path):
    with open(path,encoding='utf-8') as fh:return json.load(fh)
# ---------------------------------------------------------------- 已知答案 oracle
def oracle_tight_clusters():
    '''两个紧致簇 + 一个离群站：簇内全核心，离群为噪声（手工核算标准化距离）。'''
    temps=[20.00,20.01,19.99,20.02,19.98, 35.00,35.01,34.99,35.02, 25.0]
    hums =[50.00,50.01,49.99,50.02,49.98, 80.00,80.01,79.99,80.02, 65.0]
    pars =[30.00,30.01,29.99,30.02,29.98, 90.00,90.01,89.99,90.02, 55.0]
    records=[{'reference':'P%d'%i,'values':{'temperature':t,'humidity':h,'particles':p}} for i,(t,h,p) in enumerate(zip(temps,hums,pars))]
    rows=verify.ALGORITHMS['density-dbscan-v1'](records,{'eps':0.55,'min_samples':3})
    expect=[(0,'核心')]*5+[(1,'核心')]*4+[(-1,'噪声')]
    got=[(r['cluster'],r['kind']) for r in rows]
    assert got==expect,('簇划分不符',got)
def oracle_border_chain():
    '''等距链式布站：端点邻居不足为核心但可达核心站，应为边界；两簇各自独立。'''
    temps=[20.0,20.3,20.6,20.9,21.2, 35.0,35.3,35.6,35.9]
    hums =[50.00,50.01,49.99,50.00,50.01, 80.00,80.01,79.99,80.00]
    pars =[30.00,30.01,29.99,30.00,30.01, 90.00,90.01,89.99,90.00]
    records=[{'reference':'P%d'%i,'values':{'temperature':t,'humidity':h,'particles':p}} for i,(t,h,p) in enumerate(zip(temps,hums,pars))]
    rows=verify.ALGORITHMS['density-dbscan-v1'](records,{'eps':0.08,'min_samples':3})
    expect=[(0,'边界'),(0,'核心'),(0,'核心'),(0,'核心'),(0,'边界'),(1,'边界'),(1,'核心'),(1,'核心'),(1,'边界')]
    got=[(r['cluster'],r['kind']) for r in rows]
    assert got==expect,('核心/边界判定不符',got)
def oracle_params_rejected():
    for bad in ({'eps':0,'min_samples':5},{'eps':0.55,'min_samples':1.5},{'eps':0.55,'min_samples':5,'x':1}):
        try:verify.ALGORITHMS['density-dbscan-v1']([{'reference':'a','values':{'temperature':1,'humidity':2,'particles':3}}],bad);raise SystemExit('应拒绝参数')
        except verify.VerifyError:pass
# ---------------------------------------------------------------- 打包结构
def builder_structure():
    records=make_records();rows=v1_rows(records)
    package_id,files=package_files(make_run(records,rows))
    assert package_id.startswith('recalc-run-3-')
    assert 'manifest.json' in files and 'batches/batch-000.json' in files
    m=json.loads(files['manifest.json'])
    assert m['package_type']=='station-density-recalc' and m['format_version']==1
    assert m['algorithm_version']=='density-dbscan-v1' and m['parameters']==PARAMS
    assert m['record_count']==240 and m['input_checksum']==sha(json.dumps(records,sort_keys=True))
    assert len(m['batches'])==(240+BATCH_SIZE-1)//BATCH_SIZE==3
    start=0
    for b in m['batches']:
        assert b['start']==start and b['sha256']==sha(files[b['file']])
        payload=json.loads(files[b['file']])
        assert payload['package_id']==package_id and payload['batch_index']==b['index']
        assert len(payload['records'])==b['count']==len(payload['expected'])
        for r,e in zip(payload['records'],payload['expected']):assert r['reference']==e['reference']
        start+=b['count']
    assert start==240
    # 版本机制前的旧结果（无 algorithm_version）仍按初版锁定
    _,files2=package_files(make_run(records,rows,stamped=False))
    assert json.loads(files2['manifest.json'])['algorithm_version']=='density-dbscan-v1'
    # zip 可解包
    pid,data=build_recalc_package(make_run(records,rows))
    with zipfile.ZipFile(io.BytesIO(data)) as z:assert 'manifest.json' in z.namelist()
# ---------------------------------------------------------------- 端到端 CLI
def cli_full_pass():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data=build_recalc_package(make_run(records,rows))
        unpack(d,data)
        r=cli(d);assert r.returncode==0,('应核验通过',r.stderr)
        cred=read(os.path.join(d,'verification-credential.json'))
        assert cred['run_id']==3 and cred['record_count']==240 and cred['verified_rows']==240
        assert cred['algorithm_version']=='density-dbscan-v1' and cred['parameters']==PARAMS
        assert cred['input_checksum']==sha(json.dumps(records,sort_keys=True))
        assert cred['manifest_sha256']==hashlib.sha256(open(os.path.join(d,'manifest.json'),'rb').read()).hexdigest()
        assert cred['clusters']>=2 and cred['noise']>=1,('演示数据应含簇与噪声',cred)
        assert cred['credential_sha256']==sha(canonical({k:v for k,v in cred.items() if k!='credential_sha256'}))
        first=open(os.path.join(d,'verification-credential.json'),'rb').read()
        r=cli(d);assert r.returncode==0 and '已完成核验' in r.stdout,('应幂等确认',r.stdout)
        r=cli(d);assert r.returncode==0,('重复执行应稳定',r.stdout)
        assert open(os.path.join(d,'verification-credential.json'),'rb').read()==first,'凭据不应被重复签发改动'
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_out_of_order_resume():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data=build_recalc_package(make_run(records,rows))
        with zipfile.ZipFile(io.BytesIO(data)) as z:names=z.namelist()
        batches=sorted(n for n in names if n.startswith('batches/'))
        unpack(d,data,only={'manifest.json',batches[1]})
        r=cli(d);assert r.returncode==2 and batches[0] in r.stdout and batches[2] in r.stdout,('应待补两批',r.stdout)
        p=read(os.path.join(d,'.recalc-progress.json'));assert set(p['accepted'])=={'1'}
        unpack(d,data,only={batches[2]})
        r=cli(d);assert r.returncode==2 and batches[0] in r.stdout,('应只待补首批',r.stdout)
        p=read(os.path.join(d,'.recalc-progress.json'));assert set(p['accepted'])=={'1','2'}
        before=os.path.getsize(os.path.join(d,'.recalc-progress.json'))
        r=cli(d);assert r.returncode==2,('无变化时应保持待补且跳过已验批次',r.stdout)
        p=read(os.path.join(d,'.recalc-progress.json'));assert set(p['accepted'])=={'1','2'}
        unpack(d,data,only={batches[0]})
        r=cli(d);assert r.returncode==0,('补到后应续验通过',r.stderr)
        assert os.path.exists(os.path.join(d,'verification-credential.json'))
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_tamper_refuse():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data=build_recalc_package(make_run(records,rows))
        with zipfile.ZipFile(io.BytesIO(data)) as z:names=z.namelist()
        batches=sorted(n for n in names if n.startswith('batches/'))
        unpack(d,data,only={'manifest.json',batches[0]})
        r=cli(d);assert r.returncode==2
        target=os.path.join(d,batches[0])
        original=open(target,'rb').read()
        with open(target,'wb') as fh:fh.write(original+b' ')
        r=cli(d);assert r.returncode==1 and '拒用旧进度' in r.stderr,('已验批次被改动应拒用旧进度',r.stderr)
        r=cli(d,'--reset');assert r.returncode==1 and '校验和不符' in r.stderr,('重置后改动批次应被清单拒绝',r.stderr)
        with open(target,'wb') as fh:fh.write(original)
        r=cli(d);assert r.returncode==2,('恢复原批次后应能续验',r.stderr)
        unpack(d,data)
        r=cli(d);assert r.returncode==0,('补齐后应通过',r.stderr)
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_swap_refuse():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data_a=build_recalc_package(make_run(records,rows,run_id=3))
        _,data_b=build_recalc_package(make_run(records,rows,run_id=8,name='另一研究'))
        unpack(d,data_a)
        r=cli(d);assert r.returncode==0
        os.remove(os.path.join(d,'manifest.json'))
        shutil.rmtree(os.path.join(d,'batches'))
        unpack(d,data_b)
        r=cli(d);assert r.returncode==1 and '拒用旧进度' in r.stderr,('换包应拒用旧进度',r.stderr)
        r=cli(d,'--reset');assert r.returncode==0,('确认后重置应通过',r.stderr)
        cred=read(os.path.join(d,'verification-credential.json'));assert cred['run_id']==8
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_cluster_renumber_ok():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        k=max(r['cluster'] for r in rows)+1
        renumbered=[dict(r,cluster=(r['cluster']+1)%k if r['cluster']!=-1 else -1) for r in rows]
        _,data=build_recalc_package(make_run(records,renumbered))
        unpack(d,data)
        r=cli(d);assert r.returncode==0,('簇整体改号不应算差异',r.stderr)
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_membership_mismatch():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        pair=None
        for i in range(len(rows)):
            for j in range(i+1,len(rows)):
                if rows[i]['cluster']!=-1 and rows[j]['cluster']!=-1 and rows[i]['cluster']!=rows[j]['cluster'] and rows[i]['kind']==rows[j]['kind']:pair=(i,j);break
            if pair:break
        assert pair,'演示数据应有两个同类型不同簇的样本'
        i,j=pair;bad=[dict(r) for r in rows];bad[i]['cluster'],bad[j]['cluster']=bad[j]['cluster'],bad[i]['cluster']
        _,data=build_recalc_package(make_run(records,bad))
        unpack(d,data)
        r=cli(d);assert r.returncode==1,('簇成员被换应核验失败',r.stdout)
        report=read(os.path.join(d,'recalc-report.json'))
        assert report['mismatch_count']>=1 and any('簇成员不一致' in m['problems'] for m in report['mismatches'])
        assert not os.path.exists(os.path.join(d,'verification-credential.json')),'失败时不得出具凭据'
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_noise_flip():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        i=next(k for k,r in enumerate(rows) if r['cluster']!=-1)
        bad=[dict(r) for r in rows];bad[i]['cluster']=-1;bad[i]['kind']='噪声'
        _,data=build_recalc_package(make_run(records,bad))
        unpack(d,data)
        r=cli(d);assert r.returncode==1,('噪声判定被改应核验失败',r.stdout)
        report=read(os.path.join(d,'recalc-report.json'))
        assert any('噪声判定不一致' in m['problems'] for m in report['mismatches'])
        assert not os.path.exists(os.path.join(d,'verification-credential.json'))
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_unknown_algorithm():
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data=build_recalc_package(make_run(records,rows))
        unpack(d,data)
        m=read(os.path.join(d,'manifest.json'));m['algorithm_version']='density-dbscan-v999'
        with open(os.path.join(d,'manifest.json'),'w',encoding='utf-8') as fh:json.dump(m,fh,ensure_ascii=False)
        r=cli(d);assert r.returncode==1 and '算法版本' in r.stderr,('未知算法版本应明确拒绝',r.stderr)
    finally:shutil.rmtree(d,ignore_errors=True)
def cli_constant_column():
    d=tempfile.mkdtemp()
    try:
        rng=random.Random(7);records=[]
        for i in range(30):
            records.append({'id':i+1,'source_id':1,'reference':'C-%03d'%i,'values':{'temperature':round(rng.uniform(10,35),2),'humidity':50.0,'particles':round(rng.uniform(5,90),2)}})
        rows=[{'reference':r['reference'],'cluster':0,'kind':'核心'} for r in records]
        _,data=build_recalc_package(make_run(records,rows))
        unpack(d,data)
        r=cli(d);assert r.returncode==1 and '常量列' in r.stderr,('常量列应明确拒绝',r.stderr)
    finally:shutil.rmtree(d,ignore_errors=True)
def version_pinning():
    '''注册表存在更新版本时，旧包仍按包内锁定的原版复算。'''
    d=tempfile.mkdtemp()
    try:
        records=make_records();rows=v1_rows(records)
        _,data=build_recalc_package(make_run(records,rows))
        calls=[]
        def fake_v2(recs,params):
            calls.append(1);return verify.ALGORITHMS['density-dbscan-v1'](recs,params)
        verify.ALGORITHMS['density-dbscan-v2']=fake_v2
        unpack(d,data)
        assert verify.main([d])==0 and not calls,'v1 包不得动用 v2 实现'
        shutil.rmtree(d);os.makedirs(d)
        unpack(d,data)
        m=read(os.path.join(d,'manifest.json'));m['algorithm_version']='density-dbscan-v2'
        with open(os.path.join(d,'manifest.json'),'w',encoding='utf-8') as fh:json.dump(m,fh,ensure_ascii=False)
        assert verify.main([d])==0 and calls==[1],'v2 包应分发到 v2 实现'
    finally:
        verify.ALGORITHMS.pop('density-dbscan-v2',None);shutil.rmtree(d,ignore_errors=True)
def sklearn_cross_check():
    try:
        from src.services.algorithm import calculate
    except Exception:
        print('SKIP sklearn_cross_check（本环境无 numpy/sklearn；容器/CI 内自动执行）');return
    records=make_records();result=calculate(records,PARAMS)
    assert result['algorithm_version']=='density-dbscan-v1'
    run=types.SimpleNamespace(id=9,name='sklearn对照',parameters=dict(PARAMS),snapshot={'records':records},result=result)
    _,data=build_recalc_package(run)
    d=tempfile.mkdtemp()
    try:
        unpack(d,data)
        r=cli(d);assert r.returncode==0,('离线复算应与线上 sklearn 结果一致',r.stderr)
    finally:shutil.rmtree(d,ignore_errors=True)
CASES=(oracle_tight_clusters,oracle_border_chain,oracle_params_rejected,builder_structure,
       cli_full_pass,cli_out_of_order_resume,cli_tamper_refuse,cli_swap_refuse,
       cli_cluster_renumber_ok,cli_membership_mismatch,cli_noise_flip,
       cli_unknown_algorithm,cli_constant_column,version_pinning,sklearn_cross_check)
for fn in CASES:test(fn)
print('\n%d/%d 项通过'%(len(CASES)-len(FAILED),len(CASES)))
sys.exit(1 if FAILED else 0)
