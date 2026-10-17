"""Evidence must reject differences; publication failure must preserve accepted bytes."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import duckdb
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from verification import compare_database_tables,publish_database,invalidate_downstream,require_stage

class VerificationTests(unittest.TestCase):
 def test_replay_multiset_and_tolerance_and_failure(self):
  with tempfile.TemporaryDirectory() as d:
   paths=[Path(d)/x for x in ['a.duckdb','b.duckdb']]
   for p in paths:
    with duckdb.connect(str(p)) as c:
     c.execute('CREATE TABLE t(id VARCHAR,rate DOUBLE)');c.execute("INSERT INTO t VALUES ('01',.2),('01',.2),('02',NULL)")
   self.assertEqual(compare_database_tables(*paths,['t'])['t']['comparison'],'exact_multiset')
   with duckdb.connect(str(paths[1])) as c:c.execute('UPDATE t SET rate=rate+1e-12')
   self.assertEqual(compare_database_tables(*paths,['t'])['t']['comparison'],'float_tolerance')
   with duckdb.connect(str(paths[1])) as c:c.execute('UPDATE t SET rate=.3')
   with self.assertRaisesRegex(RuntimeError,'Replay values differ'):compare_database_tables(*paths,['t'])
   with duckdb.connect(str(paths[1])) as c:c.execute("DELETE FROM t WHERE id='02'")
   with self.assertRaisesRegex(RuntimeError,'Replay count differs'):compare_database_tables(*paths,['t'])
 def test_publish_metadata_failure_rolls_back(self):
  for existing in (True,False):
   with self.subTest(existing=existing),tempfile.TemporaryDirectory() as d:
    build=Path(d)/'new.duckdb';final=Path(d)/'final.duckdb';pointer=Path(d)/'latest.json';build.write_bytes(b'new');pointer.write_bytes(b'old pointer')
    if existing:final.write_bytes(b'old db')
    def fail():pointer.write_bytes(b'new pointer');raise OSError('injected latest/state failure')
    with self.assertRaises(OSError):publish_database(build,final,fail,[pointer])
    self.assertEqual(final.read_bytes() if final.exists() else None,b'old db' if existing else None)
    self.assertEqual(pointer.read_bytes(),b'old pointer')
    self.assertEqual(list(Path(d).glob('*.previous.duckdb')),[])
 def test_stage_conflict_and_running_invalidation(self):
  state={'stage0':'PASS','stage1':'PASS','stage2':'RUNNING','stages':{'0':'PASS','1':'PASS','2':'RUNNING','3':'PASS'}}
  invalidate_downstream(state,0)
  for i in (1,2,3):
   self.assertEqual(state['stages'][str(i)],'STALE')
   with self.assertRaises(RuntimeError):require_stage(state,i)
  with self.assertRaises(RuntimeError):require_stage({'stage1':'FAIL','stages':{'1':'PASS'}},1)
 def test_empty_rate_plot_is_missing(self):
  # Execute the exact helper embedded by the notebook author, not a mirrored calculation.
  import ast,stage3_notebook,math
  tree=ast.parse(Path(stage3_notebook.__file__).read_text())
  setup=next(n for n in ast.walk(tree) if isinstance(n,ast.JoinedStr) and any(isinstance(x,ast.Constant) and isinstance(x.value,str) and 'def number_or_nan' in x.value for x in n.values))
  text=''.join(v.value if isinstance(v,ast.Constant) and isinstance(v.value,str) else repr('fixture') for v in setup.values)
  helper=next(n for n in ast.parse(text).body if isinstance(n,ast.FunctionDef) and n.name=='number_or_nan')
  ns={};exec(compile(ast.Module(body=[helper],type_ignores=[]),'notebook_helper','exec'),ns)
  self.assertTrue(math.isnan(ns['number_or_nan']('')));self.assertTrue(math.isnan(ns['number_or_nan'](None)));self.assertEqual(ns['number_or_nan']('0'),0)
 def test_empty_segment_actual_plot_cells(self):
  import ast,math,csv
  import matplotlib
  matplotlib.use('Agg')
  import matplotlib.pyplot as plt
  from matplotlib.ticker import PercentFormatter
  import stage3_notebook
  # Use the published aggregate fixture, independent of local execution receipts.
  out=Path(stage3_notebook.WORK)/'results/stage3/runs/20261015T154103504261'
  tree=ast.parse(Path(stage3_notebook.__file__).read_text())
  cells=[n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and (n.value.startswith('ci={(r') or n.value.startswith("data={r['segment_id']:r for r in load('segment_user_evaluation')}"))]
  self.assertEqual(len(cells),2)
  order=['unknown_history','high_receipt_zero_redemption','no_observed_consumption','recent_high_frequency','recent_low_frequency','not_recent_consumption']
  empty='high_receipt_zero_redemption'
  def load(name):
   with (out/(name+'.csv')).open() as f:values=list(csv.DictReader(f))
   for r in values:
    if r['segment_id']==empty:
     for k in ['point_estimate','ci95_low','ci95_high','record_redemption_rate15','mean_within_user_redemption_rate15']:
      if k in r:r[k]=''
   return values
  captured=[]
  def finish(fig,name):
   ax=fig.axes[0];self.assertTrue(any('N/A' in t.get_text() for t in ax.texts))
   # Both series' bars must remain missing at this segment, never 0%.
   self.assertTrue(math.isnan(ax.patches[1].get_width()));self.assertTrue(math.isnan(ax.patches[7].get_width()))
   with tempfile.TemporaryDirectory() as td:fig.savefig(Path(td)/(name+'.png'))
   captured.append(name);plt.close(fig)
  ns={'load':load,'plt':plt,'PercentFormatter':PercentFormatter,'ORDER':order,'SHORT':order,'y':list(range(6)),'BLUE':'blue','GOLD':'orange','number_or_nan':lambda v:float('nan') if v in ('',None) else float(v),'style':lambda ax:None,'table':lambda *args:None,'finish':finish}
  for cell in cells:exec(cell,ns)
  self.assertEqual(len(captured),2)
if __name__=='__main__':unittest.main(verbosity=2)
