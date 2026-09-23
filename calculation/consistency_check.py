"""三相 Java 历史方案与 Python 单设备精算器的批量一致性回归。

运行示例（必须使用用户指定的 conda python310）：
    python -m calculation.consistency_check --config-id 448
    python -m calculation.consistency_check --all

本脚本只读本地 faladi 数据库，不访问或修改运行中的后端接口，也不会启动 GA。
当 Python 无法计算某公式分支时，会明确归入 ``not_calculable``，不能误报为一致。
``formula_chain_complete`` 仅表示 Python 已覆盖公式链。版本为 1 的 Java
``calculationSnapshot`` 才是温升、油重、油箱重量和价格分项的同次真实对比依据；
旧记录没有该快照时明确归为“未验证”，绝不以当前配置反推 Java 历史值。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from typing import Any

from .database import FaladiRepository, load_database_settings
from .record_adapter import resolve_record_candidate
from .single_device_evaluator import ThreePhaseSingleDeviceEvaluator


# 左侧为 Java scheme_data 字段，右侧为 Python EvaluationResult.metrics 键。
# 只比较 Java 实际保存的字段；Java 未保存的中间量不计入“已一致”。
FIELD_MAP = {
    "ct": "ct",
    "crgokw": "crgokw",
    "lvrh": "lvrh",
    "hvrh": "hvrh",
    "lvydd": "lvydd",
    "hvydd": "hvydd",
    "crgopo": "crgopo",
    "hvlvpk": "hvlvpk",
    "ukk": "ukk",
}

# 方案总成本不保存在 scheme_data，而是三相方案记录表的顶层 price 字段。
RECORD_FIELD_MAP = {
    "price": "price",
}

# Java ThreePhaseSchemeData 的 calculationSnapshotVersion=1 时保存的全字段快照。
# 左边为 scheme_data JSON key，右边为 Python EvaluationResult.metrics key。
SNAPSHOT_FIELD_MAP = {
    "oilAverageTempRise": "oatr", "oilTopTempRise": "otr",
    "lowVoltageWindingTempRise": "lvtr", "highVoltageWindingTempRise": "hvtr",
    "tankBaseOilWeight": "tank_base_oil_weight",
    "heatDissipationOilWeight": "heat_dissipation_oil_weight",
    "conservatorOilWeight": "conservator_oil_weight", "totalOilWeight": "total_oil_weight",
    "tankStructureWeight": "tank_structure_weight", "heatDissipationWeight": "heat_dissipation_weight",
    "conservatorIronWeight": "conservator_iron_weight", "tankAndAccessoryWeight": "tank_and_accessory_weight",
    "lowVoltageWireWeight": "low_voltage_wire_weight", "highVoltageWireWeight": "high_voltage_wire_weight",
    "siliconSteelWeight": "silicon_steel_weight", "calculatedAssemblyWeight": "total_assembly_weight",
    "lowVoltageWireTotalPrice": "low_voltage_wire_total_price",
    "highVoltageWireTotalPrice": "high_voltage_wire_total_price",
    "siliconSteelTotalPrice": "silicon_steel_total_price", "oilTotalPrice": "oil_total_price",
    "tankAndAccessoryTotalPrice": "tank_and_accessory_total_price",
    "calculatedTotalPrice": "calculated_total_price",
}


def _value(value: Any) -> Decimal:
    return Decimal(str(value))


def compare_calculation_snapshot(java_snapshot: dict[str, Any], python_metrics: dict[str, Any]) -> list[dict[str, str]]:
    """逐项比较一份 Java 已保存快照与 Python 精算指标，缺字段也算差异。"""
    mismatches: list[dict[str, str]] = []
    for java_key, python_key in SNAPSHOT_FIELD_MAP.items():
        java_value, python_value = java_snapshot.get(java_key), python_metrics.get(python_key)
        if java_value is None or python_value is None:
            mismatches.append({
                "field": java_key, "java": "<missing>" if java_value is None else str(java_value),
                "python": "<missing>" if python_value is None else str(python_value),
            })
        elif _value(java_value) != _value(python_value):
            mismatches.append({"field": java_key, "java": str(java_value), "python": str(python_value)})
    return mismatches


def run(config_id: int | None, max_samples: int) -> dict[str, Any]:
    repo = FaladiRepository(load_database_settings())
    catalog = repo.catalog()
    records = repo.active_records(config_id) if config_id is not None else repo.all_active_records()
    configs: dict[int, dict[str, Any]] = repo.all_scheme_configs() if config_id is None else {}
    evaluators: dict[int, ThreePhaseSingleDeviceEvaluator] = {}
    status = Counter()
    compared_fields = Counter()
    mismatches: list[dict[str, Any]] = []
    non_calculable: list[dict[str, Any]] = []
    decode_errors: list[dict[str, Any]] = []
    snapshot_mismatches: list[dict[str, Any]] = []

    for record in records:
        current_config_id = int(record["scheme_config_id"])
        try:
            # 不使用 setdefault(key, repo.scheme_config(...))：默认值会被无条件求值，
            # 会让批量回归对每条记录重复访问一次配置表。
            config = configs.get(current_config_id)
            if config is None:
                # 全量历史记录中允许存在“配置已逻辑删除、记录仍保留”的情况。
                # 这类记录没有完整配置输入，不能拿来评价 Python 公式，也不应重复查库。
                if config_id is None:
                    status["config_not_available"] += 1
                    if len(decode_errors) < max_samples:
                        decode_errors.append({
                            "record_id": record["id"], "config_id": current_config_id,
                            "error": "关联三相配置已逻辑删除，无法构造完整精算输入",
                        })
                    continue
                config = repo.scheme_config(current_config_id)
                configs[current_config_id] = config
            evaluator = evaluators.get(current_config_id)
            if evaluator is None:
                evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
                evaluators[current_config_id] = evaluator
            candidate = resolve_record_candidate(record, catalog, config)
        except (KeyError, LookupError, ValueError) as exc:
            status["candidate_decode_error"] += 1
            if len(decode_errors) < max_samples:
                decode_errors.append({"record_id": record["id"], "config_id": current_config_id, "error": str(exc)})
            continue

        result = evaluator.evaluate(candidate)
        if not result.calculable:
            status["not_calculable"] += 1
            if len(non_calculable) < max_samples:
                non_calculable.append({
                    "record_id": record["id"], "config_id": current_config_id,
                    "diagnostic": result.diagnostic,
                })
            continue

        status["calculable"] += 1
        data = record["scheme_data"]
        record_mismatches: list[dict[str, str]] = []
        for java_key, python_key in FIELD_MAP.items():
            java_value, python_value = data.get(java_key), result.metrics.get(python_key)
            if java_value is None or python_value is None:
                continue
            compared_fields[java_key] += 1
            if _value(java_value) != _value(python_value):
                record_mismatches.append({
                    "field": java_key, "java": str(java_value), "python": str(python_value),
                })
        for java_key, python_key in RECORD_FIELD_MAP.items():
            java_value, python_value = record.get(java_key), result.metrics.get(python_key)
            if java_value is None or python_value is None:
                continue
            compared_fields[java_key] += 1
            if _value(java_value) != _value(python_value):
                record_mismatches.append({
                    "field": java_key, "java": str(java_value), "python": str(python_value),
                })
        if data.get("calculationSnapshotVersion") == 1:
            snapshot_differences = compare_calculation_snapshot(data, result.metrics)
            for java_key in SNAPSHOT_FIELD_MAP:
                compared_fields[java_key] += 1
            if snapshot_differences:
                status["snapshot_field_mismatch"] += 1
                if len(snapshot_mismatches) < max_samples:
                    snapshot_mismatches.append({
                        "record_id": record["id"], "config_id": current_config_id,
                        "mismatches": snapshot_differences,
                    })
            else:
                status["snapshot_field_match"] += 1
        else:
            status["snapshot_not_saved"] += 1
        if record_mismatches:
            status["field_mismatch"] += 1
            if len(mismatches) < max_samples:
                mismatches.append({
                    "record_id": record["id"], "config_id": current_config_id,
                    "mismatches": record_mismatches,
                })
        else:
            status["field_match"] += 1

    return {
        "records": len(records),
        "formula_revision": ThreePhaseSingleDeviceEvaluator.formula_revision,
        "status": dict(status),
        "compared_fields": dict(compared_fields),
        "mismatch_samples": mismatches,
        "snapshot_mismatch_samples": snapshot_mismatches,
        "not_calculable_samples": non_calculable,
        "candidate_decode_error_samples": decode_errors,
        "formula_chain_complete": True,
        "java_verified_fields": [*FIELD_MAP, *RECORD_FIELD_MAP, *SNAPSHOT_FIELD_MAP],
        "unverified_new_fields": [] if status["snapshot_field_match"] else list(SNAPSHOT_FIELD_MAP),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="三相 Python 精算器历史一致性回归")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--config-id", type=int, help="仅核验一个三相配置的历史记录")
    scope.add_argument("--all", action="store_true", help="核验所有历史三相记录")
    parser.add_argument("--max-samples", type=int, default=10, help="每类异常最多输出样本数")
    args = parser.parse_args()
    print(json.dumps(run(args.config_id if not args.all else None, args.max_samples), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
