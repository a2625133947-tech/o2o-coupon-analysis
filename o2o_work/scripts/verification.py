"""Measured replay/integrity evidence; visual acceptance requires a separate review."""
from pathlib import Path
import hashlib,json,math,os
import duckdb


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def invalidate_downstream(state, stage):
    stages=state.setdefault('stages',{})
    for i in range(stage+1,4):
        if stages.get(str(i)) in ('PASS','RUNNING') or state.get(f'stage{i}') in ('PASS','RUNNING'):
            stages[str(i)]='STALE'
            state[f'stage{i}']='STALE'


def require_stage(state,stage):
    values=[state.get(f'stage{stage}'),state.get('stages',{}).get(str(stage))]
    present=[v for v in values if v is not None]
    if not present or any(v!='PASS' for v in present):
        raise RuntimeError(f'上游阶段{stage}未通过或状态冲突: {values}')


def presentation_checks(out,qa,expected):
    from PIL import Image
    from bs4 import BeautifulSoup
    out=Path(out);figures=sorted((out/'figures').glob('*.png'))
    if len(figures)!=expected:raise RuntimeError(f'Expected {expected} figures, got {len(figures)}')
    for p in figures:
        with Image.open(p) as im:im.verify()
    html=next(out.glob('*预览.html'));soup=BeautifulSoup(html.read_text(),'html.parser')
    if soup.find_all('script') or len(soup.find_all('img'))!=expected:
        raise RuntimeError('Preview scripts/image count failed')
    result={'status':'PENDING','automated_checks':'PASS','scope':'PNG decode, figure count, embedded HTML images and no scripts; no rendered visual inspection',
            'html_sha256':digest(html),'figure_sha256':{p.name:digest(p) for p in figures}}
    (Path(qa)/'visual_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def compare_database_tables(current,previous,tables,tolerance=1e-10):
    """Compare multisets, preserving duplicates. Float-only differences have bounded tolerance."""
    results={}
    with duckdb.connect(':memory:') as c:
        for alias,path in [('cur',current),('prev',previous)]:
            c.execute("ATTACH '"+str(path).replace("'","''")+f"' AS {alias} (READ_ONLY)")
        for t in tables:
            ident='"'+t.replace('"','""')+'"'
            schemas=[c.execute(f'DESCRIBE {a}.{ident}').fetchall() for a in ('cur','prev')]
            if schemas[0]!=schemas[1]:raise RuntimeError(f'Replay schema differs: {t}')
            counts=[c.execute(f'SELECT count(*) FROM {a}.{ident}').fetchone()[0] for a in ('cur','prev')]
            if counts[0]!=counts[1]:raise RuntimeError(f'Replay count differs: {t}: {counts}')
            delta=c.execute(f'SELECT count(*) FROM ((SELECT * FROM cur.{ident} EXCEPT ALL SELECT * FROM prev.{ident}) UNION ALL (SELECT * FROM prev.{ident} EXCEPT ALL SELECT * FROM cur.{ident}))').fetchone()[0]
            mode='exact_multiset'
            if delta:
                cols=schemas[0];order=[i+1 for i,x in enumerate(cols) if x[1] not in ('DOUBLE','FLOAT','REAL')]+[i+1 for i,x in enumerate(cols) if x[1] in ('DOUBLE','FLOAT','REAL')]
                ordering=','.join(map(str,order))
                # Two independent cursors keep memory bounded for user-level tables.
                a=c.cursor();b=c.cursor()
                a.execute(f'SELECT * FROM cur.{ident} ORDER BY {ordering}')
                b.execute(f'SELECT * FROM prev.{ident} ORDER BY {ordering}')
                while True:
                    aa=a.fetchmany(10000);bb=b.fetchmany(10000)
                    if not aa:break
                    for ra,rb in zip(aa,bb):
                        for va,vb in zip(ra,rb):
                            if va==vb:continue
                            if isinstance(va,float) and isinstance(vb,float) and math.isfinite(va) and math.isfinite(vb) and math.isclose(va,vb,rel_tol=0,abs_tol=tolerance):continue
                            raise RuntimeError(f'Replay values differ: {t}: {ra!r} != {rb!r}')
                a.close();b.close();mode='float_tolerance'
            results[t]={'rows':counts[0],'status':'PASS','comparison':mode}
    return results


def replay(out,previous,tables,database_name,work):
    out=Path(out);work=Path(work);manifest=json.loads((out/'manifest.json').read_text())
    for name,spec in manifest['files'].items():
        if digest(out/(name+'.csv'))!=spec['sha256']:raise RuntimeError(f'CSV hash differs: {name}')
    hashes=manifest.get('script_hashes',{})|manifest.get('companion_script_hashes',{})
    if 'stage2_script_sha256' in manifest:hashes['stage2.py']=manifest['stage2_script_sha256']
    for name,value in hashes.items():
        if digest(work/'scripts'/name)!=value:raise RuntimeError(f'Script hash differs: {name}')
    for name,value in manifest['sql_hashes'].items():
        if digest(work/'sql'/name)!=value:raise RuntimeError(f'SQL hash differs: {name}')
    nb=json.loads(next(out.glob('*.ipynb')).read_text())
    cells=[ce for ce in nb['cells'] if ce['cell_type']=='code']
    if any(ce.get('execution_count') is None or any(o.get('output_type')=='error' for o in ce.get('outputs',[])) for ce in cells):
        raise RuntimeError('Notebook has unexecuted/error cells')
    # This connection has no source attachment: output tables must be standalone.
    with duckdb.connect(str(out/database_name),read_only=True) as c:
        counts={t:c.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in tables}
    result={'status':'NOT_COMPARED','current_run':out.name,'previous_run':None,'csv_hashes_verified':len(manifest['files']),
            'script_hashes_verified':len(hashes),'sql_hashes_verified':len(manifest['sql_hashes']),
            'notebook_executed_no_errors':True,'standalone_read_only_counts':counts,'floating_absolute_tolerance':1e-10,
            'invocation_cwd':str(Path.cwd()),'comparison_method':'multiset including duplicates; bounded float tolerance; no row-order assumption'}
    if previous:
        prev=Path(previous)
        if not prev.is_dir() or not (prev/database_name).is_file():raise RuntimeError(f'Previous replay artifacts missing: {prev}')
        result['tables']=compare_database_tables(out/database_name,prev/database_name,tables)
        result['previous_run']=prev.name;result['status']='PASS'
        result['figure_byte_comparison']={p.name:((prev/'figures'/p.name).is_file() and digest(p)==digest(prev/'figures'/p.name)) for p in (out/'figures').glob('*.png')}
    return result


def publish_database(build,final,publish,metadata_paths):
    """Rollback database and pointer bytes on a handled publication failure."""
    build=Path(build);final=Path(final);backup=build.with_suffix('.previous.duckdb')
    old={Path(p):Path(p).read_bytes() if Path(p).exists() else None for p in metadata_paths}
    had_final=final.exists()
    if had_final:os.link(final,backup)
    try:
        os.replace(build,final)
        publish()
    except Exception:
        if had_final:os.replace(backup,final)
        else:final.unlink(missing_ok=True)
        for p,value in old.items():
            if value is None:p.unlink(missing_ok=True)
            else:p.write_bytes(value)
        raise
    finally:
        backup.unlink(missing_ok=True)
