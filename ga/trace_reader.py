"""只读解析 GA / 初始种群 SQLite 轨迹，供实验台离线回放。

该模块不导入数据库仓储和精算器。即使 Java 后端或 MySQL 没有启动，研究者仍可
在 Streamlit 页面中查看此前某次实验的参数、代际统计、算子构成和候选结果。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SOURCE_LABELS = {
    "history_seed": "历史种子",
    "stratified_random_seed": "分层随机种子",
    "block_crossover": "参数块交叉",
    "effective_domain_random_injection": "经验有效域随机注入",
    "random_injection_raw_fallback": "原始搜索域随机注入（经验池不足）",
    "random_injection_raw_only": "原始搜索域随机注入（消融策略强制）",
    "parent_copy": "父代复制",
    "parent_copy_novelty_exhausted": "新颖候选重试耗尽后的父代复制",
    "initial_recovery_sampling": "第 0 代可计算起点恢复采样",
    "pre_guided_mutation": "定向变异前候选",
    "diagnostic_probe": "主导约束的单模块局部探测",
    "random_mutation": "随机离散变异",
    "random_mutation_no_diagnostic_direction": "无可用诊断方向时的随机变异",
    "random_mutation_fallback": "定向变异回退",
    "random_injection": "每代随机注入候选",
    "elitism": "精英保留",
    "neighborhood_refinement": "严格精英邻域细化",
}

ALGORITHM_LABELS = {
    "initial_population_preview": "第 0 代覆盖与精算预览",
    "dual_archive_discrete_ga": "完整双档案遗传优化",
}

_COVERAGE_DIMENSION_LABELS = {
    "steel_brand": "硅钢牌号", "core": "铁芯记录", "lv_wire": "低压完整线规",
    "hv_wire": "高压完整线规", "lv_turns": "低压匝数", "lv_layers": "低压层数",
    "hv_layers": "高压层（段）数", "conversion_c": "长圆铁芯直线段 C 值",
    "lv_duct": "低压油道", "hv_duct": "高压油道", "cooling": "完整冷却型号",
}

_CONSTRAINT_LABELS = {
    "flux_density": "铁芯磁密", "no_load_loss": "空载损耗 P0", "load_loss": "负载损耗 PK",
    "impedance": "短路阻抗 UK", "oil_top_temp_rise": "油顶温升",
    "lv_temp_rise": "低压绕组温升", "hv_temp_rise": "高压绕组温升",
    "corrugation_expansion": "波纹片可补偿油膨胀量", "radiator_center_distance": "散热器中心距",
    "lv_current_density": "低压导线电流密度", "hv_current_density": "高压导线电流密度",
    "lv_reactance_height": "低压绕组高度", "hv_reactance_height": "高压绕组高度",
    "lv_wire_thickness": "低压扁线厚度", "hv_wire_thickness": "高压扁线厚度",
    "lv_wire_width": "低压扁线宽度", "hv_wire_width": "高压扁线宽度",
    "lv_wire_aspect_ratio": "低压扁线宽厚比", "hv_wire_aspect_ratio": "高压扁线宽厚比",
    "lv_layer_count": "低压层数", "hv_layer_count": "高压层（段）数",
}

_LOW_VOLTAGE_WIRE_TYPE_LABELS = {0: "LM（类别 0）", 1: "TM（类别 1）", 2: "ZBL（类别 2）", 3: "ZB（类别 3）"}
_HIGH_VOLTAGE_WIRE_TYPE_LABELS = {
    0: "QZ（类别 0）", 1: "QZL / QQL（类别 1）", 2: "ZBL（类别 2）", 3: "ZB（类别 3）",
    4: "QZB（类别 4）", 5: "QZBL / QQLB（类别 5）",
}


def _coverage_dimension_label(name: str) -> str:
    return _COVERAGE_DIMENSION_LABELS.get(name, name)


def source_label(source: str) -> str:
    """把内部来源编码转为研究者可读的候选生成方式。"""
    if source.startswith("coverage_background:"):
        return "多背景基线候选"
    if source.startswith("coverage_single:"):
        return f"A 层单项覆盖：{_coverage_dimension_label(source.split(':', 1)[1])}"
    if source.startswith("coverage_pair:"):
        pair = source.rsplit(":", 1)[-1]
        if "×" in pair:
            left, right = pair.split("×", 1)
            return f"B 层关键成对覆盖：{_coverage_dimension_label(left)} × {_coverage_dimension_label(right)}"
        return "B 层关键成对覆盖"
    if source.startswith("history_seed:similar_config"):
        return "相似配置历史种子"
    if source.startswith("history_seed:") or source == "history_seed":
        return "当前配置历史种子"
    if source == "random_seed":
        return "第 0 代随机合法候选"
    return SOURCE_LABELS.get(source, source)


def source_explanation(label: str) -> str:
    """来源汇总旁的中文说明，避免把“算子/来源”误当成工程指标。"""
    if label == "多背景基线候选":
        return "作为 A/B 覆盖的不同固定背景，避免所有替换都只围绕同一基线。"
    if label.startswith("A 层单项覆盖"):
        return "固定其它结构，只替换这一项；用于保证每个允许取值至少被精算一次。"
    if label.startswith("B 层关键成对覆盖"):
        return "同时替换这一对强耦合结构项；不是全部参数的笛卡尔积。"
    if label == "当前配置历史种子":
        return "从当前产品已保存且能还原为完整目录记录的历史方案引入。"
    if label == "相似配置历史种子":
        return "由相似产品配置评分筛出的历史结构，进入后仍按当前产品重新精算。"
    if label == "第 0 代随机合法候选":
        return "从当前搜索域随机抽取此前未出现的完整合法组合，补充探索范围。"
    if label == "第 0 代可计算起点恢复采样":
        return "原始第 0 代不足两个可计算完整候选时，在同一页面允许域补抽未见完整组合；它不扩大搜索域，也不永久排除材料或线规。"
    if label == "经验有效域随机注入":
        return "从本次已真实精算且可完成计算的候选中随机重组结构电磁块或冷却块，再筛除已见完整组合。"
    if label == "原始搜索域随机注入（经验池不足）":
        return "尚未形成足够的可计算经验候选时，从页面原始搜索域随机抽取完整合法组合。"
    if label == "原始搜索域随机注入（消融策略强制）":
        return "本次消融策略明确关闭经验有效域注入，始终从页面原始搜索域抽取完整合法组合。"
    return "遗传主循环中产生或保留的候选方式；具体父代与操作可展开单个候选查看。"


def population_decision_label(status: str | None, reason: str | None) -> str:
    """把种群去向转换为回放页面可直接理解的中文事实。"""
    labels = {
        ("retained", "population_retained"): "进入本代工作种群",
        ("not_retained", "initial_coverage_not_selected_for_evolution"): "已完成第 0 代覆盖，未选入后续遗传",
        ("not_retained", "not_calculable_or_incomplete"): "已精算但不可计算或结果不完整，不能进入父代池",
        ("not_retained", "diagnostic_probe_not_selected"): "仅用于定向局部探测，未作为子代保留",
        ("not_retained", "guided_base_not_selected"): "定向变异前的基准候选，未作为子代保留",
        ("not_retained", "refinement_only"): "仅用于最终邻域细化，不进入种群",
        ("not_retained", "operator_output_not_selected"): "算子生成但未选入本代种群",
        ("not_retained", "evaluation_budget_hard_stop"): "实际精算预算已耗尽，本代未完整生成",
    }
    return labels.get((status, reason), "该轨迹未保存种群去向" if status is None else "未进入本代种群")


def archive_entry_last_action_label(operator: dict[str, Any], cache_hit: bool) -> str:
    """显示入档前最后创建动作，不将操作链误说成算子因果贡献。"""
    if cache_hit or operator.get("archive_entry_last_action_reason") == "cache_hit_no_new_operator":
        return "不适用：缓存命中，未触发新的算子"
    if operator.get("archive_entry_last_action_reason") == "elitism_copy_no_new_operator":
        return "不适用：精英复制，未创建新候选"
    action = operator.get("archive_entry_last_action")
    return source_label(str(action)) if action else "未记录"


def archive_membership_label(membership: str | None) -> str:
    """把严格/近可行档案的内部值转为回放页可读文字。"""
    return {"strict": "严格合格档案", "near": "近可行档案"}.get(
        membership, "未进入档案",
    )


def archive_reason_label(reason: str | None) -> str:
    """展示写入当时真实发生的档案去向，避免暴露英文内部枚举。"""
    labels = {
        "strict_retained": "严格合格，已保留在严格档案",
        "near_retained": "可计算但有约束超限，已保留在近可行档案",
        "not_calculable": "未完成可计算条件，未进入档案",
        "incomplete": "计算结果不完整，未进入档案",
        "archive_duplicate_dominated": "与同一完整方案重复，且该副本排序更差",
        "archive_duplicate_equivalent": "与已收录的同一完整方案等价，不重复收录",
        "archive_superseded_by_strict": "同一完整方案已有严格合格结果，近可行副本不保留",
        "archive_capacity_eliminated": "满足入档条件，但档案容量已由更优候选占满",
        "archive_not_selected": "未被本次档案更新保留",
        "archive_not_considered_auxiliary": "仅作为局部探测或变异前基准，不参与本次档案更新",
        "final_archive_member": "运行结束时仍保留在最终档案",
    }
    return labels.get(reason, "该轨迹未保存档案去向" if reason is None else f"未识别档案状态：{reason}")


def _candidate_structure_text(candidate: dict[str, Any]) -> str:
    """回放总表只保留足以识别方案结构的中文摘要，完整 JSON 留给详情区。"""
    if not candidate:
        return "候选结构未保存"
    core = candidate.get("core_data_id", "—")
    low_wire = candidate.get("low_voltage_wire_id", "—")
    high_wire = candidate.get("high_voltage_wire_id", "—")
    low_type = candidate.get("low_voltage_wire_type")
    high_type = candidate.get("high_voltage_wire_type")
    try:
        low_label = _LOW_VOLTAGE_WIRE_TYPE_LABELS.get(int(low_type), f"未知低压类别 {low_type}")
    except (TypeError, ValueError):
        low_label = "低压类别未记录"
    try:
        high_label = _HIGH_VOLTAGE_WIRE_TYPE_LABELS.get(int(high_type), f"未知高压类别 {high_type}")
    except (TypeError, ValueError):
        high_label = "高压类别未记录"
    return (
        f"铁芯 #{core}；低压 {low_label} · 线规 #{low_wire}（{candidate.get('low_voltage_turns', '—')} 匝 / "
        f"{candidate.get('low_voltage_layers', '—')} 层）；高压 {high_label} · 线规 #{high_wire}"
        f"（{candidate.get('high_voltage_layers', '—')} 层（段））"
    )


@dataclass(frozen=True)
class TraceFile:
    path: Path
    modified_at: float
    bytes_size: int


def discover_trace_files(outputs_dir: Path) -> list[TraceFile]:
    """按最近修改时间返回本地 SQLite 实验结果，不扫描或修改业务数据库。"""
    if not outputs_dir.exists():
        return []
    return sorted(
        (TraceFile(path, path.stat().st_mtime, path.stat().st_size) for path in outputs_dir.glob("*.sqlite3")),
        key=lambda item: item.modified_at,
        reverse=True,
    )


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"轨迹文件不存在: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _decimal_text(value: Any) -> str:
    return "—" if value is None else str(value)


def _evaluation_status(result: dict[str, Any]) -> str:
    """旧轨迹没有审计表时的只读降级标签；不把它当作真实档案去向。"""
    if not result.get("calculable"):
        return "not_calculable"
    if not result.get("complete"):
        return "incomplete"
    if result.get("feasible"):
        return "strict_feasible"
    return "constraint_violation"


def read_trace_overview(path: Path) -> dict[str, Any]:
    """读取轨迹首屏需要的轻量摘要，不下载或解析每条候选的大 JSON。

    大型实验轨迹的 `individuals` 表可能达到数万条；进入回放页时只需要
    运行信息、候选数量、来源汇总和代际快照。候选结构、父代与完整精算结果
    则由 :func:`read_trace` 在用户主动打开候选视图时再读取。
    """
    conn = _connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"runs", "generations", "individuals"}
        missing = required - tables
        if missing:
            raise ValueError(f"不是可识别的实验轨迹，缺少表: {', '.join(sorted(missing))}")
        row = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            raise ValueError("轨迹中没有运行记录")
        run = dict(row)
        run["domain"] = _json(run.pop("domain_json", None))
        run["settings"] = _json(run.pop("settings_json", None))
        run_id = run["run_id"]
        trace_kind = str(run.get("algorithm"))
        is_initial_preview = trace_kind == "initial_population_preview"
        run["algorithm_label"] = ALGORITHM_LABELS.get(trace_kind, trace_kind)
        generations = [dict(item) for item in conn.execute(
            "SELECT generation, exact_evaluations, cache_hits, feasible_count, note "
            "FROM generations WHERE run_id = ? ORDER BY generation", (run_id,)
        )]
        archive_snapshots = []
        if "archive_snapshots" in tables:
            archive_snapshots = [dict(item) for item in conn.execute(
                "SELECT generation, strict_count, near_count, best_strict_cost, best_near_violation "
                "FROM archive_snapshots WHERE run_id = ? ORDER BY generation", (run_id,)
            )]
        termination: dict[str, Any] | None = None
        if "run_terminations" in tables:
            row = conn.execute(
                "SELECT generation, reason, exact_evaluations, completed_generation, recorded_at "
                "FROM run_terminations WHERE run_id = ?", (run_id,),
            ).fetchone()
            termination = None if row is None else dict(row)
        individual_count = int(conn.execute(
            "SELECT COUNT(*) FROM individuals WHERE run_id = ?", (run_id,)
        ).fetchone()[0])
        source_counts: dict[str, int] = {}
        for source, count in conn.execute(
            "SELECT source, COUNT(*) FROM individuals WHERE run_id = ? GROUP BY source", (run_id,)
        ):
            label = source_label(str(source))
            source_counts[label] = source_counts.get(label, 0) + int(count)
        feasible_count: int | None = None
        try:
            feasible_count = int(conn.execute(
                "SELECT COUNT(*) FROM individuals WHERE run_id = ? "
                "AND json_extract(result_json, '$.feasible') = 1 "
                "AND json_extract(result_json, '$.complete') = 1", (run_id,),
            ).fetchone()[0])
        except sqlite3.OperationalError:
            # 少数 SQLite 环境没有 JSON1；此时不为首屏回退到逐条解析。
            feasible_count = None
        final_archive_counts = {"strict": 0, "near": 0}
        final_archive_available = False
        if "archive_memberships" in tables:
            final_rows = list(conn.execute(
                "SELECT archive_membership, COUNT(*) FROM archive_memberships "
                "WHERE run_id = ? AND stage = 'final_archive_snapshot' GROUP BY archive_membership", (run_id,)
            ))
            final_archive_available = bool(final_rows)
            for membership, count in final_rows:
                if membership in final_archive_counts:
                    final_archive_counts[str(membership)] = int(count)
    finally:
        conn.close()
    return {
        "file_name": path.name,
        "file_path": str(path),
        "run": run,
        "is_initial_preview": is_initial_preview,
        "generations": generations,
        "archive_snapshots": archive_snapshots,
        "summary": {
            "individual_count": individual_count,
            "feasible_count": feasible_count,
            "source_counts": source_counts,
            "source_explanations": {label: source_explanation(label) for label in source_counts},
            "final_archive_counts": final_archive_counts,
            "final_archive_available": final_archive_available,
            "termination": termination,
        },
        "overview_only": True,
    }


def read_trace(path: Path) -> dict[str, Any]:
    """读取单一 SQLite 轨迹的页面展示模型。

    返回值刻意是标准 dict/list，避免 UI 层依赖 pandas；也便于后续导出 JSON。
    """
    conn = _connect(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"runs", "generations", "individuals"}
        missing = required - tables
        if missing:
            raise ValueError(f"不是可识别的实验轨迹，缺少表: {', '.join(sorted(missing))}")

        run_rows = [dict(row) for row in conn.execute("SELECT * FROM runs ORDER BY created_at DESC")]
        if not run_rows:
            raise ValueError("轨迹中没有运行记录")
        run = run_rows[0]
        run["domain"] = _json(run.pop("domain_json", None))
        run["settings"] = _json(run.pop("settings_json", None))
        run_id = run["run_id"]
        trace_kind = str(run.get("algorithm"))
        is_initial_preview = trace_kind == "initial_population_preview"
        run["algorithm_label"] = ALGORITHM_LABELS.get(trace_kind, trace_kind)

        generations = [dict(row) for row in conn.execute(
            "SELECT generation, exact_evaluations, cache_hits, feasible_count, note "
            "FROM generations WHERE run_id = ? ORDER BY generation", (run_id,)
        )]
        archive_snapshots = []
        if "archive_snapshots" in tables:
            archive_snapshots = [dict(row) for row in conn.execute(
                "SELECT generation, strict_count, near_count, best_strict_cost, best_near_violation "
                "FROM archive_snapshots WHERE run_id = ? ORDER BY generation", (run_id,)
            )]
        termination: dict[str, Any] | None = None
        if "run_terminations" in tables:
            row = conn.execute(
                "SELECT generation, reason, exact_evaluations, completed_generation, recorded_at "
                "FROM run_terminations WHERE run_id = ?", (run_id,),
            ).fetchone()
            termination = None if row is None else dict(row)
        population_outcomes: dict[str, dict[str, Any]] = {}
        if "population_outcomes" in tables:
            population_outcomes = {
                str(row["individual_id"]): dict(row)
                for row in conn.execute(
                    "SELECT individual_id, generation, population_status, population_reason "
                    "FROM population_outcomes WHERE run_id = ?", (run_id,)
                )
            }
        archive_events: list[dict[str, Any]] = []
        if "archive_memberships" in tables:
            archive_events = [dict(row) for row in conn.execute(
                "SELECT event_id, individual_id, generation, stage, archive_membership, archive_reason, "
                "evaluation_status, first_strict_generation "
                "FROM archive_memberships WHERE run_id = ? ORDER BY event_id", (run_id,)
            )]
        latest_archive_event: dict[str, dict[str, Any]] = {}
        for event in archive_events:
            latest_archive_event[str(event["individual_id"])] = event
        # 新版完整 GA 在结束时额外写一份最终成员快照；只有它能准确回答
        # “最终档案里现在有哪些候选”。旧事件仅描述某一时刻的处理结果。
        final_archive_events = [event for event in archive_events if event.get("stage") == "final_archive_snapshot"]
        raw_individuals = [dict(row) for row in conn.execute(
            "SELECT individual_id, generation, parent_a_id, parent_b_id, source, operator_json, "
            "candidate_json, result_json, cache_hit, selected_archive, created_at "
            "FROM individuals WHERE run_id = ? ORDER BY generation, created_at", (run_id,)
        )]
    finally:
        conn.close()

    individuals: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    calculable = feasible = 0
    for item in raw_individuals:
        candidate = _json(item.pop("candidate_json", None))
        result = _json(item.pop("result_json", None))
        operator = _json(item.pop("operator_json", None))
        source = str(item["source"])
        display_source = source_label(source)
        source_counts[display_source] = source_counts.get(display_source, 0) + 1
        if result.get("calculable"):
            calculable += 1
        if result.get("feasible") and result.get("complete"):
            feasible += 1
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        violations = result.get("violations") if isinstance(result.get("violations"), list) else []
        violation_total = sum(float(row.get("normalized_excess", 0) or 0) for row in violations)
        individual_id = str(item["individual_id"])
        population = population_outcomes.get(individual_id, {})
        archive = latest_archive_event.get(individual_id, {})
        individuals.append({
            **item,
            "source_label": display_source,
            "candidate_structure": _candidate_structure_text(candidate),
            "operator": operator,
            "archive_entry_last_action": archive_entry_last_action_label(operator, bool(item.get("cache_hit"))),
            "candidate": candidate,
            "result": result,
            "cost": metrics.get("price"),
            "p0": metrics.get("crgopo"),
            "pk": metrics.get("hvlvpk"),
            "uk": metrics.get("ukk"),
            "total_violation": violation_total,
            "violation_names": "、".join(
                _CONSTRAINT_LABELS.get(str(row.get("name", "")), str(row.get("name", "未知")))
                for row in violations
            ) or "无",
            # 第 0 代预览从未执行 GA 选择或档案更新，因此必须是“未执行”，而
            # 不是把轨迹格式兼容字段暴露成 unknown / legacy_* 的伪结论。
            "population_status": population.get("population_status") if not is_initial_preview else None,
            "population_reason": population.get("population_reason") if not is_initial_preview else None,
            "population_decision": population_decision_label(
                population.get("population_status") if not is_initial_preview else None,
                population.get("population_reason") if not is_initial_preview else None,
            ) if not is_initial_preview else None,
            "archive_membership": archive.get("archive_membership", item.get("selected_archive")) if not is_initial_preview else None,
            "archive_reason": archive.get("archive_reason") if not is_initial_preview else None,
            "archive_membership_label": archive_membership_label(
                archive.get("archive_membership", item.get("selected_archive")) if not is_initial_preview else None,
            ),
            "archive_reason_label": archive_reason_label(
                archive.get("archive_reason") if not is_initial_preview else None,
            ),
            "evaluation_status": archive.get("evaluation_status", _evaluation_status(result)),
            "first_strict_generation": archive.get("first_strict_generation"),
            "archive_stage": archive.get("stage"),
        })

    def rank_key(item: dict[str, Any]) -> tuple[int, float, float]:
        result = item["result"]
        try:
            cost = float(item["cost"])
        except (TypeError, ValueError):
            cost = float("inf")
        if result.get("feasible") and result.get("complete"):
            return (0, cost, 0.0)
        if result.get("calculable") and result.get("complete"):
            return (1, item["total_violation"], cost)
        return (2, float("inf"), float("inf"))

    ranked = sorted(individuals, key=rank_key)
    strict_best = next((item for item in ranked if item["result"].get("feasible") and item["result"].get("complete")), None)
    near_best = next((item for item in ranked if item["result"].get("calculable") and item["result"].get("complete")), None)
    archive_reason_counts: dict[str, int] = {}
    if not is_initial_preview:
        for item in individuals:
            reason = item["archive_reason"]
            if reason:
                archive_reason_counts[str(reason)] = archive_reason_counts.get(str(reason), 0) + 1
    final_member_ids = {str(event["individual_id"]): str(event["archive_membership"]) for event in final_archive_events}
    final_strict = [item for item in individuals if final_member_ids.get(str(item["individual_id"])) == "strict"]
    final_near = [item for item in individuals if final_member_ids.get(str(item["individual_id"])) == "near"]
    table_rows: list[dict[str, Any]] = []
    for item in ranked:
        row = {
            "代": item["generation"], "候选生成方式": item["source_label"], "候选结构摘要": item["candidate_structure"],
            "可完成精算": "是" if item["result"].get("calculable") else "否",
            "不可计算原因": "—" if item["result"].get("calculable") else "；".join(
                str(reason) for reason in item["result"].get("diagnostic", [])
            ) or "未返回原因",
            "严格合格": "是" if item["result"].get("feasible") and item["result"].get("complete") else "否",
            "成本（元）": _decimal_text(item["cost"]), "P0（W）": _decimal_text(item["p0"]),
            "PK（W）": _decimal_text(item["pk"]), "UK（%）": _decimal_text(item["uk"]),
            "总超限": f"{item['total_violation']:.6f}", "超限项": item["violation_names"],
        }
        if not is_initial_preview:
            row.update({
                "父代 A": item["parent_a_id"] or "无（第 0 代或非交叉产生）",
                "父代 B": item["parent_b_id"] or "无（第 0 代或非交叉产生）",
                "本代去向": item["population_decision"],
                "档案归属": item["archive_membership_label"],
                "档案处理结果": item["archive_reason_label"],
                "入档前最后动作": item["archive_entry_last_action"],
            })
        table_rows.append(row)
    return {
        "file_name": path.name,
        "file_path": str(path),
        "run": run,
        "is_initial_preview": is_initial_preview,
        "generations": generations,
        "archive_snapshots": archive_snapshots,
        "archive_events": archive_events,
        "individuals": individuals,
        "ranked_individuals": ranked,
        "summary": {
            "individual_count": len(individuals), "calculable_count": calculable, "feasible_count": feasible,
            "source_counts": source_counts,
            "source_explanations": {label: source_explanation(label) for label in source_counts},
            "strict_best": strict_best, "near_best": near_best,
            "final_strict_archive": final_strict, "final_near_archive": final_near,
            "archive_reason_counts": archive_reason_counts,
            "semantics_available": bool(archive_events),
            "final_archive_available": bool(final_archive_events),
            "termination": termination,
        },
        "table_rows": table_rows,
    }
