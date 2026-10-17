"""Read-only acceptance of the repaired pipeline; writes only audit evidence."""
from pathlib import Path
import csv,difflib,hashlib,importlib.metadata,json,os,stat,sys,subprocess
import duckdb
WORK=Path(__file__).resolve().parents[1];ROOT=WORK.parent
QA=WORK/'qa/audit_stage0_3/codex_fix_20261015'
sys.path.insert(0,str(WORK/'tests'))
from verification import digest

def main():
 result={'scope':'阶段0—3本次修复；无来源真实性/因果营销效果/用户独立复现主张','checks':{},'limitations':['HTML浏览器视觉检查被URL安全策略阻止；未绕过，保留PENDING','iCloud当前无占位文件不保证未来不会再次逐出；未更改系统同步设置','发布回滚覆盖可捕获I/O异常；不承诺断电或强制终止时跨文件事务原子性']}
 for name in ('test_stage1','test_stage2','test_stage3'):
  r=__import__(name).run_tests();result['checks'][name]=r
 for name in ('test_verification.py','test_stage0_state.py'):
  run=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s',str(WORK/'tests'),'-p',name,'-v'],capture_output=True,text=True)
  (QA/(name+'.log')).write_text(run.stdout+run.stderr)
  if run.returncode:raise RuntimeError(run.stderr)
  result['checks'][name]={'status':'PASS','returncode':run.returncode}
 inputs=[]
 for sp in json.loads((WORK/'config/input_lock.json').read_text())['files']:
  p=ROOT/sp['path'];actual=digest(p);mode=stat.S_IMODE(p.stat().st_mode)
  assert actual==sp['sha256'] and p.stat().st_size==sp['bytes'] and mode==0o444
  inputs.append({'path':sp['path'],'sha256':actual,'bytes':p.stat().st_size,'mode':oct(mode),'status':'PASS'})
 result['inputs']=inputs
 with duckdb.connect(str(WORK/'data/o2o.duckdb'),read_only=True) as c:
  counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['raw_offline','fact_offline_records','fact_coupon_receipts','fact_offline_records_dedup','fact_coupon_receipts_dedup','offline_anomalies']}
  assert list(counts.values())==[1754884,1754884,1053282,1716991,1015389,0]
  assert c.execute('SELECT count(*),sum(redeemed15_mature::INT) FROM fact_coupon_receipts').fetchone()==(1053282,64395)
  assert c.execute('SELECT count(*) FROM fact_coupon_receipts JOIN offline_anomalies USING(record_id)').fetchone()[0]==0
  source_run=c.execute('SELECT run_id FROM pipeline_metadata').fetchone()[0]
  p=json.loads(c.execute('SELECT provenance_json FROM pipeline_metadata').fetchone()[0])
  assert p['script_sha256']==digest(WORK/'scripts/stage1.py') and p['verification_script_sha256']==digest(WORK/'scripts/verification.py')
  result['database']={'counts':counts,'source_run':source_run,'sha256':digest(WORK/'data/o2o.duckdb'),'receipt_numerator':64395,'receipt_denominator':1053282}
 result['stages']={}
 for st in (1,2,3):
  s=json.loads((WORK/f'qa/stage{st}/latest.json').read_text());assert s[f'stage{st}']=='PASS'
  result['stages'][str(st)]={'run_id':s['run_id'],'checks_passed':s['checks_passed']}
  if st==1:
   assert source_run==s['run_id'];continue
  out=Path(s['results_dir']);q=Path(s['qa_dir']);m=json.loads((out/'manifest.json').read_text())
  assert m.get('source_stage1_run',m.get('stage1_run_id'))==source_run
  assert m.get('source_database_sha256',m.get('stage1_database_sha256'))==result['database']['sha256']
  for n,sp in m['files'].items():assert digest(out/(n+'.csv'))==sp['sha256']
  hashes=m.get('script_hashes',{})|m.get('companion_script_hashes',{})
  if st==2:hashes['stage2.py']=m['stage2_script_sha256']
  for n,h in hashes.items():assert digest(WORK/'scripts'/n)==h
  for n,h in m['sql_hashes'].items():assert digest(WORK/'sql'/n)==h
  nb=next(out.glob('*.ipynb'));assert digest(nb)==digest(WORK/'notebooks'/nb.name)
  for cell in json.loads(nb.read_text())['cells']:
   if cell['cell_type']=='code':assert cell.get('execution_count') is not None and not any(o.get('output_type')=='error' for o in cell.get('outputs',[]))
  replay=json.loads((q/'replay_verification.json').read_text());assert replay['status']=='PASS' and len(replay['tables'])==len(m['files'])
  assert Path(replay['invocation_cwd']).resolve()==Path('/tmp').resolve()
  result['stages'][str(st)].update(results_dir=str(out),qa_dir=str(q),csv_tables=len(m['files']),replay_status=replay['status'],notebook='executed and synced',figures_byte_identical_to_inspected_run=all(replay['figure_byte_comparison'].values()))
  if st==3:
   with duckdb.connect(str(out/'stage3_users.duckdb'),read_only=True) as c:
    assert c.execute('SELECT count(*),count(DISTINCT user_id) FROM user_segments').fetchone()==(501041,501041)
    may=c.execute('SELECT sum(mature_receipt_records),sum(redeemed15_records),sum(mature_receipt_users),sum(redeemed15_users) FROM segment_evaluation').fetchone();assert may==(215348,21580,148439,14860)
    assert c.execute("SELECT count(*) FROM historical_events WHERE event_date>=DATE '2016-05-01'").fetchone()[0]==0
    result['may']={'receipts':may[0],'redeemed15':may[1],'receipt_users':may[2],'redeemed_users':may[3],'universe_unique_users':501041}
 state=json.loads((WORK/'qa/stage_status.json').read_text());assert all(state.get(f'stage{i}')=='PASS' and state['stages'][str(i)]=='PASS' for i in range(4))
 placeholders=[];files=0
 for parent,dirs,names in os.walk(WORK):
  for n in names:
   p=Path(parent)/n
   if p.is_symlink():continue
   files+=1
   if p.stat().st_flags & 0x40000000:placeholders.append(str(p))
 result['local_files']={'files_scanned':files,'sf_dataless':placeholders};assert not placeholders
 versions={}
 for st in (2,3):
  for line in (WORK/f'requirements-stage{st}.lock.txt').read_text().splitlines():
   if '==' not in line or line.startswith('#'):continue
   name,version=line.split('==',1);actual=importlib.metadata.version(name);assert actual==version,(name,actual,version);versions[name]=actual
 result['locked_dependencies_verified']=len(versions)
 result['status']='PASS_WITH_LIMITATION';result['computation_status']='PASS';result['html_rendered_visual_status']='PENDING'
 (QA/'acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
 diffs=[]
 for p in (QA/'before').iterdir():
  target=WORK/('sql' if p.suffix=='.sql' else 'scripts')/p.name
  if target.exists():diffs.extend(difflib.unified_diff(p.read_text().splitlines(True),target.read_text().splitlines(True),fromfile='before/'+p.name,tofile=str(target.relative_to(ROOT))))
 (QA/'changes.patch').write_text(''.join(diffs))
 print(json.dumps({k:result[k] for k in ['status','database','may','stages','local_files','locked_dependencies_verified']},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
