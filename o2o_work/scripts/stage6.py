"""Register or read-only verify the current engineering-repair acceptance receipt.

The original phase6 failed replay remains in qa/stage6 as historical evidence.
Computational success and native GUI acceptance remain separate facts.
"""
from pathlib import Path
from datetime import datetime
import argparse,hashlib,json,os,subprocess
WORK=Path(__file__).resolve().parents[1]
ROOT=WORK.parent
QA=WORK/'qa/repair_20261016'
REPLAY=QA/'replay_project/o2o_work'

def read(p):return json.loads(Path(p).read_text())
def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def need(ok,message):
 if not ok:raise RuntimeError(message)
def dump(p,o):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_name(p.name+'.tmp');tmp.write_text(json.dumps(o,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,p)
def verify_stage5(w):
 p=subprocess.run([str(w/'.venv/bin/python'),'-B',str(w/'scripts/stage5.py'),'--verify'],cwd='/tmp',text=True,capture_output=True)
 need(p.returncode==0,'Stage5 current verification failed: '+p.stdout+p.stderr)
 return json.loads(p.stdout)
def collect():
 baseline=read(QA/'baseline.json')
 for name,info in baseline['protected'].items():
  path=ROOT/name;need(sha(path)==info['sha256'] and path.stat().st_size==info['bytes'],'Protected source changed: '+name)
 comparisons=read(QA/'data_reconciliation.json')
 need(comparisons['status']=='PASS','Business reconciliation failed')
 need({k:v['table_count'] for k,v in comparisons['stages'].items()}=={'stage1':8,'stage2':19,'stage3':14,'stage4':7},'48-table coverage incomplete')
 for i in range(1,5):
  pointer=read(REPLAY/f'qa/stage{i}/latest.json')
  need(comparisons['stages'][f'stage{i}']['isolated_run']==pointer['run_id'],'Comparison uses stale run: stage'+str(i))
 commands=read(QA/'commands.json')
 need(all(any(c['argv'][-1]==f'stage{i}' and c['exit_code']==0 for c in commands) for i in range(6)),'Real stage0-5 entrypoints not all successful')
 for command in commands:
  need(sha(command['log'])==command['log_sha256'],'Command log differs: '+command['log'])
 replay_state=read(REPLAY/'qa/stage_status.json');stage4=read(REPLAY/'qa/stage4/latest.json')
 need(replay_state['stage4']=='PARTIAL' and replay_state['stage5']=='PARTIAL','Unverified rebuilt native must remain PARTIAL')
 need(comparisons['stages']['stage4']['isolated_run']==stage4['run_id'],'Comparison uses stale stage4 run')
 need(stage4['tableau_native_open']=='NOT_RUN','Unexpected regenerated native acceptance')
 m4=read(Path(stage4['results_dir'])/'manifest.json')
 for name,digest in m4['script_hashes'].items():
  need(sha(REPLAY/'scripts'/name)==digest==sha(WORK/'scripts'/name),'Rebuilt code differs: '+name)
 environment=read(QA/'environment_validation.json')
 need(environment['fresh_venv'] and environment['pip_check_exit_code']==0 and environment['install_exit_code']==0,'Fresh dependency environment not proven')
 native=read(QA/'native_attempt.json')
 need(native['status']=='NOT_VERIFIED','Native results require separate actual evidence')
 confirmation_path=QA/'user_native_confirmation.json'
 confirmed=False
 if confirmation_path.is_file():
  confirmation=read(confirmation_path)
  need(confirmation['status']=='USER_CONFIRMED_PASS' and confirmation['source']=='Direct user messages in this conversation','Invalid user confirmation provenance')
  need(Path(confirmation['target_workbook'])==Path(stage4['workbooks']['top_twbx']),'User confirmation workbook differs')
  need(sha(confirmation['target_workbook'])==confirmation['workbook_sha256_at_registration'],'User-confirmed workbook changed')
  confirmed=True
 return {'stage6':'PASS' if confirmed else 'PARTIAL','engineering_repairs':'IMPLEMENTED_AND_TESTED','computational_replay_0_5':'PASS',
         'business_tables_compared':48,'fresh_python_environment':'PASS_ON_THIS_MAC',
         'rebuilt_native_acceptance':'USER_CONFIRMED_PASS' if confirmed else 'NOT_VERIFIED',
         'agent_native_verification':'NOT_VERIFIED','user_independent_reproduction':'NOT_VERIFIED',
         'protected_originals_unchanged':True,'formal_stage5_current_verify':verify_stage5(WORK),
         'isolated_stage5_current_verify':verify_stage5(REPLAY),'publicly_published':False}
def expected_details(result,created):
 return dict(result,reviewed_at=created,evidence_manifest=str(QA/'manifest.json'),report=str(WORK/'docs/工程修复与复现验收.md'))
def verify(manifest):
 need(manifest['result']['stage6'] in ('PASS','PARTIAL'),'Invalid acceptance state')
 for name,digest in manifest['files'].items():need(sha(ROOT/name)==digest,'Evidence hash mismatch: '+name)
 result=collect();need(result==manifest['result'],'Live evidence differs from receipt')
 details=expected_details(result,manifest['created_at']);state=read(WORK/'qa/stage_status.json')
 need(state.get('stage6')==result['stage6'] and state['stages']['6']==result['stage6'],'Stage6 status conflict')
 need(state['stage6_details']==details,'Stage6 details conflict')
 for path in [WORK/'qa/stage6/latest.json',WORK/'results/stage6/latest.json']:need(read(path)==details,'Stage6 pointer conflict')
 return result

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--verify',action='store_true');args=parser.parse_args()
 if args.verify:
  result=verify(read(QA/'manifest.json'));print(json.dumps({'receipt_integrity':'PASS','writes':False,'result':result},ensure_ascii=False,indent=2));return
 result=collect();files=[]
 for name in ['scripts','docs','sql','config']:
  files.extend(p for p in (WORK/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
 files += [WORK/'run.sh',WORK/'README.md',*WORK.glob('requirements*.txt')]
 for name in ['results','data','notebooks','tableau','config','sql','scripts']:
  files.extend(p for p in (REPLAY/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
 for p in QA.iterdir():
  if p.is_file() and p.name not in ('manifest.json',):files.append(p)
 files.extend(p for p in (QA/'before').rglob('*') if p.is_file())
 for stage in (4,5):
  files.append(WORK/f'qa/stage{stage}/latest.json')
  latest=read(WORK/f'qa/stage{stage}/latest.json')
  if stage==5:files.append(Path(latest['results_dir'])/'manifest.json')
 files.append(REPLAY/'qa/stage5/latest.json')
 files.append(REPLAY/'qa/stage4/latest.json')
 manifest={'created_at':datetime.now().astimezone().isoformat(),'result':result,
           'files':{str(p.relative_to(ROOT)):sha(p) for p in files},
           'scope':'48-table computational replay; native acceptance may use explicit hash-bound user attestation; Agent native verification remains separate'}
 dump(QA/'manifest.json',manifest);details=expected_details(result,manifest['created_at'])
 state=read(WORK/'qa/stage_status.json');state.update(stage6=result['stage6'],stage6_details=details,updated_at=manifest['created_at']);state['stages']['6']=result['stage6'];state['manual_learning']='看板重测由用户确认通过；独立SQL、Notebook、全流程重建及面试讲解仍未验证';dump(WORK/'qa/stage_status.json',state)
 dump(WORK/'qa/stage6/latest.json',details);dump(WORK/'results/stage6/latest.json',details)
 verify(manifest);print(json.dumps(details,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
