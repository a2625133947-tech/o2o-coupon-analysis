"""Meaningful isolated regressions for document/package acceptance, no production writes."""
from pathlib import Path
from unittest.mock import patch
import copy,json,sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stage5

class Stage5Tests(unittest.TestCase):
 def test_snapshot_missing_or_modified_and_failed_current_state_rejected(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);w=root/'o2o_work';q=w/'qa/stage5';out=w/'results/stage5/runs/r1';out.mkdir(parents=True);q.mkdir(parents=True)
   doc=w/'README.md';doc.write_text('reviewed document');ledger=q/'findings_evidence.json';ledger.write_text('{}')
   snap=out/'README.md';snap.write_bytes(doc.read_bytes());(out/'findings_evidence.json').write_bytes(ledger.read_bytes())
   source=root/'source.csv';source.write_text('verified source')
   state={f'stage{i}':'PASS' for i in range(6)};state.update(stages={str(i):'PASS' for i in range(6)},stage5_details={'run_id':'r1'})
   (w/'qa/stage_status.json').write_text(json.dumps(state))
   pointer={'stage5':'PASS','run_id':'r1','results_dir':str(out)}
   for path in [q/'latest.json',w/'results/stage5/latest.json']:path.write_text(json.dumps(pointer))
   manifest={'stage5':'PASS','run_id':'r1','upstream':{'files':{'source.csv':stage5.digest(source)}},'documents':{'document_hashes':{'README.md':stage5.digest(doc)},'ledger_sha256':stage5.digest(ledger)},'snapshot_files':{str(p.relative_to(w)):stage5.digest(p) for p in out.iterdir()},'script_sha256':stage5.digest(stage5.__file__)}
   # check_hashes' default base is bound when defined; explicit patch routes it to fixture WORK.
   real_check=stage5.check_hashes
   with patch.multiple(stage5,WORK=w,ROOT=root,QA=q,DOCS=['README.md']),patch.object(stage5,'check_hashes',side_effect=lambda files,base=None:real_check(files,w if base is None else base)):
    self.assertTrue(stage5.verify_snapshot(manifest,accepted=True))
    snap.write_text('tampered snapshot')
    with self.assertRaisesRegex(RuntimeError,'Hash mismatch'):stage5.verify_snapshot(manifest,accepted=True)
    snap.unlink()
    with self.assertRaisesRegex(RuntimeError,'Missing snapshot file'):stage5.verify_snapshot(manifest,accepted=True)
    snap.write_bytes(doc.read_bytes());state['stage5']='FAIL';(w/'qa/stage_status.json').write_text(json.dumps(state))
    with self.assertRaises(RuntimeError):stage5.verify_snapshot(manifest,accepted=True)
    state['stage5']='PASS';state['stage5_details']['run_id']='wrong';(w/'qa/stage_status.json').write_text(json.dumps(state))
    with self.assertRaisesRegex(RuntimeError,'state run differs'):stage5.verify_snapshot(manifest,accepted=True)
    state['stage5_details']['run_id']='r1';(w/'qa/stage_status.json').write_text(json.dumps(state));pointer['run_id']='wrong';(q/'latest.json').write_text(json.dumps(pointer))
    with self.assertRaisesRegex(RuntimeError,'pointer differs'):stage5.verify_snapshot(manifest,accepted=True)
 def test_link_target_is_checked(self):
  with tempfile.TemporaryDirectory() as td:
   doc=Path(td)/'README.md';doc.write_text('[missing](does_not_exist.csv)')
   with self.assertRaisesRegex(RuntimeError,'Broken link'):stage5.doc_links(doc)
if __name__=='__main__':unittest.main(verbosity=2)
