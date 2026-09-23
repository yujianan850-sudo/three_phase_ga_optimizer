"""跨配置历史方案的只读检索与种子预筛选。

这不是“把旧方案直接当作当前结果”。本模块只根据配置相似度、Java 已保存的
合规等级和价格预筛选来源记录；每一条入选记录仍须映射为完整目录 ID、通过当前
``GeneDomain`` 的边界校验，并由当前配置的精算器重新计算。

模块刻意不写业务数据库，也不依赖 Python 的运行轨迹数据库。这样可先作为 CLI 和
页面共同使用的适配层，后续再把入选数量同自动种群预算统一起来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from calculation.models import ThreePhaseDesignCandidate
from calculation.record_adapter import resolve_record_candidate
from ga.gene_codec import GeneDomain


@dataclass(frozen=True)
class SimilarConfigMatch:
    """一个历史配置与当前配置的可解释相似度结果。"""

    config_id: int
    score: Decimal
    matched_features: tuple[str, ...]
    different_features: tuple[str, ...]
    feature_scores: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class SimilarHistorySeed:
    """通过当前搜索域校验、可以作为第 0 代候选的历史种子。"""

    candidate: ThreePhaseDesignCandidate
    source_config_id: int
    source_record_id: int
    source_compliance_level: str | None
    source_price: Decimal | None
    config_similarity: Decimal
    matched_features: tuple[str, ...]
    different_features: tuple[str, ...]
    feature_scores: tuple[tuple[str, Decimal], ...]


def _nested(mapping: dict[str, Any], section: str, field: str) -> Any:
    value = mapping.get(section)
    return value.get(field) if isinstance(value, dict) else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _relative_similarity(current: Any, history: Any, tolerance: Decimal) -> Decimal:
    """连续数值的相似度；缺失不伪造为相同，返回 0 分。"""
    current_value, history_value = _decimal(current), _decimal(history)
    if current_value is None or history_value is None:
        return Decimal("0")
    denominator = max(abs(current_value), Decimal("1"))
    distance = abs(current_value - history_value) / denominator
    return max(Decimal("0"), Decimal("1") - distance / tolerance)


def _exact_similarity(current: Any, history: Any) -> Decimal:
    return Decimal("1") if current is not None and history is not None and current == history else Decimal("0")


def _list_similarity(current: Any, history: Any) -> Decimal:
    """多选项采用 Jaccard 相似度；空值不会被误判为相同。"""
    if not isinstance(current, list) or not isinstance(history, list) or not current or not history:
        return Decimal("0")
    left, right = {str(item) for item in current}, {str(item) for item in history}
    return Decimal(len(left & right)) / Decimal(len(left | right))


# 名称、权重和阈值均集中在这里，避免把“相似”散落在 CLI/UI 里而无法追溯。
# 权重总和为 1。它们是当前可执行的初始规则，不代表已经由历史数据标定。
_FEATURES: tuple[tuple[str, Decimal, str, str, Decimal | None], ...] = (
    ("频率", Decimal("0.08"), "performance_index", "frequency", None),
    ("容量", Decimal("0.16"), "performance_index", "capacity", Decimal("0.25")),
    ("高压额定电压", Decimal("0.10"), "performance_index", "highVoltageRated", Decimal("0.20")),
    ("低压额定电压", Decimal("0.08"), "performance_index", "lowVoltageRated", Decimal("0.20")),
    ("绕法类型", Decimal("0.10"), "performance_index", "windingMethodType", None),
    ("联结方式", Decimal("0.08"), "performance_index", "windingConnection", None),
    ("铁芯形状", Decimal("0.08"), "craft_core", "isRolledCore", None),
    ("铁芯直径范围", Decimal("0.08"), "craft_core", "coreDiameterMin", Decimal("0.30")),
    ("油箱类型", Decimal("0.08"), "craft_fuel_tank", "tankType", None),
    ("低压导线类别", Decimal("0.08"), "craft_low_coil", "wireSpecification", None),
    ("高压导线类别", Decimal("0.08"), "craft_high_coil", "wireSpecification", None),
)


def compare_config_similarity(target: dict[str, Any], history: dict[str, Any]) -> SimilarConfigMatch | None:
    """比较两个真实三相配置，返回可解释的 0–1 分数。

    ``transformer_id`` 与频率是硬门槛：前者防止跨产品类型混用，后者避免把 50/60Hz
    的损耗基础直接混在一起。其余特征只影响排序；历史导线类别不同的方案最终仍会被
    当前 ``GeneDomain`` 拒绝或接受，因此这里不把它提前误当硬编码业务规则。
    """
    if target.get("transformer_id") != history.get("transformer_id"):
        return None
    if _nested(target, "performance_index", "frequency") != _nested(history, "performance_index", "frequency"):
        return None

    score = Decimal("0")
    matched: list[str] = []
    different: list[str] = []
    feature_scores: list[tuple[str, Decimal]] = []
    for label, weight, section, field, tolerance in _FEATURES:
        current, old = _nested(target, section, field), _nested(history, section, field)
        if tolerance is None:
            similarity = _exact_similarity(current, old)
        else:
            similarity = _relative_similarity(current, old, tolerance)
        score += weight * similarity
        feature_scores.append((label, similarity))
        (matched if similarity == Decimal("1") else different).append(label)

    # 页面中允许多选的牌号不按排列顺序比较。
    steel_similarity = _list_similarity(
        _nested(target, "craft_core", "siliconSteelGrade"),
        _nested(history, "craft_core", "siliconSteelGrade"),
    )
    steel_weight = Decimal("0.08")
    # 上表的权重已合计 1；硅钢牌号是在“铁芯形状”的一半权重内细分，保持总分 1。
    score -= Decimal("0.04") * _exact_similarity(
        _nested(target, "craft_core", "isRolledCore"), _nested(history, "craft_core", "isRolledCore"),
    )
    score += Decimal("0.04") * steel_similarity
    feature_scores.append(("硅钢片牌号", steel_similarity))
    (matched if steel_similarity == Decimal("1") else different).append("硅钢片牌号")
    return SimilarConfigMatch(
        config_id=int(history["id"]), score=max(Decimal("0"), min(Decimal("1"), score)),
        matched_features=tuple(matched), different_features=tuple(different), feature_scores=tuple(feature_scores),
    )


def suggested_history_seed_count(domain: GeneDomain) -> int:
    """按当前离散搜索域复杂度建议保留 5–30 条跨配置历史种子。

    此值只决定“最多尝试多少历史记录”，不是最终种群大小，也不会覆盖 A/B 覆盖候选。
    5 条对应小域，之后每增加一个数量级增加 5 条，上限 30；后续应使用真实精算
    对比数据校准，而不是把它宣传为最优参数。
    """
    dimensions = (
        len(domain.steel_brands), len(domain.cores), len(domain.lv_wire_options), len(domain.hv_wire_options),
        len(domain.lv_turns), len(domain.hv_layers), len(domain.lv_ducts), len(domain.hv_ducts),
        max(1, len(domain.cooling_options)),
    )
    complexity = math.prod(max(1, value) for value in dimensions)
    return max(5, min(30, 5 * max(1, math.ceil(math.log10(complexity)))))


def _candidate_in_target_domain(candidate: ThreePhaseDesignCandidate, domain: GeneDomain) -> ThreePhaseDesignCandidate:
    """补足 ``normalize_candidate_wire_types`` 不负责的整数边界校验。"""
    normalized = domain.normalize_candidate_wire_types(candidate)
    if normalized.low_voltage_turns not in domain.lv_turns:
        raise ValueError("历史低压匝数不在当前页面允许范围")
    if normalized.high_voltage_layers not in domain.hv_layers:
        raise ValueError("历史高压层数不在当前页面允许范围")
    if normalized.low_voltage_layers not in domain.low_voltage_layers_for_candidate(normalized):
        raise ValueError("历史低压层数不满足当前导线类别规则")
    if normalized.steel_brand not in domain.steel_brands:
        raise ValueError("历史硅钢片牌号不在当前页面允许范围")
    return normalized


def select_similar_history_seeds(
    target_config: dict[str, Any], configs_by_id: dict[int, dict[str, Any]],
    records_by_config: dict[int, list[dict[str, Any]]], catalog: dict[str, list[dict[str, Any]]],
    domain: GeneDomain, *, limit: int | None = None,
) -> list[SimilarHistorySeed]:
    """从所有其它三相配置中筛选可直接进入当前 GA 的历史种子。

    排序顺序：配置相似度降序、``FULL`` 合规优先、Java 冗余价格升序、记录 ID。
    同一完整候选只保留一条来源记录。返回值仍须在调用处以当前配置重新精算。
    """
    requested = suggested_history_seed_count(domain) if limit is None else int(limit)
    if requested < 1:
        return []
    target_id = int(target_config.get("id", -1))
    ranked_configs: dict[int, SimilarConfigMatch] = {}
    for config_id, history_config in configs_by_id.items():
        if int(config_id) == target_id:
            continue
        match = compare_config_similarity(target_config, history_config)
        if match is not None:
            ranked_configs[int(config_id)] = match

    candidates: list[SimilarHistorySeed] = []
    for source_config_id, match in ranked_configs.items():
        source_config = configs_by_id[source_config_id]
        for record in records_by_config.get(source_config_id, []):
            try:
                restored = resolve_record_candidate(record, catalog, source_config)
                # 跨配置时不能把“旧记录未保存的冷却选择”静默替换为当前目录第一项。
                # 那不是历史方案本身，只能在未来明确标记为“投影种子”后另行支持。
                if domain.cooling_options and restored.cooling_option is None:
                    continue
                current_candidate = _candidate_in_target_domain(restored, domain)
            except (KeyError, LookupError, TypeError, ValueError):
                continue
            candidates.append(SimilarHistorySeed(
                candidate=current_candidate, source_config_id=source_config_id,
                source_record_id=int(record["id"]),
                source_compliance_level=(None if record.get("compliance_level") is None else str(record["compliance_level"])),
                source_price=_decimal(record.get("price")), config_similarity=match.score,
                matched_features=match.matched_features, different_features=match.different_features,
                feature_scores=match.feature_scores,
            ))

    def sort_key(seed: SimilarHistorySeed) -> tuple[Decimal, int, Decimal, int]:
        # None 的价格不是“免费”，只能排在有真实价格的记录之后。
        compliance_rank = 0 if seed.source_compliance_level == "FULL" else 1
        price = seed.source_price if seed.source_price is not None else Decimal("Infinity")
        return -seed.config_similarity, compliance_rank, price, seed.source_record_id

    seen: set[str] = set()
    selected: list[SimilarHistorySeed] = []
    for seed in sorted(candidates, key=sort_key):
        key = str(seed.candidate.canonical_dict())
        if key in seen:
            continue
        seen.add(key)
        selected.append(seed)
        if len(selected) == requested:
            break
    return selected
