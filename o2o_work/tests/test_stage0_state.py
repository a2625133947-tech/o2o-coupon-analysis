"""Run the real stage0 entrypoint in an isolated project, including valid config changes."""
from pathlib import Path
import json,shutil,subprocess,sys,tempfile,unittest
WORK=Path(__file__).resolve().parents[1]

class Stage0StateTests(unittest.TestCase):
 @unittest.skipUnless((WORK.parent/'o2o/ccf_offline_stage1_train.csv').is_file(), 'requires separately obtained original CSV inputs')
 def test_real_entrypoint_invalidates_old_descendants(self):
  with tempfile.TemporaryDirectory(prefix='o2o_stage0_test_') as td:
   root=Path(td);w=root/'o2o_work'
   for name in ('scripts','config','docs','sql'):
    shutil.copytree(WORK/name,w/name)
   (root/'outputs').symlink_to(WORK.parent/'outputs',target_is_directory=True)
   (root/'o2o').symlink_to(WORK.parent/'o2o',target_is_directory=True)
   (w/'qa').mkdir();(w/'data').mkdir()
   state={'stage0':'PASS','stage1':'PASS','stage2':'PASS','stage3':'PASS','stages':{str(i):'PASS' for i in range(4)},'error':'old error'}
   (w/'qa/stage_status.json').write_text(json.dumps(state))
   config=json.loads((w/'config/analysis.json').read_text());config.update(segmentation_asof='2016-05-02',history_end_exclusive='2016-05-02',evaluation_receipt_start='2016-05-02');(w/'config/analysis.json').write_text(json.dumps(config))
   r=subprocess.run([sys.executable,'-B',str(w/'scripts/stage0.py')],cwd=td,capture_output=True,text=True)
   self.assertEqual(r.returncode,0,r.stderr)
   after=json.loads((w/'qa/stage_status.json').read_text());self.assertNotIn('error',after)
   self.assertEqual(after['stages'],{'0':'PASS','1':'STALE','2':'STALE','3':'STALE'})
   (w/'docs/数据字典.md').unlink()
   r=subprocess.run([sys.executable,'-B',str(w/'scripts/stage0.py')],cwd=td,capture_output=True,text=True)
   self.assertEqual(r.returncode,1)
   after=json.loads((w/'qa/stage_status.json').read_text());self.assertEqual(after['checks']['dictionaries_and_time_config'],'FAIL');self.assertEqual(after['stages']['0'],'FAIL')
if __name__=='__main__':unittest.main(verbosity=2)
