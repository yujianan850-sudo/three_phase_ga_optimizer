"""GA 运行前的种群构造、缓存和追踪基础设施。

此模块故意不在精算器未完整前启动遗传迭代。它先把“初始种群如何构造、每个个体
来自哪里、产生过哪些动作”落实为可审计数据结构，避免日后补日志导致无法复现实验。
"""

from __future__ import annotations

import os
import json
import math
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from calculation.models import EvaluationResult, ThreePhaseDesignCandidate
from ga.gene_codec import GeneDomain
from ga.runtime_provenance import runtime_provenance
from ga.trace_store import TraceStore


@dataclass(frozen=True)
class InitialPopulation:
    candidates: list[ThreePhaseDesignCandidate]
    history_count: int
    random_count: int
    coverage_count: int = 0
    sources: tuple[str, ...] = ()
    # 这些值是根据本次真实搜索域推导出的初始化预算，写入轨迹后可解释“为什么
    # 本次不是固定 24/100 个候选”。它们不是业务输入，也不是人工可调下限。
    estimated_legal_candidate_count: int = 0
    automatic_target_size: int = 0
    coverage_target_ratio: float = 0.0
    metadata: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class InitialPopulationBudget:
    """第 0 代自动预算：先保住 A/B 覆盖，再按域大小补历史和随机探索。"""

    estimated_legal_candidate_count: int
    coverage_count: int
    coverage_target_ratio: float
    target_size: int


def estimate_legal_candidate_count(domain: GeneDomain) -> int:
    """计算当前域可构造的完整合法候选数，绝不枚举或精算这些候选。

    低压层数依赖“线规类别 × 低压匝数”，长圆 conversionC 又依赖具体铁芯，
    因此不能把所有字段长度简单相乘。
    """
    cooling_count = max(1, len(domain.cooling_options))
    winding_count = sum(
        len(domain.low_voltage_layers_for(wire.wire_type, turns))
        for wire in domain.lv_wire_options
        for turns in domain.lv_turns
    )
    core_conversion_count = sum(max(1, len(domain.conversion_values_for_core(core))) for core in domain.cores)
    return (
        len(domain.steel_brands)
        * core_conversion_count
        * winding_count
        * len(domain.hv_wire_options)
        * len(domain.hv_layers)
        * len(domain.low_voltage_duct_options)
        * len(domain.high_voltage_duct_options)
        * cooling_count
    )


def automatic_initial_population_budget(domain: GeneDomain, coverage_count: int) -> InitialPopulationBudget:
    """根据搜索域复杂度确定第 0 代规模，不接受人为填写的最低种群数。

    A/B 覆盖已经承担“每个允许离散值和关键配对至少精算一次”的职责。因此扩展
    部分只需提供历史种子和随机探索，不应在大域中反过来膨胀到覆盖数的数倍。当前
    初步规则使用 ``max(48, min(25%C, 4√C))`` 条额外槽位；它会随域增长，但增长
    速度低于覆盖数。具体常数仍需真实精算实验标定。
    """
    if coverage_count < 1:
        raise ValueError("A/B 覆盖候选数必须大于 0")
    estimated = estimate_legal_candidate_count(domain)
    exploration_slots = max(
        48,
        min(math.ceil(coverage_count * 0.25), math.ceil(4 * math.sqrt(coverage_count))),
    )
    target = coverage_count + exploration_slots
    # 对真实小域，直接允许把所有不重复合法候选纳入第 0 代；不能请求不存在的
    # 候选并在随机循环中无限尝试。
    target = min(target, estimated)
    coverage_ratio = coverage_count / max(1, target)
    return InitialPopulationBudget(estimated, coverage_count, coverage_ratio, target)


class EvaluationCache:
    """按完整规范化基因缓存，绝不把部分相同的候选视为同一个个体。"""

    def __init__(self, precomputed_results: Mapping[str, EvaluationResult] | None = None) -> None:
        # 跨“第 0 代预览 → 完整 GA”复用时，调用方只能传入已通过输入指纹校验的
        # 完整候选结果。计数从零开始，表示本次 GA 新发生的精算/缓存命中。
        self._values: dict[str, EvaluationResult] = dict(precomputed_results or {})
        self.exact_evaluations = 0
        self.cache_hits = 0

    @property
    def results(self) -> tuple[EvaluationResult, ...]:
        """返回缓存中的已评价结果，供实验统计使用。"""
        return tuple(self._values.values())

    @staticmethod
    def _key(candidate: ThreePhaseDesignCandidate) -> str:
        return json.dumps(candidate.canonical_dict(), ensure_ascii=False, sort_keys=True)

    def evaluate(self, candidate: ThreePhaseDesignCandidate,
                 calculator: Callable[[ThreePhaseDesignCandidate], EvaluationResult]) -> tuple[EvaluationResult, bool]:
        key = self._key(candidate)
        existing = self._values.get(key)
        if existing is not None:
            self.cache_hits += 1
            return existing, True
        result = calculator(candidate)
        self._values[key] = result
        self.exact_evaluations += 1
        return result, False


def build_mixed_initial_population(
    domain: GeneDomain,
    history_candidates: list[ThreePhaseDesignCandidate | tuple[object, ...]], size: int | None,
    history_ratio: float, seed: int, include_key_pair_coverage: bool = True,
    pair_coverage_strategy: str = "balanced",
) -> InitialPopulation:
    """历史种子、确定性 A/B 覆盖和随机补充的混合初始化。

    ``size`` 仅为旧调用方兼容保留；新入口必须传 ``None``，由实际搜索域自动
    决定规模。即使旧调用方传入数值，它也只能提高目标，不能截断 A/B 覆盖。
    """
    if not 0 <= history_ratio < 1:
        raise ValueError("history_ratio 必须在 [0, 1) 内")
    if size is not None and size <= 0:
        raise ValueError("种群规模必须大于 0")
    rng = random.Random(seed)
    def key(candidate: ThreePhaseDesignCandidate) -> str:
        return json.dumps(candidate.canonical_dict(), ensure_ascii=False, sort_keys=True, default=str)

    candidates: list[ThreePhaseDesignCandidate] = []
    sources: list[str] = []
    metadata: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(candidate: ThreePhaseDesignCandidate, source: str, detail: dict[str, object] | None = None) -> bool:
        candidate = domain.normalize_candidate_wire_types(candidate)
        candidate_key = key(candidate)
        if candidate_key in seen:
            return False
        candidates.append(candidate)
        sources.append(source)
        metadata.append(detail or {})
        seen.add(candidate_key)
        return True

    # A/B 覆盖必须先入种群并保持确定性顺序，避免随机历史种子掩盖覆盖缺口。
    # 先放入少量分层背景，再让 A/B 轮换背景，避免所有覆盖候选都只像排序首项 P0。
    backgrounds = domain.stratified_background_candidates()
    for index, candidate in enumerate(backgrounds):
        add(candidate, f"coverage_background:{index + 1}/{len(backgrounds)}", {"background_index": index + 1})
    for source, candidate in domain.single_value_coverage_candidates(backgrounds=backgrounds):
        add(candidate, source)
    if include_key_pair_coverage:
        for source, candidate in domain.key_pair_coverage_candidates(
            strategy=pair_coverage_strategy, backgrounds=backgrounds,
        ):
            add(candidate, source)
    coverage_count = len(candidates)
    budget = automatic_initial_population_budget(domain, coverage_count)

    compatible_history: list[tuple[ThreePhaseDesignCandidate, str, dict[str, object], Decimal, int]] = []
    for input_index, history_item in enumerate(history_candidates):
        if isinstance(history_item, tuple):
            candidate, history_source = history_item[0], str(history_item[1])
            history_detail = dict(history_item[2]) if len(history_item) > 2 and isinstance(history_item[2], dict) else {}
        else:
            candidate, history_source, history_detail = history_item, "history_seed:same_config", {}
        if not isinstance(candidate, ThreePhaseDesignCandidate):
            raise TypeError("历史种子必须是 ThreePhaseDesignCandidate")
        try:
            used_cooling_fallback = candidate.cooling_option is None and bool(domain.cooling_options)
            normalized = domain.normalize_candidate_wire_types(candidate)
            priority = Decimal(str(history_detail.get("selection_priority", "1")))
            compatible_history.append((
                normalized,
                "history_seed:cooling_domain_fallback" if used_cooling_fallback else history_source,
                history_detail, priority, input_index,
            ))
        except ValueError:
            # 历史方案不在本次用户允许的材料/铁芯范围内时不能偷偷带入覆盖域。
            continue
    # 历史种子只占 A/B 覆盖之外的槽位。否则覆盖已大时，旧公式会把“历史比例”
    # 误解成全体比例，几乎没有随机探索空间。
    automatic_target = budget.target_size
    if size is not None:
        automatic_target = max(automatic_target, size)
    target_history = min(len(compatible_history), round(max(0, automatic_target - coverage_count) * history_ratio))
    # 相似度/历史质量已经给出优先级时，不能再随机把高质量种子打散。相同优先级
    # 按输入顺序稳定保留，因而同一配置、同一随机种子可复现。
    selected_history = sorted(compatible_history, key=lambda item: (-item[3], item[4]))[:target_history]
    history_count = sum(add(candidate, source, detail) for candidate, source, detail, _, _ in selected_history)
    target_size = max(automatic_target, coverage_count, len(candidates))
    attempts = 0
    while len(candidates) < target_size:
        candidate = domain.random_candidate(rng)
        attempts += 1
        add(candidate, "random_seed")
        if attempts > target_size * 100:
            raise RuntimeError("候选域过小，无法生成指定数量的不重复初始个体")
    return InitialPopulation(
        candidates, history_count, len(candidates) - coverage_count - history_count,
        coverage_count, tuple(sources), budget.estimated_legal_candidate_count,
        target_size, budget.coverage_target_ratio, tuple(metadata),
    )


def trace_initial_population(
    output_path: Path, config_id: int, seed: int, population: InitialPopulation,
    evaluator: Callable[[ThreePhaseDesignCandidate], EvaluationResult], domain_summary: dict,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    parallel_workers: int | None = None,
    run_id: str | None = None,
) -> tuple[str, EvaluationCache]:
    """并行精算第 0 代并写入轨迹。

    候选计算彼此独立，精算器只读取已加载的配置和目录，因此可在工作线程并行
    执行。SQLite 轨迹写入、缓存计数和页面进度仍由调用线程统一处理，避免并发
    写入破坏轨迹，也不改变候选、随机数或排序语义。
    """
    run_id = run_id or uuid.uuid4().hex
    trace = TraceStore(output_path)
    cache = EvaluationCache()
    started_at = time.monotonic()
    total = len(population.candidates)
    cpu_count = os.cpu_count() or 2
    worker_count = parallel_workers if parallel_workers is not None else min(8, max(1, cpu_count - 1))
    worker_count = max(1, min(int(worker_count), max(1, total)))
    try:
        evaluator_owner = getattr(evaluator, "__self__", None)
        formula_revision = getattr(evaluator_owner, "formula_revision", None)
        trace.create_run(run_id, seed, config_id, "initial_population_preview", domain_summary, {
            "history_count": population.history_count, "random_count": population.random_count,
            "coverage_count": population.coverage_count, "effective_population_size": len(population.candidates),
            "estimated_legal_candidate_count": population.estimated_legal_candidate_count,
            "automatic_target_size": population.automatic_target_size,
            "coverage_target_ratio": population.coverage_target_ratio,
            "parallel_workers": worker_count,
            "provenance": runtime_provenance(
                formula_revision=formula_revision, entrypoint="initial_population_preview",
            ),
        })
        last_reported_at = 0.0
        # ``InitialPopulation`` 由构造器按完整基因去重；此处仍再次按缓存键分组，
        # 以保证外部调用传入重复候选时也只精算一次，并保留原有缓存统计口径。
        grouped: dict[str, tuple[ThreePhaseDesignCandidate, list[int]]] = {}
        for index, candidate in enumerate(population.candidates):
            key = cache._key(candidate)
            if key not in grouped:
                grouped[key] = (candidate, [])
            grouped[key][1].append(index)

        completed_count = 0
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ga-initial") as executor:
            futures = {
                executor.submit(evaluator, candidate): (key, indexes)
                for key, (candidate, indexes) in grouped.items()
            }
            for future in as_completed(futures):
                key, indexes = futures[future]
                result = future.result()
                cache._values[key] = result
                cache.exact_evaluations += 1
                for position, index in enumerate(indexes):
                    cache_hit = position > 0
                    if cache_hit:
                        cache.cache_hits += 1
                    candidate = population.candidates[index]
                    trace.record_individual(
                        individual_id=f"{run_id}-g0-{index}", run_id=run_id, generation=0,
                        candidate=candidate, result=result,
                        source=population.sources[index] if index < len(population.sources) else "legacy_initial_population",
                        operator=(population.metadata[index] if index < len(population.metadata) else None),
                        cache_hit=cache_hit,
                    )
                    completed_count += 1
                # 以时间节流而不是固定“每 N 条”上报：单条精算很慢时不会让页面长时间
                # 没有反馈，单条精算很快时也不会因数千次前端刷新拖慢实验。
                now = time.monotonic()
                if progress_callback and (completed_count == 1 or completed_count == total or now - last_reported_at >= 0.5):
                    progress_callback({
                        "阶段": "正在并行精算第 0 代候选",
                        "当前代": 0,
                        "最大代数": 0,
                        "已处理候选": completed_count,
                        "本阶段候选总数": total,
                        "实际精算调用": cache.exact_evaluations,
                        "缓存命中": cache.cache_hits,
                        "并行工作线程": worker_count,
                    })
                    last_reported_at = now
        trace.conn.execute(
            "INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, 0, cache.exact_evaluations, cache.cache_hits, 0, "当前仅进行单设备精算验证，未启动遗传档案排序"),
        )
        trace.commit()
        trace.merge_run_settings(run_id, {"elapsed_seconds": time.monotonic() - started_at})
        if progress_callback:
            progress_callback({
                "阶段": "第 0 代精算与轨迹写入完成",
                "当前代": 0,
                "最大代数": 0,
                "已处理候选": total,
                "本阶段候选总数": total,
                "实际精算调用": cache.exact_evaluations,
                "缓存命中": cache.cache_hits,
                "并行工作线程": worker_count,
            })
    finally:
        trace.close()
    return run_id, cache
