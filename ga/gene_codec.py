"""离散候选域构建和完整导线选项编码；不包含任何性能前置筛选。

一条导线基因从来不是“宽度 + 厚度”两个可拆数字，而是
``(wire_type, record_id, catalog_family)`` 三元组选项。这样即使不同目录表的 ID
碰巧相同，也不会把 QQL、QQLB 等不同类别混为一个线规。
"""

from __future__ import annotations

import random
import math
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml

from calculation.models import CoolingOption, OilDuctScheme, ThreePhaseDesignCandidate


Side = Literal["lv", "hv"]
PairCoverageStrategy = Literal["balanced", "exhaustive"]
INTEGER_GENE_NAMES = ("lv_turns", "lv_layers", "hv_layers")
# conversionC 的离散步长是固定工艺/算法规则，不是可由 yml、CLI 或页面覆盖的产品参数。
CONVERSION_C_STEP = Decimal("1")

# B 层只覆盖工程耦合最强的两两维度；不要误解为全部基因笛卡尔积。
KEY_PAIR_COVERAGE: tuple[tuple[str, str], ...] = (
    ("steel_brand", "core"),
    ("core", "lv_wire"),
    ("core", "hv_wire"),
    ("lv_wire", "hv_wire"),
    ("lv_wire", "lv_duct"),
    ("hv_wire", "hv_duct"),
    ("core", "lv_turns"),
    # 低压层数依赖“导线类别 × 匝数”，因此该组合要显式覆盖；不能只把层数
    # 当作一个独立数字随机拼到任意导线上。
    ("lv_wire", "lv_turns"),
    ("lv_wire", "lv_layers"),
    ("hv_wire", "hv_layers"),
    # 长圆 conversionC 不是所有铁芯通用的数字；只生成“某个长圆铁芯 × 该铁芯
    # 直径 0.55–0.75 内的 conversionC”的合法组合。
    ("core", "conversion_c"),
    # 热工性能由铁芯尺寸、窗口/油箱尺寸和具体散热型号共同决定，必须显式覆盖。
    ("core", "cooling"),
)


@dataclass(frozen=True)
class WireOption:
    """一个可进入 GA 的完整导线方案，禁止把其中字段拆开交叉。"""

    wire_type: int
    record_id: int
    catalog_family: str  # foil / round / flat
    # 目录原始裸线尺寸。它们只用于“宽/厚/直径方向”的局部邻域查找，绝不用于
    # 拼接出新规格；真正替换时仍然只替换完整 record_id。
    bare_width: Decimal | None = None
    bare_thickness: Decimal | None = None

    def key(self) -> tuple[int, int, str]:
        return self.wire_type, self.record_id, self.catalog_family


def load_optimizer_defaults(config_path: Path | None = None) -> dict[str, Any]:
    """读取本项目的 GA 默认项，不读取 Java 的业务配置。

    ``optimizer.yml`` 只保存 GA 运行策略默认值，不保存任何产品工艺范围。
    """
    path = config_path or Path(__file__).resolve().parents[1] / "config" / "optimizer.yml"
    if not path.exists():
        return {"history_seed_ratio": 0.35, "random_injection_ratio": 0.10}
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    run = content.get("run") or {}
    result = {
        "history_seed_ratio": float(run.get("history_seed_ratio", 0.35)),
        "random_injection_ratio": float(run.get("random_injection_ratio", 0.10)),
    }
    if not 0 <= result["history_seed_ratio"] < 1:
        raise ValueError("optimizer.yml 的 history_seed_ratio 必须位于 [0, 1)")
    if not 0 <= result["random_injection_ratio"] <= 1:
        raise ValueError("optimizer.yml 的 random_injection_ratio 必须位于 [0, 1]")
    return result


@dataclass(frozen=True)
class GeneDomain:
    steel_brands: tuple[str, ...]
    cores: tuple[dict[str, Any], ...]
    lv_wire_options: tuple[WireOption, ...]
    hv_wire_options: tuple[WireOption, ...]
    lv_turns: tuple[int, ...]
    lv_layers: tuple[int, ...]
    hv_layers: tuple[int, ...]
    lv_ducts: tuple[OilDuctScheme, ...]
    hv_ducts: tuple[OilDuctScheme, ...]
    cooling_options: tuple[CoolingOption, ...] = ()
    # key 为 lv_turns / lv_layers / hv_layers；值包含实际范围、步长及其来源。
    integer_domain_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def lv_wire_ids(self) -> tuple[int, ...]:
        """历史兼容视图；多类别场景不可用作候选唯一键。"""
        return tuple(option.record_id for option in self.lv_wire_options)

    @property
    def hv_wire_ids(self) -> tuple[int, ...]:
        """历史兼容视图；多类别场景不可用作候选唯一键。"""
        return tuple(option.record_id for option in self.hv_wire_options)

    @property
    def low_voltage_duct_options(self) -> tuple[OilDuctScheme, ...]:
        """低压页面选择的油道组合。"""
        return self.lv_ducts

    @property
    def high_voltage_duct_options(self) -> tuple[OilDuctScheme, ...]:
        """高压页面选择的油道组合。"""
        return self.hv_ducts

    @staticmethod
    def low_voltage_layers_for(wire_type: int, low_voltage_turns: int) -> tuple[int, ...]:
        """严格复刻 Java 三相低压层数规则。

        LV 0/1 是箔材，层数必须等于低压匝数；LV 2/3 是扁线，仅允许 2 或 4 层。
        它不是一个可脱离导线类别独立任选的全局整数域。
        """
        if wire_type in (0, 1):
            return (int(low_voltage_turns),)
        if wire_type in (2, 3):
            return (2, 4)
        raise ValueError(f"不支持的低压导线类别: {wire_type}")

    def low_voltage_layers_for_candidate(self, candidate: ThreePhaseDesignCandidate) -> tuple[int, ...]:
        option = self._option_for_candidate("lv", candidate.low_voltage_wire_type, candidate.low_voltage_wire_id)
        return self.low_voltage_layers_for(option.wire_type, candidate.low_voltage_turns)

    @staticmethod
    def _required_java_integer_domain(
        name: str, label: str, minimum: Any, maximum: Any,
    ) -> tuple[tuple[int, ...], dict[str, Any]]:
        """只接受 Java 页面已保存的整数边界；缺失时拒绝启动 GA。"""
        if minimum is None or maximum is None:
            raise ValueError(f"Java 页面未填写{label}范围，不能构造 GA 搜索域")
        lower, upper = int(minimum), int(maximum)
        if lower < 1 or upper < lower:
            raise ValueError(f"Java 页面{label}范围不合法: {lower}–{upper}")
        return tuple(range(lower, upper + 1)), {
            "minimum": lower, "maximum": upper, "step": 1,
            "source": "Java 页面范围", "configured_minimum": lower,
            "configured_maximum": upper, "history_sample_count": 0,
            "history_extension": None,
        }

    def conversion_values_for_core(self, core: dict[str, Any]) -> tuple[Decimal, ...]:
        """仅为长圆铁芯生成 conversionC：直径 × [0.55, 0.75]，含边界。"""
        if int(core["core_type"]) != 1:
            return ()
        diameter = Decimal(str(core["core_diameter"]))
        lower, upper = diameter * Decimal("0.55"), diameter * Decimal("0.75")
        values: list[Decimal] = []
        value = lower
        while value <= upper:
            values.append(value)
            value += CONVERSION_C_STEP
        if values[-1] != upper:
            values.append(upper)
        return tuple(values)

    def _default_conversion_for_core(self, core: dict[str, Any]) -> Decimal | None:
        values = self.conversion_values_for_core(core)
        return values[0] if values else None

    @staticmethod
    def _section(row: dict[str, Any]) -> Decimal:
        for name in ("section", "calculate_section"):
            value = row.get(name)
            if value is not None:
                return Decimal(str(value))
        # 目录中个别旧记录没有截面积时，仍以真实宽厚或直径作稳定排序，
        # 但不以此推导或创建新的规格。
        if row.get("line_width") is not None and row.get("line_thickness") is not None:
            return Decimal(str(row["line_width"])) * Decimal(str(row["line_thickness"]))
        if row.get("breadth") is not None and row.get("thickness") is not None:
            return Decimal(str(row["breadth"])) * Decimal(str(row["thickness"]))
        if row.get("nominal_diameter") is not None:
            diameter = Decimal(str(row["nominal_diameter"]))
            return diameter * diameter
        return Decimal("0")

    @classmethod
    def _wire_options(cls, rows: list[dict[str, Any]], wire_type: int, family: str) -> tuple[WireOption, ...]:
        def dimensions(row: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
            if family == "flat":
                return Decimal(str(row["line_width"])), Decimal(str(row["line_thickness"]))
            if family == "foil":
                return Decimal(str(row["breadth"])), Decimal(str(row["thickness"]))
            # 圆线没有独立宽/厚；bare_width 复用为直径，厚度保留 None。
            return Decimal(str(row["nominal_diameter"])), None
        return tuple(
            WireOption(
                wire_type=wire_type, record_id=int(row["id"]), catalog_family=family,
                bare_width=dimensions(row)[0], bare_thickness=dimensions(row)[1],
            )
            for row in sorted(rows, key=lambda row: (cls._section(row), int(row["id"])))
        )

    @staticmethod
    def _page_duct_options(craft: dict[str, Any], side: str) -> tuple[OilDuctScheme, ...]:
        """复刻 Java ``deduplicateChannelPairs``：页面数量×类型，多选笛卡尔积后归一化。

        count=0 或 type=0 都表示同一个 ``(0, 0)`` 无油道方案。页面必须提供这两个
        数组；缺失时不以 Python 固定列表或历史方案代替。
        """
        counts, types = craft.get("channelCount"), craft.get("channelType")
        if not isinstance(counts, list) or not isinstance(types, list) or not counts or not types:
            raise ValueError(f"{side}侧页面油道数量/类型不能为空")
        result: list[OilDuctScheme] = []
        seen: set[tuple[int, int]] = set()
        for raw_count in counts:
            for raw_type in types:
                count, duct_type = int(raw_count), int(raw_type)
                if count == 0 or duct_type == 0:
                    count, duct_type = 0, 0
                scheme = OilDuctScheme(count, duct_type)
                key = scheme.count, scheme.duct_type
                if key not in seen:
                    result.append(scheme)
                    seen.add(key)
        if not result:
            raise ValueError(f"{side}侧页面没有形成有效油道组合")
        return tuple(result)

    @staticmethod
    def _wire_family(side: Side, wire_type: int) -> str:
        if side == "lv":
            return "foil" if wire_type in (0, 1) else "flat"
        return "round" if wire_type in (0, 1) else "flat"

    @staticmethod
    def _configured_steel_brands(config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]]) -> tuple[str, ...]:
        """将三相配置中的牌号编码还原为真实硅钢片牌号。"""
        all_brands = {str(row["brand"]) for row in catalog["steel"]}
        code_to_brand = {
            int(row["brand_code"]): str(row["brand"])
            for row in catalog.get("steel_brand_mapping", [])
            if str(row["brand"]) in all_brands
        }
        mapped_brands = tuple(sorted(set(code_to_brand.values())))
        if not mapped_brands:
            raise ValueError("缺少三相硅钢片牌号映射，不能将整个硅钢片目录误作为三相候选域")
        grade_codes = config["craft_core"].get("siliconSteelGrade")
        if not grade_codes:
            return mapped_brands
        brands = tuple(sorted({code_to_brand[int(code)] for code in grade_codes if int(code) in code_to_brand} & all_brands))
        if not brands:
            raise ValueError("三相配置的硅钢片牌号编码无法在 tb_silicon_steel_brand_mapping 中还原")
        return brands

    @staticmethod
    def _flat_rows_in_configured_range(rows: list[dict[str, Any]], craft: dict[str, Any]) -> list[dict[str, Any]]:
        """按页面/配置的宽、厚、宽厚比约束过滤真实扁线记录。

        这是候选域边界，不是性能计算的替代；进入域后的电流密度、温升、绕组高度
        等仍必须由精算器重新验证。缺失某个边界时，不虚构默认范围。
        """
        def in_range(value: Decimal, lower: Any, upper: Any) -> bool:
            return (lower is None or value >= Decimal(str(lower))) and (upper is None or value <= Decimal(str(upper)))

        filtered: list[dict[str, Any]] = []
        for row in rows:
            if row.get("line_width") is None or row.get("line_thickness") is None:
                continue
            width = Decimal(str(row["line_width"]))
            thickness = Decimal(str(row["line_thickness"]))
            if thickness <= 0:
                continue
            ratio = width / thickness
            if not in_range(width, craft.get("flatWireWidthMin"), craft.get("flatWireWidthMax")):
                continue
            if not in_range(thickness, craft.get("flatWireThicknessMin"), craft.get("flatWireThicknessMax")):
                continue
            if not in_range(ratio, craft.get("flatWireAspectRatioLimitMin"), craft.get("flatWireAspectRatioLimitMax")):
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def _radiator_group_range(tank: dict[str, Any]) -> tuple[int, int]:
        """读取 Java 已支持的新旧散热片组数配置，拒绝缺失而不擅自补默认值。"""
        group_min, group_max = tank.get("heatSinkGroupsMin"), tank.get("heatSinkGroupsMax")
        old_groups = tank.get("heatSinkGroups")
        if group_min is None and group_max is None and old_groups is not None:
            group_min = group_max = old_groups
        group_min, group_max = int(group_min or 0), int(group_max or 0)
        if group_min <= 0 or group_max < group_min:
            raise ValueError("散热器油箱未配置有效的散热片组数范围")
        return group_min, group_max

    @classmethod
    def _cooling_options(
        cls, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], *,
        allowed_tank_types: tuple[int, ...] | None = None,
        allowed_corrugation_long_faces: tuple[int, ...] | None = None,
        allowed_corrugation_short_faces: tuple[int, ...] | None = None,
        allowed_radiator_width_codes: tuple[int, ...] | None = None,
        allowed_radiator_groups: tuple[int, ...] | None = None,
    ) -> tuple[CoolingOption, ...]:
        """从真实目录构造完整冷却型号，不把独立参数行拼成虚构散热器。

        波纹片一条记录就是一个型号。散热器则以固定宽度表中同一中心距、片数的
        三条 ``G/Q/SZ`` 记录为一个最小可计算型号；任一参数类型缺失时该组合不能
        使用，某类型重复时直接报数据契约错误，避免沿用 Java ``toMap`` 的偶然首行。
        """
        # 独立的导线/覆盖单元测试和旧调用方可以没有热工配置；此时不伪造型号，
        # 仍可构建无冷却维度的兼容域。真实优化配置必须提供该段。
        if "craft_fuel_tank" not in config:
            return ()
        tank = config["craft_fuel_tank"]
        active_types = tuple(dict.fromkeys(
            (int(tank["tankType"]),) if allowed_tank_types is None else tuple(int(value) for value in allowed_tank_types)
        ))
        if not active_types or any(value not in (0, 1) for value in active_types):
            raise ValueError("冷却形式至少选择波纹油箱或散热器油箱之一")
        options: list[CoolingOption] = []
        if 0 in active_types:
            rows = sorted(catalog.get("corrugation", []), key=lambda row: int(row["id"]))
            if not rows:
                raise ValueError("波纹油箱没有可用的 tb_corrugation 记录")
            long_faces = tuple(dict.fromkeys(
                (int(tank.get("longAxisCorrugatedSurfaces") or 0),)
                if allowed_corrugation_long_faces is None else tuple(int(value) for value in allowed_corrugation_long_faces)
            ))
            short_faces = tuple(dict.fromkeys(
                (int(tank.get("shortAxisCorrugatedSurfaces") or 0),)
                if allowed_corrugation_short_faces is None else tuple(int(value) for value in allowed_corrugation_short_faces)
            ))
            if not long_faces or not short_faces or min(*long_faces, *short_faces) < 0:
                raise ValueError("波纹长轴/短轴面数必须提供非负整数候选")
            options.extend(
                CoolingOption(tank_type=0, corrugation_id=int(row["id"]),
                              corrugation_long_axis_faces=long, corrugation_short_axis_faces=short)
                for row in rows for long in long_faces for short in short_faces
            )
        if 1 in active_types:
            width_codes = tuple(dict.fromkeys(
                (int(tank.get("heatSinkWidth") or 0),)
                if allowed_radiator_width_codes is None else tuple(int(value) for value in allowed_radiator_width_codes)
            ))
            if not width_codes or any(value not in (0, 1, 2) for value in width_codes):
                raise ValueError("散热片宽度只能选择 310、480、520 mm")
            if allowed_radiator_groups is None:
                group_min, group_max = cls._radiator_group_range(tank)
                groups = tuple(range(group_min, group_max + 1))
            else:
                groups = tuple(dict.fromkeys(int(value) for value in allowed_radiator_groups))
                if not groups or min(groups) <= 0:
                    raise ValueError("散热器组数必须提供正整数候选")
            for width_code in width_codes:
                table_name = {0: "thermawide_310", 1: "thermawide_480", 2: "thermawide_520"}[width_code]
                grouped: dict[tuple[int, int], dict[str, list[dict[str, Any]]]] = {}
                for row in catalog.get(table_name, []):
                    try:
                        center, slices = int(row["center_distance"]), int(row["value_condition_slice_number"])
                        parameter_type = str(row["parameter_type"]).upper()
                        int(row["id"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError(f"{table_name} 存在无法作为散热器型号追溯的记录: {row}") from exc
                    if parameter_type in {"G", "Q", "SZ"}:
                        grouped.setdefault((center, slices), {}).setdefault(parameter_type, []).append(row)
                for (center, slices), rows_by_type in sorted(grouped.items()):
                    required = {name: rows_by_type.get(name, []) for name in ("G", "Q", "SZ")}
                    if not all(required.values()):
                        continue
                    duplicated = {name: sorted(int(row["id"]) for row in rows) for name, rows in required.items() if len(rows) != 1}
                    if duplicated:
                        raise ValueError(f"{table_name} 的中心距={center}、片数={slices} 参数行不唯一: {duplicated}")
                    options.extend(CoolingOption(
                        tank_type=1, radiator_width_code=width_code, radiator_center_distance=center,
                        radiator_slice_count=slices, radiator_groups=group,
                        radiator_body_row_id=int(required["G"][0]["id"]),
                        radiator_oil_row_id=int(required["Q"][0]["id"]),
                        radiator_area_row_id=int(required["SZ"][0]["id"]),
                    ) for group in groups)
        if not options:
            raise ValueError("所选冷却形式下没有可用的完整波纹片或散热器型号")
        return tuple(options)

    @classmethod
    def _options_for_types(
        cls, side: Side, wire_types: tuple[int, ...], craft: dict[str, Any], catalog: dict[str, list[dict[str, Any]]],
    ) -> tuple[WireOption, ...]:
        allowed_types = set(range(0, 4) if side == "lv" else range(0, 6))
        invalid_types = sorted(set(wire_types) - allowed_types)
        if invalid_types:
            raise ValueError(f"{side} 侧导线类别编码不合法: {invalid_types}；允许值为 {sorted(allowed_types)}")
        result: list[WireOption] = []
        for wire_type in wire_types:
            family = cls._wire_family(side, wire_type)
            rows = catalog[family]
            if family == "flat":
                rows = cls._flat_rows_in_configured_range(rows, craft)
            options = cls._wire_options(rows, wire_type, family)
            if not options:
                raise ValueError(f"{side} 侧导线类别 {wire_type} 在真实目录和当前宽厚范围内没有可选记录")
            result.extend(options)
        return tuple(result)

    @classmethod
    def from_catalog(
        cls, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]],
        allowed_steel_brands: tuple[str, ...] | None = None,
        allowed_core_types: tuple[int, ...] | None = None,
        allowed_low_voltage_wire_types: tuple[int, ...] | None = None,
        allowed_high_voltage_wire_types: tuple[int, ...] | None = None,
        allowed_tank_types: tuple[int, ...] | None = None,
        allowed_corrugation_long_faces: tuple[int, ...] | None = None,
        allowed_corrugation_short_faces: tuple[int, ...] | None = None,
        allowed_radiator_width_codes: tuple[int, ...] | None = None,
        allowed_radiator_groups: tuple[int, ...] | None = None,
    ) -> "GeneDomain":
        craft_core = config["craft_core"]
        diameter_min = int(craft_core["coreDiameterMin"])
        diameter_max = int(craft_core["coreDiameterMax"])
        configured_core_type = int(craft_core.get("isRolledCore", 0))
        active_core_types = set(allowed_core_types if allowed_core_types is not None else (configured_core_type,))
        cores = tuple(sorted(
            (row for row in catalog["core"] if diameter_min <= int(row["core_diameter"]) <= diameter_max
             and int(row["core_type"]) in active_core_types),
            key=lambda row: (int(row["core_type"]), int(row["core_diameter"]), int(row["id"])),
        ))
        if not cores:
            raise ValueError(f"基础目录中没有直径范围 {diameter_min}-{diameter_max} 的铁芯记录")
        low_craft = config["craft_low_coil"]
        high_craft = config["craft_high_coil"]
        low_types = tuple(dict.fromkeys(
            (int(low_craft["wireSpecification"]),)
            if allowed_low_voltage_wire_types is None else allowed_low_voltage_wire_types
        ))
        high_types = tuple(dict.fromkeys(
            (int(high_craft["wireSpecification"]),)
            if allowed_high_voltage_wire_types is None else allowed_high_voltage_wire_types
        ))
        if not low_types or not high_types:
            raise ValueError("低压和高压导线类别至少各需要一个")
        # 先校验类别、目录和页面宽厚过滤，避免在低压层数派生阶段把不合法类别
        # 报成一个误导性的“层数规则不支持”。
        lv_options = cls._options_for_types("lv", low_types, low_craft, catalog)
        hv_options = cls._options_for_types("hv", high_types, high_craft, catalog)
        scope = config.get("optimization_scope") or {}
        lv_turns_domain, lv_turns_metadata = cls._required_java_integer_domain(
            "lv_turns", "低压匝数", scope.get("lowVoltageTurnsMin"), scope.get("lowVoltageTurnsMax"),
        )
        hv_layers_domain, hv_layers_metadata = cls._required_java_integer_domain(
            "hv_layers", "高压层数", high_craft.get("minLayerCount"), high_craft.get("maxLayerCount"),
        )
        # Java 三相没有全局“低压层数 min/max”。它由低压导线类别和当前低压匝数派生：
        # 箔材 0/1 -> 层数=LVT；扁线 2/3 -> 仅 2 或 4。
        lv_layers_domain = tuple(sorted({
            layer
            for wire_type in low_types
            for turns_value in lv_turns_domain
            for layer in cls.low_voltage_layers_for(wire_type, turns_value)
        }))
        lv_layers_metadata = {
            "source": "Java 低压导线类别派生：箔材=低压匝数；扁线=2/4",
            "history_sample_count": 0,
            "configured_minimum": None, "configured_maximum": None,
            "minimum": min(lv_layers_domain), "maximum": max(lv_layers_domain), "step": None,
        }
        configured_brands = cls._configured_steel_brands(config, catalog)
        if allowed_steel_brands is not None:
            existing = {str(row["brand"]) for row in catalog["steel"]}
            configured_brands = tuple(sorted(set(allowed_steel_brands) & existing))
            if not configured_brands:
                raise ValueError("本次研究范围没有保留任何目录中存在的硅钢片牌号")
        return cls(
            steel_brands=configured_brands,
            cores=cores,
            lv_wire_options=lv_options, hv_wire_options=hv_options,
            lv_turns=lv_turns_domain, lv_layers=lv_layers_domain, hv_layers=hv_layers_domain,
            lv_ducts=cls._page_duct_options(low_craft, "低压"),
            hv_ducts=cls._page_duct_options(high_craft, "高压"),
            cooling_options=cls._cooling_options(
                config, catalog,
                allowed_tank_types=allowed_tank_types,
                allowed_corrugation_long_faces=allowed_corrugation_long_faces,
                allowed_corrugation_short_faces=allowed_corrugation_short_faces,
                allowed_radiator_width_codes=allowed_radiator_width_codes,
                allowed_radiator_groups=allowed_radiator_groups,
            ),
            integer_domain_metadata={
                "lv_turns": lv_turns_metadata, "lv_layers": lv_layers_metadata, "hv_layers": hv_layers_metadata,
            },
        )

    def _option_for_candidate(self, side: Side, wire_type: int | None, wire_id: int) -> WireOption:
        options = self.lv_wire_options if side == "lv" else self.hv_wire_options
        matches = [option for option in options if option.record_id == int(wire_id) and (wire_type is None or option.wire_type == int(wire_type))]
        if len(matches) != 1:
            raise ValueError(f"{side} 侧候选导线不在本次完整搜索域中: type={wire_type}, id={wire_id}")
        return matches[0]

    def normalize_candidate_wire_types(self, candidate: ThreePhaseDesignCandidate) -> ThreePhaseDesignCandidate:
        """将历史候选规范化为可进入 GA 的完整域个体。

        单设备历史一致性可以保留 ``cooling_option=None`` 并由精算器复刻旧规则；
        但一旦进入 GA，None 会确定性落到当前域首个实际冷却型号，避免第 0 代和
        后续交叉继续依赖“计算时临时挑一个”的隐藏选择。
        """
        if candidate.steel_brand not in self.steel_brands:
            raise ValueError(f"候选硅钢片牌号不在本次页面允许集合中: {candidate.steel_brand}")
        if candidate.low_voltage_turns not in self.lv_turns:
            raise ValueError(f"候选低压匝数不在本次页面允许范围中: {candidate.low_voltage_turns}")
        if candidate.high_voltage_layers not in self.hv_layers:
            raise ValueError(f"候选高压层数不在本次页面允许范围中: {candidate.high_voltage_layers}")
        lv = self._option_for_candidate("lv", candidate.low_voltage_wire_type, candidate.low_voltage_wire_id)
        hv = self._option_for_candidate("hv", candidate.high_voltage_wire_type, candidate.high_voltage_wire_id)
        if candidate.low_voltage_duct not in self.low_voltage_duct_options:
            raise ValueError(f"候选低压油道不在本次页面允许集合中: {candidate.low_voltage_duct}")
        if candidate.high_voltage_duct not in self.high_voltage_duct_options:
            raise ValueError(f"候选高压油道不在本次页面允许集合中: {candidate.high_voltage_duct}")
        core_matches = [core for core in self.cores if int(core["id"]) == int(candidate.core_data_id)]
        if len(core_matches) != 1:
            raise ValueError(f"候选铁芯不在本次搜索域中: id={candidate.core_data_id}")
        core = core_matches[0]
        conversion_values = self.conversion_values_for_core(core)
        if not conversion_values:
            conversion = None
        elif candidate.conversion_c is None:
            conversion = conversion_values[0]
        else:
            raw = Decimal(str(candidate.conversion_c))
            # 历史方案可能使用目录旧默认值；进入严格 GA 域时量化到当前铁芯的合法
            # 离散格点，避免越过直径 0.55–0.75 的边界。
            conversion = min(conversion_values, key=lambda value: (abs(value - raw), value))
        cooling_option = candidate.cooling_option
        if cooling_option is None and self.cooling_options:
            cooling_option = self.cooling_options[0]
        allowed_lv_layers = self.low_voltage_layers_for(lv.wire_type, candidate.low_voltage_turns)
        # Java 不接受脱离绕组类别的低压层数。箔材确定为匝数，扁线的旧历史值
        # 若不在 2/4 中，量化到最近一个合法档，而不是让后续精算收到虚构组合。
        low_voltage_layers = (
            candidate.low_voltage_layers if candidate.low_voltage_layers in allowed_lv_layers
            else min(allowed_lv_layers, key=lambda value: (abs(value - candidate.low_voltage_layers), value))
        )
        return replace(candidate, low_voltage_wire_type=lv.wire_type, low_voltage_wire_id=lv.record_id,
                       high_voltage_wire_type=hv.wire_type, high_voltage_wire_id=hv.record_id,
                       low_voltage_layers=low_voltage_layers,
                       core_type=int(core["core_type"]), conversion_c=conversion,
                       cooling_option=self._normalize_cooling_option(cooling_option))

    def _normalize_cooling_option(self, option: CoolingOption | None) -> CoolingOption | None:
        """新候选必须来自本次页面选择的完整冷却域；历史 None 只保留兼容读取。"""
        if option is None:
            return None
        if option not in self.cooling_options:
            raise ValueError(f"候选冷却型号不在本次完整搜索域中: {option.key()}")
        return option

    def baseline_candidate(self) -> ThreePhaseDesignCandidate:
        """确定性覆盖的基准点；每个单项覆盖候选只替换其中一个完整基因。"""
        core, lv, hv = self.cores[0], self.lv_wire_options[0], self.hv_wire_options[0]
        candidate = ThreePhaseDesignCandidate(
            steel_brand=self.steel_brands[0], core_type=int(core["core_type"]), core_data_id=int(core["id"]),
            low_voltage_turns=self.lv_turns[0], low_voltage_wire_id=lv.record_id,
            low_voltage_layers=self.low_voltage_layers_for(lv.wire_type, self.lv_turns[0])[0],
            low_voltage_duct=self.low_voltage_duct_options[0],
            high_voltage_wire_id=hv.record_id, high_voltage_layers=self.hv_layers[0],
            high_voltage_duct=self.high_voltage_duct_options[0],
            conversion_c=self._default_conversion_for_core(core),
            low_voltage_wire_type=lv.wire_type, high_voltage_wire_type=hv.wire_type,
            cooling_option=self.cooling_options[0] if self.cooling_options else None,
        )
        return candidate

    @staticmethod
    def _stratified_index(index: int, length: int, count: int) -> int:
        """在确定数量的背景中均匀取目录位置，避免又回到永远取第 1 条。"""
        if length < 1:
            raise ValueError("覆盖背景维度不能为空")
        return min(length - 1, (index * length) // max(1, count))

    def stratified_background_candidates(self) -> tuple[ThreePhaseDesignCandidate, ...]:
        """构造少量、确定性的完整合法背景，而非把 A/B 扩成全组合。

        背景数量只由各维最大取值数决定，限制在 1～6 条；每条按位置分层抽取各个
        维度。A/B 随后轮换这些背景，因此某一条线规或某一对基因不会总与 P0 的
        铁芯、高压、油道和冷却型号绑定。
        """
        cardinality = max(
            len(self.steel_brands), len(self.cores), len(self.lv_wire_options), len(self.hv_wire_options),
            len(self.lv_turns), len(self.hv_layers), len(self.low_voltage_duct_options),
            len(self.high_voltage_duct_options), len(self.cooling_options) if self.cooling_options else 1,
        )
        count = min(6, max(1, math.ceil(math.log2(cardinality + 1))))
        result: list[ThreePhaseDesignCandidate] = []
        for index in range(count):
            core = self.cores[self._stratified_index(index, len(self.cores), count)]
            lv = self.lv_wire_options[self._stratified_index(index, len(self.lv_wire_options), count)]
            hv = self.hv_wire_options[self._stratified_index(index, len(self.hv_wire_options), count)]
            turns = self.lv_turns[self._stratified_index(index, len(self.lv_turns), count)]
            cooling = None if not self.cooling_options else self.cooling_options[
                self._stratified_index(index, len(self.cooling_options), count)
            ]
            result.append(ThreePhaseDesignCandidate(
                steel_brand=self.steel_brands[self._stratified_index(index, len(self.steel_brands), count)],
                core_type=int(core["core_type"]), core_data_id=int(core["id"]),
                low_voltage_turns=turns, low_voltage_wire_id=lv.record_id,
                low_voltage_layers=self.low_voltage_layers_for(lv.wire_type, turns)[0],
                low_voltage_duct=self.low_voltage_duct_options[
                    self._stratified_index(index, len(self.low_voltage_duct_options), count)
                ],
                high_voltage_wire_id=hv.record_id,
                high_voltage_layers=self.hv_layers[self._stratified_index(index, len(self.hv_layers), count)],
                high_voltage_duct=self.high_voltage_duct_options[
                    self._stratified_index(index, len(self.high_voltage_duct_options), count)
                ],
                conversion_c=self._default_conversion_for_core(core),
                low_voltage_wire_type=lv.wire_type, high_voltage_wire_type=hv.wire_type,
                cooling_option=cooling,
            ))
        return tuple(result)

    @staticmethod
    def _coverage_base(
        backgrounds: tuple[ThreePhaseDesignCandidate, ...] | None, position: int,
        fallback: ThreePhaseDesignCandidate,
    ) -> ThreePhaseDesignCandidate:
        active = backgrounds or (fallback,)
        return active[position % len(active)]

    def single_value_coverage_candidates(
        self, backgrounds: tuple[ThreePhaseDesignCandidate, ...] | None = None,
    ) -> tuple[tuple[str, ThreePhaseDesignCandidate], ...]:
        """确定性 A 层覆盖：每个允许离散值至少一次真实精算。

        B 层两两组合不在这里隐式随机生成；调用方可据该明确的维度命名建立后续
        ``铁芯×导线`` 等覆盖计划并记录其来源。
        """
        fallback = self.baseline_candidate()
        result: list[tuple[str, ThreePhaseDesignCandidate]] = []
        for brand in self.steel_brands:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:steel_brand", replace(base, steel_brand=brand)))
        for core in self.cores:
            base = self._coverage_base(backgrounds, len(result), fallback)
            conversion = self._default_conversion_for_core(core)
            result.append(("coverage_single:core", replace(base, core_type=int(core["core_type"]), core_data_id=int(core["id"]), conversion_c=conversion)))
        for option in self.lv_wire_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:lv_wire", replace(
                base, low_voltage_wire_type=option.wire_type, low_voltage_wire_id=option.record_id,
                low_voltage_layers=self.low_voltage_layers_for(option.wire_type, base.low_voltage_turns)[0],
            )))
        for option in self.hv_wire_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:hv_wire", replace(base, high_voltage_wire_type=option.wire_type, high_voltage_wire_id=option.record_id)))
        for turns in self.lv_turns:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:lv_turns", replace(
                base, low_voltage_turns=turns,
                low_voltage_layers=self.low_voltage_layers_for(base.low_voltage_wire_type, turns)[0],
            )))
        # “低压层数”的 A 层不是把全局数列逐个塞给基线导线；而是在基线匝数下，
        # 对每种实际低压导线生成它自己的合法层数。
        for option in self.lv_wire_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            for layers in self.low_voltage_layers_for(option.wire_type, base.low_voltage_turns):
                result.append(("coverage_single:lv_layers", replace(
                    base, low_voltage_wire_type=option.wire_type, low_voltage_wire_id=option.record_id,
                    low_voltage_layers=layers,
                )))
        for layers in self.hv_layers:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:hv_layers", replace(base, high_voltage_layers=layers)))
        for core in self.cores:
            for conversion in self.conversion_values_for_core(core):
                base = self._coverage_base(backgrounds, len(result), fallback)
                result.append((
                    "coverage_single:conversion_c",
                    replace(base, core_type=1, core_data_id=int(core["id"]), conversion_c=conversion),
                ))
        for duct in self.low_voltage_duct_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:lv_duct", replace(base, low_voltage_duct=duct)))
        for duct in self.high_voltage_duct_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:hv_duct", replace(base, high_voltage_duct=duct)))
        for option in self.cooling_options:
            base = self._coverage_base(backgrounds, len(result), fallback)
            result.append(("coverage_single:cooling", replace(base, cooling_option=option)))
        return tuple(result)

    def _dimension_values(self, name: str) -> tuple[object, ...]:
        values: dict[str, tuple[object, ...]] = {
            "steel_brand": self.steel_brands,
            "core": self.cores,
            "lv_wire": self.lv_wire_options,
            "hv_wire": self.hv_wire_options,
            "lv_turns": self.lv_turns,
            "lv_layers": self.lv_layers,
            "hv_layers": self.hv_layers,
            "lv_duct": self.low_voltage_duct_options,
            "hv_duct": self.high_voltage_duct_options,
            "cooling": self.cooling_options,
        }
        return values[name]

    def _set_dimension(self, candidate: ThreePhaseDesignCandidate, name: str, value: object) -> ThreePhaseDesignCandidate:
        if name == "steel_brand":
            return replace(candidate, steel_brand=str(value))
        if name == "core":
            core = value
            assert isinstance(core, dict)
            core_type = int(core["core_type"])
            return replace(candidate, core_type=core_type, core_data_id=int(core["id"]),
                           conversion_c=self._default_conversion_for_core(core))
        if name in {"lv_wire", "hv_wire"}:
            option = value
            assert isinstance(option, WireOption)
            prefix = "low_voltage" if name == "lv_wire" else "high_voltage"
            return replace(candidate, **{f"{prefix}_wire_type": option.wire_type, f"{prefix}_wire_id": option.record_id})
        if name == "cooling":
            assert isinstance(value, CoolingOption)
            return replace(candidate, cooling_option=value)
        fields = {
            "lv_turns": "low_voltage_turns", "lv_layers": "low_voltage_layers", "hv_layers": "high_voltage_layers",
            "lv_duct": "low_voltage_duct", "hv_duct": "high_voltage_duct",
        }
        return replace(candidate, **{fields[name]: value})

    def key_pair_coverage_candidates(
        self, pairs: tuple[tuple[str, str], ...] = KEY_PAIR_COVERAGE,
        strategy: PairCoverageStrategy = "balanced",
        backgrounds: tuple[ThreePhaseDesignCandidate, ...] | None = None,
    ) -> tuple[tuple[str, ThreePhaseDesignCandidate], ...]:
        """实际构造 B 层关键两两覆盖候选，供初始化直接精算并记录来源。

        默认 ``balanced`` 为循环均衡配对：每个维度对只产生
        ``max(len(left), len(right))`` 个候选，但左、右每个允许值均至少参与一次。
        这不是全组合覆盖；精确的组合覆盖率会写入计划摘要。显式传 ``exhaustive``
        才会使用完整笛卡尔积，适合小域验证，不可作为大材料目录的默认策略。
        """
        if strategy not in ("balanced", "exhaustive"):
            raise ValueError(f"未知 B 层两两覆盖策略: {strategy}")
        fallback = self.baseline_candidate()
        result: list[tuple[str, ThreePhaseDesignCandidate]] = []
        for left, right in pairs:
            if (left, right) == ("core", "conversion_c"):
                # conversionC 的合法集合由当前长圆铁芯直径决定，不能把某个铁芯的
                # 数字与另一个铁芯拼接。这里的每一条都是兼容的核心×直线段组合。
                for core in self.cores:
                    for conversion in self.conversion_values_for_core(core):
                        base = self._coverage_base(backgrounds, len(result), fallback)
                        result.append((
                            "coverage_pair:compatible:core×conversion_c",
                            replace(base, core_type=1, core_data_id=int(core["id"]), conversion_c=conversion),
                        ))
                continue
            if (left, right) == ("lv_wire", "lv_turns"):
                # 每一对均由完整低压线规和页面允许匝数组成，层数同时按 Java
                # 规则生成；不会出现“箔材 + 2 层”这类不可能组合。
                if strategy == "exhaustive":
                    pair_values = (
                        (wire, turns) for wire in self.lv_wire_options for turns in self.lv_turns
                    )
                else:
                    count = max(len(self.lv_wire_options), len(self.lv_turns))
                    pair_values = (
                        (self.lv_wire_options[index % len(self.lv_wire_options)], self.lv_turns[index % len(self.lv_turns)])
                        for index in range(count)
                    )
                for wire, turns in pair_values:
                    base = self._coverage_base(backgrounds, len(result), fallback)
                    result.append((f"coverage_pair:{strategy}:lv_wire×lv_turns", replace(
                        base, low_voltage_wire_type=wire.wire_type, low_voltage_wire_id=wire.record_id,
                        low_voltage_turns=turns,
                        low_voltage_layers=self.low_voltage_layers_for(wire.wire_type, turns)[0],
                    )))
                continue
            if (left, right) == ("lv_wire", "lv_layers"):
                # 这里的“兼容”含义是：固定基线低压匝数后，每一种导线仅搭配它
                # 自己允许的层数。层数不是与所有线规作笛卡尔积的独立维度。
                for wire in self.lv_wire_options:
                    base = self._coverage_base(backgrounds, len(result), fallback)
                    for layers in self.low_voltage_layers_for(wire.wire_type, base.low_voltage_turns):
                        result.append(("coverage_pair:compatible:lv_wire×lv_layers", replace(
                            base, low_voltage_wire_type=wire.wire_type, low_voltage_wire_id=wire.record_id,
                            low_voltage_layers=layers,
                        )))
                continue
            left_values, right_values = self._dimension_values(left), self._dimension_values(right)
            if not left_values or not right_values:
                continue
            if strategy == "exhaustive":
                pair_values = (
                    (left_value, right_value)
                    for left_value in left_values for right_value in right_values
                )
            else:
                count = max(len(left_values), len(right_values))
                pair_values = (
                    (left_values[index % len(left_values)], right_values[index % len(right_values)])
                    for index in range(count)
                )
            for left_value, right_value in pair_values:
                base = self._coverage_base(backgrounds, len(result), fallback)
                candidate = self._set_dimension(base, left, left_value)
                candidate = self._set_dimension(candidate, right, right_value)
                result.append((f"coverage_pair:{strategy}:{left}×{right}", candidate))
        return tuple(result)

    def coverage_plan_summary(
        self, include_key_pairs: bool = True, pair_strategy: PairCoverageStrategy = "balanced",
    ) -> dict[str, Any]:
        """纯计数地返回 A/B 覆盖计划，绝不为页面预览构造笛卡尔候选。

        不同 A/B 组之间会和基准候选重合；若不真正构造并按完整基因去重，就不能
        诚实地声称“唯一候选最小数”。因此此处只给去重前容量上界，实际唯一覆盖数
        只能由初始化完成后记录的 ``InitialPopulation.coverage_count`` 报告。
        """
        if pair_strategy not in ("balanced", "exhaustive"):
            raise ValueError(f"未知 B 层两两覆盖策略: {pair_strategy}")
        single_dimension_counts = {
            name: len(self._dimension_values(name))
            for name in ("steel_brand", "core", "lv_wire", "hv_wire", "lv_turns", "lv_layers", "hv_layers", "lv_duct", "hv_duct", "cooling")
        }
        single_dimension_counts["conversion_c"] = sum(len(self.conversion_values_for_core(core)) for core in self.cores)
        pair_coverage: dict[str, dict[str, int | float | str]] = {}
        if include_key_pairs:
            for left, right in KEY_PAIR_COVERAGE:
                if (left, right) == ("core", "conversion_c"):
                    compatible = sum(len(self.conversion_values_for_core(core)) for core in self.cores)
                    long_cores = sum(1 for core in self.cores if int(core["core_type"]) == 1)
                    pair_coverage["core×conversion_c"] = {
                        "strategy": "compatible_by_core_diameter",
                        "left_value_count": long_cores,
                        "right_value_count": compatible,
                        "covered_pair_count": compatible,
                        "total_pair_count": compatible,
                        "coverage_rate": 1.0 if compatible else 0.0,
                    }
                    continue
                if (left, right) == ("lv_wire", "lv_layers"):
                    compatible = sum(
                        len(self.low_voltage_layers_for(option.wire_type, self.lv_turns[0]))
                        for option in self.lv_wire_options
                    )
                    pair_coverage["lv_wire×lv_layers"] = {
                        "strategy": "compatible_by_wire_type_and_turns",
                        "left_value_count": len(self.lv_wire_options),
                        "right_value_count": len(self.lv_layers),
                        "covered_pair_count": compatible,
                        "total_pair_count": compatible,
                        "coverage_rate": 1.0 if compatible else 0.0,
                    }
                    continue
                left_count, right_count = len(self._dimension_values(left)), len(self._dimension_values(right))
                total = left_count * right_count
                covered = total if pair_strategy == "exhaustive" else (max(left_count, right_count) if total else 0)
                pair_coverage[f"{left}×{right}"] = {
                    "strategy": pair_strategy,
                    "left_value_count": left_count,
                    "right_value_count": right_count,
                    "covered_pair_count": covered,
                    "total_pair_count": total,
                    "coverage_rate": 0.0 if total == 0 else covered / total,
                }
        single_total = sum(single_dimension_counts.values())
        pair_total = sum(int(item["covered_pair_count"]) for item in pair_coverage.values())
        return {
            "pair_strategy": pair_strategy if include_key_pairs else None,
            "single_dimension_counts": single_dimension_counts,
            "pair_coverage": pair_coverage,
            "single_candidate_count_before_dedup": single_total,
            "pair_candidate_count_before_dedup": pair_total,  # 本策略实际生成的 B 候选数
            "coverage_candidate_capacity_before_dedup": single_total + pair_total,
            "actual_unique_coverage_available_after_initialization": False,
            "integer_domain": self.integer_domain_metadata,
            "conversion_c": {
                "rule": "仅长圆铁芯；当前铁芯直径 × 0.55–0.75（含边界）",
                "step": str(CONVERSION_C_STEP),
                "compatible_value_count": single_dimension_counts["conversion_c"],
            },
        }

    def summary(self) -> dict[str, Any]:
        """写入 SQLite 的搜索域摘要；包含范围值及其来源，供结果回放而非猜测。"""
        return {
            "steel_brands": len(self.steel_brands), "cores": len(self.cores),
            "lv_wires": len(self.lv_wire_options), "hv_wires": len(self.hv_wire_options),
            "lv_wire_types": sorted({option.wire_type for option in self.lv_wire_options}),
            "hv_wire_types": sorted({option.wire_type for option in self.hv_wire_options}),
            "lv_turns": list(self.lv_turns), "lv_layers": list(self.lv_layers), "hv_layers": list(self.hv_layers),
            "lv_oil_duct_schemes": len(self.low_voltage_duct_options),
            "hv_oil_duct_schemes": len(self.high_voltage_duct_options),
            "cooling_options": len(self.cooling_options),
            "integer_domain": self.integer_domain_metadata,
            "conversion_c": {
                "rule": "仅长圆铁芯；直径 × 0.55–0.75",
                "step": str(CONVERSION_C_STEP),
                "per_core_value_count": {
                    str(int(core["id"])): len(self.conversion_values_for_core(core))
                    for core in self.cores if int(core["core_type"]) == 1
                },
            },
        }

    def random_candidate(self, rng: random.Random) -> ThreePhaseDesignCandidate:
        core, lv, hv = rng.choice(self.cores), rng.choice(self.lv_wire_options), rng.choice(self.hv_wire_options)
        core_type = int(core["core_type"])
        turns = rng.choice(self.lv_turns)
        conversion_values = self.conversion_values_for_core(core)
        candidate = ThreePhaseDesignCandidate(
            steel_brand=rng.choice(self.steel_brands), core_type=core_type, core_data_id=int(core["id"]),
            low_voltage_turns=turns, low_voltage_wire_id=lv.record_id,
            low_voltage_layers=rng.choice(self.low_voltage_layers_for(lv.wire_type, turns)),
            low_voltage_duct=rng.choice(self.low_voltage_duct_options),
            high_voltage_wire_id=hv.record_id, high_voltage_layers=rng.choice(self.hv_layers),
            high_voltage_duct=rng.choice(self.high_voltage_duct_options),
            conversion_c=rng.choice(conversion_values) if conversion_values else None,
            low_voltage_wire_type=lv.wire_type, high_voltage_wire_type=hv.wire_type,
            cooling_option=rng.choice(self.cooling_options) if self.cooling_options else None,
        )
        return self.normalize_candidate_wire_types(candidate)
