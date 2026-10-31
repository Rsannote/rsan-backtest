from pathlib import Path
import csv,datetime as dt,hashlib,io,json,math,statistics,subprocess,sys,shutil,tempfile
ROOT=Path(__file__).resolve().parents[4]; OUT=ROOT/'logs/test/T14b/独立复验'
sys.path.insert(0,str(ROOT/'scripts'))
import mainline_cases as cases
from mainline_cases_data import HUMAN_FIELDS

def read(p):
 with p.open(newline='') as f:return list(csv.DictReader(f))
def num(v):
 try:
  x=float(v);return x if math.isfinite(x) else None
 except (ValueError,TypeError):return None
old=subprocess.check_output(['git','show','3781fe50fc394ddb33bccca97f7aa78a7b0457b9:data/事件库/主线.csv'],cwd=ROOT)
now=(ROOT/'data/事件库/主线.csv').read_bytes();assert now.startswith(old)
a=read(ROOT/'data/事件库/主线.csv'); previous={r['事件id']:r for r in csv.DictReader(io.StringIO(old.decode('utf-8-sig')))};latest={r['事件id']:r for r in a}
assert len(previous)==18 and len(a)==96 and len(latest)==60
row_count=len(a)
for id,row in previous.items():
 assert sum(r['事件id']==id and r['状态']==cases.SUPERSEDED for r in a)==1
 assert latest[id]['状态']==cases.SUPERSEDED
 for k in row:
  if k not in HUMAN_FIELDS+['状态','程序说明','追加时间','更新原因']:assert row[k]==latest[id][k],(id,k)
v1={id:r for id,r in latest.items() if '-ML1-' in id and r['状态']!='输入修订后不再满足'};assert len(v1)==42
seen={};hum={}
for r in a:
 id=r['事件id']
 if '-ML1-' in id and id not in seen:assert all(not r[k] for k in HUMAN_FIELDS)
 seen[id]=True
 for k in HUMAN_FIELDS:
  if r[k]:hum.setdefault(id,{})[k]=r[k]
for id,vals in hum.items():assert all(latest[id][k]==v for k,v in vals.items())
registry=read(ROOT/'system/行业清单.csv');codes=sorted(r['申万代码'] for r in registry if r['主线判定']=='参与' and r['申万代码'] not in ('801960','801970','801980'));assert len(codes)==27
flow={}
for r in read(ROOT/'data/资金轮动/日表.csv'):flow[r['date'],r['code']]=r
bench={}
for r in read(ROOT/'data/行情库/指数/sh000300.csv'):
 c,f=num(r['close']),num(r['factor']);bench[r['date']]=c*f if c is not None and f is not None else None
calendar=sorted(d for d,x in bench.items() if x is not None and x>0 and d>='2005-01-01'); end=min(calendar[-1],max(d for d,c in flow));start=min(d for d,c in flow);cutoff=end;days=[d for d in calendar if start<=d<=end];index={d:i for i,d in enumerate(days)}
states={c:[] for c in codes};rank_history={};legacy_states={c:[] for c in codes};all28=sorted(codes+['801230'])
for d in days:
 rel={c:num(flow.get((d,c),{}).get('相对沪深300_60')) for c in codes}
 rank={c:i+1 for i,c in enumerate(sorted(codes,key=lambda c:(-rel[c],c)))} if all(x is not None for x in rel.values()) else {}
 rank_history[d]=rank
 shares={c:num(flow.get((d,c),{}).get('占比_28')) for c in all28}
 srank={c:i+1 for i,c in enumerate(sorted(codes,key=lambda c:(-shares[c],c)))} if all(x is not None and x>=0 for x in shares.values()) and abs(sum(shares.values())-1)<=1e-7 else {}
 for c in codes:
  legacy_states[c].append(False if (c in rank and rank[c]>3) or (c in srank and srank[c]>5) else True if c in rank and c in srank else None)
  p=num(flow.get((d,c),{}).get('占比一年分位_28'));p=p if p is not None and 0<=p<=100 else None
  if (c in rank and rank[c]>3) or (p is not None and p<70):v=False
  elif c in rank and p is not None:v=True
  else:v=None
  states[c].append(v)
expected={}
for c,s in states.items():
 hits=[i for i,v in enumerate(s) if v is True];groups=[]
 for h in hits:
  if not groups or h-groups[-1][-1]>6:groups.append([])
  groups[-1].append(h)
 for g in groups:
  if len(g)>=40:expected[f'EVT-SW{c}-ML1-{days[g[0]]}']=(c,g)
assert set(expected)==set(v1)
prices={}
for c in codes:
 p={}
 for r in read(ROOT/'data/资金轮动/原始/申万'/f'{c}.csv'):p[r['date']]=num(r['close'])
 prices[c]=p
excess=[]
for id,(c,hits) in expected.items():
 r=v1[id];s=states[c];start,end=days[hits[0]],days[hits[-1]];tail=s[hits[-1]+1:min(hits[-1]+7,len(s))]
 status=('已确认结束' if all(x is False for x in tail) else '数据中断待核实') if len(tail)==6 else ('数据不足待确认' if None in tail else '待中断确认')
 confirmed=days[hits[-1]+6] if len(tail)==6 and all(x is False for x in tail) else ''
 assert (r['开始'],r['结束'],r['成立日期'],r['状态'],r['确认结束日期'])==(start,end,days[hits[39]],status,confirmed),id
 assert int(r['实际命中交易日'])==len(hits)
 assert int(r['桥接跨度交易日'])==hits[-1]-hits[0]+1
 assert int(r['桥接已知失败日'])==sum(x is False for x in s[hits[0]:hits[-1]+1])
 assert int(r['桥接未知日'])==sum(x is None for x in s[hits[0]:hits[-1]+1])
 value=prices[c][end]/prices[c][start]-bench[end]/bench[start]
 assert abs(float(r['区间内相对沪深300超额'])-value)<1e-9
 excess.append(value)
 ranks=sorted(codes,key=lambda x:(-(prices[x][end]/prices[x][start]),x));assert r['同期第二强代码']==ranks[1]
 window=json.loads(r['启动前20日五个维度的读数']);n=calendar.index(start);assert [x['date'] for x in window['读数']]==calendar[max(0,n-20):n]
report=json.loads((ROOT/'logs/test/T14b/验收与对照.json').read_text());comp=report['v0v1对照']['v1同输入重算'];assert comp['段数']==42 and comp['超额有效段数']==42 and comp['超额为负段数']==sum(x<0 for x in excess);assert abs(comp['平均区间超额']-statistics.mean(excess))<1e-12
assert len(report['公开窗口对照'])==12
legacy=[]
for c,st in legacy_states.items():
 groups=[]
 for n,v in enumerate(st):
  if v is not True:continue
  if not groups or n-groups[-1][-1]>6:groups.append([])
  groups[-1].append(n)
 for h in groups:
  if len(h)>=40:
   a,b=days[h[0]],days[h[-1]];legacy.append(prices[c][b]/prices[c][a]-bench[b]/bench[a])
assert len(legacy)==18
for k,v in [('平均区间超额',statistics.mean(legacy)),('超额为负段数',sum(x<0 for x in legacy)),('段数',18)]:
 assert abs(report['v0v1对照']['v0同输入重算'][k]-v)<1e-12
public_checks=[]
for item in report['公开窗口对照']:
 c=item['申万代码']
 if not c:continue
 positions=[i for i,d in enumerate(days) if item['窗口开始']<=d<=item['窗口结束']]
 observed=[states[c][i] for i in positions];groups=[]
 for i,v in enumerate(observed):
  if v is not True:continue
  if not groups or i-groups[-1][-1]>6:groups.append([])
  groups[-1].append(i)
 vals={'可用交易日':len(positions),'相对前3日':sum(rank_history[days[i]].get(c,999)<=3 for i in positions),'自身分位至少70日':sum((lambda x:x is not None and 70<=x<=100)(num(flow.get((days[i],c),{}).get('占比一年分位_28'))) for i in positions),'共同命中日':sum(v is True for v in observed),'未知日':sum(v is None for v in observed),'最长连接段实际命中':max(map(len,groups),default=0)}
 for k,v in vals.items():assert item[k]==v,(item['公开组'],c,k,item[k],v)
 overlaps=sorted(r['事件id']+'（成立'+r['成立日期']+'）' for r in v1.values() if r['申万代码']==c and r['开始']<=item['窗口结束'] and r['结束']>=item['窗口开始'] and r['成立日期']<=item['窗口结束'])
 assert sorted(item['窗口交集案例'])==overlaps
 public_checks.append({'组':item['公开组'],'对象':item['对象'],**vals,'案例':overlaps})
(OUT/'12对象逐项独立核对.json').write_text(json.dumps({'真实行业8对象':public_checks,'4概念或风格对象不映射':True},ensure_ascii=False,indent=2)+'\n')
for item in report['公开窗口对照']:
 if item['对象']=='计算机' and item['公开组']=='2015上半年':assert item['最长连接段实际命中']==10 and not item['窗口交集案例']
 if item['对象']=='银行' and item['公开组']=='2024全年':assert item['最长连接段实际命中']==33 and not item['窗口交集案例']
 for d in ['data/事件库/主线_总表.md','data/事件库/说明.md']:
  original=subprocess.check_output(['git','show','3781fe50fc394ddb33bccca97f7aa78a7b0457b9:'+d],cwd=ROOT);assert (ROOT/d).read_bytes().startswith(original)
text=(ROOT/'data/事件库/主线_总表.md').read_text().split('\n<!-- T14快照 -->\n')[-1];assert text.count('-ML1-')==42 and '-ML-' not in text
result={'独立纯CSV条件分组':42,'候选':len(codes),'截止':cutoff,'v0同输入段数':len(legacy),'v0平均超额':statistics.mean(legacy),'v0负超额':sum(x<0 for x in legacy),'原CSV前缀字节':len(old),'物理行':row_count,'旧事件取代':18,'现行v1':42,'v1平均超额':statistics.mean(excess),'v1负超额':sum(x<0 for x in excess),'公开对象':12,'v0机器值与人工文字原样':True,'旧总表及说明前缀完整':True}
(OUT/'独立原始条件与价格核验.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
if '--no-isolation' in sys.argv:raise SystemExit(0)
# 真正运行放隔离root，输入只读软链；输出拷贝，验证数据写入完全幂等。
iso=Path(tempfile.mkdtemp(prefix='t14b-independent-'));(iso/'data').mkdir();(iso/'logs/test').mkdir(parents=True)
for p in (ROOT/'data').iterdir():
 target=iso/'data'/p.name
 if p.name=='事件库':shutil.copytree(p,target)
 else:target.symlink_to(p,target_is_directory=p.is_dir())
(iso/'system').symlink_to(ROOT/'system',target_is_directory=True)
shutil.copytree(ROOT/'logs/test/T14',iso/'logs/test/T14');shutil.copytree(ROOT/'logs/test/T14b',iso/'logs/test/T14b')
paths=['data/事件库/主线.csv','data/事件库/主线_总表.md','data/事件库/说明.md'];before={p:hashlib.sha256((iso/p).read_bytes()).hexdigest() for p in paths}
r=cases.run(iso);assert r['追加行数']==0
assert all(hashlib.sha256((iso/p).read_bytes()).hexdigest()==h for p,h in before.items())
(OUT/'隔离真实幂等.json').write_text(json.dumps({'隔离root':str(iso),'新增':r['追加行数'],'案例数':r['案例数'],'SHA256不变':before},ensure_ascii=False,indent=2)+'\n');print('隔离真实复跑0追加，三份data输出SHA256不变')
