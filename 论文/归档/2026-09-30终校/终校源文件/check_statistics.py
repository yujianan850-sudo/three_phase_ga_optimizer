from pathlib import Path
import importlib.util,json,numpy as np
# NumPy 1.20 names the same linear quantile option 'interpolation'.
import inspect
if 'method' not in inspect.signature(np.quantile).parameters:
    original_quantile=np.quantile
    def compatible_quantile(a,q,method='linear',**kwargs):return original_quantile(a,q,interpolation=method,**kwargs)
    np.quantile=compatible_quantile
repo=next(p for p in Path(__file__).resolve().parents if (p/'ga').is_dir() and (p/'论文').is_dir())
p=repo/'测试归档/2026-09-24测试/论文真实数据/05_统计汇总与作图数据/recompute_paper_statistics.py'
spec=importlib.util.spec_from_file_location('stats_audit',p);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
rows=mod.load_runs();frozen=json.loads((p.parent/'论文主比较统一统计复核-2026-09-27.json').read_text(encoding='utf-8'))['results']
checks=[]
for rec in frozen:
    scene=rec['scene'];b={r['seed']:r for r in rows if r['scene']==scene and r['method']=='BGA'};m={r['seed']:r for r in rows if r['scene']==scene and r['method']=='MGA'}
    seeds=sorted(set(b)&set(m));pairs=[(float(b[s]['best_strict_cost']),float(m[s]['best_strict_cost'])) for s in seeds if b[s]['strict_success'] and m[s]['strict_success']]
    assert sum(b[s]['strict_success'] for s in seeds)==rec['bga_success']
    assert sum(m[s]['strict_success'] for s in seeds)==rec['mga_success']
    if len(pairs)>=6:
        arr=np.array(pairs);diff=arr[:,1]-arr[:,0];est,lo,hi=mod.paired_bootstrap_hl_ci(diff,seed=20260927+int(scene.split('-')[1]));calc=100*np.array([est,lo,hi])/np.median(arr[:,0]);expect=np.array([rec[k] for k in ['hl_pct','hl_ci_lo_pct','hl_ci_hi_pct']]);assert np.allclose(calc,expect,rtol=0,atol=1e-10)
        checks.append({'scene':scene,'n':len(pairs),'HL_CI':calc.tolist()})
print(json.dumps({'primary_runs':sum(r['n_seeds']*2 for r in frozen),'success_counts_verified':True,'bootstrap_HL_verified':checks},ensure_ascii=False))
