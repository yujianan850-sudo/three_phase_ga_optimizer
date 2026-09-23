"""固定 Java 样本的全字段快照回归。

样本清单把“圆形/长圆/椭圆 × 波纹/散热器”明确固定到六个三相方案记录 ID。
Java 在保存方案时写入 calculationSnapshotVersion=1；本工具只读该保存结果并以同一
配置重放 Python 精算。它不调用 Java 接口、不重新触发 Java 计算、更不会写业务数据库。

运行：
    python -m calculation.snapshot_regression
    python -m calculation.snapshot_regression --samples config/java_python_regression_samples.yml
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from .consistency_check import SNAPSHOT_FIELD_MAP, compare_calculation_snapshot
from .database import FaladiRepository, load_database_settings
from .record_adapter import resolve_record_candidate
from .single_device_evaluator import ThreePhaseSingleDeviceEvaluator


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CASES = {
    "round_corrugated": (0, 0), "long_oval_corrugated": (1, 0), "ellipse_corrugated": (2, 0),
    "round_radiator": (0, 1), "long_oval_radiator": (1, 1), "ellipse_radiator": (2, 1),
}


def load_samples(path: Path) -> list[dict[str, Any]]:
    """读取并验证六个固定样本定义；记录 ID 可暂缺，但不会被当作已验证。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    samples = raw.get("samples")
    if not isinstance(samples, list):
        raise ValueError("样本清单缺少 samples 列表")
    by_name = {str(item.get("name")): item for item in samples if isinstance(item, dict)}
    if set(by_name) != set(EXPECTED_CASES):
        missing, extra = set(EXPECTED_CASES) - set(by_name), set(by_name) - set(EXPECTED_CASES)
        raise ValueError(f"样本清单必须刚好包含六种组合；缺少={sorted(missing)}，多余={sorted(extra)}")
    result: list[dict[str, Any]] = []
    for name, expected in EXPECTED_CASES.items():
        item = dict(by_name[name])
        actual = (int(item.get("core_type")), int(item.get("tank_type")))
        if actual != expected:
            raise ValueError(f"样本 {name} 的 core_type/tank_type 应为 {expected}，实际为 {actual}")
        record_id = item.get("record_id")
        if record_id is not None and int(record_id) <= 0:
            raise ValueError(f"样本 {name} 的 record_id 必须为正数或 null")
        item["name"] = name
        item["record_id"] = None if record_id is None else int(record_id)
        result.append(item)
    return result


def run(samples_path: Path) -> dict[str, Any]:
    """比较已登记样本；返回 pending 而不是掩盖尚未跑出的 Java 快照。"""
    samples = load_samples(samples_path)
    # 全部尚待登记时连数据库都不需要；这样清单本身可在 CI/开发机先验证，
    # 也不会把“未生成 Java 样本”误表现为“数据库连接失败”。
    needs_database = any(sample["record_id"] is not None for sample in samples)
    repo = FaladiRepository(load_database_settings()) if needs_database else None
    catalog = repo.catalog() if repo is not None else {}
    rows: list[dict[str, Any]] = []
    for sample in samples:
        name, record_id = sample["name"], sample["record_id"]
        if record_id is None:
            rows.append({"name": name, "status": "pending_java_record",
                         "message": "尚未登记由新 Java 代码实际保存的方案记录 ID"})
            continue
        try:
            if repo is None:
                raise RuntimeError("内部错误：已登记样本但未初始化数据库")
            record = repo.scheme_record(record_id)
            data = record["scheme_data"]
            config = repo.scheme_config(int(record["scheme_config_id"]))
            actual_case = (int(data["coretype"]), int(config["craft_fuel_tank"]["tankType"]))
            expected_case = EXPECTED_CASES[name]
            if actual_case != expected_case:
                raise ValueError(f"记录组合应为 {expected_case}，实际为 {actual_case}")
            if data.get("calculationSnapshotVersion") != 1:
                raise ValueError("记录没有 calculationSnapshotVersion=1；请用新 Java 代码重新计算并保存")
            candidate = resolve_record_candidate(record, catalog, config)
            result = ThreePhaseSingleDeviceEvaluator(config, catalog).evaluate(candidate)
            if not result.calculable:
                rows.append({"name": name, "record_id": record_id, "status": "python_not_calculable",
                             "diagnostic": result.diagnostic})
                continue
            differences = compare_calculation_snapshot(data, result.metrics)
            rows.append({"name": name, "record_id": record_id,
                         "status": "match" if not differences else "field_mismatch",
                         "compared_field_count": len(SNAPSHOT_FIELD_MAP), "mismatches": differences})
        except (KeyError, LookupError, ValueError) as exc:
            rows.append({"name": name, "record_id": record_id, "status": "invalid_sample", "message": str(exc)})
    return {
        "samples_file": str(samples_path), "snapshot_field_count": len(SNAPSHOT_FIELD_MAP), "samples": rows,
        "summary": {status: sum(1 for row in rows if row["status"] == status)
                    for status in sorted({row["status"] for row in rows})},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="六类固定 Java/Python 全字段快照回归")
    parser.add_argument("--samples", type=Path, default=ROOT / "config" / "java_python_regression_samples.yml")
    args = parser.parse_args()
    print(json.dumps(run(args.samples), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
