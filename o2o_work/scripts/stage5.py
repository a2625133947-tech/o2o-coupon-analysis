"""Register an evidence-backed stage5 document package; --verify is read-only.

No upstream stage entrypoint or Tableau process is invoked. The native dashboard
acceptance is inherited from the hash-bound final stage4 evidence, not re-proven.
"""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import argparse,csv,hashlib,json,math,os,re,sys,traceback,urllib.parse,zipfile
import xml.etree.ElementTree as ET
import duckdb
from verification import require_stage
WORK=Path(__file__).resolve().parents[1]
ROOT=WORK.parent
DOCS=['README.md','docs/结论与建议.md','docs/复现与交付说明.md']
LEGACY_DOCS=[*DOCS,'docs/项目经历草稿.md','docs/阶段5文档验收说明.md']
QA=WORK/'qa/stage5'

def digest(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def read(p):return json.loads(Path(p).read_text())
def dump(p,o):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(o,ensure_ascii=False,indent=2,default=str)+'\n');os.replace(tmp,p)
def need(ok,message):
 if not ok:raise RuntimeError(message)
def check_hashes(files,base=None):
 base=WORK if base is None else base
 for name,value in files.items():
  p=base/name;need(p.is_file(),'Missing snapshot file: '+str(p));need(digest(p)==value,'Hash mismatch: '+str(p))
def project_path(path, allow_external=False):
 """Keep lexical aliases; reject traversal and unapproved symlink escapes.

 Locked inputs alone may target outside ROOT. Their lexical path and resolved
 origin are both recorded, and byte/hash checks still bind the actual content.
 """
 p=Path(os.path.abspath(path));root=Path(os.path.abspath(ROOT))
 try:name=str(p.relative_to(root))
 except ValueError:raise RuntimeError('Source outside project: '+str(p))
 need(allow_external or p.resolve().is_relative_to(root.resolve()),'Unapproved source symlink escape: '+str(p))
 return p,name

def delivery_sources(delivery, archive):
 """Bind original hashes to explicit archived source aliases, never hide drift.

 Workbook/evidence entries remain at their original paths. Only generator
 sources may use an archive; current generators are bound separately upstream.
 """
 need(isinstance(archive,dict),'source_archive must be an explicit map')
 need(set(archive).issubset(delivery['files']),'source_archive key absent from delivery manifest')
 bound={}
 for name,value in delivery['files'].items():
  need(not Path(name).is_absolute() and '..' not in Path(name).parts,'Invalid delivery relative path: '+name)
  mapped=archive.get(name,name)
  if name in archive:
   need(Path(name).parts[0]=='scripts','Only generator sources may use source_archive: '+name)
   need(not Path(mapped).is_absolute() and '..' not in Path(mapped).parts,'Invalid archive relative path: '+str(mapped))
  path=WORK/mapped;project_path(path)
  need(path.is_file(),'Missing delivery source: '+str(path))
  need(digest(path)==value,'Delivery hash mismatch: '+str(path));bound[mapped]=value
 return bound

def stage4_contract(state,latest):
 for st in range(4):require_stage(state,st)
 value=latest.get('stage4');need(value in ('PASS','PARTIAL'),'Stage4 not prepared')
 for actual in [state.get('stage4'),state.get('stages',{}).get('4')]:
  need(actual==value,'Stage4 state/pointer conflict')
 for field in ['data_preparation','hyper_extract_generation','workbook_xsd_validation']:
  need(latest.get(field)=='PASS','Stage4 prerequisite missing: '+field)
 review=latest.get('final_review',{})
 native=(latest.get('final_acceptance')=='PASS' or (review.get('stage4')=='PASS' and review.get('native_runtime')=='PASS' and bool(review.get('workbook_sha256'))))
 if value=='PASS':need(native,'Stage4 PASS without final native acceptance')
 else:need(not native,'Stage4 PARTIAL conflicts with final PASS')
 return 'PASS' if native else 'PARTIAL'

def doc_links(doc):
 text=doc.read_text();resolved=[]
 for value in re.findall(r'!?\[[^\]\n]*\]\(([^\n]+?)\)',text):
  value=value.strip().strip('<>')
  if value.startswith(('https://','http://','#')):continue
  value=urllib.parse.unquote(value.split('#',1)[0])
  target=(doc.parent/value).resolve();need(target.exists(),f'Broken link in {doc.name}: {value}');resolved.append(str(target))
 return resolved

def upstream():
 state=read(WORK/'qa/stage_status.json')
 latest={i:read(WORK/f'qa/stage{i}/latest.json') for i in range(1,5)}
 status=stage4_contract(state,latest[4])
 db=WORK/'data/o2o.duckdb';dbh=digest(db)
 p2=Path(latest[2]['results_dir']);p3=Path(latest[3]['results_dir'])
 m2=read(p2/'manifest.json');m3=read(p3/'manifest.json')
 need(m2['stage1_database_sha256']==dbh==m3['source_database_sha256']==latest[4]['source_database_sha256'],'Upstream DB fingerprint mismatch')
 need(m2['stage1_run_id']==m3['source_stage1_run']==latest[4]['source_stage1_run']==latest[1]['run_id'],'Source stage1 run mismatch')
 need(latest[4]['stage2_run']==latest[2]['run_id'] and latest[4]['stage3_run']==latest[3]['run_id'],'Stage4 inputs stale')
 need(latest[4].get('publicly_published') is False,'Stage4 publication differs')
 qa4=Path(latest[4]['qa_dir']);project_path(qa4)
 delivery_path=Path(latest[4].get('delivery_manifest',qa4/'delivery_manifest.json'))
 package=Path(latest[4].get('accepted_workbook_path') or latest[4]['workbooks'].get('top_twbx',latest[4]['workbooks'].get('run_twbx','')))
 project_path(package)
 if status=='PASS' and latest[4].get('final_acceptance')!='PASS':
  need(latest[4]['final_review'].get('workbook_sha256')==digest(package),'Legacy native review package hash differs')
 if delivery_path.is_file():
  delivery=read(delivery_path)
  need(str(package.relative_to(WORK)) in delivery['files'],'Package absent from delivery manifest')
  if status=='PASS':need(delivery.get('workbook_sha256')==digest(package),'Native acceptance package hash differs')
  delivery={'files':delivery_sources(delivery,latest[4].get('source_archive',{}))}
 else:
  # New data-only runs are registered against their generated manifest. Native
  # status can only be inherited from an explicit hash-bound acceptance record.
  out4=Path(latest[4]['results_dir']);project_path(out4)
  manifest4=read(out4/'manifest.json');bound={}
  for name,spec in manifest4['files'].items():
   p=out4/'data'/name;project_path(p);need(digest(p)==spec['sha256'],'Stage4 output mismatch: '+name);bound[str(p.relative_to(WORK))]=spec['sha256']
  for name,value in manifest4['script_hashes'].items():
   p=WORK/'scripts'/name;project_path(p);need(digest(p)==value,'Stage4 generator version differs: '+name);bound[str(p.relative_to(WORK))]=value
  bound[str((out4/'manifest.json').relative_to(WORK))]=digest(out4/'manifest.json')
  bound[str(package.relative_to(WORK))]=digest(package)
  delivery={'files':bound}
  if status=='PASS':
   evidence=Path(latest[4]['tableau_runtime']['evidence_path']);project_path(evidence)
   need(digest(package)==latest[4]['native_workbook_sha256'],'Native acceptance package hash differs')
   need(digest(evidence)==latest[4]['native_evidence_sha256'],'Native acceptance evidence changed')
   delivery['files'][str(evidence.relative_to(WORK))]=digest(evidence)
   native=read(evidence)
   need(native.get('workbook_sha256')==digest(package),'Native evidence binds another package')
   for name in ['native_open','three_dashboards','representative_filters','clear_reset','save_close_reopen','visual_inspection']:
    need(native.get('checks',{}).get(name)=='PASS','Native acceptance incomplete: '+name)
   need(bool(native.get('evidence_files')),'Native evidence files missing')
   for entry in native['evidence_files']:
    p=Path(entry['path']);p=p if p.is_absolute() else evidence.parent/p
    project_path(p);need(digest(p)==entry['sha256'],'Native evidence file changed: '+str(p));delivery['files'][str(p.relative_to(WORK))]=entry['sha256']
 with zipfile.ZipFile(package) as z:
  need(z.testzip() is None,'TWBX CRC failure');names=set(z.namelist());twb=ET.fromstring(z.read(next(n for n in names if n.endswith('.twb'))))
  actual=[len(twb.findall(x)) for x in ['./worksheets/worksheet','./dashboards/dashboard','./actions/edit-parameter-action']]
  need(actual==[24,3,6],f'Workbook structure differs: {actual}')
  for conn in twb.findall('.//connection'):
   if conn.get('class')=='hyper':need(conn.get('dbname') in names,'Missing packaged Hyper '+str(conn.get('dbname')))
 files={};external_sources={}
 def add(p,allow_external=False):
  p,name=project_path(p,allow_external);files[name]=digest(p)
  if not p.resolve().is_relative_to(ROOT.resolve()):external_sources[name]={'resolved_path':str(p.resolve()),'authorization':'input_lock.json','sha256':files[name]}
 for i in range(1,5):add(WORK/f'qa/stage{i}/latest.json')
 add(db)
 for name in ['qa/environment.json','qa/input_manifest.json','run.sh','requirements.txt','requirements-stage3.lock.txt','requirements-stage4.lock.txt']:
  add(WORK/name)
 for directory,manifest in [(p2,m2),(p3,m3)]:
  add(directory/'manifest.json')
  for name,spec in manifest['files'].items():
   p=directory/(name+'.csv');need(digest(p)==spec['sha256'],'Source CSV mismatch: '+str(p));add(p)
  add(directory/('stage2_metrics.duckdb' if directory==p2 else 'stage3_users.duckdb'))
 for name in delivery['files']:add(WORK/name)
 if delivery_path.is_file():add(delivery_path)
 for p in qa4.iterdir():
  if p.is_file():add(p)
 for sp in read(WORK/'config/input_lock.json')['files']:
  p,_=project_path(ROOT/sp['path'],allow_external=True);need(digest(p)==sp['sha256'] and p.stat().st_size==sp['bytes'],'Raw input version mismatch: '+sp['path']);add(p,allow_external=True)
 for folder in ['config','sql']:
  for p in (WORK/folder).iterdir():
   if p.is_file():add(p)
 for name in ['stage0.py','stage1.py','stage2.py','stage3.py','stage4.py','stage4_workbook.py','stage4_docs.py','stage4_tableau_runtime.py','stage4_dependencies.py','prepare_replay_documents.py','stage6.py','verification.py']:add(WORK/'scripts'/name)
 for p in (WORK/'tableau/data').iterdir():
  if p.is_file():add(p)
 for name in ['filter_expected.csv','worksheet_specs.json']:add(WORK/'tableau'/name)
 for name in ['数据字典.md','指标字典.md','输入清单.md','环境说明.md','阶段4看板配置与复现.md']:add(WORK/'docs'/name)
 with duckdb.connect(str(db),read_only=True) as c:
  n=c.execute('SELECT count(*),sum(redeemed15_mature::BIGINT),count(DISTINCT user_id),count(DISTINCT user_id) FILTER(WHERE redeemed15_mature) FROM fact_coupon_receipts WHERE mature15').fetchone()
  need(n==(1053282,64395,510698,40075),'Overall baseline mismatch')
  raw=c.execute('SELECT count(*) FROM raw_offline').fetchone()[0];need(raw==1754884,'Raw count mismatch')
  d=c.execute('SELECT count(*),sum(redeemed15_mature::BIGINT) FROM fact_coupon_receipts_dedup WHERE mature15').fetchone();need(d==(1015389,62153),'Dedup mismatch')
  lag=c.execute('SELECT count(*) FROM fact_coupon_receipts WHERE mature15 AND redeemed15_mature AND lag_days<=7').fetchone()[0];need(lag==47274,'Day7 mismatch')
  source_run=c.execute('SELECT run_id FROM pipeline_metadata').fetchone()[0];need(source_run==latest[1]['run_id'],'DB run differs')
 with duckdb.connect(str(p3/'stage3_users.duckdb'),read_only=True) as c:
  may=c.execute('SELECT sum(mature_receipt_records),sum(redeemed15_records),sum(mature_receipt_users),sum(redeemed15_users) FROM segment_evaluation').fetchone();need(may==(215348,21580,148439,14860),'May baseline mismatch')
  unique=c.execute('SELECT count(*),count(DISTINCT user_id) FROM user_segments').fetchone();need(unique==(501041,501041),'Segment grain differs')
 metrics={'source_rows':raw,'receipt_records':n[0],'redeemed15_records':n[1],'receipt_users':n[2],'redeemed15_users':n[3],'rate15':n[1]/n[0],'dedup_receipts':d[0],'dedup_redeemed':d[1],'day7_cumulative':lag,'may_receipts':may[0],'may_redeemed':may[1],'may_users':may[2],'may_redeemed_users':may[3],'unique_segment_users':unique[0]}
 return {'source_runs':{str(i):latest[i]['run_id'] for i in latest},'upstream_states':{str(i):state.get(f'stage{i}') for i in range(5)},'registration_status':status,'source_archive':latest[4].get('source_archive',{}),'external_sources':external_sources,'files':files,'metrics':metrics,'tableau':{'worksheets':24,'dashboards':3,'actions':6,'scenario_count':23,'sha256':digest(package),'publicly_published':False,'native_acceptance':('inherited from final hash-bound stage4 evidence; no GUI rerun in stage5' if status=='PASS' else 'NOT VERIFIED: data/workbook preparation only; stage5 registration PARTIAL')}}

def document_checks():
 links={name:doc_links(WORK/name) for name in DOCS}
 ledger=read(QA/'findings_evidence.json');claims=ledger.get('claims',[])
 need(len(claims)>=4,'Not enough supported findings')
 ids=[c['id'] for c in claims];need(len(ids)==len(set(ids)),'Duplicate claim IDs')
 text=(WORK/'docs/结论与建议.md').read_text()
 sources=[]
 for claim in claims:
  need(claim['id'] in text,'Claim missing from report: '+claim['id'])
  need(claim.get('observations') and claim.get('limitations') and claim.get('sources'),'Incomplete claim: '+claim['id'])
  for source in claim['sources']:
   path=Path(source['path']);path=path if path.is_absolute() else WORK/path
   need(path.is_file(),'Missing claim source '+str(path));sources.append(str(path))
 readme=(WORK/'README.md').read_text()
 need(all(term in readme for term in ['Agent','本人','未公开发布']),'Experience truthfulness statements missing')
 need('23' in readme and '24' in readme and '6项' in readme,'README Tableau inventory stale')
 return {'claims':ids,'claim_source_files':sorted(set(sources)),'local_links':links,'document_hashes':{name:digest(WORK/name) for name in DOCS},'ledger_sha256':digest(QA/'findings_evidence.json')}

def verify_snapshot(manifest,accepted=False,archive=False):
 status=manifest['stage5'];need(status in ('PASS','PARTIAL'),'Snapshot not accepted')
 snapshot_docs=DOCS
 if archive:
  snapshot_docs=list(manifest.get('documents',{}).get('document_hashes',dict.fromkeys(DOCS)))
  need(set(snapshot_docs) in (set(DOCS),set(LEGACY_DOCS)),'Unknown archived document inventory')
 expected_snapshot={f"results/stage5/runs/{manifest['run_id']}/{name}" for name in [*snapshot_docs,'findings_evidence.json']}
 need(set(manifest['snapshot_files'])==expected_snapshot,'Incomplete document snapshot inventory')
 check_hashes(manifest['snapshot_files'])
 # Archive mode proves frozen document integrity only. It deliberately makes no
 # assertion about current source files, current acceptance or current code.
 if archive:return True
 check_hashes(manifest['upstream']['files'],ROOT)
 check_hashes(manifest['documents']['document_hashes'])
 need(digest(QA/'findings_evidence.json')==manifest['documents']['ledger_sha256'],'Evidence ledger changed')
 need(digest(Path(__file__))==manifest['script_sha256'],'Stage5 validator changed; use --verify-archive for historical documents only')
 state=read(WORK/'qa/stage_status.json')
 for st in range(4):require_stage(state,st)
 expected=manifest['upstream'].get('upstream_states',{}).get('4','PASS')
 need(state.get('stage4')==expected,'Stage4 current state differs')
 if accepted:
  need(state.get('stage5')==status and state.get('stages',{}).get('5')==status,'Stage5 current state differs')
  need(state['stage5_details']['run_id']==manifest['run_id'],'Stage5 state run differs')
  for path in [QA/'latest.json',WORK/'results/stage5/latest.json']:
   pointer=read(path)
   need(pointer.get('stage5')==status and pointer['run_id']==manifest['run_id'],'Stage5 latest pointer differs')
   need(Path(pointer['results_dir']).resolve()==(WORK/'results/stage5/runs'/manifest['run_id']).resolve(),'Stage5 pointer directory differs')
 for name in DOCS:doc_links(WORK/name)
 return True

def main():
 p=argparse.ArgumentParser();modes=p.add_mutually_exclusive_group();modes.add_argument('--verify',action='store_true');modes.add_argument('--verify-archive',metavar='RUN_ID',help='read-only frozen document integrity; does not validate current sources or native acceptance');args=p.parse_args()
 if args.verify or args.verify_archive:
  if args.verify_archive:
   need(Path(args.verify_archive).name==args.verify_archive and args.verify_archive not in ('.','..'),'Invalid archive run ID');pointer={'run_id':args.verify_archive,'results_dir':str(WORK/'results/stage5/runs'/args.verify_archive)}
  else:pointer=read(QA/'latest.json')
  manifest=read(Path(pointer['results_dir'])/'manifest.json');verify_snapshot(manifest,accepted=not args.verify_archive,archive=bool(args.verify_archive))
  print(json.dumps({'stage5':manifest['stage5'],'verification_scope':('frozen_documents_only; current_sources_not_verified' if args.verify_archive else 'current_sources_and_documents'),'mode':'read_only_verify','run_id':pointer['run_id'],'source_files_recorded':len(manifest['upstream']['files']),'current_source_files_verified':(0 if args.verify_archive else len(manifest['upstream']['files'])),'documents':len(manifest['documents']['document_hashes']),'claims':manifest['documents']['claims'],'writes':False},ensure_ascii=False,indent=2));return
 runid=datetime.now(ZoneInfo('Asia/Singapore')).strftime('%Y%m%dT%H%M%S%f');out=WORK/'results/stage5/runs'/runid;qa=QA/'runs'/runid
 out.mkdir(parents=True);qa.mkdir(parents=True)
 try:
  up=upstream();docs=document_checks()
  for source in docs['claim_source_files']:
   path,name=project_path(source);up['files'][name]=digest(path)
  manifest={'stage5':up['registration_status'],'run_id':runid,'created_at':datetime.now(ZoneInfo('Asia/Singapore')).isoformat(),'upstream':up,'documents':docs,'script_sha256':digest(Path(__file__)),'scope':'documentation and read-only data/provenance reconciliation; no new Tableau GUI or marketing experiment','personal_independent_reproduction':'NOT VERIFIED','stage6':'NOT IMPLEMENTED'}
  # Immutable document and ledger copies make the handoff reviewable after later edits.
  for name in DOCS:
   target=out/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((WORK/name).read_bytes())
  (out/'findings_evidence.json').write_bytes((QA/'findings_evidence.json').read_bytes())
  manifest['snapshot_files']={str(p.relative_to(WORK)):digest(p) for p in out.rglob('*') if p.is_file()}
  dump(out/'manifest.json',manifest);verify_snapshot(manifest)
  details={'stage5':manifest['stage5'],'run_id':runid,'results_dir':str(out),'qa_dir':str(qa),'created_at':manifest['created_at'],'documents':DOCS,'claims':docs['claims'],'source_files_verified':len(up['files']),'upstream_0_4_unchanged':True,'scope':manifest['scope'],'personal_independent_reproduction':'NOT VERIFIED','publicly_published':False}
  dump(qa/'checks.json',{'upstream':up,'documents':docs,'snapshot_verified':True});dump(qa/'status.json',details)
  state=read(WORK/'qa/stage_status.json');state['stage5']=manifest['stage5'];state['stages']['5']=manifest['stage5'];state['stage5_details']=details;state['updated_at']=manifest['created_at'];dump(WORK/'qa/stage_status.json',state)
  dump(QA/'latest.json',details);dump(WORK/'results/stage5/latest.json',details)
  print(json.dumps(details,ensure_ascii=False,indent=2))
 except Exception as exc:
  fail={'stage5':'FAIL','run_id':runid,'error':str(exc)};dump(qa/'status.json',fail)
  state=read(WORK/'qa/stage_status.json');state['stage5']='FAIL';state['stages']['5']='FAIL';state['stage5_details']=fail;dump(WORK/'qa/stage_status.json',state)
  raise
if __name__=='__main__':
 try:main()
 except Exception:traceback.print_exc();sys.exit(1)
