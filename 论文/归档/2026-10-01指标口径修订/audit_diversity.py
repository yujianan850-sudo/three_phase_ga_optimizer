"""Audit retained main-run and diversity JSON. Never recreate missing SQLite files."""
from pathlib import Path
from collections import Counter
from hashlib import sha256
from statistics import median
import json

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'ga').is_dir())
DATA = ROOT/'测试归档/2026-09-24测试/论文真实数据'
OUT = Path(__file__).resolve().parent
SCENES = dict(zip(['S-429','S-387','S-409','S-469','S-487'], 'ABCDE'))

def load(path):
    return json.loads(path.read_text(encoding='utf-8'))

def main():
    sources = []
    runs = []
    for name in ['runs_t2full.json','runs_t2rad.json','runs_t2r429.json']:
        p = DATA/'02_场景筛选与预算预试验'/name
        sources.append(p)
        runs.extend(r for r in load(p) if r['scene'] in SCENES)
    assert len(runs) == 300
    assert len({(r['scene'], r['method'], r['seed']) for r in runs}) == 300
    assert set(Counter((r['scene'],r['method']) for r in runs).values()) == {30}
    retained = sum(Path(r['trace']).exists() and Path(r['trace']).stat().st_size > 0 for r in runs)
    diversity = []
    for name in ['diversity_failure_summary.json','diversity_failure_runs_t2rad_runs_t2r429.json']:
        p = DATA/'03_主比较_BGA_MGA'/name
        sources.append(p)
        diversity.extend(load(p)['diversity'])
    panels = []
    for scene, label in SCENES.items():
        for method in ['BGA','MGA']:
            r = next(r for r in diversity if r['scene']==scene and r['method']==method and 'final_distance' in r)
            s = next(r for r in diversity if r['scene']==scene and r['method']==method and 'signature_median' in r)
            assert 0 <= r['final_distance'] <= 5
            assert r['curve'][-1]['median'] == r['final_distance']
            assert 0 <= s['signature_median'] <= 24
            panels.append(dict(scene=label, scene_id=scene, method=method,
                distance_median=r['final_distance'], common_last_generation=r['curve'][-1]['generation'],
                signature_median=s['signature_median'], signature_min=s['signature_min'],
                signature_max=s['signature_max']))
    main_statistics=[]
    for scene,label in SCENES.items():
        b={r['seed']:r for r in runs if r['scene']==scene and r['method']=='BGA'}
        m={r['seed']:r for r in runs if r['scene']==scene and r['method']=='MGA'}
        assert set(b)==set(m)
        common=[s for s in b if b[s]['strict_success'] and m[s]['strict_success']]
        main_statistics.append(dict(scene=label,bga_success=sum(r['strict_success'] for r in b.values()),
            mga_success=sum(r['strict_success'] for r in m.values()),common_success=len(common),
            bga_common_cost_median=median(float(b[s]['best_strict_cost']) for s in common),
            mga_common_cost_median=median(float(m[s]['best_strict_cost']) for s in common)))
    report=dict(main_run_count=300, available_original_sqlite=retained,
        raw_trace_recalculation_performed=False, algorithm_rerun_performed=False,
        metric='Unnormalized Hamming distance across five stored fields',
        fields=['steel_brand','core_data_id','low_voltage_wire_id','high_voltage_wire_id','cooling_option'],
        metric_range=[0,5],
        candidate_scope='Per-generation recorded candidates with calculable and complete true; fixed-step sample up to 120',
        endpoint='Last generation with a distance statistic in all 30 runs of this scene/method; not each run final population',
        signature_scope='Last generation_population stage strict archive in historical extractor; not final refinement snapshot',
        panels=panels,main_statistics=main_statistics,
        sources=[dict(path=str(p.relative_to(ROOT)),sha256=sha256(p.read_bytes()).hexdigest()) for p in sources])
    (OUT/'指标口径复核.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
