#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''河砾微站密度巡检图 · 离线复算核验器（纯标准库，零依赖，可离线运行）

外业收到复算包后本地核验：
  python3 verify.py PACKAGE_DIR [--reset] [--credential PATH] [--report PATH]

- PACKAGE_DIR 为解包后的目录，内含 manifest.json 与 batches/batch-NNN.json；
- 批次文件可分批、乱序到达，缺批补到后重新执行本命令即可续验；
- 中断恢复时自动跳过已验批次；换包或已验批次被改动时拒用旧进度（按提示 --reset）；
- 全部批次到齐后按原样本顺序逐站复算，核对噪声与簇成员（簇改号不算差异）；
- 全部相符才出具核验凭据 verification-credential.json；
- 复算严格按包内锁定的算法版本执行，算法升级后旧包仍按原版计算。

退出码：0 核验通过（或该包已完成核验）；1 核验失败 / 包异常；2 批次未齐，等待补批。
'''
import argparse,hashlib,json,math,os,sys
from datetime import datetime,timezone
PACKAGE_TYPE='station-density-recalc'
FORMAT_VERSION=1
PROGRESS_FILE='.recalc-progress.json'
CREDENTIAL_FILE='verification-credential.json'
REPORT_FILE='recalc-report.json'
FEATURES=('temperature','humidity','particles')
class VerifyError(Exception):pass
# ---------------------------------------------------------------- 算法注册表
# 按包内 algorithm_version 分发复算；升级算法时新增版本实现并保留旧版，旧包仍按原版计算。
ALGORITHMS={}
def algorithm(version):
    def wrap(fn):ALGORITHMS[version]=fn;return fn
    return wrap
def _parameters_v1(parameters):
    if not isinstance(parameters,dict):raise VerifyError('清单中的参数不是对象')
    unknown=set(parameters)-{'eps','min_samples'}
    if unknown:raise VerifyError('清单含不支持的参数：'+','.join(sorted(unknown)))
    try:eps=float(parameters['eps']);ms=float(parameters['min_samples'])
    except (TypeError,ValueError):raise VerifyError('参数必须为数值')
    if not(math.isfinite(eps)and math.isfinite(ms)):raise VerifyError('参数必须为有限数值')
    if not 0.01<=eps<=5:raise VerifyError('eps 需在 0.01 至 5 之间')
    if ms!=int(ms) or not 2<=ms<=100:raise VerifyError('min_samples 需为 2 至 100 的整数')
    return eps,int(ms)
def _matrix(records):
    cols={f:[] for f in FEATURES}
    for i,r in enumerate(records):
        values=r.get('values') if isinstance(r,dict) else None
        if not isinstance(values,dict):raise VerifyError('第%d条样本缺少观测值'%(i+1))
        for f in FEATURES:
            v=values.get(f)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
                raise VerifyError('第%d条样本的 %s 不是有限数值'%(i+1,f))
            cols[f].append(float(v))
    return cols
def _dbscan(points,eps,min_samples):
    '''与线上 density-dbscan-v1 同口径：标准化后欧氏距离 <= eps 记为邻居，
    邻居数（含自身）>= min_samples 为核心点，密度连通成簇，其余可达点为边界，剩余为噪声。'''
    n=len(points);neighbors=[]
    for i in range(n):
        xi=points[i];nb=[]
        for j in range(n):
            xj=points[j]
            d=math.sqrt((xi[0]-xj[0])**2+(xi[1]-xj[1])**2+(xi[2]-xj[2])**2)
            if d<=eps:nb.append(j)
        neighbors.append(nb)
    core=[len(nb)>=min_samples for nb in neighbors]
    labels=[-1]*n;cluster=0
    for i in range(n):
        if labels[i]!=-1 or not core[i]:continue
        labels[i]=cluster
        seeds=list(neighbors[i]);head=0
        while head<len(seeds):
            j=seeds[head];head+=1
            if labels[j]!=-1:continue
            labels[j]=cluster
            if core[j]:seeds.extend(neighbors[j])
        cluster+=1
    return labels,core
@algorithm('density-dbscan-v1')
def recalc_v1(records,parameters):
    '''逐站复算，返回 [{'reference','cluster','kind'}]，顺序与输入样本一致。'''
    eps,min_samples=_parameters_v1(parameters)
    if len(records)<min_samples:raise VerifyError('样本数少于核心点最少邻居数')
    cols=_matrix(records);n=len(records);z=[]
    for f in FEATURES:
        col=cols[f];mean=math.fsum(col)/n
        var=math.fsum((x-mean)*(x-mean) for x in col)/n
        if var==0:raise VerifyError('特征含常量列，无法解释标准化密度')
        scale=math.sqrt(var)
        z.append([(x-mean)/scale for x in col])
    labels,core=_dbscan(list(zip(*z)),eps,min_samples)
    return [{'reference':r.get('reference'),'cluster':labels[i],
             'kind':'噪声' if labels[i]<0 else ('核心' if core[i] else '边界')} for i,r in enumerate(records)]
# ---------------------------------------------------------------- 工具函数
def _canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def _sha_bytes(b):return hashlib.sha256(b).hexdigest()
def _sha_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as fh:
        for chunk in iter(lambda:fh.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()
def _load_json(path):
    try:
        with open(path,'rb') as fh:return json.loads(fh.read().decode('utf-8'))
    except FileNotFoundError:raise VerifyError('缺少文件：'+os.path.basename(path)+'，复算包不完整')
    except (UnicodeDecodeError,json.JSONDecodeError):raise VerifyError('文件不是合法JSON：'+os.path.basename(path))
def _now():return datetime.now(timezone.utc).isoformat()
def _batch_path(pkg_dir,b):
    rel=b['file']
    if not isinstance(rel,str) or os.path.isabs(rel) or '..' in rel.split('/'):raise VerifyError('清单批次路径非法')
    return os.path.join(pkg_dir,*rel.split('/'))
# ---------------------------------------------------------------- 清单与进度
def _check_manifest(m):
    if not isinstance(m,dict):raise VerifyError('清单结构非法')
    if m.get('package_type')!=PACKAGE_TYPE:raise VerifyError('不是本系统的复算包清单')
    if m.get('format_version')!=FORMAT_VERSION:raise VerifyError('复算包格式版本不支持：'+str(m.get('format_version')))
    for key in ('package_id','run_id','algorithm_version','parameters','record_count','input_checksum','batches'):
        if key not in m:raise VerifyError('清单缺少字段：'+key)
    batches=m['batches']
    if not isinstance(batches,list) or not batches:raise VerifyError('清单批次为空')
    if not isinstance(m['record_count'],int) or m['record_count']<=0:raise VerifyError('清单样本数非法')
    seen=set();start=0
    for b in batches:
        if not isinstance(b,dict):raise VerifyError('清单批次结构非法')
        for key in ('file','index','start','count','sha256'):
            if key not in b:raise VerifyError('清单批次缺少字段：'+key)
        if b['index'] in seen:raise VerifyError('清单批次序号重复')
        seen.add(b['index'])
        if b['start']!=start:raise VerifyError('清单批次区间不连续')
        start+=b['count']
    if seen!=set(range(len(batches))):raise VerifyError('清单批次序号不连续')
    if start!=m['record_count']:raise VerifyError('清单批次总数与样本数不符')
    return batches
def _load_progress(pkg_dir,manifest_sha):
    path=os.path.join(pkg_dir,PROGRESS_FILE)
    if not os.path.exists(path):return None
    try:
        with open(path,encoding='utf-8') as fh:p=json.load(fh)
    except (OSError,json.JSONDecodeError):raise VerifyError('进度文件损坏，拒用旧进度；确认后用 --reset 清空重验')
    if p.get('manifest_sha256')!=manifest_sha:
        raise VerifyError('检测到复算包已更换（清单摘要不一致），拒用旧进度；确认更换来源后用 --reset 清空重验')
    return p
def _save_progress(pkg_dir,p):
    p['updated_at']=_now()
    with open(os.path.join(pkg_dir,PROGRESS_FILE),'w',encoding='utf-8') as fh:json.dump(p,fh,ensure_ascii=False,indent=1)
# ---------------------------------------------------------------- 比对
def _compare(expected,actual):
    '''逐站核对噪声与簇成员；簇编号允许整体改号（双射映射一致即视为相同分区）。'''
    mismatches=[];forward={};backward={}
    for i,(e,a) in enumerate(zip(expected,actual)):
        problems=[]
        if a.get('reference')!=e.get('reference'):problems.append('样本编号不一致')
        ec,ac=e.get('cluster'),a.get('cluster')
        if (ec==-1)!=(ac==-1):problems.append('噪声判定不一致')
        elif ec!=-1:
            if forward.get(ac,ec)!=ec or backward.get(ec,ac)!=ac:problems.append('簇成员不一致')
            else:forward[ac]=ec;backward[ec]=ac
        if e.get('kind')!=a.get('kind'):problems.append('微站类型不一致')
        if problems:mismatches.append({'index':i,'reference':e.get('reference'),
            'expected':{'cluster':ec,'kind':e.get('kind')},'actual':{'cluster':ac,'kind':a.get('kind')},'problems':problems})
    return mismatches
# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap=argparse.ArgumentParser(description='河砾微站密度巡检图 · 离线复算核验器')
    ap.add_argument('package_dir',help='解包后的复算包目录（含 manifest.json 与 batches/）')
    ap.add_argument('--reset',action='store_true',help='清空本目录下的旧进度、凭据与报告后重新验收')
    ap.add_argument('--credential',help='核验凭据输出路径（默认写入包目录）')
    ap.add_argument('--report',help='差异报告输出路径（默认写入包目录）')
    args=ap.parse_args(argv)
    pkg_dir=args.package_dir
    credential_path=args.credential or os.path.join(pkg_dir,CREDENTIAL_FILE)
    report_path=args.report or os.path.join(pkg_dir,REPORT_FILE)
    if not os.path.isdir(pkg_dir):print('目录不存在：'+pkg_dir,file=sys.stderr);return 1
    if args.reset:
        for path in (os.path.join(pkg_dir,PROGRESS_FILE),credential_path,report_path):
            if os.path.exists(path):
                try:os.remove(path)
                except OSError as e:print('无法清除旧文件：%s'%e,file=sys.stderr);return 1
        print('已清空旧进度，重新验收。')
    try:
        with open(os.path.join(pkg_dir,'manifest.json'),'rb') as fh:manifest_bytes=fh.read()
    except FileNotFoundError:
        print('缺少 manifest.json，复算包不完整。',file=sys.stderr);return 1
    manifest_sha=_sha_bytes(manifest_bytes)
    try:
        m=json.loads(manifest_bytes.decode('utf-8'))
    except (UnicodeDecodeError,json.JSONDecodeError):
        print('manifest.json 不是合法JSON。',file=sys.stderr);return 1
    try:
        batches=_check_manifest(m)
        progress=_load_progress(pkg_dir,manifest_sha)
        accepted=(progress or {}).get('accepted') or {}
        if not isinstance(accepted,dict):raise VerifyError('进度文件与清单不符，拒用旧进度；确认后用 --reset 清空重验')
        accepted=dict(accepted)
        by_index={b['index']:b for b in batches}
        # 已验批次完整性复核：被改动则拒用旧进度；被取走则回到待补
        for idx in list(accepted):
            try:b=by_index[int(idx)]
            except (KeyError,ValueError):raise VerifyError('进度文件与清单不符，拒用旧进度；确认后用 --reset 清空重验')
            path=_batch_path(pkg_dir,b)
            if not os.path.exists(path):del accepted[idx];print('批次 '+b['file']+' 已不在包内，回到待补。')
            elif _sha_file(path)!=accepted[idx]:
                raise VerifyError('已验批次 '+b['file']+' 被改动，拒用旧进度；请核对来源后用 --reset 重新验收')
        # 批次验收：可乱序到达，缺批登记待补，校验和不符即拒绝
        pending=[];corrupt=[]
        for b in batches:
            idx=str(b['index'])
            if idx in accepted:continue
            path=_batch_path(pkg_dir,b)
            if not os.path.exists(path):pending.append(b['file']);continue
            digest=_sha_file(path)
            if digest!=b['sha256']:corrupt.append(b['file']);continue
            accepted[idx]=digest
        phase=(progress or {}).get('phase') or 'accepting'
        if len(accepted)<len(batches):phase='accepting'
        state={'manifest_sha256':manifest_sha,'package_id':m['package_id'],'accepted':accepted,'phase':phase}
        _save_progress(pkg_dir,state)
        if corrupt:
            print('批次校验和不符，已拒绝：'+', '.join(corrupt)+'。请重新拷贝该批后续验。',file=sys.stderr);return 1
        if pending:
            print('批次验收中：%d/%d 已验，待补：%s'%(len(accepted),len(batches),', '.join(pending)))
            print('补到后重新执行本命令即可续验（已验批次自动跳过）。')
            return 2
        # 全部批次到齐：按原样本顺序拼接，逐站复算
        records=[];expected=[]
        for b in sorted(batches,key=lambda x:x['start']):
            payload=_load_json(_batch_path(pkg_dir,b))
            if payload.get('package_id')!=m['package_id']:raise VerifyError('批次 '+b['file']+' 不属于本包')
            if payload.get('batch_index')!=b['index'] or payload.get('start')!=b['start']:raise VerifyError('批次 '+b['file']+' 序号与清单不符')
            recs=payload.get('records');exps=payload.get('expected')
            if not isinstance(recs,list) or not isinstance(exps,list) or len(recs)!=b['count'] or len(exps)!=b['count']:
                raise VerifyError('批次 '+b['file']+' 记录数与清单不符')
            for r,e in zip(recs,exps):
                if not isinstance(r,dict) or 'reference' not in r or 'values' not in r:raise VerifyError('批次 '+b['file']+' 样本结构非法')
                if not isinstance(e,dict) or e.get('reference')!=r.get('reference'):raise VerifyError('批次 '+b['file']+' 样本与明细顺序不符')
            records.extend(recs);expected.extend(exps)
        if len(records)!=m['record_count']:raise VerifyError('样本总数与清单不符')
        version=m['algorithm_version']
        fn=ALGORITHMS.get(version)
        if not fn:raise VerifyError('算法版本不受支持：%s（本核验器支持：%s），请使用与包匹配的核验器'%(version,', '.join(sorted(ALGORITHMS))))
        actual=fn(records,m['parameters'])
        mismatches=_compare(expected,actual)
        if mismatches:
            report={'package_id':m['package_id'],'run_id':m['run_id'],'manifest_sha256':manifest_sha,
                    'mismatch_count':len(mismatches),'mismatches':mismatches[:50],'reported_at':_now()}
            with open(report_path,'w',encoding='utf-8') as fh:json.dump(report,fh,ensure_ascii=False,indent=1)
            if os.path.exists(credential_path):os.remove(credential_path)
            print('核验未通过：%d 站与密度明细不符，差异报告见 %s；不出具核验凭据。'%(len(mismatches),report_path),file=sys.stderr)
            return 1
        # 幂等：已出具过凭据且自校验一致，直接确认；凭据损坏则重算后重签
        if (progress or {}).get('phase')=='completed' and os.path.exists(credential_path):
            try:
                cred=_load_json(credential_path)
                ok=cred.get('manifest_sha256')==manifest_sha and cred.get('credential_sha256')==_sha_bytes(_canonical({k:v for k,v in cred.items() if k!='credential_sha256'}).encode())
            except VerifyError:ok=False
            if ok:
                print('该包已完成核验，凭据有效：'+cred['credential_sha256'])
                return 0
            os.remove(credential_path)
        clusters=len({r['cluster'] for r in actual if r['cluster']!=-1})
        noise=sum(1 for r in actual if r['cluster']==-1)
        core=sum(1 for r in actual if r['kind']=='核心')
        credential={'credential_type':'station-density-recalc-credential','package_id':m['package_id'],
                    'run_id':m['run_id'],'run_name':m.get('run_name',''),'algorithm_version':version,
                    'parameters':m['parameters'],'record_count':len(records),'input_checksum':m['input_checksum'],
                    'manifest_sha256':manifest_sha,'verified_rows':len(records),
                    'clusters':clusters,'noise':noise,'core':core,'verified_at':_now()}
        credential['credential_sha256']=_sha_bytes(_canonical(credential).encode())
        with open(credential_path,'w',encoding='utf-8') as fh:json.dump(credential,fh,ensure_ascii=False,indent=1)
        state['phase']='completed';_save_progress(pkg_dir,state)
        print('核验通过：研究 #%s 共 %d 站逐站复算全部相符（密度分区 %d 个 / 核心 %d 站 / 噪声 %d 站）。'%(m['run_id'],len(records),clusters,core,noise))
        print('核验凭据：%s'%credential_path)
        print('凭据摘要：%s'%credential['credential_sha256'])
        return 0
    except VerifyError as e:
        print('核验失败：'+str(e),file=sys.stderr);return 1
    except OSError as e:
        print('核验失败（文件读写）：'+str(e),file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
