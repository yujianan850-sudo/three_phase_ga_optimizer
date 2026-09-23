"""Python 3.10 CLI：先做数据核验，再进入可追溯优化。"""

from __future__ import annotations

import argparse
import csv
import json
import uuid
from decimal import Decimal
from pathlib import Path

from calculation.database import FaladiRepository, load_database_settings
from calculation.record_adapter import resolve_record_candidate
from calculation.similar_history import select_similar_history_seeds
from calculation.single_device_evaluator import ThreePhaseSingleDeviceEvaluator
from ga.gene_codec import GeneDomain, load_optimizer_defaults
from ga.full_optimizer import (
    EXPERIMENT_STRATEGIES, GASettings, apply_experiment_strategy,
    automatic_evolution_population_size, run_optimization,
)
from ga.runtime_provenance import runtime_provenance
from ga.optimizer_runtime import build_mixed_initial_population, trace_initial_population


def _run_defaults(args: argparse.Namespace) -> tuple[float, float]:
    defaults = load_optimizer_defaults()
    history_ratio = defaults["history_seed_ratio"] if args.history_ratio is None else args.history_ratio
    random_injection_ratio = defaults["random_injection_ratio"] if getattr(args, "random_injection_ratio", None) is None else args.random_injection_ratio
    return float(history_ratio), float(random_injection_ratio)


def _ga_settings_from_args(
    args: argparse.Namespace, *, population_size: int, history_ratio: float,
    random_injection_ratio: float, strategy: str | None = None,
) -> GASettings:
    """把 CLI 参数与消融策略统一还原为完整、可写入轨迹的 GASettings。"""
    values: dict[str, object] = {
        "population_size": population_size,
        "generations": args.generations,
        "crossover_rate": args.crossover_rate,
        "coupled_electromagnetic_crossover_rate": args.coupled_electromagnetic_crossover_rate,
        "mutation_rate": args.mutation_rate,
        "guided_mutation_rate": args.guided_mutation_rate,
        "history_ratio": history_ratio,
        "random_injection_ratio": random_injection_ratio,
        "evaluation_budget": args.evaluation_budget,
        "evolution_evaluation_budget": getattr(args, "evolution_evaluation_budget", None),
        "archive_size": args.archive_size,
        "elite_count": args.elite_count,
        "local_probe_limit": args.local_probe_limit,
        "local_refine_elites": args.local_refine_elites,
        "local_refine_limit": args.local_refine_limit,
        "stagnation_limit": args.stagnation_limit,
    }
    # 命名策略与页面共用 full_optimizer 的唯一映射，避免 CLI / 页面出现同名不同义。
    if strategy is None:
        values["injection_mode"] = args.injection_mode
        values["guided_mutation_mode"] = args.guided_mutation_mode
    return apply_experiment_strategy(GASettings(**values), strategy)  # type: ignore[arg-type]


def _history_seed_inputs(
    repo: FaladiRepository, config: dict, catalog: dict, domain: GeneDomain, records: list[dict],
) -> tuple[list[tuple], int]:
    """合并当前配置与相似配置的历史结构；所有候选仍会按当前配置精算。"""
    current: list[tuple] = []
    skipped = 0
    for record in records:
        try:
            candidate = domain.normalize_candidate_wire_types(resolve_record_candidate(record, catalog, config))
            current.append((candidate, "history_seed:same_config"))
        except (KeyError, LookupError, ValueError):
            skipped += 1
    configs_by_id, records_by_config = repo.all_page_context()
    similar = select_similar_history_seeds(config, configs_by_id, records_by_config, catalog, domain)
    current.extend((
        seed.candidate,
        f"history_seed:similar_config={seed.source_config_id};record={seed.source_record_id};score={seed.config_similarity}",
        {
            "selection_priority": str(seed.config_similarity),
            "similarity_score": str(seed.config_similarity),
            "source_config_id": seed.source_config_id,
            "source_record_id": seed.source_record_id,
            "source_compliance_level": seed.source_compliance_level,
            "source_price": None if seed.source_price is None else str(seed.source_price),
            "similarity_feature_scores": {name: str(score) for name, score in seed.feature_scores},
        },
    ) for seed in similar)
    return current, skipped


def command_catalog(_: argparse.Namespace) -> None:
    repo = FaladiRepository(load_database_settings())
    catalog = repo.catalog()
    print(json.dumps({name: len(rows) for name, rows in catalog.items()}, ensure_ascii=False, indent=2))


def command_check_core(args: argparse.Namespace) -> None:
    repo = FaladiRepository(load_database_settings())
    record = repo.scheme_record(args.record_id)
    config = repo.scheme_config(int(record["scheme_config_id"]))
    catalog = repo.catalog()
    candidate = resolve_record_candidate(record, catalog, config)
    evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
    result = evaluator.evaluate(candidate)
    java_data = record["scheme_data"]
    print(f"record_id={record['id']} config_id={record['scheme_config_id']} formula_revision={evaluator.formula_revision}")
    # 这些是当前已迁入且在历史 scheme_data 中可直接逐字段比对的公式输出。
    for name in ("ct", "crgokw", "lvrh", "hvrh", "crgoa", "lvydd", "hvydd", "crgog", "crgopo", "hvlvpk", "ukk"):
        java_value = java_data.get(name)
        python_value = result.metrics.get(name)
        delta = None if java_value is None or python_value is None else Decimal(str(python_value)) - Decimal(str(java_value))
        print(f"{name}: java={java_value} python={python_value} delta={delta}")
    print("complete=", result.complete, "calculable=", result.calculable)
    for note in result.diagnostic:
        print("-", note)


def command_initial_preview(args: argparse.Namespace) -> None:
    """生成并记录第一代；该命令用于验证编码、种子和日志，不宣称优化结果。"""
    repo = FaladiRepository(load_database_settings())
    config = repo.scheme_config(args.config_id)
    catalog = repo.catalog()
    history = repo.active_records(args.config_id)
    history_ratio, _ = _run_defaults(args)
    domain = GeneDomain.from_catalog(config, catalog)
    history_candidates, skipped = _history_seed_inputs(repo, config, catalog, domain, history)
    population = build_mixed_initial_population(
        domain, history_candidates, None, history_ratio, args.seed,
    )
    evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
    # 文件名带 run_id：每次运行写独立文件，避免重跑追加污染同一轨迹。
    run_id = uuid.uuid4().hex
    output = Path(__file__).resolve().parent / "outputs" / f"initial-preview-{args.config_id}-{args.seed}-{run_id}.sqlite3"
    _, cache = trace_initial_population(
        output, args.config_id, args.seed, population, evaluator.evaluate,
        domain.summary(), run_id=run_id,
    )
    print(json.dumps({
        "run_id": run_id, "trace": str(output), "population": len(population.candidates),
        "history_seed": population.history_count, "random_seed": population.random_count,
        "coverage_seed": population.coverage_count,
        "estimated_legal_candidate_count": population.estimated_legal_candidate_count,
        "automatic_target_size": population.automatic_target_size,
        "coverage_target_ratio": population.coverage_target_ratio,
        "unmappable_history": skipped, "formula_exact_evaluations": cache.exact_evaluations,
        "cache_hits": cache.cache_hits, "formula_chain_complete": True,
    }, ensure_ascii=False, indent=2))


def command_ga_run(args: argparse.Namespace) -> None:
    """运行完整双档案离散 GA；仅写入 outputs 下的本地 SQLite 轨迹。"""
    repo = FaladiRepository(load_database_settings())
    config = repo.scheme_config(args.config_id)
    catalog = repo.catalog()
    history = repo.active_records(args.config_id)
    history_ratio, random_injection_ratio = _run_defaults(args)
    domain = GeneDomain.from_catalog(config, catalog)
    history_candidates, _ = _history_seed_inputs(repo, config, catalog, domain, history)
    population = build_mixed_initial_population(domain, history_candidates, None, history_ratio, args.seed)
    settings = _ga_settings_from_args(
        args, population_size=automatic_evolution_population_size(len(population.candidates)),
        history_ratio=history_ratio, random_injection_ratio=random_injection_ratio,
        strategy=args.experiment_strategy,
    )
    # 文件名带 run_id：每次运行写独立文件，避免重跑追加污染同一轨迹。
    run_id = uuid.uuid4().hex
    output = Path(__file__).resolve().parent / "outputs" / f"ga-run-{args.config_id}-{args.seed}-{run_id}.sqlite3"
    evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
    summary = run_optimization(
        output, args.config_id, args.seed, domain, population,
        evaluator.evaluate, settings,
        run_id=run_id,
        formula_revision=evaluator.formula_revision,
        run_metadata={"execution_entry": "cli_ga_run", "experiment_strategy": args.experiment_strategy or "custom"},
    )
    print(json.dumps({
        "run_id": summary.run_id, "trace": str(summary.trace_path),
        "generations_completed": summary.generations_completed,
        "stopped_by_stagnation": summary.stopped_by_stagnation,
        "termination_reason": summary.termination_reason,
        "budget_exhausted": summary.budget_exhausted,
        "exact_evaluations": summary.exact_evaluations,
        "initial_evaluations": summary.initial_evaluations,
        "evolution_evaluations": summary.evolution_evaluations,
        "elapsed_seconds": summary.elapsed_seconds,
        "cache_hits": summary.cache_hits,
        "strict_archive": len(summary.strict_archive), "near_archive": len(summary.near_archive),
        "best_strict_cost": None if summary.best_strict is None else str(summary.best_strict.result.metrics.get("price")),
        "best_near_violation": None if summary.best_near is None else str(summary.best_near.result.total_violation),
    }, ensure_ascii=False, indent=2))


def _parse_seed_list(raw: str) -> list[int]:
    """解析逗号分隔种子并拒绝重复，保证策略之间的配对关系可追溯。"""
    try:
        seeds = [int(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ValueError("--seeds 必须是逗号分隔的整数，例如 20260918,20260919") from exc
    if not seeds:
        raise ValueError("--seeds 至少需要一个随机种子")
    if len(set(seeds)) != len(seeds):
        raise ValueError("--seeds 不允许重复；同一策略只能对每个种子运行一次")
    return seeds


def _parse_strategy_list(raw: str) -> list[str]:
    valid = {"baseline", "coupled", "empirical_injection", "guided_mutation"}
    strategies = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = set(strategies) - valid
    if unknown:
        raise ValueError(f"--strategies 包含未知策略: {', '.join(sorted(unknown))}")
    if not strategies:
        raise ValueError("--strategies 至少需要一个策略")
    if len(set(strategies)) != len(strategies):
        raise ValueError("--strategies 不允许重复")
    return strategies


def command_ga_ablation(args: argparse.Namespace) -> None:
    """按同一配置、预算和种子顺序运行可配对的策略消融批次。

    此入口不解释“谁更好”，只产生可复盘的独立 SQLite 与机器可读汇总；统计
    显著性和论文结论必须由后续对比步骤基于完整配对结果完成。
    """
    if args.evaluation_budget is None and args.evolution_evaluation_budget is None:
        raise ValueError("ga-ablation 必须至少指定 --evaluation-budget 或 --evolution-evaluation-budget 之一")
    seeds = _parse_seed_list(args.seeds)
    strategies = _parse_strategy_list(args.strategies)
    repo = FaladiRepository(load_database_settings())
    config = repo.scheme_config(args.config_id)
    catalog = repo.catalog()
    history = repo.active_records(args.config_id)
    history_ratio, random_injection_ratio = _run_defaults(args)
    domain = GeneDomain.from_catalog(config, catalog)
    history_candidates, skipped = _history_seed_inputs(repo, config, catalog, domain, history)
    evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
    output_dir = Path(__file__).resolve().parent / "outputs"
    batch_name = args.batch_name or f"ablation-{args.config_id}-{min(seeds)}-{max(seeds)}"
    if any(char in batch_name for char in '\\/:*?\"<>|'):
        raise ValueError("--batch-name 不能包含文件名非法字符")

    rows: list[dict[str, object]] = []
    for strategy in strategies:
        for seed in seeds:
            # 每个策略/种子都重新构造第 0 代，确保随机序列从相同入口开始，
            # 但不会跨策略共享缓存或档案，避免互相污染。
            population = build_mixed_initial_population(domain, history_candidates, None, history_ratio, seed)
            settings = _ga_settings_from_args(
                args, population_size=automatic_evolution_population_size(len(population.candidates)),
                history_ratio=history_ratio, random_injection_ratio=random_injection_ratio,
                strategy=strategy,
            )
            # 文件名带 run_id：每个策略/种子每次运行写独立文件，重跑不追加污染。
            run_id = uuid.uuid4().hex
            output = output_dir / f"{batch_name}-{strategy}-seed{seed}-{run_id}.sqlite3"
            summary = run_optimization(
                output, args.config_id, seed, domain, population, evaluator.evaluate, settings,
                run_id=run_id,
                formula_revision=evaluator.formula_revision,
                run_metadata={"execution_entry": "cli_ga_ablation", "experiment_strategy": strategy},
            )
            rows.append({
                "batch_name": batch_name,
                "strategy": strategy,
                "seed": seed,
                "trace": str(output),
                "strict_feasible": bool(summary.strict_archive),
                "best_strict_cost": None if summary.best_strict is None else str(summary.best_strict.result.metrics.get("price")),
                "best_near_violation": None if summary.best_near is None else str(summary.best_near.result.total_violation),
                "exact_evaluations": summary.exact_evaluations,
                "initial_evaluations": summary.initial_evaluations,
                "evolution_evaluations": summary.evolution_evaluations,
                "elapsed_seconds": summary.elapsed_seconds,
                "cache_hits": summary.cache_hits,
                "generations_completed": summary.generations_completed,
                "termination_reason": summary.termination_reason,
                "budget_exhausted": summary.budget_exhausted,
            })

    manifest = {
        "batch_name": batch_name,
        "config_id": args.config_id,
        "seeds": seeds,
        "strategies": strategies,
        "evaluation_budget": args.evaluation_budget,
        "evolution_evaluation_budget": getattr(args, "evolution_evaluation_budget", None),
        "history_ratio": history_ratio,
        "random_injection_ratio": random_injection_ratio,
        "domain": domain.summary(),
        "unmappable_history": skipped,
        "provenance": runtime_provenance(formula_revision=evaluator.formula_revision, entrypoint="cli_ga_ablation"),
        "rows": rows,
    }
    manifest_path = output_dir / f"{batch_name}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    csv_path = output_dir / f"{batch_name}-summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]) if rows else ["batch_name"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "batch_name": batch_name, "manifest": str(manifest_path), "summary_csv": str(csv_path),
        "run_count": len(rows), "strategies": strategies, "seeds": seeds,
        "evaluation_budget": args.evaluation_budget,
        "evolution_evaluation_budget": args.evolution_evaluation_budget,
    }, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="三相变压器遗传优化研究原型（Python 3.10）")
    sub = parser.add_subparsers(required=True)
    catalog = sub.add_parser("catalog", help="读取本地 faladi 基础目录并输出数量")
    catalog.set_defaults(handler=command_catalog)
    check = sub.add_parser("check-core", help="对一个 Java 历史方案核验当前已迁入的精算字段（命令名保留兼容）")
    check.add_argument("--record-id", type=int, required=True)
    check.set_defaults(handler=command_check_core)
    preview = sub.add_parser("initial-preview", help="生成并追踪混合初始种群；仅用于首段公式和日志验证")
    preview.add_argument("--config-id", type=int, required=True)
    preview.add_argument("--seed", type=int, default=20260903)
    preview.add_argument("--history-ratio", type=float, default=None, help="覆盖 optimizer.yml 的历史种子比例")
    preview.set_defaults(handler=command_initial_preview)
    ga_run = sub.add_parser("ga-run", help="运行完整双档案离散 GA，并保存每代与每个个体的本地轨迹")
    ga_run.add_argument("--config-id", type=int, required=True)
    ga_run.add_argument("--generations", type=int, default=20)
    ga_run.add_argument("--seed", type=int, default=20260907)
    ga_run.add_argument("--history-ratio", type=float, default=None, help="覆盖 optimizer.yml 的历史种子比例")
    ga_run.add_argument("--random-injection-ratio", type=float, default=None, help="覆盖 optimizer.yml 的每代随机注入比例")
    ga_run.add_argument("--crossover-rate", type=float, default=0.85)
    ga_run.add_argument("--coupled-electromagnetic-crossover-rate", type=float, default=0.75,
                        help="发生交叉时，结构电磁耦合块整体继承同一父代的比例")
    ga_run.add_argument("--mutation-rate", type=float, default=0.45)
    ga_run.add_argument("--guided-mutation-rate", type=float, default=0.65)
    ga_run.add_argument("--injection-mode", choices=("empirical_preferred", "raw_only"), default="empirical_preferred",
                        help="经验有效域优先，或强制从原始页面搜索域注入")
    ga_run.add_argument("--guided-mutation-mode", choices=("single_primary", "random_only"), default="single_primary",
                        help="单主导约束局部探测，或强制全部普通随机变异")
    ga_run.add_argument("--experiment-strategy", choices=EXPERIMENT_STRATEGIES, default=None,
                        help="按与页面/ga-ablation 相同的命名策略覆盖三项算子开关；不等同于历史旧版 GA")
    ga_run.add_argument("--evaluation-budget", type=int, default=None,
                        help="真实精算调用的硬上限；达到后立即停止本次运行")
    ga_run.add_argument("--evolution-evaluation-budget", type=int, default=None,
                        help="初代覆盖之后的演化阶段精算调用硬上限；默认不限制")
    ga_run.add_argument("--archive-size", type=int, default=24)
    ga_run.add_argument("--elite-count", type=int, default=2)
    ga_run.add_argument("--local-probe-limit", type=int, default=3)
    ga_run.add_argument("--local-refine-elites", type=int, default=3)
    ga_run.add_argument("--local-refine-limit", type=int, default=12)
    ga_run.add_argument("--stagnation-limit", type=int, default=8)
    ga_run.set_defaults(handler=command_ga_run)

    ablation = sub.add_parser("ga-ablation", help="按同一配置、种子和真实精算预算顺序运行策略消融批次")
    ablation.add_argument("--config-id", type=int, required=True)
    ablation.add_argument("--seeds", required=True, help="逗号分隔的配对随机种子，例如 20260918,20260919")
    ablation.add_argument("--evaluation-budget", type=int, default=None,
                          help="每个策略/种子的真实精算调用硬上限 B（总预算）；与 --evolution-evaluation-budget 至少填一个")
    ablation.add_argument("--evolution-evaluation-budget", type=int, default=None,
                          help="初代覆盖之后的演化阶段精算调用硬上限；与 --evaluation-budget 至少填一个，推荐正式消融只填此项")
    ablation.add_argument("--strategies", default="baseline,coupled,empirical_injection,guided_mutation",
                           help="逗号分隔：baseline,coupled,empirical_injection,guided_mutation")
    ablation.add_argument("--batch-name", default=None, help="输出文件前缀；默认由配置 ID 与种子范围生成")
    ablation.add_argument("--generations", type=int, default=20)
    ablation.add_argument("--history-ratio", type=float, default=None, help="覆盖 optimizer.yml 的历史种子比例")
    ablation.add_argument("--random-injection-ratio", type=float, default=None, help="覆盖 optimizer.yml 的每代随机注入比例")
    ablation.add_argument("--crossover-rate", type=float, default=0.85)
    ablation.add_argument("--coupled-electromagnetic-crossover-rate", type=float, default=0.75)
    ablation.add_argument("--mutation-rate", type=float, default=0.45)
    ablation.add_argument("--guided-mutation-rate", type=float, default=0.65)
    # 消融的注入/变异来源完全由 --strategies 决定，不暴露易被覆盖的 CLI 手动值。
    ablation.add_argument("--archive-size", type=int, default=24)
    ablation.add_argument("--elite-count", type=int, default=2)
    ablation.add_argument("--local-probe-limit", type=int, default=3)
    ablation.add_argument("--local-refine-elites", type=int, default=3)
    ablation.add_argument("--local-refine-limit", type=int, default=12)
    ablation.add_argument("--stagnation-limit", type=int, default=8)
    ablation.set_defaults(handler=command_ga_ablation)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.handler(args)
