"""Read frozen JSON only: no GA, database writes or replacement of old results."""
from pathlib import Path
from collections import Counter
import ast, hashlib, json, math
import numpy as np

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
DATA = ROOT / '测试归档/2026-09-24测试/论文真实数据'
sources = [DATA/'02_场景筛选与预算预试验'/n for n in
           ('runs_t2full.json', 'runs_t2rad.json', 'runs_t2r429.json')]
rows = [r for p in sources for r in json.loads(p.read_text(encoding='utf-8'))]
scenes = ['S-429', 'S-387', 'S-409', 'S-469', 'S-487']
selected = [r for r in rows if r.get('scene') in scenes]
assert not any('error' in r for r in selected)
keys = Counter((r['scene'], r['method'], r['seed']) for r in selected)
assert max(keys.values()) == 1, 'Duplicate primary runs: investigate, do not silently overwrite'
assert len(selected) == 300
# Execute only the archived numerical definitions; avoid its SciPy imports and writing main().
script = DATA/'05_统计汇总与作图数据/recompute_paper_statistics.py'
tree = ast.parse(script.read_text(encoding='utf-8'))
names = {'holm', 'hl_estimator', 'paired_bootstrap_hl_ci'}
module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
ns = {'np': np}; exec(compile(module, str(script), 'exec'), ns)
frozen = {r['scene']: r for r in json.loads((script.parent/'论文主比较统一统计复核-2026-09-27.json').read_text(encoding='utf-8'))['results']}
out = []
for scene in scenes:
    methods = {m: {r['seed']: r for r in selected if r['scene'] == scene and r['method'] == m} for m in ('BGA', 'MGA')}
    b, m = methods['BGA'], methods['MGA']
    assert len(b) == len(m) == 30 and set(b) == set(m)
    discord_b = sum(b[s]['strict_success'] and not m[s]['strict_success'] for s in b)
    discord_m = sum(m[s]['strict_success'] and not b[s]['strict_success'] for s in b)
    n = discord_b + discord_m
    p = min(1., 2*sum(math.comb(n,k) for k in range(min(discord_b,discord_m)+1))/2**n) if n else 1.
    rec = {'scene': scene, 'seeds': sorted(b), 'b_c': [discord_b, discord_m], 'mcnemar_raw': p, 'methods': {}}
    for name, runs in methods.items():
        costs = [float(r['best_strict_cost']) for r in runs.values() if r['strict_success']]
        qs = np.quantile(costs,[0,.25,.5,.75,1]).tolist() if costs else None
        rec['methods'][name] = {'success': len(costs), 'all_success_cost_min_q1_median_q3_max': qs,
            'stop_reason_counts': dict(Counter(r['stop_reason'] for r in runs.values())),
            'median_evolution_calls': float(np.median([r['evolution_evaluations'] for r in runs.values()])),
            'median_wall_seconds': float(np.median([r['wall_seconds'] for r in runs.values()]))}
    assert [rec['methods'][x]['success'] for x in ('BGA','MGA')] == [frozen[scene]['bga_success'], frozen[scene]['mga_success']]
    pairs = [(float(b[s]['best_strict_cost']),float(m[s]['best_strict_cost'])) for s in sorted(b) if b[s]['strict_success'] and m[s]['strict_success']]
    rec['common_success_n'] = len(pairs)
    if len(pairs) >= 6:
        a = np.array(pairs); diff = a[:,1]-a[:,0]
        calc = 100*np.array(ns['paired_bootstrap_hl_ci'](diff,seed=20260927+int(scene.split('-')[1])))/np.median(a[:,0])
        expect = [frozen[scene][k] for k in ('hl_pct','hl_ci_lo_pct','hl_ci_hi_pct')]
        assert np.allclose(calc,expect,rtol=0,atol=1e-10)
        rec['HL_CI_verified_pct'] = calc.tolist()
    out.append(rec)
for rec, p in zip(out, ns['holm']([r['mcnemar_raw'] for r in out])): rec['mcnemar_holm'] = p
traces = [Path(r['trace']) for r in selected]
result = {'primary_records':len(selected),'duplicate_primary_keys':0,'primary_error_records':0,
    'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
    'trace_status':dict(Counter('missing' if not p.exists() else 'zero_bytes' if p.stat().st_size==0 else 'nonempty' for p in traces)),
    'numpy_version':np.__version__, 'scope':'Success counts, exact McNemar/Holm and 20000-resample HL reproduced; Wilcoxon not rerun (SciPy unavailable).',
    'results':out}
(BASE/'证据复核.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
