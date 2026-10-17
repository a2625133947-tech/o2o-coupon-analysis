"""Acceptance boundaries and portable aliases, using isolated fixtures only."""
from pathlib import Path
from unittest.mock import patch
import json,sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stage5

class RebuildTests(unittest.TestCase):
 def test_legacy_five_document_archive_survives_current_cleanup(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);work=root/'o2o_work';out=work/'results/stage5/runs/legacy';out.mkdir(parents=True)
   documents={}
   for name in stage5.LEGACY_DOCS:
    p=out/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('frozen '+name)
    documents[name]=stage5.digest(p)
   (out/'findings_evidence.json').write_text('{}')
   manifest={'stage5':'PASS','run_id':'legacy','documents':{'document_hashes':documents},
             'snapshot_files':{str(p.relative_to(work)):stage5.digest(p) for p in out.rglob('*') if p.is_file()}}
   with patch.multiple(stage5,ROOT=root,WORK=work):
    self.assertTrue(stage5.verify_snapshot(manifest,archive=True))
    with self.assertRaisesRegex(RuntimeError,'Incomplete document snapshot'):
     stage5.verify_snapshot(manifest)
    (out/'docs/项目经历草稿.md').unlink()
    with self.assertRaisesRegex(RuntimeError,'Missing snapshot file'):
     stage5.verify_snapshot(manifest,archive=True)
 def test_only_locked_input_can_resolve_outside_project(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td)/'project';root.mkdir();external=Path(td)/'raw.csv';external.write_text('raw')
   alias=root/'raw.csv';alias.symlink_to(external)
   with patch.object(stage5,'ROOT',root):
    p,name=stage5.project_path(alias,allow_external=True)
    self.assertEqual(name,'raw.csv');self.assertEqual(p,alias)
    with self.assertRaisesRegex(RuntimeError,'symlink escape'):stage5.project_path(alias)
    with self.assertRaisesRegex(RuntimeError,'outside project'):stage5.project_path(root/'..'/'raw.csv',allow_external=True)
 def test_explicit_archives_preserve_old_hash_and_reject_escape(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);work=root/'o2o_work';old=work/'qa/before/scripts/generator.py';old.parent.mkdir(parents=True);old.write_text('accepted version')
   current=work/'scripts/generator.py';current.parent.mkdir();current.write_text('new version')
   delivery={'files':{'scripts/generator.py':stage5.digest(old)}}
   with patch.multiple(stage5,ROOT=root,WORK=work):
    with self.assertRaisesRegex(RuntimeError,'hash mismatch'):stage5.delivery_sources(delivery,{})
    bound=stage5.delivery_sources(delivery,{'scripts/generator.py':'qa/before/scripts/generator.py'})
    self.assertEqual(bound,{'qa/before/scripts/generator.py':stage5.digest(old)})
    with self.assertRaisesRegex(RuntimeError,'Invalid archive'):stage5.delivery_sources(delivery,{'scripts/generator.py':'../outside.py'})
    old.write_text('tampered')
    with self.assertRaisesRegex(RuntimeError,'hash mismatch'):stage5.delivery_sources(delivery,{'scripts/generator.py':'qa/before/scripts/generator.py'})
    with self.assertRaisesRegex(RuntimeError,'Only generator'):stage5.delivery_sources({'files':{'tableau/final.twbx':'h'}},{'tableau/final.twbx':'qa/before/final.twbx'})
 def test_prepared_registration_stays_partial(self):
  state={f'stage{i}':'PASS' for i in range(4)};state.update(stage4='PARTIAL',stages={'4':'PARTIAL'})
  latest={'stage4':'PARTIAL','data_preparation':'PASS','hyper_extract_generation':'PASS','workbook_xsd_validation':'PASS','tableau_native_open':'NOT_RUN'}
  self.assertEqual(stage5.stage4_contract(state,latest),'PARTIAL')
  latest['final_acceptance']='PASS'
  with self.assertRaisesRegex(RuntimeError,'conflicts'):stage5.stage4_contract(state,latest)
  latest.update(stage4='PASS',final_acceptance='NOT_RUN');state.update(stage4='PASS',stages={'4':'PASS'})
  with self.assertRaisesRegex(RuntimeError,'without final'):stage5.stage4_contract(state,latest)
 def test_archive_checks_frozen_files_not_current_code_or_state(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);work=root/'o2o_work';out=work/'results/stage5/runs/r1';out.mkdir(parents=True)
   for name in ['README.md','findings_evidence.json']:(out/name).write_text('frozen')
   manifest={'stage5':'PASS','run_id':'r1','snapshot_files':{str(p.relative_to(work)):stage5.digest(p) for p in out.iterdir()},'script_sha256':'historical-code-hash','upstream':{'files':{'missing-source':'old-hash'}},'documents':{}}
   with patch.multiple(stage5,ROOT=root,WORK=work,DOCS=['README.md']):
    self.assertTrue(stage5.verify_snapshot(manifest,archive=True))
    with self.assertRaisesRegex(RuntimeError,'Missing snapshot'):stage5.verify_snapshot(manifest)
    (out/'README.md').write_text('tampered')
    with self.assertRaisesRegex(RuntimeError,'Hash mismatch'):stage5.verify_snapshot(manifest,archive=True)
if __name__=='__main__':unittest.main(verbosity=2)
