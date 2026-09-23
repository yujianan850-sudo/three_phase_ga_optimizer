"""严格约束下的离散三相变压器遗传优化器。

本模块不含任何变压器公式。所有候选（包括定向变异的局部探测点）都只能通过外部
单设备精算器评价。它实现论文原型的三个算法部件：严格/近可行双档案、诊断驱动的
参数块局部精算变异，以及严格精英周围的离散邻域细化。
"""

from __future__ import annotations

import json
import math
import random
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from calculation.models import CoolingOption, EvaluationResult, OilDuctScheme, ThreePhaseDesignCandidate
from ga.gene_codec import GeneDomain, WireOption
from ga.optimizer_runtime import EvaluationCache, InitialPopulation, estimate_legal_candidate_count
from ga.runtime_provenance import runtime_provenance
from ga.trace_store import TraceStore


ZERO = Decimal("0")
INFINITY = Decimal("Infinity")


class EvaluationBudgetExhausted(RuntimeError):
    """真实精算调用已达到本次实验的硬预算。"""


@dataclass(frozen=True)
class GASettings:
    """一次可复现实验的算法参数；不包含变压器业务参数。"""

    # 入口根据第 0 代真实规模自动计算；不保留“固定 24 条”的默认值，避免绕开
    # 覆盖规模与工作种群分离规则。
    population_size: int
    generations: int = 20
    crossover_rate: float = 0.85
    # 大多数交叉保留“铁芯/匝数 + 高低压绕组”的工程耦合关系；少量探索仍允许
    # 四块独立重组，避免算法完全锁死在父代已有的结构组合中。
    coupled_electromagnetic_crossover_rate: float = 0.75
    mutation_rate: float = 0.45
    guided_mutation_rate: float = 0.65
    history_ratio: float = 0.35
    # 每代保留精英后，优先尝试注入从未评价过的合法完整候选的比例。
    random_injection_ratio: float = 0.10
    # 消融时可以强制关闭经验有效域注入，而不是依赖“经验池不足”这一运行时回退。
    injection_mode: str = "empirical_preferred"  # empirical_preferred / raw_only
    # 配置 401 的 v9 配对实验中，诊断反馈在经验有效域注入之上未检出额外收益；
    # 日常默认保留普通随机变异。single_primary 仍作为可复核的可选实验策略保留。
    guided_mutation_mode: str = "random_only"  # single_primary / random_only
    # None 表示不限制。非空时是本次运行内真实调用精算器的硬上限，缓存命中不计入。
    evaluation_budget: int | None = None
    # 第 0 代覆盖是策略无关的公共成本；该字段专门限制第 0 代结束后的真实精算调用，
    # 使消融主指标（固定演化预算下的可行率/成本）不受初代规模差异干扰。
    evolution_evaluation_budget: int | None = None
    archive_size: int = 24
    elite_count: int = 2
    local_probe_limit: int = 3
    local_refine_elites: int = 3
    local_refine_limit: int = 12
    stagnation_limit: int = 8

    def validate(self) -> None:
        if self.population_size < 1:
            raise ValueError("population_size 至少为 1")
        if self.generations < 1:
            raise ValueError("generations 至少为 1")
        if not 0 <= self.crossover_rate <= 1 or not 0 <= self.mutation_rate <= 1:
            raise ValueError("交叉率和变异率必须位于 [0, 1]")
        if not 0 <= self.coupled_electromagnetic_crossover_rate <= 1:
            raise ValueError("结构电磁耦合交叉比例必须位于 [0, 1]")
        if not 0 <= self.guided_mutation_rate <= 1 or not 0 <= self.history_ratio < 1:
            raise ValueError("定向变异率和历史种子比例不在合法范围")
        if not 0 <= self.random_injection_ratio <= 1:
            raise ValueError("随机注入比例必须位于 [0, 1]")
        if self.injection_mode not in {"empirical_preferred", "raw_only"}:
            raise ValueError("注入模式必须为 empirical_preferred 或 raw_only")
        if self.guided_mutation_mode not in {"single_primary", "random_only"}:
            raise ValueError("定向变异模式必须为 single_primary 或 random_only")
        if self.evaluation_budget is not None and self.evaluation_budget < 1:
            raise ValueError("实际精算预算必须至少为 1，或设为 None 表示不限制")
        if self.evolution_evaluation_budget is not None and self.evolution_evaluation_budget < 1:
            raise ValueError("演化精算预算必须至少为 1，或设为 None 表示不限制")
        if self.archive_size < 1 or self.elite_count < 0:
            raise ValueError("档案规模和精英数量不合法")
        if self.elite_count > self.population_size:
            raise ValueError("精英数量不能大于后续工作种群规模")


EXPERIMENT_STRATEGIES = (
    "baseline",
    "coupled",
    "empirical_injection",
    "guided_mutation",
)


def apply_experiment_strategy(settings: GASettings, strategy: str | None) -> GASettings:
    """按唯一规则应用页面与 CLI 共用的四步消融策略。

    ``None`` 表示普通运行，完全使用调用方提供的页面/CLI 参数。命名策略必须固定
    自己定义的耦合率、注入模式和诊断反馈参数，不能继承页面滑条或 CLI 的残留值；
    否则同名策略会出现同名不同义，无法用于配对消融。它们不回退任何历史版本代码。
    """
    if strategy is None:
        return settings
    if strategy == "baseline":
        return replace(settings, coupled_electromagnetic_crossover_rate=0.0,
                       injection_mode="raw_only", guided_mutation_mode="random_only")
    if strategy == "coupled":
        return replace(settings, coupled_electromagnetic_crossover_rate=0.75,
                       injection_mode="raw_only", guided_mutation_mode="random_only")
    if strategy == "empirical_injection":
        return replace(settings, coupled_electromagnetic_crossover_rate=0.75,
                       injection_mode="empirical_preferred", guided_mutation_mode="random_only")
    if strategy == "guided_mutation":
        return replace(settings, coupled_electromagnetic_crossover_rate=0.75,
                       injection_mode="empirical_preferred", guided_mutation_mode="single_primary",
                       guided_mutation_rate=0.65)
    raise ValueError(f"未知实验策略: {strategy}")


@dataclass(frozen=True)
class Individual:
    """带完整来历的一次候选评价；同一候选在不同代可有不同 individual_id。"""

    individual_id: str
    candidate: ThreePhaseDesignCandidate
    result: EvaluationResult
    generation: int
    source: str
    parent_a_id: str | None = None
    parent_b_id: str | None = None


@dataclass(frozen=True)
class ArchiveDecision:
    """一次实际档案更新对某个已精算个体作出的结论。

    ``archive_reason`` 是更新时写入 SQLite 的原始语义，不允许回放页面重新排序后
    猜测。例如同一候选被更优副本替换与档案容量不足，是两个不同原因。
    """

    individual: Individual
    archive_membership: str  # strict / near / neither
    archive_reason: str
    evaluation_status: str


@dataclass
class DualArchive:
    """严格合格档案按成本排序，近可行档案按约束超限向量排序。"""

    strict: list[Individual]
    near: list[Individual]
    limit: int

    @staticmethod
    def _candidate_key(individual: Individual) -> str:
        return json.dumps(individual.candidate.canonical_dict(), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def strict_key(individual: Individual) -> tuple[Decimal, Decimal]:
        return (_cost(individual.result), individual.result.total_violation)

    @staticmethod
    def near_key(individual: Individual) -> tuple[int, Decimal, Decimal, int, Decimal]:
        result = individual.result
        if not result.calculable or not result.complete:
            return (1, INFINITY, INFINITY, 9999, INFINITY)
        return (0, result.total_violation, result.max_violation, len(result.violations), _cost(result))

    @staticmethod
    def _evaluation_status(individual: Individual) -> str:
        result = individual.result
        # 当前 _record 总会先调用精算器，所以正常 GA 轨迹不会出现 not_evaluated。
        # 仍保留该词给迁移/外部写入方，避免把“未计算”误标成“不可计算”。
        if not result.calculable:
            return "not_calculable"
        if not result.complete:
            return "incomplete"
        if result.feasible:
            return "strict_feasible"
        return "constraint_violation"

    @classmethod
    def _representatives(
        cls, items: list[Individual], key_fn: Callable[[Individual], tuple],
    ) -> tuple[dict[str, Individual], set[str], set[str]]:
        """同一规范候选只留一个代表，并区分“更差副本”和“等价副本”。

        正常情况下，同一完整候选经确定性精算得到的排序键完全相同。它未被
        “更优方案”压制，只是档案不重复收录；轨迹不能把这类事实误写成
        ``archive_duplicate_dominated``。保留三元结果，使档案内容不变而审计
        语义准确。
        """
        winners: dict[str, Individual] = {}
        dominated_losers: set[str] = set()
        equivalent_losers: set[str] = set()
        for item in items:
            key = cls._candidate_key(item)
            previous = winners.get(key)
            if previous is None:
                winners[key] = item
            elif key_fn(item) < key_fn(previous):
                dominated_losers.add(previous.individual_id)
                winners[key] = item
            elif key_fn(item) == key_fn(previous):
                equivalent_losers.add(item.individual_id)
            else:
                dominated_losers.add(item.individual_id)
        return winners, dominated_losers, equivalent_losers

    def update(self, candidates: Iterable[Individual]) -> list[ArchiveDecision]:
        """更新双档案并返回每个参与者在本次更新中的真实去向。

        参与者包含上一代档案成员和本次传入个体；因此旧档案成员因重复候选被替换、
        因容量被挤出，也会留下事件，而非只记录新生成候选。
        """
        incoming = list(candidates)
        participants_by_id: dict[str, Individual] = {}
        for item in [*self.strict, *self.near, *incoming]:
            participants_by_id[item.individual_id] = item
        participants = list(participants_by_id.values())

        strict_items = [item for item in participants if item.result.feasible and item.result.complete]
        near_items = [
            item for item in participants
            if item.result.calculable and item.result.complete and not item.result.feasible
        ]
        strict_winners, strict_duplicate_losers, strict_equivalent_losers = self._representatives(
            strict_items, self.strict_key,
        )
        near_winners, near_duplicate_losers, near_equivalent_losers = self._representatives(
            near_items, self.near_key,
        )

        # 理论上同一候选在固定配置的确定性精算中不应同时是 strict/near；防御性地
        # 优先严格档案，并把近可行代表标成“被严格状态取代”，不能当容量淘汰处理。
        strict_keys = set(strict_winners)
        reclassified_near_ids = {
            item.individual_id for key, item in near_winners.items() if key in strict_keys
        }
        for key in strict_keys:
            near_winners.pop(key, None)

        strict_ranked = sorted(strict_winners.values(), key=self.strict_key)
        near_ranked = sorted(near_winners.values(), key=self.near_key)
        strict_kept = strict_ranked[:self.limit]
        near_kept = near_ranked[:self.limit]
        strict_kept_ids = {item.individual_id for item in strict_kept}
        near_kept_ids = {item.individual_id for item in near_kept}
        strict_capacity_ids = {item.individual_id for item in strict_ranked[self.limit:]}
        near_capacity_ids = {item.individual_id for item in near_ranked[self.limit:]}
        self.strict = strict_kept
        self.near = near_kept

        decisions: list[ArchiveDecision] = []
        for item in participants:
            status = self._evaluation_status(item)
            if item.individual_id in strict_kept_ids:
                decisions.append(ArchiveDecision(item, "strict", "strict_retained", status))
            elif item.individual_id in near_kept_ids:
                decisions.append(ArchiveDecision(item, "near", "near_retained", status))
            elif status == "not_calculable":
                decisions.append(ArchiveDecision(item, "neither", "not_calculable", status))
            elif status == "incomplete":
                decisions.append(ArchiveDecision(item, "neither", "incomplete", status))
            elif item.individual_id in strict_duplicate_losers or item.individual_id in near_duplicate_losers:
                decisions.append(ArchiveDecision(item, "neither", "archive_duplicate_dominated", status))
            elif item.individual_id in strict_equivalent_losers or item.individual_id in near_equivalent_losers:
                decisions.append(ArchiveDecision(item, "neither", "archive_duplicate_equivalent", status))
            elif item.individual_id in reclassified_near_ids:
                decisions.append(ArchiveDecision(item, "neither", "archive_superseded_by_strict", status))
            elif item.individual_id in strict_capacity_ids or item.individual_id in near_capacity_ids:
                decisions.append(ArchiveDecision(item, "neither", "archive_capacity_eliminated", status))
            else:
                # 防御性兜底：不会把严格/近可行的实际漏记说成“有约束超限”。
                decisions.append(ArchiveDecision(item, "neither", "archive_not_selected", status))
        return decisions


@dataclass(frozen=True)
class OptimizationSummary:
    run_id: str
    trace_path: Path
    generations_completed: int
    exact_evaluations: int
    initial_evaluations: int
    cache_hits: int
    strict_archive: tuple[Individual, ...]
    near_archive: tuple[Individual, ...]
    final_population: tuple[Individual, ...]
    stopped_by_stagnation: bool
    termination_reason: str
    budget_exhausted: bool
    elapsed_seconds: float = 0.0

    @property
    def best_strict(self) -> Individual | None:
        return self.strict_archive[0] if self.strict_archive else None

    @property
    def best_near(self) -> Individual | None:
        return self.near_archive[0] if self.near_archive else None

    @property
    def evolution_evaluations(self) -> int:
        """第 0 代结束后的真实精算调用数（总精算 − 初代覆盖精算）。"""
        return self.exact_evaluations - self.initial_evaluations


def _cost(result: EvaluationResult) -> Decimal:
    value = result.metrics.get("price")
    try:
        return Decimal(str(value)) if value is not None else INFINITY
    except Exception:
        return INFINITY


def _population_key(individual: Individual) -> tuple[int, Decimal, Decimal, Decimal]:
    """严格合格绝对优先；不可计算个体始终排在最后。"""
    if individual.result.feasible and individual.result.complete:
        return (0, _cost(individual.result), ZERO, ZERO)
    if individual.result.calculable and individual.result.complete:
        return (1, individual.result.total_violation, individual.result.max_violation, _cost(individual.result))
    return (2, INFINITY, INFINITY, INFINITY)


def automatic_evolution_population_size(initial_candidate_count: int) -> int:
    """由第 0 代实际规模推导后续 GA 的工作种群规模。

    第 0 代承担“每个允许离散值至少真实精算一次”的覆盖职责，规模可能达到数千；
    若把它原样复制到每一代，运行成本会随覆盖数线性膨胀，却不等于获得同等的新
    搜索信息。后续代因此采用平方根级的自动工作规模：小域不低于 96 条，大域按
    ``ceil(6 × sqrt(N0))`` 增长，且永远不超过已评价的第 0 代数。该常数是当前
    原型的保守默认值，轨迹会保存实际工作种群数，仍需通过多随机种子实验标定。
    """
    if initial_candidate_count < 1:
        raise ValueError("第 0 代候选数必须大于 0")
    return min(initial_candidate_count, max(96, math.ceil(6 * math.sqrt(initial_candidate_count))))


class ThreePhaseGeneticOptimizer:
    """离散 GA 主循环。评价器必须是完整的单设备精算器。"""

    def __init__(
        self,
        domain: GeneDomain,
        evaluator: Callable[[ThreePhaseDesignCandidate], EvaluationResult],
        settings: GASettings,
        rng: random.Random,
        trace: TraceStore,
        run_id: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        precomputed_results: Mapping[str, EvaluationResult] | None = None,
    ) -> None:
        settings.validate()
        self.domain = domain
        self.evaluator = evaluator
        self.settings = settings
        self.rng = rng
        self.trace = trace
        self.run_id = run_id
        self.cache = EvaluationCache(precomputed_results)
        self.archive = DualArchive([], [], settings.archive_size)
        self._core_by_id = {int(row["id"]): row for row in domain.cores}
        self._seen_candidate_keys: set[str] = set()
        # 只保留已被真实精算证明“可完成计算”的候选。每代随机注入优先从这个
        # 经验有效域取块，避免再从巨大的原始笛卡尔域反复抽到明显不可计算组合。
        self._calculable_individuals: dict[str, Individual] = {}
        # 记录“某个 individual 是否真正进入种群”和“相同规范候选首次严格可行”的
        # 原始事实；二者不能在回放阶段根据排序倒推。
        self._created_by_generation: dict[int, list[Individual]] = {}
        self._population_outcomes: dict[str, tuple[str, str]] = {}
        self._first_strict_generation: dict[str, int] = {}
        # 第 0 代全量覆盖完成时的真实精算计数。演化预算只统计此后的调用；
        # 若第 0 代因预算中途停止，则保持 None，表示初代未完成、演化阶段尚未开始。
        self._initial_exact_evaluations: int | None = None
        # 该回调仅把已发生的运行状态交给界面显示；绝不能参与选择、排序或
        # 随机数消费，确保是否打开实验页面不改变 GA 的结果。
        self._progress_callback = progress_callback
        self._progress_stage: str | None = None
        self._progress_generation: int | None = None
        self._progress_record_count = 0
        self._progress_population_size = 0
        self._progress_last_reported_at = 0.0

    def _notify_progress(self, stage: str, generation: int, *, completed_generation: bool,
                         population_size: int, generated_records: int | None = None,
                         run_status: str = "运行中", stop_reason: str | None = None) -> None:
        """上报已完成的精算/档案状态，供交互界面实时显示。"""
        if self._progress_callback is None:
            return
        strict_best = self.archive.strict[0] if self.archive.strict else None
        near_best = self.archive.near[0] if self.archive.near else None
        payload = {
            "阶段": stage,
            "运行状态": run_status,
            "提前停止原因": stop_reason,
            "当前代": generation,
            "最大代数": self.settings.generations,
            "本代完成": completed_generation,
            "本代种群规模": population_size,
            "本代累计评价记录": generated_records,
            "实际精算调用": self.cache.exact_evaluations,
            "缓存命中": self.cache.cache_hits,
            "严格档案": tuple(self.archive.strict),
            "近可行档案": tuple(self.archive.near),
            "当前最优严格方案": strict_best,
            "当前最优近可行方案": near_best,
        }
        self._progress_callback(payload)

    def _start_progress_stage(self, stage: str, generation: int, population_size: int) -> None:
        self._progress_stage = stage
        self._progress_generation = generation
        self._progress_record_count = 0
        self._progress_population_size = population_size
        self._progress_last_reported_at = time.monotonic()
        self._notify_progress(stage, generation, completed_generation=False, population_size=population_size, generated_records=0)

    def _evolution_budget_reached(self) -> bool:
        evolution_budget = self.settings.evolution_evaluation_budget
        if evolution_budget is None or self._initial_exact_evaluations is None:
            return False
        return self.cache.exact_evaluations - self._initial_exact_evaluations >= evolution_budget

    def _budget_reached(self) -> bool:
        budget = self.settings.evaluation_budget
        if budget is not None and self.cache.exact_evaluations >= budget:
            return True
        return self._evolution_budget_reached()

    def _budget_stop_reason(self) -> str:
        """判定触发硬截止的是演化预算还是总预算，写出可区分的终止原因。"""
        if self._evolution_budget_reached():
            return f"演化精算预算 B={self.settings.evolution_evaluation_budget} 硬截止"
        return f"总精算预算 B={self.settings.evaluation_budget} 硬截止"

    def _budget_stop_kind(self) -> str:
        """机器可读的硬截止类别，供 CLI / 汇总表区分两种预算。"""
        if self._evolution_budget_reached():
            return "evolution_evaluation_budget_hard_stop"
        return "evaluation_budget_hard_stop"

    def _require_evaluation_budget(self) -> None:
        """在每次评价前执行硬截止检查。

        预算只累计真正调用精算器的次数；但一旦已经达到 B，运行立即结束，
        不再继续写入缓存命中、精英复制或下一代候选，避免把“固定预算”变成
        事后统计口径。
        """
        if self._budget_reached():
            raise EvaluationBudgetExhausted("实际精算调用已达到硬预算")

    def _record(
        self,
        candidate: ThreePhaseDesignCandidate,
        generation: int,
        source: str,
        operator: dict | None = None,
        parent_a: Individual | None = None,
        parent_b: Individual | None = None,
    ) -> Individual:
        self._require_evaluation_budget()
        candidate_key = json.dumps(candidate.canonical_dict(), ensure_ascii=False, sort_keys=True, default=str)
        result, cache_hit = self.cache.evaluate(candidate, self.evaluator)
        operator_payload = dict(operator or {})
        # “入档前最后动作”只描述最后一次创建记录，绝不用于宣称算子因果贡献。
        # 精英复制和缓存复用都没有新的创建动作，必须显式写 N/A，避免回放页误标。
        # 精英复制语义优先于缓存命中：档案精英一定已精算过、必然命中缓存，
        # 但它的“最后动作”是精英保留而非缓存复用，需单独留痕以便回放页区分。
        if source == "elitism":
            operator_payload.update({
                "archive_entry_last_action": None,
                "archive_entry_last_action_reason": "elitism_copy_no_new_operator",
            })
        elif cache_hit:
            operator_payload.update({
                "archive_entry_last_action": None,
                "archive_entry_last_action_reason": "cache_hit_no_new_operator",
            })
        else:
            operator_payload.setdefault("archive_entry_last_action", source)
            operator_payload.setdefault("archive_entry_last_action_reason", "record_creation_source")
        self._seen_candidate_keys.add(candidate_key)
        individual = Individual(
            individual_id=f"{self.run_id}-g{generation}-{uuid.uuid4().hex[:10]}",
            candidate=candidate,
            result=result,
            generation=generation,
            source=source,
            parent_a_id=None if parent_a is None else parent_a.individual_id,
            parent_b_id=None if parent_b is None else parent_b.individual_id,
        )
        self.trace.record_individual(
            individual.individual_id, self.run_id, generation, candidate, result, source,
            operator=operator_payload, parent_a_id=individual.parent_a_id, parent_b_id=individual.parent_b_id,
            cache_hit=cache_hit,
        )
        self._created_by_generation.setdefault(generation, []).append(individual)
        if result.calculable and result.complete:
            # 精英复制、父代复制或缓存命中不能因出现次数更多而在经验池里获得更高
            # 抽样权重；同一完整候选只保存一个代表。
            self._calculable_individuals.setdefault(candidate_key, individual)
        if result.feasible and result.complete:
            trace_key = self.trace.candidate_key(candidate)
            # 同一个完整候选可能因精英保留或交叉重现而反复出现。首个严格可行
            # 代数一经本次运行记录便不会改变，无需每个重复副本都执行一次
            # ``INSERT OR IGNORE + SELECT`` 的 SQLite 往返。
            if trace_key not in self._first_strict_generation:
                self._first_strict_generation[trace_key] = self.trace.mark_first_strict(
                    self.run_id, candidate, generation, individual.individual_id,
                )
        if self._progress_stage is not None and self._progress_generation == generation:
            self._progress_record_count += 1
            # 单条精算较慢时，下一条完成就会立即推进；精算较快时按时间节流，
            # 避免数千次前端刷新反过来拖慢 GA。
            if time.monotonic() - self._progress_last_reported_at >= 0.5:
                self._notify_progress(
                    self._progress_stage, generation, completed_generation=False,
                    population_size=self._progress_population_size,
                    generated_records=self._progress_record_count,
                )
                self._progress_last_reported_at = time.monotonic()
        return individual

    @staticmethod
    def _candidate_key(candidate: ThreePhaseDesignCandidate) -> str:
        return json.dumps(candidate.canonical_dict(), ensure_ascii=False, sort_keys=True, default=str)

    def _is_novel_candidate(self, candidate: ThreePhaseDesignCandidate, occupied: set[str] | None = None) -> bool:
        """判断候选是否尚未作为本次运行的新个体精算过。精英复制不走本规则。"""
        key = self._candidate_key(candidate)
        return key not in self._seen_candidate_keys and (occupied is None or key not in occupied)

    def _record_population_outcomes(self, generation: int, population: list[Individual]) -> None:
        """写出本代所有已创建个体是否真正进入种群。

        当前主循环不会批量生成候选后再按种群容量淘汰；诊断探测、变异前基准和
        邻域细化是不同路径，不能伪写成 ``population_capacity_eliminated``。
        """
        retained_ids = {item.individual_id for item in population}
        outcomes: list[dict[str, str]] = []
        for item in self._created_by_generation.get(generation, []):
            if item.individual_id in retained_ids:
                status, reason = "retained", "population_retained"
            elif not item.result.calculable or not item.result.complete:
                # 已真实精算并保留诊断，但不能让它在下一代重新成为父代；这不是
                # “容量淘汰”，而是结果本身没有可比较的优化排序依据。
                status, reason = "not_retained", "not_calculable_or_incomplete"
            elif item.source == "diagnostic_probe":
                status, reason = "not_retained", "diagnostic_probe_not_selected"
            elif item.source == "pre_guided_mutation":
                status, reason = "not_retained", "guided_base_not_selected"
            elif item.source == "neighborhood_refinement":
                status, reason = "not_retained", "refinement_only"
            elif generation == 0:
                # 第 0 代已经真实精算并参与双档案；未被带入后续 GA 工作种群不等于
                # 被淘汰或未覆盖，必须保留这一可解释语义。
                status, reason = "not_retained", "initial_coverage_not_selected_for_evolution"
            else:
                status, reason = "not_retained", "operator_output_not_selected"
            self._population_outcomes[item.individual_id] = (status, reason)
            outcomes.append({
                "individual_id": item.individual_id,
                "population_status": status,
                "population_reason": reason,
            })
        if outcomes:
            self.trace.record_population_outcomes(self.run_id, generation, outcomes)

    def _update_archive(self, generation: int, candidates: Iterable[Individual], stage: str) -> None:
        """更新双档案，并立即写出本次更新真实发生的去向和淘汰原因。"""
        decisions = self.archive.update(candidates)
        rows: list[dict[str, object]] = []
        decision_ids: set[str] = set()
        for decision in decisions:
            individual = decision.individual
            decision_ids.add(individual.individual_id)
            candidate_key = self.trace.candidate_key(individual.candidate)
            rows.append({
                "individual_id": individual.individual_id,
                "candidate_key": candidate_key,
                "archive_membership": decision.archive_membership,
                "archive_reason": decision.archive_reason,
                "evaluation_status": decision.evaluation_status,
                "first_strict_generation": self._first_strict_generation.get(candidate_key),
            })

        # 当前算法只把正式种群（或最终细化批次）送入 archive.update。其余已计算的
        # 辅助候选也必须被留痕，但准确原因是“未参与档案更新”，不是容量淘汰。
        for individual in self._created_by_generation.get(generation, []):
            if individual.individual_id in decision_ids:
                continue
            candidate_key = self.trace.candidate_key(individual.candidate)
            rows.append({
                "individual_id": individual.individual_id,
                "candidate_key": candidate_key,
                "archive_membership": "neither",
                "archive_reason": "archive_not_considered_auxiliary",
                "evaluation_status": DualArchive._evaluation_status(individual),
                "first_strict_generation": self._first_strict_generation.get(candidate_key),
            })
        if rows:
            self.trace.record_archive_memberships(self.run_id, generation, stage, rows)

    def _random_injections(
        self, generation: int, existing: list[Individual], population_size: int, remaining_slots: int,
    ) -> list[Individual]:
        """注入未见候选，优先在“已证实可计算”的经验有效域内随机重组。

        原始页面范围仍是唯一的合法边界；这里不删除任何页面允许值。区别在于，
        第 0 代已经用真实精算发现一批可计算的结构后，后续探索不再盲目从全域
        笛卡尔组合抽样，而是从这些结构中随机选择基座和供体块，再保持完整目录
        记录与派生层数关系。这是降低无效精算的经验采样，不是性能公式的前置判定。
        """
        requested = math.ceil(population_size * self.settings.random_injection_ratio)
        planned = min(requested, max(0, remaining_slots))
        if planned <= 0:
            return []
        result: list[Individual] = []
        occupied = {
            json.dumps(item.candidate.canonical_dict(), ensure_ascii=False, sort_keys=True, default=str)
            for item in existing
        }
        attempts = 0
        limit = max(100, planned * 100)
        while len(result) < planned and attempts < limit:
            attempts += 1
            if self.settings.injection_mode == "raw_only":
                candidate = self.domain.random_candidate(self.rng)
                sampling_metadata = {
                    "sampling_space": "原始页面搜索域",
                    "reason": "消融策略强制原始域注入",
                    "injection_mode": "raw_only",
                }
            else:
                candidate, sampling_metadata = self._effective_domain_random_candidate()
            key = self._candidate_key(candidate)
            if key in occupied or key in self._seen_candidate_keys:
                continue
            occupied.add(key)
            if sampling_metadata["sampling_space"] == "经验有效域":
                source = "effective_domain_random_injection"
            elif self.settings.injection_mode == "raw_only":
                source = "random_injection_raw_only"
            else:
                source = "random_injection_raw_fallback"
            individual = self._record(
                candidate, generation, source,
                {
                    "type": "random_injection", "requested": requested, "planned": planned,
                    "attempt": attempts, **sampling_metadata,
                },
            )
            # 注入候选仍保留在轨迹中；但不可计算/不完整结果不占用工作种群槽位，
            # 继续尝试未见候选直到达到计划数量或尝试上限。
            if individual.result.calculable and individual.result.complete:
                result.append(individual)
        return result

    def _effective_domain_random_candidate(self) -> tuple[ThreePhaseDesignCandidate, dict[str, object]]:
        """由两个可计算候选随机重组，作为每代注入的优先采样方式。"""
        pool = tuple(self._calculable_individuals.values())
        if len(pool) < 2:
            return self.domain.random_candidate(self.rng), {
                "sampling_space": "原始页面搜索域", "reason": "可计算经验池不足",
            }
        base = self.rng.choice(pool)
        donor = self.rng.choice(pool)
        # 至少替换一个块，最多替换两个块；保持电磁结构优先整体继承的原则。
        groups = ["electromagnetic", "cooling"]
        selected = self.rng.sample(groups, k=1 if self.rng.random() < 0.75 else 2)
        candidate = base.candidate
        if "electromagnetic" in selected:
            candidate = replace(
                candidate,
                steel_brand=donor.candidate.steel_brand,
                core_type=donor.candidate.core_type,
                core_data_id=donor.candidate.core_data_id,
                low_voltage_turns=donor.candidate.low_voltage_turns,
                low_voltage_wire_id=donor.candidate.low_voltage_wire_id,
                low_voltage_wire_type=donor.candidate.low_voltage_wire_type,
                low_voltage_layers=donor.candidate.low_voltage_layers,
                low_voltage_duct=donor.candidate.low_voltage_duct,
                high_voltage_wire_id=donor.candidate.high_voltage_wire_id,
                high_voltage_wire_type=donor.candidate.high_voltage_wire_type,
                high_voltage_layers=donor.candidate.high_voltage_layers,
                high_voltage_duct=donor.candidate.high_voltage_duct,
                conversion_c=donor.candidate.conversion_c,
            )
        if "cooling" in selected:
            candidate = replace(candidate, cooling_option=donor.candidate.cooling_option)
        return self._normalize_candidate(candidate), {
            "sampling_space": "经验有效域", "base_individual_id": base.individual_id,
            "donor_individual_id": donor.individual_id, "recombined_groups": selected,
        }

    def _normalize_core_gene(self, candidate: ThreePhaseDesignCandidate) -> ThreePhaseDesignCandidate:
        core = self._core_by_id[int(candidate.core_data_id)]
        core_type = int(core["core_type"])
        if core_type != 1:
            return replace(candidate, core_type=core_type, conversion_c=None)
        values = self.domain.conversion_values_for_core(core)
        conversion = candidate.conversion_c
        if conversion is None:
            conversion = values[0]
        elif Decimal(str(conversion)) not in values:
            conversion = min(values, key=lambda value: (abs(value - Decimal(str(conversion))), value))
        return replace(candidate, core_type=core_type, conversion_c=conversion)

    def _normalize_candidate(self, candidate: ThreePhaseDesignCandidate) -> ThreePhaseDesignCandidate:
        """同时收束 Java 低压层数规则和铁芯 conversionC 规则。"""
        return self._normalize_core_gene(self.domain.normalize_candidate_wire_types(candidate))

    def _replace_core(self, candidate: ThreePhaseDesignCandidate, core_id: int) -> ThreePhaseDesignCandidate:
        """真正切换铁芯记录时，长圆 conversionC 才改为新铁芯的目录默认值。"""
        core = self._core_by_id[int(core_id)]
        core_type = int(core["core_type"])
        values = self.domain.conversion_values_for_core(core)
        conversion = values[0] if core_type == 1 else None
        return replace(candidate, core_data_id=int(core_id), core_type=core_type, conversion_c=conversion)

    def _tournament(self, pool: list[Individual], size: int = 3) -> Individual:
        if not pool:
            raise RuntimeError("没有可用于选择的个体")
        sampled = [self.rng.choice(pool) for _ in range(min(size, len(pool)))]
        return min(sampled, key=_population_key)

    def _select_parents(self, population: list[Individual]) -> tuple[Individual, Individual]:
        """严格档案优先，同时保留近可行档案跨越约束边界的机会。"""
        strict_pool = self.archive.strict
        near_pool = self.archive.near
        ranked = sorted(population, key=_population_key)
        if strict_pool:
            first = self._tournament(strict_pool)
            second_pool = near_pool if near_pool and self.rng.random() < 0.30 else strict_pool + ranked
        elif near_pool:
            first = self._tournament(near_pool)
            second_pool = near_pool + ranked
        else:
            first = self._tournament(ranked)
            second_pool = ranked
        return first, self._tournament(second_pool)

    def _cross(self, first: ThreePhaseDesignCandidate, second: ThreePhaseDesignCandidate) -> tuple[ThreePhaseDesignCandidate, dict]:
        """优先继承耦合电磁块，冷却型号始终作为独立完整块交叉。"""
        coupled = self.rng.random() < self.settings.coupled_electromagnetic_crossover_rate
        electromagnetic_parent = first if self.rng.random() < 0.5 else second
        core_parent = electromagnetic_parent if coupled else (first if self.rng.random() < 0.5 else second)
        lv_parent = electromagnetic_parent if coupled else (first if self.rng.random() < 0.5 else second)
        hv_parent = electromagnetic_parent if coupled else (first if self.rng.random() < 0.5 else second)
        cooling_parent = first if self.rng.random() < 0.5 else second
        child = ThreePhaseDesignCandidate(
            steel_brand=core_parent.steel_brand,
            core_type=core_parent.core_type,
            core_data_id=core_parent.core_data_id,
            low_voltage_turns=core_parent.low_voltage_turns,
            low_voltage_wire_id=lv_parent.low_voltage_wire_id,
            low_voltage_wire_type=lv_parent.low_voltage_wire_type,
            low_voltage_layers=lv_parent.low_voltage_layers,
            low_voltage_duct=lv_parent.low_voltage_duct,
            high_voltage_wire_id=hv_parent.high_voltage_wire_id,
            high_voltage_wire_type=hv_parent.high_voltage_wire_type,
            high_voltage_layers=hv_parent.high_voltage_layers,
            high_voltage_duct=hv_parent.high_voltage_duct,
            conversion_c=core_parent.conversion_c,
            # 冷却型号是独立交叉块，但仍作为一个完整型号整体传递；波纹片 ID 或
            # 散热器的中心距、片数、组数与 G/Q/SZ 行 ID 绝不拆开混搭。
            cooling_option=cooling_parent.cooling_option,
        )
        # 低压匝数来自铁芯块、导线和层数来自低压块；两块父代不同的时候，
        # 箔材层数必须重新等于新匝数，扁线则必须落在 2/4。这不是“修补交叉”，
        # 而是 Java 本来就定义的派生参数关系。
        return self._normalize_candidate(child), {
            "type": "coupled_electromagnetic_crossover" if coupled else "exploratory_four_block_crossover",
            "electromagnetic_mode": "耦合继承" if coupled else "四块探索重组",
            "electromagnetic_from": "A" if electromagnetic_parent is first else "B",
            "core_from": "A" if core_parent is first else "B",
            "lv_from": "A" if lv_parent is first else "B",
            "hv_from": "A" if hv_parent is first else "B",
            "cooling_from": "A" if cooling_parent is first else "B",
        }

    @staticmethod
    def _adjacent(values: tuple, current: object) -> list[object]:
        try:
            index = values.index(current)
        except ValueError:
            return []
        result: list[object] = []
        if index > 0:
            result.append(values[index - 1])
        if index + 1 < len(values):
            result.append(values[index + 1])
        return result

    def _nearby_core_ids(self, candidate: ThreePhaseDesignCandidate) -> list[int]:
        same_type = tuple(
            int(row["id"]) for row in self.domain.cores if int(row["core_type"]) == candidate.core_type
        )
        return [int(value) for value in self._adjacent(same_type, candidate.core_data_id)]

    def _wire_option(self, side: str, candidate: ThreePhaseDesignCandidate) -> WireOption:
        options = self.domain.lv_wire_options if side == "lv" else self.domain.hv_wire_options
        wire_type = candidate.low_voltage_wire_type if side == "lv" else candidate.high_voltage_wire_type
        wire_id = candidate.low_voltage_wire_id if side == "lv" else candidate.high_voltage_wire_id
        matches = [option for option in options if option.wire_type == wire_type and option.record_id == wire_id]
        if len(matches) != 1:
            raise ValueError(f"{side} 侧候选导线不是完整搜索域选项: type={wire_type}, id={wire_id}")
        return matches[0]

    @staticmethod
    def _replace_wire_option(candidate: ThreePhaseDesignCandidate, side: str, option: WireOption) -> ThreePhaseDesignCandidate:
        """始终同时替换类别和记录 ID，绝不拆分导线属性。"""
        if side == "lv":
            return replace(candidate, low_voltage_wire_type=option.wire_type, low_voltage_wire_id=option.record_id)
        return replace(candidate, high_voltage_wire_type=option.wire_type, high_voltage_wire_id=option.record_id)

    def _adjacent_wire_options(self, side: str, candidate: ThreePhaseDesignCandidate) -> list[WireOption]:
        """按裸线尺寸建立局部邻居，绝不再按截面积排序。

        扁线/箔材只收录两种真实目录记录：保持厚度不变的相邻宽度，以及保持宽度
        不变的相邻厚度。因此“加宽”和“加厚”是可区分的变异方向；若目录没有
        另一维完全相同的记录，就不凭空推导一个邻居。圆线没有宽厚两维，仅按
        直径相邻。跨 QQL/QQLB 等类别仍由普通完整线规变异处理。
        """
        current = self._wire_option(side, candidate)
        options = self.domain.lv_wire_options if side == "lv" else self.domain.hv_wire_options
        same_type = tuple(option for option in options if option.wire_type == current.wire_type)
        if current.bare_width is None:
            return []
        if current.bare_thickness is None:
            ordered = tuple(sorted(same_type, key=lambda option: (option.bare_width or Decimal("0"), option.record_id)))
            return [option for option in self._adjacent(ordered, current) if isinstance(option, WireOption)]

        neighbors: list[WireOption] = []
        same_thickness = tuple(sorted(
            (option for option in same_type if option.bare_thickness == current.bare_thickness),
            key=lambda option: (option.bare_width or Decimal("0"), option.record_id),
        ))
        neighbors.extend(option for option in self._adjacent(same_thickness, current) if isinstance(option, WireOption))
        same_width = tuple(sorted(
            (option for option in same_type if option.bare_width == current.bare_width),
            key=lambda option: (option.bare_thickness or Decimal("0"), option.record_id),
        ))
        neighbors.extend(option for option in self._adjacent(same_width, current) if isinstance(option, WireOption))
        return list(dict.fromkeys(neighbors))

    def _directional_wire_options(
        self, side: str, candidate: ThreePhaseDesignCandidate, direction: str,
    ) -> list[WireOption]:
        """按宽、厚或直径分别筛选相邻线规，供负载损耗的局部探测使用。

        真实轨迹显示：当负载损耗已偏高时，盲目试“减宽/减厚”不仅没有改善，且常
        触发不可计算。这里不按截面积把宽厚混为一谈：扁线只保留同厚加/减宽、同宽
        加/减厚的真实目录记录；圆线只比较直径。普通随机变异仍保留双向探索。
        """
        current = self._wire_option(side, candidate)
        options = self._adjacent_wire_options(side, candidate)
        if direction not in {"upper", "lower"}:
            return options

        def is_requested_direction(option: WireOption) -> bool:
            if current.bare_thickness is None:
                # 高压圆线：bare_width 承载直径。
                if current.bare_width is None or option.bare_width is None:
                    return False
                return option.bare_width > current.bare_width if direction == "upper" else option.bare_width < current.bare_width
            if option.bare_thickness == current.bare_thickness:
                if option.bare_width is None or current.bare_width is None:
                    return False
                return option.bare_width > current.bare_width if direction == "upper" else option.bare_width < current.bare_width
            if option.bare_width == current.bare_width and option.bare_thickness is not None:
                return option.bare_thickness > current.bare_thickness if direction == "upper" else option.bare_thickness < current.bare_thickness
            return False

        return [option for option in options if is_requested_direction(option)]

    def _adjacent_cooling_options(self, candidate: ThreePhaseDesignCandidate) -> list[CoolingOption]:
        """局部热工探测只替换完整冷却型号，不拆 G/Q/SZ 参数行。"""
        option = candidate.cooling_option
        if option is None:
            return []
        return [value for value in self._adjacent(self.domain.cooling_options, option) if isinstance(value, CoolingOption)]

    def _random_mutation(self, candidate: ThreePhaseDesignCandidate) -> tuple[ThreePhaseDesignCandidate, dict]:
        """普通离散变异：只替换合法目录记录或整数候选值。"""
        actions: list[tuple[str, Callable[[], ThreePhaseDesignCandidate]]] = []
        if len(self.domain.steel_brands) > 1:
            actions.append(("steel_brand", lambda: replace(
                candidate, steel_brand=self.rng.choice(tuple(value for value in self.domain.steel_brands if value != candidate.steel_brand))
            )))
        if len(self.domain.cores) > 1:
            actions.append(("core", lambda: self._replace_core(
                candidate, int(self.rng.choice(tuple(row for row in self.domain.cores if int(row["id"]) != candidate.core_data_id))["id"])
            )))
        for name, field, values, current in (
            ("lv_layers", "low_voltage_layers", self.domain.low_voltage_layers_for_candidate(candidate), candidate.low_voltage_layers),
            ("lv_duct", "low_voltage_duct", self.domain.low_voltage_duct_options, candidate.low_voltage_duct),
            ("hv_layers", "high_voltage_layers", self.domain.hv_layers, candidate.high_voltage_layers),
            ("hv_duct", "high_voltage_duct", self.domain.high_voltage_duct_options, candidate.high_voltage_duct),
            ("lv_turns", "low_voltage_turns", self.domain.lv_turns, candidate.low_voltage_turns),
        ):
            if len(values) > 1:
                actions.append((name, lambda field=field, values=values, current=current: replace(
                    candidate, **{field: self.rng.choice(tuple(value for value in values if value != current))}
                )))
        # conversionC 只属于当前长圆铁芯；不能从另一铁芯借边界，也不能生成连续任意值。
        core = self._core_by_id[int(candidate.core_data_id)]
        conversion_values = self.domain.conversion_values_for_core(core)
        conversion_alternatives = tuple(
            value for value in conversion_values if value != candidate.conversion_c
        )
        if conversion_alternatives:
            actions.append(("conversion_c", lambda: replace(
                candidate, conversion_c=self.rng.choice(conversion_alternatives)
            )))
        for side, options in (("lv", self.domain.lv_wire_options), ("hv", self.domain.hv_wire_options)):
            current = self._wire_option(side, candidate)
            alternatives = tuple(option for option in options if option != current)
            if alternatives:
                actions.append((f"{side}_wire_option", lambda side=side, alternatives=alternatives: self._replace_wire_option(
                    candidate, side, self.rng.choice(alternatives)
                )))
        cooling_alternatives = tuple(option for option in self.domain.cooling_options if option != candidate.cooling_option)
        if cooling_alternatives:
            actions.append(("cooling_option", lambda: replace(candidate, cooling_option=self.rng.choice(cooling_alternatives))))
        if not actions:
            return candidate, {"type": "random_mutation", "changed": "none"}
        name, action = self.rng.choice(actions)
        changed = action()
        return self._normalize_candidate(changed), {"type": "random_mutation", "changed": name}

    @staticmethod
    def _dominant_violation(result: EvaluationResult) -> object | None:
        """只选归一化超限最大的约束，避免一次定向变异同时乱动多个模块。"""
        return max(result.violations, key=lambda item: item.normalized_excess, default=None)

    @staticmethod
    def _primary_module_for_violation(result: EvaluationResult, violation: object) -> str | None:
        """把一个主导约束映射到最直接的一个可变模块。

        这是“优先模块”而不是未经数据验证的单调物理结论：实际增减方向仍由相邻
        目录记录逐条精算确认，并完整记录到轨迹。
        """
        name = getattr(violation, "name", "")
        if name in {"flux_density", "no_load_loss"}:
            return "core"
        if name == "impedance":
            # UK 的漏抗对高低压绕组的径向油道更直接敏感。这里返回的是只服务
            # 于定向局部探测的虚拟模块：每个候选仍然只会替换低压或高压一侧的
            # 一个完整油道方案，绝不把两侧油道同时混改。
            return "radial_duct"
        if name == "load_loss":
            metrics = result.metrics
            return "lv" if Decimal(str(metrics.get("wlvpk", 0))) >= Decimal(str(metrics.get("hvpk", 0))) else "hv"
        if name in {"oil_top_temp_rise", "corrugation_expansion", "radiator_center_distance"}:
            return "cooling"
        if name.startswith("lv_"):
            return "lv"
        if name.startswith("hv_"):
            return "hv"
        return None

    def _candidate_change_description(
        self, before: ThreePhaseDesignCandidate, after: ThreePhaseDesignCandidate, module: str,
    ) -> list[str]:
        """将局部探测真实改动写成业务可读方向，不从记录 ID 猜导线尺寸。"""
        changes: list[str] = []
        if module == "lv" and (before.low_voltage_wire_type, before.low_voltage_wire_id) != (after.low_voltage_wire_type, after.low_voltage_wire_id):
            old, new = self._wire_option("lv", before), self._wire_option("lv", after)
            if old.bare_thickness == new.bare_thickness and old.bare_width is not None and new.bare_width is not None:
                changes.append("低压导线宽度增加" if new.bare_width > old.bare_width else "低压导线宽度减小")
            elif old.bare_width == new.bare_width and old.bare_thickness is not None and new.bare_thickness is not None:
                changes.append("低压导线厚度增加" if new.bare_thickness > old.bare_thickness else "低压导线厚度减小")
            else:
                changes.append("低压完整线规相邻替换")
        if module == "hv" and (before.high_voltage_wire_type, before.high_voltage_wire_id) != (after.high_voltage_wire_type, after.high_voltage_wire_id):
            old, new = self._wire_option("hv", before), self._wire_option("hv", after)
            if old.bare_thickness == new.bare_thickness and old.bare_width is not None and new.bare_width is not None:
                changes.append("高压导线宽度增加" if new.bare_width > old.bare_width else "高压导线宽度减小")
            elif old.bare_width == new.bare_width and old.bare_thickness is not None and new.bare_thickness is not None:
                changes.append("高压导线厚度增加" if new.bare_thickness > old.bare_thickness else "高压导线厚度减小")
            else:
                changes.append("高压完整线规相邻替换")
        if module == "radial_duct":
            if before.low_voltage_duct != after.low_voltage_duct:
                changes.append(
                    f"低压油道 {self._duct_description(before.low_voltage_duct)} → "
                    f"{self._duct_description(after.low_voltage_duct)}"
                )
            if before.high_voltage_duct != after.high_voltage_duct:
                changes.append(
                    f"高压油道 {self._duct_description(before.high_voltage_duct)} → "
                    f"{self._duct_description(after.high_voltage_duct)}"
                )
            return changes or ["高低压径向油道相邻调整"]
        fields = {
            "core": (("steel_brand", "硅钢牌号"), ("core_data_id", "铁芯记录"), ("low_voltage_turns", "低压匝数"), ("conversion_c", "长圆直线段 C")),
            "lv": (("low_voltage_layers", "低压层数"), ("low_voltage_duct", "低压油道")),
            "hv": (("high_voltage_layers", "高压层数"), ("high_voltage_duct", "高压油道")),
            "cooling": (("cooling_option", "完整冷却型号"),),
        }
        for field, label in fields.get(module, ()):
            if getattr(before, field) != getattr(after, field):
                changes.append(f"{label}相邻调整")
        return changes or [f"{module} 模块相邻调整"]

    @staticmethod
    def _duct_description(duct: OilDuctScheme) -> str:
        """将页面油道组合写成轨迹可读文字。"""
        if duct.count == 0:
            return "无油道"
        type_name = "半油道" if duct.duct_type == 1 else "全油道"
        return f"{duct.count} 个{type_name}"

    @staticmethod
    def _directional_duct_neighbors(
        options: tuple[OilDuctScheme, ...], current: OilDuctScheme, direction: str,
    ) -> list[OilDuctScheme]:
        """按阻抗偏高/偏低寻找同侧油道的离散相邻档位。

        ``upper`` 表示 UK 偏高，优先找更少/更小的油道方案；``lower`` 表示
        UK 偏低，优先找更多/更大的方案。比较只使用页面真实允许的数量和
        半/全油道类型：它只负责缩小局部探测方向，最终是否改善仍必须交给
        精算器判定，不能把该顺序当作未经验证的单调物理结论。
        """
        if direction not in {"upper", "lower"}:
            return []
        if direction == "upper":
            eligible = [
                value for value in options
                if value.count < current.count
                or (value.count == current.count and value.duct_type < current.duct_type)
            ]
            eligible.sort(key=lambda value: (
                current.count - value.count,
                current.duct_type - value.duct_type,
                -value.count,
                -value.duct_type,
            ))
        else:
            eligible = [
                value for value in options
                if value.count > current.count
                or (value.count == current.count and value.duct_type > current.duct_type)
            ]
            eligible.sort(key=lambda value: (
                value.count - current.count,
                value.duct_type - current.duct_type,
                value.count,
                value.duct_type,
            ))
        return eligible

    def _impedance_duct_candidates(
        self, candidate: ThreePhaseDesignCandidate, direction: str,
    ) -> list[ThreePhaseDesignCandidate]:
        """给出两侧最近的同向油道邻居，避免有限探测跨越多个离散档位。

        阻抗局部探测的 ``local_probe_limit`` 默认只有 3。过去会把同侧所有同向
        油道档位依次塞入候选，例如 ``2 个全油道 → 1 个全油道 → 无油道``；后者
        已在真实轨迹中出现过大幅恶化，虽方向正确，却不是“局部”调整。现在每侧
        只取一个最近合法档位，低高压两侧交错尝试。更远档位仍可经后续世代的相邻
        变异到达，而非在一次三点探测中跳过。没有同向油道邻居时，交给普通随机
        变异探索，不再用铁芯邻居凑满局部探测配额：真实轨迹显示这类 fallback 在
        当前数据表缺口下均不可计算，不能把无效精算伪装成“阻抗定向修复”。
        """
        lv = self._directional_duct_neighbors(
            self.domain.low_voltage_duct_options, candidate.low_voltage_duct, direction,
        )
        hv = self._directional_duct_neighbors(
            self.domain.high_voltage_duct_options, candidate.high_voltage_duct, direction,
        )
        values: list[ThreePhaseDesignCandidate] = []
        if lv:
            values.append(replace(candidate, low_voltage_duct=lv[0]))
        if hv:
            values.append(replace(candidate, high_voltage_duct=hv[0]))
        return [self._normalize_candidate(value) for value in values]

    def _local_block_candidates(
        self, candidate: ThreePhaseDesignCandidate, module: str, violation_direction: str | None = None,
        violation_name: str | None = None,
    ) -> list[ThreePhaseDesignCandidate]:
        """只产生页面/目录已有的相邻合法档位，再以精算结果决定是否保留。"""
        values: list[ThreePhaseDesignCandidate] = []
        if module == "radial_duct":
            # 只在阻抗有明确“偏高/偏低”诊断时使用方向性油道候选；若无方向，
            # 宁可回退普通随机变异，也不伪装成定向调整。
            if violation_direction is not None:
                values.extend(self._impedance_duct_candidates(candidate, violation_direction))
        elif module == "core":
            for steel in self._adjacent(self.domain.steel_brands, candidate.steel_brand):
                values.append(replace(candidate, steel_brand=steel))
            for core_id in self._nearby_core_ids(candidate):
                values.append(self._replace_core(candidate, core_id))
            for turns in self._adjacent(self.domain.lv_turns, candidate.low_voltage_turns):
                values.append(replace(candidate, low_voltage_turns=int(turns)))
            core = self._core_by_id[int(candidate.core_data_id)]
            for conversion in self._adjacent(self.domain.conversion_values_for_core(core), candidate.conversion_c):
                values.append(replace(candidate, conversion_c=conversion))
        elif module == "lv":
            wire_options = (
                self._directional_wire_options("lv", candidate, violation_direction)
                if violation_name == "load_loss" else self._adjacent_wire_options("lv", candidate)
            )
            for wire in wire_options:
                values.append(self._replace_wire_option(candidate, "lv", wire))
            for layers in self._adjacent(self.domain.low_voltage_layers_for_candidate(candidate), candidate.low_voltage_layers):
                values.append(replace(candidate, low_voltage_layers=int(layers)))
            for duct in self._adjacent(self.domain.low_voltage_duct_options, candidate.low_voltage_duct):
                values.append(replace(candidate, low_voltage_duct=duct))
        elif module == "hv":
            wire_options = (
                self._directional_wire_options("hv", candidate, violation_direction)
                if violation_name == "load_loss" else self._adjacent_wire_options("hv", candidate)
            )
            for wire in wire_options:
                values.append(self._replace_wire_option(candidate, "hv", wire))
            for layers in self._adjacent(self.domain.hv_layers, candidate.high_voltage_layers):
                values.append(replace(candidate, high_voltage_layers=int(layers)))
            for duct in self._adjacent(self.domain.high_voltage_duct_options, candidate.high_voltage_duct):
                values.append(replace(candidate, high_voltage_duct=duct))
        elif module == "cooling":
            for option in self._adjacent_cooling_options(candidate):
                values.append(replace(candidate, cooling_option=option))
        return [self._normalize_candidate(value) for value in values]

    def _guided_mutation(self, base: Individual, generation: int, parent_a: Individual, parent_b: Individual,
                         occupied_keys: set[str]) -> Individual:
        """针对一个主导约束、一个模块做有限相邻精算；无改善才回退随机变异。"""
        violation = self._dominant_violation(base.result)
        module = None if violation is None else self._primary_module_for_violation(base.result, violation)
        if module is None:
            # 严格合格（或没有可解释违规项）的候选没有“应修哪个模块”的诊断依据。
            # 不能把遍历全部模块的成本试探伪称为定向变异；本轮退回普通随机变异，
            # 轨迹来源也必须明确区分。
            fallback, metadata = self._random_mutation(base.candidate)
            metadata.update({"fallback_from": "no_diagnostic_direction", "primary_violation": None, "primary_module": None})
            if not self._is_novel_candidate(fallback, occupied_keys):
                return base
            return self._record(
                fallback, generation, "random_mutation_no_diagnostic_direction", metadata, parent_a, parent_b,
            )
        proposals = self._local_block_candidates(
            base.candidate, module, getattr(violation, "direction", None), getattr(violation, "name", None),
        )
        seen: set[str] = {self._candidate_key(base.candidate)}
        unique: list[tuple[ThreePhaseDesignCandidate, list[str], str, str]] = []

        def append_novel(
            source: Iterable[ThreePhaseDesignCandidate], probe_module: str, tier: str,
        ) -> None:
            for proposal in source:
                if len(unique) >= self.settings.local_probe_limit:
                    return
                key = self._candidate_key(proposal)
                if key in seen or key in self._seen_candidate_keys or key in occupied_keys:
                    continue
                unique.append((
                    proposal,
                    self._candidate_change_description(base.candidate, proposal, probe_module),
                    probe_module,
                    tier,
                ))
                seen.add(key)

        append_novel(proposals, module, "primary")
        probes = [
            self._record(
                proposal, generation, "diagnostic_probe",
                {
                    "type": "single_constraint_local_probe", "base_individual_id": base.individual_id,
                    "primary_violation": violation.name, "violation_direction": violation.direction,
                    "primary_module": module, "probe_module": probe_module, "probe_tier": tier,
                    "directional_policy": (
                        "负载损耗偏高：仅试相邻加宽/加厚/加粗线规"
                        if violation.name == "load_loss" and violation.direction == "upper" else
                        "负载损耗偏低：仅试相邻减宽/减厚/减细线规"
                        if violation.name == "load_loss" and violation.direction == "lower" else None
                    ),
                    "base_total_violation": str(base.result.total_violation),
                    "base_primary_normalized_excess": str(violation.normalized_excess), "change": change,
                }, parent_a, parent_b,
            )
            for proposal, change, probe_module, tier in unique
        ]
        choices = [base, *probes]
        best = min(choices, key=_population_key)
        if best is not base and _population_key(best) < _population_key(base):
            return best
        fallback, metadata = self._random_mutation(base.candidate)
        metadata.update({
            "fallback_from": "single_constraint_local_probe", "base_individual_id": base.individual_id,
            "primary_violation": violation.name, "primary_module": module,
            "base_total_violation": str(base.result.total_violation),
        })
        if not self._is_novel_candidate(fallback, occupied_keys):
            return base
        return self._record(fallback, generation, "random_mutation_fallback", metadata, parent_a, parent_b)

    def _make_child(self, parent_a: Individual, parent_b: Individual, generation: int,
                    occupied_keys: set[str]) -> Individual | None:
        """生成一个新候选；交叉/变异重现已评价候选时不写重复精算轨迹。"""
        if self.rng.random() < self.settings.crossover_rate:
            candidate, metadata = self._cross(parent_a.candidate, parent_b.candidate)
            source = "block_crossover"
        else:
            candidate, metadata, source = parent_a.candidate, {"type": "parent_copy"}, "parent_copy"
        if self.rng.random() >= self.settings.mutation_rate:
            # 复制是刻意的精英扩散行为，允许复用；块交叉若只重现旧组合，则交回
            # 主循环重试，避免把缓存命中误记成新的交叉探索。
            if source == "block_crossover" and not self._is_novel_candidate(candidate, occupied_keys):
                return None
            return self._record(candidate, generation, source, metadata, parent_a, parent_b)
        if (
            self.settings.guided_mutation_mode == "single_primary"
            and self.rng.random() < self.settings.guided_mutation_rate
        ):
            if not self._is_novel_candidate(candidate, occupied_keys):
                # 基体已精算过时，不再为同一基体重复开局部探测；改用一次普通新颖
                # 变异，仍失败则交由主循环重新选父代。
                mutated, mutation_metadata = self._random_mutation(candidate)
                mutation_metadata.update({"fallback_from": "duplicate_crossover_base", "before": metadata})
                if not self._is_novel_candidate(mutated, occupied_keys):
                    return None
                return self._record(mutated, generation, "random_mutation", mutation_metadata, parent_a, parent_b)
            # 基体的来源可能是耦合交叉或四块交叉，但此记录本身的角色是“等待
            # 诊断的基体”。若沿用交叉 ``type``，轨迹回放会把它误读为一次普通
            # 交叉结果，无法区分后续是否真正进入了定向局部探测。
            guided_base_metadata = {
                **metadata,
                "type": "guided_mutation_base",
                "base_creation_type": metadata.get("type"),
                "base_creation_mode": metadata.get("electromagnetic_mode"),
            }
            base = self._record(candidate, generation, "pre_guided_mutation", guided_base_metadata, parent_a, parent_b)
            return self._guided_mutation(base, generation, parent_a, parent_b, occupied_keys)
        mutated, mutation_metadata = self._random_mutation(candidate)
        mutation_metadata["before"] = metadata
        if not self._is_novel_candidate(mutated, occupied_keys):
            return None
        return self._record(mutated, generation, "random_mutation", mutation_metadata, parent_a, parent_b)

    def _elite_copies(self, population: list[Individual], generation: int) -> list[Individual]:
        source = self.archive.strict if self.archive.strict else self.archive.near
        source = source or sorted(population, key=_population_key)
        copied: list[Individual] = []
        for elite in source[:self.settings.elite_count]:
            copied.append(self._record(elite.candidate, generation, "elitism", {"type": "archive_elitism"}, elite))
        return copied

    def _refine_strict_elites(self, generation: int) -> list[Individual]:
        """全局 GA 后，对严格精英的有限相邻离散组合再精算。"""
        refined: list[Individual] = []
        remaining = self.settings.local_refine_limit
        for elite in self.archive.strict[:self.settings.local_refine_elites]:
            if remaining <= 0:
                break
            candidates: list[ThreePhaseDesignCandidate] = []
            for module in ("core", "lv", "hv", "cooling"):
                candidates.extend(self._local_block_candidates(elite.candidate, module))
            seen: set[str] = set()
            for candidate in candidates:
                key = json.dumps(candidate.canonical_dict(), sort_keys=True, default=str)
                if key in seen or remaining <= 0:
                    continue
                seen.add(key)
                refined.append(self._record(
                    candidate, generation, "neighborhood_refinement",
                    {"type": "strict_elite_neighbor", "base": elite.individual_id}, elite,
                ))
                remaining -= 1
        return refined

    def _archive_progress_key(self) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
        """严格解优先；尚未跨越可行边界时，以近可行向量判断是否仍在进步。"""
        if self.archive.strict:
            return (0, _cost(self.archive.strict[0].result), ZERO, ZERO, ZERO)
        if self.archive.near:
            result = self.archive.near[0].result
            return (1, result.total_violation, result.max_violation, Decimal(len(result.violations)), _cost(result))
        return (2, INFINITY, INFINITY, INFINITY, INFINITY)

    @staticmethod
    def _gene_group_values(individual: Individual) -> tuple[tuple[str, str], ...]:
        """返回全部可变基因模块的当前完整取值，用于工作种群的代表保留。

        这一步只保证每一类取值有机会进入后续父代池，不拆导线记录，也不把
        派生合法关系变成独立的任意组合。长圆铁芯的 conversionC 与铁芯记录
        绑定，避免跨铁芯错误比较。
        """
        candidate = individual.candidate
        cooling = "无" if candidate.cooling_option is None else json.dumps(
            candidate.canonical_dict().get("cooling_option"), ensure_ascii=False, sort_keys=True, default=str,
        )
        values: list[tuple[str, str]] = [
            ("硅钢牌号", str(candidate.steel_brand)),
            ("铁芯记录", str(candidate.core_data_id)),
            ("低压完整线规", f"{candidate.low_voltage_wire_type}:{candidate.low_voltage_wire_id}"),
            ("低压匝数", str(candidate.low_voltage_turns)),
            ("低压层数", str(candidate.low_voltage_layers)),
            ("低压油道", repr(candidate.low_voltage_duct)),
            ("高压完整线规", f"{candidate.high_voltage_wire_type}:{candidate.high_voltage_wire_id}"),
            ("高压层数", str(candidate.high_voltage_layers)),
            ("高压油道", repr(candidate.high_voltage_duct)),
            ("完整冷却型号", cooling),
        ]
        if candidate.core_type == 1:
            values.append(("长圆铁芯直线段 C", f"{candidate.core_data_id}:{candidate.conversion_c}"))
        return tuple(values)

    def _select_evolution_population(self, evaluated_initial: list[Individual]) -> list[Individual]:
        """从全量第 0 代中挑选后续遗传的优质且多样化工作种群。

        所有第 0 代候选已评价并已更新双档案。这里不重新精算、也不删除覆盖记录；
        仅决定哪些候选继续作为父代池。不可计算/未完成候选已提供“覆盖到但不能算”
        的诊断证据，却不能提供可比较的成本或超限向量；继续把它们选为父代会使
        后续交叉和随机注入反复围绕已知失败结构消耗精算。因此父代池只保留可计算且
        完整的候选。

        在这些候选中，先保留两类档案成员，再为每个可变模块的每种当前取值取一个
        最优代表，并以轮询方式交替加入，最后按总体排序补足。轮询不能让“某个模块
        的取值数量很多”或中文字段名的排序顺序独占工作种群。若第 0 代没有任何
        可计算候选，会直接给出明确错误，而不是让不可计算个体继续做无意义迭代。
        """
        calculable = [
            individual for individual in evaluated_initial
            if individual.result.calculable and individual.result.complete
        ]
        if len(calculable) < 2:
            raise RuntimeError(
                "第 0 代可计算且完整的候选不足两条；应先执行初代恢复采样，"
                "或检查硅钢磁密损耗表、绕组几何范围、高压层间纸档位和页面研究边界"
            )
        target = min(len(calculable), self.settings.population_size)
        ranked = sorted(calculable, key=_population_key)
        chosen: list[Individual] = []
        chosen_ids: set[str] = set()

        def add(individual: Individual) -> bool:
            if individual.individual_id in chosen_ids or len(chosen) >= target:
                return False
            chosen.append(individual)
            chosen_ids.add(individual.individual_id)
            return True

        # 档案是当前已经证实最有潜力的候选，必须优先进入工作种群。
        for individual in [*self.archive.strict, *self.archive.near]:
            add(individual)
        # 同一个取值会在很多背景下出现；在每个模块内只保留其中精算排序最好的
        # 一个代表。``ranked`` 已按评价优先级排序，因此第一次出现即为该取值代表。
        representatives: dict[str, dict[str, Individual]] = {}
        for individual in ranked:
            for module, value in self._gene_group_values(individual):
                representatives.setdefault(module, {}).setdefault(value, individual)

        # 一轮中每个模块至多尝试加入一条代表，随后再开始下一轮。这样即使低压
        # 线规有数百条，匝数、层数、油道、conversionC 等方向也会得到同等的
        # 首轮进入机会；去重后不足的位置自然由该模块的下一代表补上。
        queues = [list(values.values()) for values in representatives.values()]
        positions = [0] * len(queues)
        while len(chosen) < target:
            added_in_round = False
            for queue_index, queue in enumerate(queues):
                while positions[queue_index] < len(queue):
                    representative = queue[positions[queue_index]]
                    positions[queue_index] += 1
                    if add(representative):
                        added_in_round = True
                        break
                if len(chosen) >= target:
                    break
            if not added_in_round:
                break
        for individual in ranked:
            if len(chosen) >= target:
                break
            add(individual)
        if not chosen:
            raise RuntimeError("第 0 代没有可用于后续遗传的候选")
        return chosen

    def _initial_recovery_attempt_limit(self, original_initial_size: int) -> int:
        """为“初代没有可用父代”导出一次性恢复采样上限。

        此处不是普通运行预算，也不改变页面允许域。目标仅是从未见的完整合法组合中
        找到足以启动交叉的两个可计算候选。上限随第 0 代覆盖规模增长，并受估计合法
        候选数约束，避免在小域已枚举完或大域异常时无限抽样。
        """
        estimated = estimate_legal_candidate_count(self.domain)
        unseen_upper_bound = max(0, estimated - len(self._seen_candidate_keys))
        adaptive_limit = max(2_000, min(20_000, original_initial_size * 2))
        return min(unseen_upper_bound, adaptive_limit)

    def _recover_initial_calculable_candidates(
        self, evaluated_initial: list[Individual], original_initial_size: int,
    ) -> tuple[list[Individual], int, int]:
        """在原始第 0 代没有足够父代时，补抽未见合法完整组合。

        初代 A/B 覆盖必须原样保留，不能因为它没有撞到可计算组合就被删除或收缩。
        恢复阶段只从同一个 ``GeneDomain`` 取未见候选，仍遵守页面范围、完整目录记录
        和派生层数规则；它不会凭失败文本把某个材料/铁芯永久列为黑名单，因为同一个
        零件在不同绕组、油道和冷却组合下可能重新变为可计算。
        """
        target = 2  # 交叉至少需要两个不同的、可比较的父代。
        existing_calculable = sum(
            1 for item in evaluated_initial if item.result.calculable and item.result.complete
        )
        if existing_calculable >= target:
            return [], 0, 0
        limit = self._initial_recovery_attempt_limit(original_initial_size)
        if limit <= 0:
            return [], 0, limit

        recovered: list[Individual] = []
        attempts = 0
        self._start_progress_stage("第 0 代恢复采样：正在寻找可计算起点", 0, target)
        while existing_calculable < target and attempts < limit:
            attempts += 1
            candidate = self.domain.random_candidate(self.rng)
            if not self._is_novel_candidate(candidate):
                continue
            item = self._record(
                candidate, 0, "initial_recovery_sampling",
                {
                    "type": "initial_recovery_sampling",
                    "reason": "初代可计算完整候选不足两条",
                    "minimum_calculable_parents": target,
                    "attempt": attempts,
                    "attempt_limit": limit,
                },
            )
            recovered.append(item)
            if item.result.calculable and item.result.complete:
                existing_calculable += 1
        return recovered, attempts, limit

    def _finish_initial_recovery_exhausted(
        self, evaluated_initial: list[Individual], *, original_initial_size: int,
        recovery_evaluated: int, recovery_attempts: int, recovery_limit: int,
    ) -> OptimizationSummary:
        """恢复采样仍无两个可用父代时，留下可回放终止证据而非抛出裸异常。"""
        calculable_count = sum(
            1 for item in evaluated_initial if item.result.calculable and item.result.complete
        )
        reason = "初代恢复采样未找到两个可计算完整候选"
        self._record_population_outcomes(0, [])
        self._record_generation(
            0, evaluated_initial,
            f"第 0 代全量覆盖={original_initial_size}；恢复采样评价={recovery_evaluated}；"
            f"恢复抽样尝试={recovery_attempts}/{recovery_limit}；可计算完整候选={calculable_count}；{reason}",
        )
        self.trace.record_termination(
            self.run_id, 0, reason, self.cache.exact_evaluations, completed_generation=True,
        )
        self.trace.commit()
        self._initial_exact_evaluations = self.cache.exact_evaluations
        self._notify_progress(
            reason, 0, completed_generation=True, population_size=0,
            generated_records=len(evaluated_initial), run_status="已停止（无可计算起点）", stop_reason=reason,
        )
        return OptimizationSummary(
            self.run_id, Path(self.trace.conn.execute("PRAGMA database_list").fetchone()[2]), 0,
            self.cache.exact_evaluations, self._initial_exact_evaluations, self.cache.cache_hits,
            tuple(self.archive.strict), tuple(self.archive.near), tuple(), False,
            "initial_recovery_exhausted", False,
        )

    def _record_budget_stop_outcomes(self, generation: int) -> None:
        """预算在某代中途耗尽时，标记已评价但未形成完整种群的个体。"""
        rows = [
            {
                "individual_id": item.individual_id,
                "population_status": "not_retained",
                "population_reason": "evaluation_budget_hard_stop",
            }
            for item in self._created_by_generation.get(generation, [])
        ]
        if rows:
            self.trace.record_population_outcomes(self.run_id, generation, rows)

    def _finish_budget_stop(
        self, generation: int, population: list[Individual], *, partial_generation: bool,
    ) -> OptimizationSummary:
        """将预算硬截止作为正式终止状态落入轨迹并返回已有结果。"""
        created = self._created_by_generation.get(generation, [])
        stop_reason = self._budget_stop_reason()
        if partial_generation:
            self._record_budget_stop_outcomes(generation)
            if created:
                # 最后一条耗尽预算的精算结果仍是真实证据，应允许其更新档案；
                # 但本代不被伪装成完整种群。
                self._update_archive(generation, created, "evaluation_budget_hard_stop_partial")
            self._record_generation(
                generation, created,
                f"{stop_reason}；本代未完整生成，不进入后续演化",
            )
        self._record_final_archive_members(generation)
        self.trace.record_termination(
            self.run_id, generation, stop_reason, self.cache.exact_evaluations,
            completed_generation=not partial_generation,
        )
        self.trace.commit()
        self._notify_progress(
            stop_reason, generation, completed_generation=not partial_generation,
            population_size=len(population), generated_records=len(created),
            run_status="已完成（预算硬截止）", stop_reason=stop_reason,
        )
        initial = self._initial_exact_evaluations if self._initial_exact_evaluations is not None else self.cache.exact_evaluations
        return OptimizationSummary(
            self.run_id, Path(self.trace.conn.execute("PRAGMA database_list").fetchone()[2]), generation,
            self.cache.exact_evaluations, initial, self.cache.cache_hits,
            tuple(self.archive.strict), tuple(self.archive.near),
            tuple(population), False, self._budget_stop_kind(), True,
        )

    def run(self, initial_population: InitialPopulation) -> OptimizationSummary:
        if len(initial_population.candidates) < self.settings.population_size:
            raise ValueError("第 0 代候选数不能小于自动工作种群规模")
        initial_size = len(initial_population.candidates)
        evaluated_initial: list[Individual] = []
        self._start_progress_stage("正在精算第 0 代全量覆盖候选", 0, initial_size)
        for index, candidate in enumerate(initial_population.candidates):
            source = initial_population.sources[index] if index < len(initial_population.sources) else "legacy_initial_population"
            detail = initial_population.metadata[index] if index < len(initial_population.metadata) else {}
            try:
                evaluated_initial.append(self._record(candidate, 0, source, {"type": "initial_population", **detail}))
            except EvaluationBudgetExhausted:
                return self._finish_budget_stop(0, evaluated_initial, partial_generation=True)
        # 双档案必须见过完整覆盖层，不能只由后续工作种群决定。
        self._update_archive(0, evaluated_initial, "initial_population_full_coverage")
        recovery_items: list[Individual] = []
        recovery_attempts = 0
        recovery_limit = 0
        calculable_initial_count = sum(
            1 for item in evaluated_initial if item.result.calculable and item.result.complete
        )
        # 覆盖候选的任务是“把允许域真正算过一次”，不是保证一开始就碰到可行组合。
        # 只有不足两个父代时才补抽；两个是交叉能开始工作的最低结构性门槛。
        if calculable_initial_count < 2:
            try:
                recovery_items, recovery_attempts, recovery_limit = self._recover_initial_calculable_candidates(
                    evaluated_initial, initial_size,
                )
            except EvaluationBudgetExhausted:
                return self._finish_budget_stop(0, [*evaluated_initial, *recovery_items], partial_generation=True)
            if recovery_items:
                self._update_archive(0, recovery_items, "initial_recovery_sampling")
            evaluated_initial.extend(recovery_items)
            calculable_initial_count = sum(
                1 for item in evaluated_initial if item.result.calculable and item.result.complete
            )
            if calculable_initial_count < 2:
                return self._finish_initial_recovery_exhausted(
                    evaluated_initial, original_initial_size=initial_size,
                    recovery_evaluated=len(recovery_items), recovery_attempts=recovery_attempts,
                    recovery_limit=recovery_limit,
                )

        population = self._select_evolution_population(evaluated_initial)
        population_size = len(population)
        self._record_population_outcomes(0, population)
        recovery_note = (
            "；未触发恢复采样"
            if not recovery_items else
            f"；恢复采样评价={len(recovery_items)}，抽样尝试={recovery_attempts}/{recovery_limit}"
        )
        self._record_generation(
            0, evaluated_initial,
            f"第 0 代全量覆盖完成={initial_size}；可计算完整候选={calculable_initial_count}；"
            f"后续自动工作种群={population_size}{recovery_note}",
        )
        self.trace.commit()
        # 第 0 代覆盖到此完整结束；演化预算从此刻起才开始累计。
        self._initial_exact_evaluations = self.cache.exact_evaluations
        self._notify_progress("第 0 代全量覆盖完成，已选出后续工作种群", 0, completed_generation=True,
                              population_size=population_size, generated_records=self._progress_record_count)
        if self._budget_reached():
            return self._finish_budget_stop(0, population, partial_generation=False)

        best_progress = self._archive_progress_key()
        stagnant = 0
        completed = 0
        stopped = False
        for generation in range(1, self.settings.generations + 1):
            self._start_progress_stage(f"正在生成并精算第 {generation} 代", generation, population_size)
            try:
                next_population = self._elite_copies(population, generation)
            except EvaluationBudgetExhausted:
                return self._finish_budget_stop(generation, population, partial_generation=True)
            remaining = max(0, population_size - len(next_population))
            # 注入比例的分母始终是整代规模；精英较多时只以剩余槽位作为上限，
            # 不能悄悄把“10% 的整代探索”改成“10% 的剩余槽位探索”。
            try:
                injections = self._random_injections(generation, next_population, population_size, remaining)
            except EvaluationBudgetExhausted:
                return self._finish_budget_stop(generation, population, partial_generation=True)
            next_population.extend(injections)
            occupied_keys = {self._candidate_key(item.candidate) for item in next_population}
            duplicate_retries = 0
            while len(next_population) < population_size:
                parent_a, parent_b = self._select_parents(population)
                try:
                    child = self._make_child(parent_a, parent_b, generation, occupied_keys)
                except EvaluationBudgetExhausted:
                    return self._finish_budget_stop(generation, population, partial_generation=True)
                if child is not None and child.result.calculable and child.result.complete:
                    next_population.append(child)
                    occupied_keys.add(self._candidate_key(child.candidate))
                    duplicate_retries = 0
                    continue
                # 交叉、普通变异或诊断回退产生的不可计算候选仍然会写入轨迹和
                # 失败诊断，但不能占据下一代父代槽位；主循环继续寻找未见候选。
                duplicate_retries += 1
                if duplicate_retries < 24:
                    continue
                # 搜索域过小或父代已经高度收敛时，保留一次可审计的复制来填满
                # 工作种群，不能无限重试造成页面“看似卡住”。此来源不算新探索。
                try:
                    next_population.append(self._record(
                        parent_a.candidate, generation, "parent_copy_novelty_exhausted",
                        {"type": "parent_copy", "reason": "novel_candidate_retry_exhausted", "retries": duplicate_retries},
                        parent_a, parent_b,
                    ))
                except EvaluationBudgetExhausted:
                    return self._finish_budget_stop(generation, population, partial_generation=True)
                duplicate_retries = 0
            population = next_population
            self._record_population_outcomes(generation, population)
            self._update_archive(generation, population, "generation_population")
            completed = generation
            current_progress = self._archive_progress_key()
            if current_progress < best_progress:
                best_progress, stagnant = current_progress, 0
            else:
                stagnant += 1
            self._record_generation(generation, population, f"strict={len(self.archive.strict)}, near={len(self.archive.near)}")
            # 每代落盘，便于异常中断后仍保留已完成代的审计轨迹；不会改变种群。
            self.trace.commit()
            self._notify_progress(f"第 {generation} 代完成，双档案已更新", generation, completed_generation=True,
                                  population_size=population_size, generated_records=self._progress_record_count)
            if self._budget_reached():
                return self._finish_budget_stop(generation, population, partial_generation=False)
            if stagnant >= self.settings.stagnation_limit:
                stopped = True
                break

        try:
            refinement = self._refine_strict_elites(completed + 1)
        except EvaluationBudgetExhausted:
            return self._finish_budget_stop(completed + 1, population, partial_generation=True)
        if refinement:
            # 邻域细化只为改善档案而算，不是下一代种群；该语义必须显式留痕。
            self._record_population_outcomes(completed + 1, [])
            self._update_archive(completed + 1, refinement, "strict_elite_refinement")
            self._record_generation(completed + 1, refinement, "严格精英离散邻域细化")
            self.trace.commit()
            self._notify_progress("严格精英邻域细化完成", completed + 1, completed_generation=True,
                                  population_size=len(refinement), generated_records=len(refinement))
        if self._budget_reached():
            return self._finish_budget_stop(completed + 1, population, partial_generation=False)
        self._record_final_archive_members(completed + 1)
        termination_reason = ""
        if stopped:
            stop_metric = "最佳严格成本" if self.archive.strict else "近可行超限向量"
            termination_reason = f"连续 {self.settings.stagnation_limit} 代{stop_metric}无改进"
        else:
            termination_reason = "已完成最大代数"
        self.trace.record_termination(
            self.run_id, completed, termination_reason, self.cache.exact_evaluations,
            completed_generation=True,
        )
        self.trace.commit()
        if stopped:
            stop_reason = termination_reason
            self._notify_progress(
                f"已提前结束：{stop_reason}", completed, completed_generation=True,
                population_size=population_size, generated_records=self._progress_record_count,
                run_status="已完成（提前停止）", stop_reason=stop_reason,
            )
        else:
            self._notify_progress(
                "已完成最大代数，双档案已保存", completed, completed_generation=True,
                population_size=population_size, generated_records=self._progress_record_count,
                run_status="已完成",
            )
        return OptimizationSummary(
            self.run_id, Path(self.trace.conn.execute("PRAGMA database_list").fetchone()[2]), completed,
            self.cache.exact_evaluations, self._initial_exact_evaluations if self._initial_exact_evaluations is not None else self.cache.exact_evaluations,
            self.cache.cache_hits, tuple(self.archive.strict), tuple(self.archive.near),
            tuple(population), stopped, "stagnation" if stopped else "max_generations", False,
        )

    def _record_generation(self, generation: int, population: list[Individual], note: str) -> None:
        feasible_count = sum(1 for item in population if item.result.feasible and item.result.complete)
        self.trace.conn.execute(
            "INSERT OR REPLACE INTO generations VALUES (?, ?, ?, ?, ?, ?)",
            (self.run_id, generation, self.cache.exact_evaluations, self.cache.cache_hits, feasible_count, note),
        )
        best_strict_cost = None if not self.archive.strict else str(_cost(self.archive.strict[0].result))
        best_near_violation = None if not self.archive.near else str(self.archive.near[0].result.total_violation)
        self.trace.record_archive_snapshot(
            self.run_id, generation, len(self.archive.strict), len(self.archive.near),
            best_strict_cost, best_near_violation,
        )

    def _record_final_archive_members(self, generation: int) -> None:
        """写入最终双档案成员快照，回放时不再由历史事件猜测最终归属。"""
        rows: list[dict[str, object]] = []
        for membership, items in (("strict", self.archive.strict), ("near", self.archive.near)):
            for individual in items:
                candidate_key = self.trace.candidate_key(individual.candidate)
                rows.append({
                    "individual_id": individual.individual_id,
                    "candidate_key": candidate_key,
                    "archive_membership": membership,
                    "archive_reason": "final_archive_member",
                    "evaluation_status": DualArchive._evaluation_status(individual),
                    "first_strict_generation": self._first_strict_generation.get(candidate_key),
                })
        if rows:
            self.trace.record_archive_memberships(self.run_id, generation, "final_archive_snapshot", rows)


def run_optimization(
    output_path: Path,
    config_id: int,
    random_seed: int,
    domain: GeneDomain,
    initial_population: InitialPopulation,
    evaluator: Callable[[ThreePhaseDesignCandidate], EvaluationResult],
    settings: GASettings,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    precomputed_results: Mapping[str, EvaluationResult] | None = None,
    run_id: str | None = None,
    formula_revision: str | None = None,
    run_metadata: Mapping[str, object] | None = None,
) -> OptimizationSummary:
    """完整 GA 的外部入口；所有运行参数和轨迹均保存在输出 SQLite。"""
    run_id = run_id or uuid.uuid4().hex
    trace = TraceStore(output_path)
    started_at = time.monotonic()
    try:
        recorded_settings = {
            **settings.__dict__,
            "initial_preview_reuse": bool(precomputed_results),
            "initial_preview_reused_candidate_count": 0 if precomputed_results is None else len(precomputed_results),
            "provenance": runtime_provenance(formula_revision=formula_revision, entrypoint="run_optimization"),
            **dict(run_metadata or {}),
        }
        trace.create_run(
            run_id, random_seed, config_id, "dual_archive_discrete_ga",
            domain.summary(),
            recorded_settings,
        )
        optimizer = ThreePhaseGeneticOptimizer(
            domain, evaluator, settings, random.Random(random_seed), trace, run_id,
            progress_callback=progress_callback, precomputed_results=precomputed_results,
        )
        summary = optimizer.run(initial_population)
        elapsed_seconds = time.monotonic() - started_at
        trace.merge_run_settings(run_id, {"elapsed_seconds": elapsed_seconds})
        return replace(summary, elapsed_seconds=elapsed_seconds)
    finally:
        trace.close()
