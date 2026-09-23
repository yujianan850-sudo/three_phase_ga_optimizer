"""Java 三相历史方案与 Python 单设备候选之间的只读适配。"""

from __future__ import annotations

from decimal import Decimal

from .models import CoolingOption, OilDuctScheme, ThreePhaseDesignCandidate


def candidate_from_record(record: dict) -> ThreePhaseDesignCandidate:
    """用 Java 已保存方案构造候选骨架，供一致性验证使用。"""
    data = record["scheme_data"]
    return ThreePhaseDesignCandidate(
        steel_brand=str(data.get("crgo_NAME") or data["crgo"]),
        core_type=int(data["coretype"]), core_data_id=int(data["crgod"]),
        low_voltage_turns=int(data["lvt"]), low_voltage_wire_id=-1, low_voltage_layers=int(data["lvl"]),
        low_voltage_duct=OilDuctScheme(int(data["lvon"]), int(data["lvot"])),
        high_voltage_wire_id=-1, high_voltage_layers=int(data["hvl"]),
        high_voltage_duct=OilDuctScheme(int(data["hvon"]), int(data["hvot"])),
        conversion_c=Decimal(str(data["conversionC"])) if data.get("conversionC") is not None else None,
        # 只有解析完整历史配置时才能确定类别；候选骨架保留 None，供精算器按配置回退。
        low_voltage_wire_type=None,
        high_voltage_wire_type=None,
    )


def _wire_record_id(data: dict, prefix: str, wire_type: int, catalog: dict) -> int:
    """将历史方案中保存的裸线尺寸映射回原始线规记录 ID。"""
    width = Decimal(str(data[f"{prefix}ilw"]))
    thickness = Decimal(str(data[f"{prefix}ilt"]))
    if wire_type in (0, 1) and prefix == "hv":
        matches = [row for row in catalog["round"] if Decimal(str(row["nominal_diameter"])) == width]
    elif wire_type in (0, 1) and prefix == "lv":
        matches = [row for row in catalog["foil"]
                   if Decimal(str(row["breadth"])) == width and Decimal(str(row["thickness"])) == thickness]
    else:
        matches = [row for row in catalog["flat"]
                   if Decimal(str(row["line_width"])) == width and Decimal(str(row["line_thickness"])) == thickness]
    if not matches:
        raise LookupError(
            f"历史方案无法映射 {prefix} 侧原始线规: category={wire_type}, "
            f"width={width}, thickness={thickness}, matched_ids=[]"
        )
    if len(matches) > 1:
        ids = sorted(int(row["id"]) for row in matches)
        raise LookupError(
            f"历史方案线规映射不唯一: side={prefix}, category={wire_type}, "
            f"width={width}, thickness={thickness}, matched_ids={ids}"
        )
    return int(matches[0]["id"])


def _snapshot_cooling_option(data: dict, catalog: dict, config: dict) -> CoolingOption | None:
    """用新 Java 保存的油箱选择还原完整冷却型号。

    老记录没有快照时仍返回 ``None``，由一致性模式显式使用旧的确定性回退；新快照
    则必须能无歧义地还原实际波纹深度或散热器中心距/片数/组数，不能悄悄选目录首项。
    """
    if data.get("calculationSnapshotVersion") != 1:
        return None
    tank_type = int(config["craft_fuel_tank"]["tankType"])
    if tank_type == 0:
        # Java 三相方案数据实际以小写历史键 ``corrd`` 保存；早期适配器
        # 仅识别迁移草案中的 ``CORRD``，会把新快照误判为缺少波纹深度。
        depth = data.get("corrd")
        if depth is None:
            depth = data.get("CORRD")
        matches = [row for row in catalog["corrugation"] if Decimal(str(row["corrugation_depth"])) == Decimal(str(depth))]
        if len(matches) != 1:
            ids = sorted(int(row["id"]) for row in matches)
            raise LookupError(f"快照波纹深度无法唯一还原目录型号: depth={depth}, matched_ids={ids}")
        return CoolingOption(
            tank_type=0, corrugation_id=int(matches[0]["id"]),
            corrugation_long_axis_faces=int(config["craft_fuel_tank"].get("longAxisCorrugatedSurfaces") or 0),
            corrugation_short_axis_faces=int(config["craft_fuel_tank"].get("shortAxisCorrugatedSurfaces") or 0),
        )

    if tank_type != 1:
        raise LookupError(f"快照不支持的油箱类型: {tank_type}")
    # 与波纹分支一致，兼容 Java 持久化使用的小写历史键。
    center = data.get("maxCenterDistance")
    slices = data.get("hsphda")
    if slices is None:
        slices = data.get("HSPHDA")
    groups = data.get("hsgn")
    if groups is None:
        groups = data.get("HSGN")
    if center is None or slices is None or groups is None:
        raise LookupError("散热器快照缺少中心距、片数或组数，不能安全重放")
    width_code = int(config["craft_fuel_tank"].get("heatSinkWidth") or 0)
    table_name = {0: "thermawide_310", 1: "thermawide_480", 2: "thermawide_520"}.get(width_code)
    if table_name is None:
        raise LookupError(f"散热器快照的宽度编码不支持: {width_code}")
    rows = [row for row in catalog[table_name] if int(row["center_distance"]) == int(center)
            and int(row["value_condition_slice_number"]) == int(slices)]
    by_type = {name: [row for row in rows if str(row["parameter_type"]).upper() == name] for name in ("G", "Q", "SZ")}
    if any(len(rows_of_type) != 1 for rows_of_type in by_type.values()):
        found = {name: sorted(int(row["id"]) for row in rows_of_type) for name, rows_of_type in by_type.items()}
        raise LookupError(f"散热器快照无法唯一还原 G/Q/SZ 行: center={center}, slices={slices}, matched_ids={found}")
    return CoolingOption(
        tank_type=1, radiator_width_code=width_code, radiator_center_distance=int(center),
        radiator_slice_count=int(slices), radiator_groups=int(groups),
        radiator_body_row_id=int(by_type["G"][0]["id"]), radiator_oil_row_id=int(by_type["Q"][0]["id"]),
        radiator_area_row_id=int(by_type["SZ"][0]["id"]),
    )


def resolve_record_candidate(record: dict, catalog: dict, config: dict) -> ThreePhaseDesignCandidate:
    """将一条 Java 历史记录还原为可直接送入 Python 精算器的完整候选。"""
    candidate = candidate_from_record(record)
    data = record["scheme_data"]
    # Java 匝数/几何计算用的 ``CALCULATECRGOD`` 对椭圆是四舍五入后的短轴，并写回
    # ``crgod``；铁芯目录行仍按原始 ``core_diameter``（椭圆为长轴×短轴对称规格）检索。
    # Java 另存 ``originalCRGOD`` 保留该原始直径，缺省时才退回 ``crgod``。
    core_diameter = data.get("originalCRGOD")
    if core_diameter is None:
        core_diameter = data["crgod"]
    matches = [row for row in catalog["core"] if int(row["core_type"]) == candidate.core_type
               and int(row["core_diameter"]) == int(core_diameter)]
    if not matches:
        raise LookupError(
            f"历史方案找不到铁芯记录: type={candidate.core_type}, "
            f"diameter={core_diameter}, matched_ids=[]"
        )
    if len(matches) > 1:
        ids = sorted(int(row["id"]) for row in matches)
        raise LookupError(
            f"历史方案铁芯映射不唯一: type={candidate.core_type}, "
            f"diameter={core_diameter}, matched_ids={ids}"
        )
    return ThreePhaseDesignCandidate(
        steel_brand=candidate.steel_brand, core_type=candidate.core_type, core_data_id=int(matches[0]["id"]),
        low_voltage_turns=candidate.low_voltage_turns,
        low_voltage_wire_id=_wire_record_id(data, "lv", int(config["craft_low_coil"]["wireSpecification"]), catalog),
        low_voltage_wire_type=int(config["craft_low_coil"]["wireSpecification"]),
        low_voltage_layers=candidate.low_voltage_layers, low_voltage_duct=candidate.low_voltage_duct,
        high_voltage_wire_id=_wire_record_id(data, "hv", int(config["craft_high_coil"]["wireSpecification"]), catalog),
        high_voltage_wire_type=int(config["craft_high_coil"]["wireSpecification"]),
        high_voltage_layers=candidate.high_voltage_layers,
        high_voltage_duct=candidate.high_voltage_duct, conversion_c=candidate.conversion_c,
        cooling_option=_snapshot_cooling_option(data, catalog, config),
    )
