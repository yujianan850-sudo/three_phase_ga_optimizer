"""三相变压器单设备精算器（唯一公式文件）。

本文件是 Java ``ThreePhaseSchemeRecordServiceImpl.calculate`` 的 Python 迁移目标。
当前已逐式迁入“接线换算 → 铁芯截面/去级 → 磁密 → 硅钢单位损耗插值 →
候选线规解码 → 两侧绕组基础电气量和电抗高度 → 五段油道半径链 →
铁芯重量与空载损耗 P0 → 圆形/长圆铁芯的 PK 与 UK”四段，并可按字段与历史
``scheme_data`` 一致性核验。

圆形、长圆和椭圆铁芯的波纹油箱和散热器油箱温升、油量、油箱重量和成本链均已迁入；
新增热工、重量、成本字段仍未取得同次 Java 运行结果逐项核验。``complete`` 仅表示 Python
公式链完整，不表示 Java—Python 一致性已经证明。
迁移后只能在本文件追加公式链，不应把单设备计算拆散进 GA 或数据库层。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import math
from typing import Any, Iterable

from .models import CoolingOption, ConstraintViolation, EvaluationResult, ThreePhaseDesignCandidate


ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
THREE = Decimal("3")
FOUR = Decimal("4")
SQRT3 = Decimal("1.7320508075688772")


def D(value: Any) -> Decimal:
    if value is None:
        raise ValueError("公式所需参数为空")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def scale(value: Decimal, digits: int, rounding: str = ROUND_HALF_UP) -> Decimal:
    return value.quantize(Decimal("1").scaleb(-digits), rounding=rounding)


def java_divide(left: Decimal, right: Decimal, digits: int, rounding: str = ROUND_HALF_UP) -> Decimal:
    if right == ZERO:
        raise ZeroDivisionError("Java 公式除数为 0")
    return scale(left / right, digits, rounding)


@dataclass(frozen=True)
class CoreStage:
    core_area: Decimal
    angle_weight: Decimal
    flux_density: Decimal
    volts_per_turn: Decimal
    conversion_c: Decimal


class CandidateRejected(ValueError):
    """候选在本配置下**不可用**，不是公式链缺失，也不是程序缺陷。

    Java 主循环对这类情况一律 ``continue``（跳过该工作项，不进候选列表），
    因此 Python 侧必须把「候选被拒」与「首段精算失败」区分开：
    前者是正常的域内筛选结果，后者才代表迁移缺口或数据缺失。
    """


@dataclass(frozen=True)
class WindingStage:
    """一个绕组由候选线规和层数直接推导的基础几何、电气量。

    这里不复用 Java 的 ``prepare*Candidates`` 过滤流程。所有由候选基因给定的
    线规和层数都走同一条公式；不满足电流密度、宽厚比或高度范围时由 evaluate
    写入约束向量，供近可行档案和定向变异使用。
    """

    bare_thickness: Decimal
    bare_width: Decimal
    insulated_thickness: Decimal
    insulated_width: Decimal
    adjusted_thickness: Decimal
    adjusted_width: Decimal
    total_section: Decimal
    current_density: Decimal
    layer_insulation: Decimal
    layer_count: int
    turns_per_section: Decimal
    reactance_height: Decimal
    eddy_loss_coefficient: Decimal
    lead_loss: Decimal
    insulation_thickness: Decimal
    radial_parallel: Decimal  # 径向并联根数 RPN，对应 Java LVRPN / HVRPN


@dataclass(frozen=True)
class P0Stage:
    window_height: Decimal
    low_yoke_distance: Decimal
    high_yoke_distance: Decimal
    coil_diameter: Decimal
    core_center_distance: Decimal
    core_weight: Decimal
    no_load_loss: Decimal
    calculated_core_diameter: int
    low_radii: tuple[Decimal, ...]
    low_stacks: tuple[Decimal, ...]
    low_ellipse_deltas: tuple[Decimal, ...]
    high_radii: tuple[Decimal, ...]
    high_stacks: tuple[Decimal, ...]
    high_ellipse_deltas: tuple[Decimal, ...]
    initial_ellipse_delta: Decimal


class ThreePhaseSingleDeviceEvaluator:
    """根据固定需求、离散候选和基础目录计算一个设备的性能。"""

    # v9：三相扁线低压层间绝缘厚度改为由导线类别派生（与 Java ThreePhaseCommon.java:1655
    # 的 LVLT = LVWG<=1 ? 0.21 : 0.24 对齐），不再读 craftLowCoil.lowVoltageLayerThickness。
    # 该字符串同时是第 0 代预览复用缓存与一致性报告的版本键，改公式必须同步改它。
    formula_revision = "all-core-thermal-cost-stage-v9-java-lvlt-by-wiretype"

    def __init__(self, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]]):
        self.config = config
        self.catalog = catalog
        self.performance = config["performance_index"]
        self.scope = config["optimization_scope"]
        self.craft_core = config["craft_core"]
        self._cores_by_id = {int(row["id"]): row for row in catalog["core"]}
        self._flat_by_id = {int(row["id"]): row for row in catalog["flat"]}
        self._foil_by_id = {int(row["id"]): row for row in catalog["foil"]}
        self._round_by_id = {int(row["id"]): row for row in catalog["round"]}

    def _frequency_hz(self) -> Decimal:
        """页面频率编码与 Java ConfigCache 保持一致：0=50Hz，其他=60Hz。"""
        return D(50) if int(self.performance["frequency"]) == 0 else D(60)

    def _phase_quantities(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """逐式对应 Java calculate() 中 HVLVWM2 的相电压和相电流换算。"""
        p = D(self.performance["capacity"])
        hvn = D(self.performance["highVoltageRated"])
        lvn = D(self.performance["lowVoltageRated"])
        winding = self.performance["windingConnection"]
        method = int(self.performance.get("windingMethodType") or 0)
        if winding == "Dy":
            lp, plvn, hp, phvn = p, java_divide(lvn, SQRT3, 2), p, hvn
        elif winding == "Yd":
            if method != 0:
                raise ValueError(f"当前 Python 首段尚未覆盖 Yd 的绕制方式: {method}")
            lp, plvn, hp, phvn = p, lvn, p, java_divide(hvn, SQRT3, 2)
        elif winding in ("Yy", "Yz"):
            lp, plvn, hp, phvn = p, java_divide(lvn, SQRT3, 2), p, java_divide(hvn, SQRT3, 0)
        elif winding == "Dd":
            lp, plvn, hp, phvn = p, lvn, p, hvn
        elif winding == "Ddyyn11":
            # Java: LP = P.divide(BD_2); HP = P.divide(BD_2); —— 不带 scale、不带
            # rounding，是精确除法（6500/2 整除，不会抛 ArithmeticException）。
            # 此处**不能**写成 java_divide(p, TWO, 99)：quantize 到 99 位小数需要
            # 103 位有效数字，而 decimal 默认上下文只有 28 位，必然抛
            # InvalidOperation，导致整个 Ddyyn11 配置 0% 可计算。
            lp, hp = p / TWO, p / TWO
            plvn, phvn = java_divide(lvn, SQRT3, 2), hvn
        else:
            raise ValueError(f"不支持的接线方式: {winding}")
        plvc = java_divide(lp * D(1000), THREE * plvn, 1)
        phvc = java_divide(hp * D(1000), THREE * phvn, 1)
        return plvn, phvn, plvc, phvc

    @staticmethod
    def _core_stages(row: dict[str, Any]) -> Iterable[tuple[Decimal, Decimal]]:
        for index in range(15, 0, -1):
            width = row.get(f"stage{index}_width")
            thickness = row.get(f"stage{index}_thickness")
            if width is not None and thickness is not None and D(width) > ZERO and D(thickness) > ZERO:
                yield D(width), D(thickness)

    def _core_stage(self, candidate: ThreePhaseDesignCandidate) -> CoreStage:
        row = self._cores_by_id.get(candidate.core_data_id)
        if row is None:
            raise ValueError(f"铁芯记录不存在: {candidate.core_data_id}")
        if int(row["core_type"]) != candidate.core_type:
            raise ValueError("候选铁芯形式与原始铁芯记录不一致")
        lamination = D(self.craft_core["laminationCoefficient"])
        process = D(self.craft_core["processCoefficient"])
        plvn, _, _, _ = self._phase_quantities()
        hz = self._frequency_hz()
        turns = D(candidate.low_voltage_turns)
        yz_coeff = D("1.1553") if self.performance["windingConnection"] == "Yz" else ONE
        volts_per_turn = scale(java_divide(plvn, turns, 4) * yz_coeff, 4)
        core_type = candidate.core_type
        conversion_c = ZERO
        if core_type in (0, 2):
            area = scale(D(row["cross_sectional_area"]) * lamination / D("0.96"), 4)
            angle = java_divide(D(row["angle_weight"]) * lamination, D("0.96"), 2)
        elif core_type == 1:
            main_width = D(row["stage1_width"])
            main_thickness = D(row["stage1_thickness"])
            conversion_c = candidate.conversion_c if candidate.conversion_c is not None else main_thickness
            base_area = java_divide(D(row["cross_sectional_area"]) * lamination, D("0.96"), 4)
            correction = java_divide(main_width * (main_thickness - conversion_c) * lamination, D(100), 4)
            area = base_area - correction
            angle_correction = java_divide(correction * main_width * D(200) * D("7.65"), D(1_000_000), 4)
            angle = scale(D(row["angle_weight"]) * java_divide(lamination, D("0.96"), 6) - angle_correction, 4)
        else:
            raise ValueError(f"不支持的铁芯形式: {core_type}")

        # Java 逻辑：低压箔绕时，按相电流去掉最小级数后重算截面、角重与磁密。
        lvwg = self._wire_type(candidate, "lv")
        if lvwg in (0, 1):
            _, _, plvc, _ = self._phase_quantities()
            foil_coeff = D("3.5") if lvwg == 1 else D("1.8")
            result = java_divide(plvc, foil_coeff, 1)
            remove_count = 1 if result < D(200) else 2 if result < D("480.00") else 3
            stages = list(self._core_stages(row))
            remove_count = min(remove_count, len(stages))
            if remove_count:
                main_width = D(row["stage1_width"])
                area_reduce = ZERO
                angle_reduce = ZERO
                density_factor = java_divide(D("7.65"), D(1_000_000), 10)
                for width, thickness in stages[:remove_count]:
                    delta_area = java_divide(width * thickness * lamination, D(100), 4)
                    area_reduce += delta_area
                    angle_reduce += scale(lamination * (main_width - width) * width * thickness * density_factor, 4)
                angle_reduce += scale(area_reduce * main_width * D(200) * density_factor, 4)
                area -= area_reduce
                angle = scale(angle - angle_reduce, 2)
        flux = java_divide(volts_per_turn * D(10000), D("4.44") * area * hz, 3)
        return CoreStage(area, angle, flux, volts_per_turn, conversion_c)

    def _normalize_candidate(self, candidate: ThreePhaseDesignCandidate) -> ThreePhaseDesignCandidate:
        """对齐 Java 在进入全部公式前的离散参数归一化。

        当前只有长圆铁芯的 ``conversionC`` 需要处理：Java 无论该值来自历史方案
        还是铁芯表 ``stage1Thickness``，均按整毫米 HALF_UP 后参与面积、周长、
        油箱尺寸及第二遍优化。Python 必须使用同一个归一化后的候选贯穿整条链，
        不能只在铁芯面积公式中局部取整。
        """
        if candidate.core_type != 1:
            return candidate
        row = self._cores_by_id.get(candidate.core_data_id)
        if row is None:
            raise ValueError(f"铁芯记录不存在: {candidate.core_data_id}")
        raw_conversion = candidate.conversion_c
        if raw_conversion is None:
            raw_conversion = D(row["stage1_thickness"])
        conversion = scale(D(raw_conversion), 0, ROUND_HALF_UP)
        return replace(candidate, conversion_c=conversion)

    def _specific_loss(self, brand: str, flux: Decimal) -> Decimal:
        """对应 Java：CT 向下截断至 0.01 后，以相邻表点线性插值。"""
        lower_flux = scale(flux, 2, ROUND_DOWN)
        rows = [row for row in self.catalog["steel"] if str(row["brand"]) == brand]
        by_flux = {D(row["flux_density"]): row for row in rows}
        lower = by_flux.get(lower_flux)
        upper = by_flux.get(lower_flux + D("0.01"))
        if lower is None or upper is None:
            # Java: ThreePhaseSchemeRecordServiceImpl:1240-1244
            #   BigDecimal bj5 = CT.setScale(2, RoundingMode.DOWN);
            #   if (brandConfigs.get(bj5) == null) { continue; }
            #   if (brandConfigs.get(bj5.add(BD_0_01)) == null) { continue; }
            # 磁密档案缺表点时 Java 跳过该工作项，属"候选不可用"，
            # 不是公式链缺失，也不得与"首段精算失败"混淆（见 CandidateRejected）。
            raise CandidateRejected(
                f"硅钢片 {brand} 缺少磁密 {lower_flux} / {lower_flux + D('0.01')} 的损耗数据"
            )
        weight = (flux * D(10) - scale(flux * D(10), 1, ROUND_FLOOR)) * D(10)
        hz_factor = ONE if self._frequency_hz() == D(50) else D("1.33")
        return scale((D(lower["specific_loss"]) + (D(upper["specific_loss"]) - D(lower["specific_loss"])) * weight) * hz_factor, 5)

    @staticmethod
    def _ceil_to_multiple(value: Decimal, unit: Decimal) -> Decimal:
        """对应 Java adjustTo0Or5 等正值向上靠档。"""
        if unit <= ZERO:
            raise ValueError("靠档单位必须大于 0")
        return (value / unit).to_integral_value(rounding=ROUND_CEILING) * unit

    @staticmethod
    def _round_to_half(value: Decimal) -> Decimal:
        return (value * TWO).to_integral_value(rounding=ROUND_CEILING) / TWO

    @staticmethod
    def _flat_insulation(wire_type: int) -> Decimal:
        if wire_type in (2, 3):
            return D("0.45")
        if wire_type in (4, 5):
            return D("0.15")
        return ZERO

    @staticmethod
    def _is_aluminium(wire_type: int, side: str) -> bool:
        return wire_type in ((0, 2) if side == "lv" else (1, 2, 5))

    @staticmethod
    def _material_properties(wire_type: int, side: str, k0: Decimal) -> tuple[Decimal, Decimal]:
        """对应 ThreePhaseCommon.calculateLow/HighVoltageProperties 的 rho、KW。"""
        aluminium = ThreePhaseSingleDeviceEvaluator._is_aluminium(wire_type, side)
        if aluminium:
            return java_divide((D(225) + k0) * D("0.0357"), D(300), 5), D("1.4")
        return java_divide((D(235) + k0) * D("0.02135"), D(310), 5), D("3.8")

    def _wire_type(self, candidate: ThreePhaseDesignCandidate, side: str) -> int:
        """返回候选的完整导线类别；旧历史候选才回退到本次工艺配置。

        目录 ID 不包含导线类别语义：同一数值 ID 只应在与其类别对应的目录表中
        查询。因此多类别搜索时绝不能继续读取配置中的单一 ``wireSpecification``。
        """
        field = "low_voltage_wire_type" if side == "lv" else "high_voltage_wire_type"
        configured = self.config["craft_low_coil" if side == "lv" else "craft_high_coil"]["wireSpecification"]
        raw = getattr(candidate, field, None)
        wire_type = int(configured if raw is None else raw)
        allowed = range(0, 4) if side == "lv" else range(0, 6)
        if wire_type not in allowed:
            raise ValueError(f"{side} 绕组导线类别不受支持: {wire_type}")
        return wire_type

    def _lookup_wire(self, candidate: ThreePhaseDesignCandidate, side: str) -> dict[str, Any]:
        wire_type = self._wire_type(candidate, side)
        wire_id = candidate.low_voltage_wire_id if side == "lv" else candidate.high_voltage_wire_id
        source = self._foil_by_id if side == "lv" and wire_type in (0, 1) else (
            self._round_by_id if side == "hv" and wire_type in (0, 1) else self._flat_by_id
        )
        row = source.get(wire_id)
        if row is None:
            raise ValueError(f"{side} 绕组线规记录不存在或类型不匹配: {wire_id}")
        return row

    def _wire_dimensions(self, candidate: ThreePhaseDesignCandidate, side: str) -> tuple[Decimal, Decimal, Decimal]:
        """返回裸厚、裸宽、目录截面；严格保留一条原始规格记录，绝不拼宽厚。"""
        wire_type = self._wire_type(candidate, side)
        row = self._lookup_wire(candidate, side)
        if side == "hv" and wire_type in (0, 1):
            diameter = D(row["nominal_diameter"])
            return diameter, diameter, D(row["calculate_section"])
        if side == "lv" and wire_type in (0, 1):
            return D(row["thickness"]), D(row["breadth"]), D(row["section"])
        return D(row["line_thickness"]), D(row["line_width"]), D(row["section"])

    @staticmethod
    def _hvl_paper_count(layer_voltage: Decimal, rated_voltage: Decimal) -> int:
        return int(java_divide(layer_voltage, D(450) if rated_voltage <= D(30000) else D(400), 0, ROUND_CEILING))

    @staticmethod
    def _hv_layer_insulation(paper_count: int) -> Decimal:
        values_n = (0, 1, 2, 3, 4, 3, 4, 4, 5, 5)
        values_m = (0, 0, 0, 0, 0, 2, 2, 3, 3, 4)
        if not 0 <= paper_count < len(values_n):
            raise CandidateRejected(
                f"高压层间纸档位 {paper_count} 已达 Java 表上限 {len(values_n)}，该高压层数候选不可用"
            )
        return (D(values_n[paper_count]) + D(values_m[paper_count]) * D("0.5")) * D("0.08")

    def _low_voltage_stage(self, candidate: ThreePhaseDesignCandidate) -> WindingStage:
        c = self.config["craft_low_coil"]
        wire_type = self._wire_type(candidate, "lv")
        bare_t, bare_w, section = self._wire_dimensions(candidate, "lv")
        apn, rpn = D(c["axialWindings"]), D(c["radialWindings"])
        total_section = section * apn * rpn
        _, _, phase_current, _ = self._phase_quantities()
        current_density = java_divide(phase_current, total_section, 3)
        insulation = self._flat_insulation(wire_type)
        foil = wire_type in (0, 1)
        insulated_t = bare_t if foil else bare_t + insulation
        insulated_w = bare_w if foil else bare_w + insulation
        # 与 Java 对齐：三相扁线（wire_type > 1）的层间绝缘厚度由导线类别推导，
        # 不读 craftLowCoil.lowVoltageLayerThickness。
        # 依据 faladi_server .../ThreePhase/ThreePhaseCommon.java:1655
        #   this.LVLT = (this.LVWG <= 1) ? BD_0_21 : BD_0_24;
        # 该配置项在三相 Java 侧**只有单相**消费（SinglePhaseCommon.java:547），三相从不读取。
        layer_insulation = (
            (D("0.26") if bare_t * rpn >= D("1.5") else D("0.21")) if foil
            else (D("0.21") if wire_type <= 1 else D("0.24"))
        )
        layer_count = candidate.low_voltage_turns if foil else candidate.low_voltage_layers
        if layer_count <= 0:
            raise ValueError("低压层数必须为正")
        turns_per_section = java_divide(D(candidate.low_voltage_turns), D(layer_count), 2)
        adjusted_t = insulated_t + (D("0.05") if wire_type > 1 else ZERO)
        adjusted_w = insulated_w + (D("0.05") if wire_type > 1 else ZERO)
        split_multiplier = D(2) if self.performance["windingConnection"] == "Ddyyn11" else ONE
        split_height_add = D(c.get("lowVoltageSectionSpacing") or 0)
        section_spacing = D(self.performance.get("lowVoltageSectionSpacing") or 0)
        if foil:
            h = adjusted_w * apn * split_multiplier + split_height_add
        else:
            h = ((turns_per_section + (ONE if section_spacing == ZERO else TWO)) * apn
                 + (ZERO if rpn == ONE else ONE)) * adjusted_w
            h = scale(h, 2) * D(c["axialWindingCoefficient"])
            h = scale(h, 0, ROUND_CEILING) * split_multiplier + split_height_add
            h = scale(h, 0, ROUND_CEILING) - adjusted_w * apn
        rho, kw = self._material_properties(wire_type, "lv", D(self.performance["loadCalculationTemperature"]))
        lead_length = scale(D("0.005") * (h - D(10)), 2)
        lead_r = java_divide(rho * lead_length, total_section, 7)
        lead_loss = scale(THREE * phase_current * phase_current * lead_r, 0)
        eddy_base = self._frequency_hz() * D(candidate.low_voltage_turns) * (insulated_t - insulation) * rpn * total_section
        eddy = java_divide(kw * eddy_base * eddy_base, D(1_000_000_000) * h * h, 4)
        return WindingStage(bare_t, bare_w, insulated_t, insulated_w, adjusted_t, adjusted_w,
                            total_section, current_density, layer_insulation, layer_count,
                            turns_per_section, h, eddy, lead_loss, insulation, rpn)

    def _high_voltage_stage(self, candidate: ThreePhaseDesignCandidate, core: CoreStage) -> WindingStage:
        c = self.config["craft_high_coil"]
        wire_type = self._wire_type(candidate, "hv")
        bare_t, bare_w, section = self._wire_dimensions(candidate, "hv")
        apn, rpn = D(c["axialWindingCount"]), D(c["radialWindingCount"])
        total_section = section * apn * rpn
        _, phase_voltage, _, phase_current = self._phase_quantities()
        current_density = java_divide(phase_current, total_section, 3)
        insulation = (D(self._lookup_wire(candidate, "hv")["outside_diameter_max"]) - bare_t
                      if wire_type in (0, 1) else self._flat_insulation(wire_type))
        insulated_t, insulated_w = bare_t + insulation, bare_w + insulation
        layer_count = candidate.high_voltage_layers
        if layer_count <= 0:
            raise ValueError("高压层数必须为正")
        td = D(self.performance["tappingInterval"]) / D(100)
        hvt_max = java_divide(phase_voltage * (ONE + D(self.performance["tappingPositivePosition"]) * td), core.volts_per_turn, 0)
        turns_per_section = java_divide(hvt_max, D(layer_count), 0, ROUND_CEILING)
        if turns_per_section - D(4) < hvt_max - (D(layer_count - 1) * turns_per_section):
            turns_per_section += ONE
        section_spacing = D(self.performance.get("highVoltageSectionSpacing") or 0)
        layer_voltage = scale(TWO * core.volts_per_turn * turns_per_section, 1)
        if section_spacing > ZERO:
            layer_voltage = java_divide(layer_voltage, TWO, 2)
        paper_count = self._hvl_paper_count(layer_voltage, D(self.performance["highVoltageRated"]))
        layer_insulation = self._hv_layer_insulation(paper_count)
        paper_adjust = D("0.05") if wire_type > 1 else ZERO
        adjusted_t, adjusted_w = insulated_t + paper_adjust, insulated_w + paper_adjust
        split_multiplier = D(2) if self.performance["windingConnection"] == "Ddyyn11" else ONE
        h = ((turns_per_section + (ONE if section_spacing == ZERO else TWO)) * apn
             + (ZERO if rpn == ONE else ONE)) * adjusted_w
        h = scale(h, 2) * D(c["axialWindingCoefficient"])
        h = scale(h, 0, ROUND_CEILING) * split_multiplier + section_spacing
        h = scale(h, 0, ROUND_CEILING) - adjusted_w * apn
        _, kw = self._material_properties(wire_type, "hv", D(self.performance["loadCalculationTemperature"]))
        rated_turns = java_divide(phase_voltage, core.volts_per_turn, 0)
        eddy_base = self._frequency_hz() * rated_turns * (insulated_t - insulation) * rpn * total_section
        eddy = java_divide(kw * eddy_base * eddy_base, D(1_000_000_000) * h * h, 4)
        return WindingStage(bare_t, bare_w, insulated_t, insulated_w, adjusted_t, adjusted_w,
                            total_section, current_density, layer_insulation, layer_count,
                            turns_per_section, h, eddy, ZERO, insulation, rpn)

    @staticmethod
    def _shift_yoke_distance(base: Decimal, original: Decimal, target: Decimal) -> Decimal | None:
        high, low = target - base, target - base - D(5)
        if original > low and original <= high:
            value = original
        elif original <= low:
            value = low.to_integral_value(rounding=ROUND_FLOOR) + ONE
        else:
            value = high
        return value if abs(value - original) <= D(5) else None

    def _window_geometry_and_p0(self, candidate: ThreePhaseDesignCandidate, core: CoreStage,
                                lv: WindingStage, hv: WindingStage, specific_loss: Decimal) -> P0Stage:
        """迁移 P0 前的共同几何段；只从候选基因及配置读取，不调用旧循环筛选。"""
        isolation = self.config["craft_isolation"]
        core_row = self._cores_by_id[candidate.core_data_id]
        lv_base = (lv.reactance_height + (lv.adjusted_width * D(self.config["craft_low_coil"]["axialWindings"])
                                          if self._wire_type(candidate, "lv") > 1 else ZERO))
        lv_base = lv_base.to_integral_value(rounding=ROUND_CEILING)
        hv_base = hv.reactance_height + hv.adjusted_width * D(self.config["craft_high_coil"]["axialWindingCount"])
        adj_lv = self._ceil_to_multiple(lv_base + D(isolation["lowVoltageToYokeDistance"]), D(5))
        adj_hv = self._ceil_to_multiple(hv_base + D(isolation["highVoltageToYokeDistance"]), D(5))
        window_height = max(adj_lv, adj_hv)
        if adj_lv >= adj_hv:
            high_yoke = self._shift_yoke_distance(hv_base, D(isolation["highVoltageToYokeDistance"]), window_height)
            low_yoke = D(isolation["lowVoltageToYokeDistance"])
        else:
            high_yoke = D(isolation["highVoltageToYokeDistance"])
            low_yoke = self._shift_yoke_distance(lv_base, D(isolation["lowVoltageToYokeDistance"]), window_height)
        if high_yoke is None or low_yoke is None:
            # Java: ThreePhaseSchemeRecordServiceImpl:1335-1337
            #   BigDecimal[] calculationResult = findCrgoaAndYdd(...);
            #   if (calculationResult == null) { continue; }
            # 两侧轭距在 ±5 内无法对齐时 Java 直接跳过该工作项。
            raise CandidateRejected("绕组高度无法在 Java 规定的轭距 ±5 范围内对齐，该候选不可用")

        _, lv_segments = self._layer_distribution(
            lv.layer_count, candidate.low_voltage_turns, candidate.low_voltage_duct.count,
            use_low_voltage_foil=self._wire_type(candidate, "lv") in (0, 1),
        )
        _, hv_segments = self._layer_distribution(
            hv.layer_count, candidate.low_voltage_turns, candidate.high_voltage_duct.count,
            use_low_voltage_foil=False,
        )
        lv_stacks = self._segment_stacks(lv_segments, lv, D(self.config["craft_low_coil"]["radialWindingCoefficient"]))
        high_first_paper = (D(3) if D(self.performance["highVoltageRated"]) > D(30000) else D(2)) * D("0.08")
        high_last_paper = TWO * D("0.08")
        hv_stacks = self._segment_stacks(
            hv_segments, hv, D(self.config["craft_high_coil"]["radialWindingCoefficient"]),
            high_first_paper, high_last_paper,
        )
        lv_half_paper = D("0.5") if lv.layer_insulation < D("0.5") else ONE
        hv_half_paper = D("0.5") if hv.layer_insulation < D("0.5") else ONE
        core_diameter = self._calculated_core_diameter(core_row, candidate.core_type)
        radius = D(core_diameter) / TWO + D(isolation["coreToLowVoltageDistance"])
        ellipse_delta = (java_divide(D(core_row["major_axis"]), TWO, 2)
                         - java_divide(D(core_row["minor_axis"]), TWO, 2)) if candidate.core_type == 2 else ZERO
        initial_ellipse_delta = ellipse_delta
        lv_radii: list[Decimal] = []
        lv_deltas: list[Decimal] = []
        for index, stack in enumerate(lv_stacks):
            lv_radii.append(radius)
            lv_deltas.append(ellipse_delta)
            has_duct = candidate.low_voltage_duct.count >= index and index < 4 and lv_segments[index] > 0
            radius = self._next_radius(radius, stack, D(self.config["craft_low_coil"]["lvOilDuctThickness"]),
                                       candidate.low_voltage_duct.duct_type, has_duct, lv_half_paper)
            if candidate.core_type == 2 and has_duct and candidate.low_voltage_duct.duct_type == 1:
                ellipse_delta += D(self.config["craft_low_coil"]["lvOilDuctThickness"]) - lv_half_paper
        # Java 第五段的外径在 LVO5CR + LVO5ST 后接主空道；上面循环已经完成这一步。
        radius += D(isolation["lowToHighMainAirway"])
        hv_radii: list[Decimal] = []
        hv_deltas: list[Decimal] = []
        for index, stack in enumerate(hv_stacks):
            hv_radii.append(radius)
            hv_deltas.append(ellipse_delta)
            # HVO5 在段 5 为空时使用“任意前段油道”规则；它不影响 HVO5 自身内径，
            # 只影响一个不存在段之后的半径，因此 P0 外径不需再向外延伸。
            has_duct = candidate.high_voltage_duct.count >= index and index < 4 and hv_segments[index] > 0
            if index == 4 and hv_segments[4] == 0:
                has_duct = candidate.high_voltage_duct.count > 0 and any(item > 0 for item in hv_segments[:4])
            radius = self._next_radius(radius, stack, D(self.config["craft_high_coil"]["hvOilDuctThickness"]),
                                       candidate.high_voltage_duct.duct_type, has_duct, hv_half_paper)
            if candidate.core_type == 2 and has_duct and candidate.high_voltage_duct.duct_type == 1:
                ellipse_delta += D(self.config["craft_high_coil"]["hvOilDuctThickness"]) - hv_half_paper
        coil_diameter = TWO * (hv_radii[4] + hv_stacks[4])
        center_distance = self._ceil_to_multiple(coil_diameter + D(isolation["coilDistance"]), D(5))
        process = D(self.craft_core["processCoefficient"])
        if candidate.core_type == 1:
            rect_area = scale(core.conversion_c * D(core_diameter) / D(100), 10) * D(self.craft_core["laminationCoefficient"])
            circle_area = core.core_area - rect_area
            correction = D(5) if D(core_diameter) > D(core_row["stage2_width"]) else ZERO
            core_weight = (
                THREE * rect_area * window_height * D("0.000765")
                + D(4) * rect_area * center_distance * D("0.000765")
                + THREE * circle_area * (window_height - correction) * D("0.000765")
                + D(4) * circle_area * (center_distance - correction) * D("0.000765")
                + core.angle_weight
            )
        else:
            core_weight = (
                THREE * core.core_area * window_height * D("0.000765")
                + D(4) * core.core_area * center_distance * D("0.000765")
                + core.angle_weight
            )
        core_weight = scale(core_weight, 0)
        no_load_loss = scale(core_weight * specific_loss * process, 0)
        return P0Stage(window_height, low_yoke, high_yoke, coil_diameter, center_distance, core_weight, no_load_loss,
                       core_diameter, tuple(lv_radii), tuple(lv_stacks), tuple(lv_deltas),
                       tuple(hv_radii), tuple(hv_stacks), tuple(hv_deltas), initial_ellipse_delta)

    @staticmethod
    def _layer_distribution(layer_count: int, low_turns: int, duct_count: int,
                            use_low_voltage_foil: bool) -> tuple[list[int], list[int]]:
        """对应 ThreePhaseCommon.calculateLayerDistribution + computeSegmentLayers。"""
        total = low_turns if use_low_voltage_foil else layer_count
        if duct_count == 0:
            cumulative = [0, 0, 0, 0, total]
        else:
            sections = min(duct_count + 1, 5)
            middle = min(sections - 1, 4)
            cumulative = [0] * 5
            for index in range(middle):
                cumulative[index] = total * (index + 1) // sections
            for index in range(middle, 4):
                cumulative[index] = cumulative[middle - 1]
            cumulative[4] = total
        segments: list[int] = []
        previous = 0
        for item in cumulative:
            if item > 0:
                segments.append(item - previous)
                previous = item
            else:
                segments.append(0)
        return cumulative, segments

    def _segment_stacks(self, segments: list[int], stage: WindingStage, radial_coefficient: Decimal,
                        first_paper: Decimal = ZERO, last_paper: Decimal = ZERO) -> list[Decimal]:
        stacks: list[Decimal] = []
        for index, layers in enumerate(segments):
            if layers <= 0:
                stacks.append(ZERO)
                continue
            raw = D(layers) * stage.adjusted_thickness * stage.radial_parallel \
                + D(layers - 1) * stage.layer_insulation
            if index == 0:
                raw += first_paper
            if index == 4:
                raw += last_paper
            stacks.append(self._round_to_half(raw * radial_coefficient))
        return stacks

    @staticmethod
    def _next_radius(radius: Decimal, stack: Decimal, duct_thickness: Decimal,
                     duct_type: int, has_previous_duct: bool, half_paper: Decimal) -> Decimal:
        if not has_previous_duct or duct_type == 0:
            return radius + stack
        if duct_type == 1:
            return radius + stack + half_paper
        if duct_type == 2:
            return radius + stack + duct_thickness
        raise ValueError(f"未知油道类型: {duct_type}")

    @staticmethod
    def _gasket_height(core_diameter: int) -> Decimal:
        """对应 Java ThreePhaseCommon.getGasketHeightByCoreDiameter。"""
        if core_diameter < 160:
            return D(15)
        return D(19) if core_diameter <= 250 else D(23)

    @staticmethod
    def _calculated_core_diameter(row: dict[str, Any], core_type: int) -> int:
        if core_type == 2:
            return int(scale(D(row["minor_axis"]), 0))
        return int(D(row["core_diameter"]))

    def _circumference(self, radius: Decimal, candidate: ThreePhaseDesignCandidate,
                       ellipse_delta: Decimal = ZERO) -> Decimal:
        """对应 ThreePhaseCommon.calculateCircumference；PK 所用圆/长圆周长。"""
        if candidate.core_type == 1:
            return scale(D("6.283") * radius + TWO * candidate.conversion_c, 1)
        if candidate.core_type == 0:
            return scale(D("6.283") * radius, 1)
        # Java: π × [1.5(a+b) - sqrt(a*b)]；radius 是短轴半径，delta 是长短轴差。
        a, b = radius + ellipse_delta, radius
        term = D("1.5") * (a + b) - Decimal(str(math.sqrt(float(a * b))))
        return scale(D("3.1415") * term, 1)

    @staticmethod
    def _density(wire_type: int, side: str) -> Decimal:
        return D("2.7") if ThreePhaseSingleDeviceEvaluator._is_aluminium(wire_type, side) else D("8.9")

    @staticmethod
    def _ceil_half(value: Decimal) -> Decimal:
        return (value / D("0.5")).to_integral_value(rounding=ROUND_CEILING) * D("0.5")

    @staticmethod
    def _ky_percent(phase_voltage: Decimal, is_y_connection: bool) -> Decimal:
        if phase_voltage >= D(35000):
            return ZERO if is_y_connection else D("0.5")
        if phase_voltage >= D(10000):
            return D("0.5") if is_y_connection else ONE
        if phase_voltage >= D(6000):
            return ONE if is_y_connection else TWO
        if phase_voltage >= D(3000):
            return D("1.5") if is_y_connection else THREE
        return ZERO

    @staticmethod
    def _sqrt_mc4(value: Decimal) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = 4
            return +value.sqrt()

    def _pk_uk_stage(self, candidate: ThreePhaseDesignCandidate, core: CoreStage, p0: P0Stage,
                     lv: WindingStage, hv: WindingStage) -> dict[str, Decimal]:
        """迁移 Java P0 后的绕组电阻、附加损耗、漏抗和 UK 计算段。

        目前圆形与长圆铁芯进入本段；椭圆的“油道导致长短轴同时变化”的周长分支
        仍须逐字段核验后再开放，不能以近似替代。
        """
        lv_cfg, hv_cfg = self.config["craft_low_coil"], self.config["craft_high_coil"]
        isolation, tank = self.config["craft_isolation"], self.config["craft_fuel_tank"]
        _, phase_voltage, phase_lv_current, phase_hv_current = self._phase_quantities()
        multiplier = TWO if self.performance["windingConnection"] == "Ddyyn11" else ONE
        lv_cum, lv_segments = self._layer_distribution(
            lv.layer_count, candidate.low_voltage_turns, candidate.low_voltage_duct.count,
            use_low_voltage_foil=self._wire_type(candidate, "lv") in (0, 1),
        )
        hv_cum, hv_segments = self._layer_distribution(
            hv.layer_count, candidate.low_voltage_turns, candidate.high_voltage_duct.count,
            use_low_voltage_foil=False,
        )
        lv_average_length = java_divide(
            (self._circumference(p0.low_radii[0], candidate, p0.low_ellipse_deltas[0])
             + self._circumference(p0.low_radii[4] + p0.low_stacks[4], candidate, p0.low_ellipse_deltas[4])) * D("0.001"), TWO, 4,
        )
        lv_total_length = scale(lv_average_length * D(candidate.low_voltage_turns) + D("0.5"), 2)
        lv_type = self._wire_type(candidate, "lv")
        hv_type = self._wire_type(candidate, "hv")
        lv_rho, _ = self._material_properties(lv_type, "lv", D(self.performance["loadCalculationTemperature"]))
        lv_resistance = java_divide(lv_rho * lv_total_length, lv.total_section, 6)
        lv_loss = scale(THREE * lv_resistance * phase_lv_current * phase_lv_current, 0) * multiplier
        lv_weight = java_divide(lv_total_length * lv.total_section * self._density(lv_type, "lv") * THREE, D(1000), 1) * multiplier
        lv_correction = java_divide(D("3.825") * (lv.bare_thickness + lv.bare_width + D("0.35325")),
                                    (lv.total_section / D(lv_cfg["axialWindings"]) / D(lv_cfg["radialWindings"])) * D(100), 3) + ONE
        lv_weight = self._ceil_half(lv_weight * lv_correction)

        hv_averages = tuple(java_divide(
            (self._circumference(p0.high_radii[index], candidate, p0.high_ellipse_deltas[index])
             + self._circumference(p0.high_radii[index] + p0.high_stacks[index], candidate, p0.high_ellipse_deltas[index])) * D("0.001"), TWO, 4,
        ) for index in range(5))
        rated_turns = java_divide(phase_voltage, core.volts_per_turn, 0)
        td = D(self.performance["tappingInterval"]) / D(100)
        hvt_max = java_divide(phase_voltage * (ONE + D(self.performance["tappingPositivePosition"]) * td), core.volts_per_turn, 0)
        hvt_min = java_divide(phase_voltage * (ONE + D(self.performance["tappingNegativePosition"]) * td), core.volts_per_turn, 0)
        hvtall_1 = sum((hv_averages[index] * D(hv_segments[index]) * hv.turns_per_section for index in range(4)), ZERO)
        hvtall_1 += hv_averages[4] * (rated_turns - sum((D(hv_segments[index]) * hv.turns_per_section for index in range(4)), ZERO))
        hvtall_1 = scale(hvtall_1, 2)
        hvtall_2_source = hvtall_1 + (
            hv_averages[1] * (hvt_max - rated_turns)
            if candidate.high_voltage_duct.count == 0
            else hv_averages[3] * (rated_turns - hvt_min) + ONE
        )
        hvtall_2 = self._ceil_half(scale(hvtall_2_source, 2))
        hv_rho, _ = self._material_properties(hv_type, "hv", D(self.performance["loadCalculationTemperature"]))
        hv_resistance = java_divide(hv_rho * hvtall_1, hv.total_section, 6)
        hv_loss = scale(THREE * hv_resistance * phase_hv_current * phase_hv_current, 0) * multiplier
        hv_weight = java_divide(hvtall_2 * hv.total_section * self._density(hv_type, "hv") * THREE, D(1000), 1) * multiplier
        if hv_type in (0, 1):
            hv_weight = self._ceil_half(hv_weight * D("1.02"))
        else:
            hv_correction = java_divide(D("3.825") * (hv.bare_thickness + hv.bare_width + D("0.35325")),
                                        (hv.total_section / D(hv_cfg["axialWindingCount"]) / D(hv_cfg["radialWindingCount"])) * D(100), 3) + ONE
            hv_weight = self._ceil_half(hv_weight * hv_correction)

        lv_eddy = scale(lv.eddy_loss_coefficient * lv_loss, 0)
        hv_eddy = scale(hv.eddy_loss_coefficient * hv_loss, 0)
        hk = java_divide(lv.reactance_height + hv.reactance_height, D(20), 2)
        tank_l = self._ceil_to_multiple(TWO * p0.core_center_distance + p0.coil_diameter + D(tank["tankLongGap"]), D(5))
        hpsoppt = D("0.5") if hv.layer_insulation < D("0.5") else ONE
        tank_w_extra = (ZERO if candidate.high_voltage_duct.duct_type == 2 else
                        D(candidate.high_voltage_duct.count) * (D(hv_cfg["hvOilDuctThickness"]) - hpsoppt) * TWO
                        if candidate.high_voltage_duct.duct_type == 1 else
                        D(candidate.high_voltage_duct.count) * D(hv_cfg["hvOilDuctThickness"]) * TWO)
        tank_w = self._ceil_to_multiple(D(tank["tankWideGap"]) + p0.coil_diameter + core.conversion_c + tank_w_extra, D(5))
        gasket_height = self._gasket_height(p0.calculated_core_diameter)
        tank_h = self._ceil_to_multiple(gasket_height + p0.window_height + TWO * D(p0.calculated_core_diameter) + D(tank["tankHighGap"]), D(5))
        mad_radius = p0.low_radii[4] + p0.low_stacks[4]
        mad_effective_radius = (D("6.283") * (mad_radius + D(isolation["lowToHighMainAirway"]) / TWO) + TWO * core.conversion_c) / D("62.83") \
            if candidate.core_type == 1 else java_divide(mad_radius + D(isolation["lowToHighMainAirway"]) / TWO, D(10), 2)
        sl_inner = java_divide(core.core_area * core.flux_density * D(10), D(1000), 4)
        sl_denominator = D("0.2") * (tank_l + tank_w) * (
            hk + TWO * (java_divide(tank_l + tank_w - p0.core_center_distance - p0.core_center_distance, D(40), 2) - mad_effective_radius)
        ) ** 2
        stray_loss = java_divide(D("2.19") * sl_inner * sl_inner * D(self.performance["impedanceStandard"]) ** 2 * hk ** 3,
                                  sl_denominator, 0)
        ky = self._ky_percent(phase_voltage, self.performance["windingConnection"] in ("Yy", "Yz"))
        hv_lead_loss = ZERO if ky == ZERO else java_divide(hv_loss * ky, D(100), 0)

        # 19 段漏抗计算：低压五段/油道、主空道、高压五段/油道。
        eq_radius = self._equivalent_core_radius(candidate, p0.calculated_core_diameter)
        cumulative_radius = eq_radius + D(isolation["coreToLowVoltageDistance"])
        total_leak, total_thickness = ZERO, ZERO
        lv_half_paper = D("0.5") if lv.layer_insulation < D("0.5") else ONE
        for index, stack in enumerate(p0.low_stacks):
            if lv_segments[index] > 0:
                total_leak += self._winding_leak(cumulative_radius, stack, lv.insulation_thickness)
                total_thickness += (stack - lv.insulation_thickness) * D("0.1")
                cumulative_radius += stack
            has_duct = candidate.low_voltage_duct.count >= index + 1 and index < 4 and lv_segments[index] > 0
            if has_duct:
                effective = self._effective_duct(candidate.low_voltage_duct.duct_type, D(lv_cfg["lvOilDuctThickness"]),
                                                 lv_half_paper, lv.insulation_thickness)
                ratio = java_divide(D(lv_cum[index]), D(lv.layer_count), 4) ** 2
                total_leak += self._duct_leak(cumulative_radius, D(lv_cfg["lvOilDuctThickness"]), effective, ratio)
                total_thickness += effective
                cumulative_radius += D(lv_cfg["lvOilDuctThickness"])
        main_effective = (D(isolation["lowToHighMainAirway"]) + java_divide(hv.insulation_thickness + lv.insulation_thickness, TWO, 3)) * D("0.1")
        main_average = cumulative_radius + java_divide(D(isolation["lowToHighMainAirway"]), TWO, 2)
        total_leak += main_effective * java_divide(main_average, D(10), 2)
        total_thickness += main_effective
        cumulative_radius += D(isolation["lowToHighMainAirway"])
        for index, stack in enumerate(p0.high_stacks):
            if index == 4 and hv_segments[index] > 0:
                average = cumulative_radius + java_divide(stack, TWO, 2)
                full_layers = hv.layer_count - 1
                last_turns = hvt_max - D(full_layers) * hv.turns_per_section
                unfilled = hv.turns_per_section + (hvt_max - rated_turns) - last_turns
                thickness = (stack - hv.insulation_thickness
                             - java_divide(unfilled, hv.turns_per_section, 3) * (hv.adjusted_thickness + hv.layer_insulation)) * D("0.1")
                total_leak += java_divide(thickness * java_divide(average, D(10), 2), THREE, 4)
                total_thickness += thickness
                cumulative_radius += stack
            elif hv_segments[index] > 0:
                total_leak += self._winding_leak(cumulative_radius, stack, hv.insulation_thickness)
                total_thickness += (stack - hv.insulation_thickness) * D("0.1")
                cumulative_radius += stack
            has_duct = candidate.high_voltage_duct.count >= index + 1 and index < 4 and hv_segments[index] > 0
            if has_duct:
                effective = self._effective_duct(candidate.high_voltage_duct.duct_type, D(hv_cfg["hvOilDuctThickness"]),
                                                 hpsoppt, hv.insulation_thickness)
                remain = rated_turns - sum((D(hv_segments[i]) * hv.turns_per_section for i in range(index + 1)), ZERO)
                ratio = java_divide(remain, rated_turns, 4) ** 2
                total_leak += self._duct_leak(cumulative_radius, D(hv_cfg["hvOilDuctThickness"]), effective, ratio)
                total_thickness += effective
                cumulative_radius += D(hv_cfg["hvOilDuctThickness"])
        avg_height = java_divide(lv.reactance_height + hv.reactance_height, TWO, 2)
        rogowski = ONE - java_divide(total_thickness * D(10), avg_height * D("3.1415"), 4)
        lv_eddy = scale(lv_eddy * rogowski * rogowski, 0)
        hv_eddy = scale(hv_eddy * rogowski * rogowski, 0)
        load_coeff = D(self.performance["loadLossCoefficient"])
        # 对齐当前 Java：负载损耗系数仅作用于高、低压电阻损耗；
        # 涡流、杂散和引线损耗仍保留为温升等后续分项计算的中间量，但不计入 PK。
        load_loss = scale((hv_loss + lv_loss) * load_coeff, 0)
        leak_factor = D(self.performance["impedanceCoefficient"])
        ni = phase_lv_current * D(candidate.low_voltage_turns) * multiplier
        # Java: LEAX 分母 = (ET × avgReactH / 10) 先四舍五入到 2 位,再 × 10000。
        # 取整点必须在乘 ET 之后,否则 ET 不进位时结果偏小(详见 28933 的 ukk 残差)。
        leax = java_divide(D("24.8") * java_divide(self._frequency_hz(), D(50), 5) * ni * total_leak * rogowski * leak_factor,
                           java_divide(core.volts_per_turn * avg_height, D(10), 2) * D(10000), 3)
        ukr = java_divide(load_loss, D(self.performance["capacity"]) * D(10), 3)
        uk = self._sqrt_mc4(leax * leax + ukr * ukr)
        return {
            "wlvtall": lv_total_length, "wlvkg": lv_weight, "wlvpk": lv_loss, "hvtall1": hvtall_1, "hvtall2": hvtall_2,
            "hvkg": hv_weight, "hvpk": hv_loss, "lvecl": lv_eddy, "hvecl": hv_eddy, "sl": stray_loss,
            "lvwll": lv.lead_loss, "hvwll": hv_lead_loss, "hvlvpk": load_loss,
            "rogowski": rogowski, "leax": leax, "ukr": ukr, "ukk": uk,
            "total_leak": total_leak, "total_thickness": total_thickness,
            "tankl": tank_l, "tankw": tank_w, "tankh": tank_h,
        }

    @staticmethod
    def _winding_leak(inner_radius: Decimal, stack: Decimal, paper: Decimal) -> Decimal:
        average = inner_radius + java_divide(stack, TWO, 2)
        effective = (stack - paper) * D("0.1")
        return java_divide(effective * java_divide(average, D(10), 2), THREE, 4)

    @staticmethod
    def _duct_leak(inner_radius: Decimal, structural: Decimal, effective: Decimal, ratio: Decimal) -> Decimal:
        average = inner_radius + java_divide(structural, TWO, 2)
        return effective * ratio * java_divide(average, D(10), 2)

    @staticmethod
    def _effective_duct(duct_type: int, thickness: Decimal, half_paper: Decimal, insulation: Decimal) -> Decimal:
        raw = thickness if duct_type == 2 else D("0.75") * thickness + D("0.25") * half_paper
        return (raw + insulation) * D("0.1")

    def _equivalent_core_radius(self, candidate: ThreePhaseDesignCandidate, core_diameter: int) -> Decimal:
        if candidate.core_type == 1:
            return java_divide(D(core_diameter), TWO, 1) + java_divide(candidate.conversion_c, D("3.1415"), 1)
        if candidate.core_type == 2:
            row = self._cores_by_id[candidate.core_data_id]
            a_half, b_half = java_divide(D(row["major_axis"]), TWO, 2), java_divide(D(row["minor_axis"]), TWO, 2)
            term = D("1.5") * (a_half + b_half) - Decimal(str(math.sqrt(float(a_half * b_half))))
            return java_divide(scale(term, 1), TWO, 1)
        return java_divide(D(core_diameter), TWO, 1)

    @staticmethod
    def _floor_to_multiple(value: Decimal, unit: Decimal) -> Decimal:
        return (value / unit).to_integral_value(rounding=ROUND_FLOOR) * unit

    @staticmethod
    def _pow(value: Decimal, exponent: float) -> Decimal:
        """对应 Java Math.pow(double) 的热工经验式实现。"""
        if value < ZERO:
            raise ValueError("热工经验式底数不能为负")
        return Decimal(str(float(value) ** exponent))

    def _half_oil_line(self, candidate: ThreePhaseDesignCandidate, duct_type: int,
                       radius: Decimal, stack: Decimal, duct_thickness: Decimal,
                       ellipse_delta: Decimal = ZERO) -> Decimal:
        """对应 ThreePhaseCommon.calculateHalfoilline。"""
        if stack == ZERO or duct_type == 0:
            return ZERO
        base = radius + stack + duct_thickness / TWO
        if candidate.core_type == 0:
            result = D("3.1415") * base
            return scale(D("0.75") * result if duct_type == 1 else result, 0)
        if candidate.core_type == 2:
            a, b = base + ellipse_delta, base
            term = D("1.5") * (a + b) - Decimal(str(math.sqrt(float(a * b))))
            half_perimeter = D("1.57075") * term
            return scale(D("0.75") * half_perimeter if duct_type == 1 else half_perimeter, 0)
        # 长圆：半油道只取半圆周的 75%，全油道增加直线段。
        if duct_type == 1:
            return scale(D("0.75") * D("3.1415") * base, 0)
        return scale(D("3.1415") * base + candidate.conversion_c, 0)

    @staticmethod
    def _winding_temp_rise(loss: Decimal, area: Decimal, oil_average: Decimal,
                           thickness: Decimal, layers: Decimal, oil_factor: Decimal) -> Decimal:
        q2 = java_divide(loss * D(1_000_000), area * THREE, 0)
        surface = scale(D("0.065") * ThreePhaseSingleDeviceEvaluator._pow(q2, 0.8), 2)
        tcj_raw = q2 * (layers - oil_factor) * (thickness - D("0.64")) * D("0.002")
        tcj = scale(tcj_raw, 2) if tcj_raw > ZERO else ZERO
        tcs_raw = q2 * (layers - TWO * oil_factor) * D("0.002") * min(thickness, D("0.64"))
        tcs = scale(tcs_raw, 2) if tcs_raw > ZERO else ZERO
        return surface + oil_average + tcj + tcs

    def _thermal_areas(self, candidate: ThreePhaseDesignCandidate, p0: P0Stage,
                       lv: WindingStage, hv: WindingStage) -> tuple[Decimal, Decimal]:
        """迁移 Phase 2B/3 的绕组散热面积。"""
        lv_cfg, hv_cfg, isolation = self.config["craft_low_coil"], self.config["craft_high_coil"], self.config["craft_isolation"]
        _, lv_segments = self._layer_distribution(lv.layer_count, candidate.low_voltage_turns, candidate.low_voltage_duct.count,
                                                   self._wire_type(candidate, "lv") in (0, 1))
        _, hv_segments = self._layer_distribution(hv.layer_count, candidate.low_voltage_turns, candidate.high_voltage_duct.count, False)
        lv_effective, hv_effective = lv.reactance_height - D(self.performance["lowVoltageSectionSpacing"] or 0), hv.reactance_height - D(self.performance["highVoltageSectionSpacing"] or 0)
        # OL、低压油道、低压最外侧；Java 中无油道段不会贡献油道散热面积。
        ol = self._floor_to_multiple(self._half_oil_line(candidate, 2, D(p0.calculated_core_diameter) / TWO,
                                                          D(isolation["coreToLowVoltageDistance"]), ZERO,
                                                          p0.initial_ellipse_delta), D(25))
        lv_area = scale(ol * lv_effective * D(lv_cfg["lvBusbarCoeff"]) * TWO, 0)
        for index in range(4):
            if candidate.low_voltage_duct.count >= index + 1 and lv_segments[index] > 0:
                perimeter = self._floor_to_multiple(self._half_oil_line(candidate, candidate.low_voltage_duct.duct_type,
                    p0.low_radii[index], p0.low_stacks[index], D(lv_cfg["lvOilDuctThickness"]),
                    p0.low_ellipse_deltas[index]), D(25))
                coeff = D(lv_cfg["lvOilDuctCoeffHalf"] if candidate.low_voltage_duct.duct_type == 1 else lv_cfg["lvOilDuctCoeffFull"])
                lv_area += scale(perimeter * lv_effective * coeff * D(4), 0)
        if lv_segments[4] > 0:
            perimeter = self._floor_to_multiple(self._half_oil_line(candidate, 2, p0.low_radii[4], p0.low_stacks[4], ZERO,
                                                                      p0.low_ellipse_deltas[4]), D(25))
            lv_area += scale(perimeter * lv_effective * D(lv_cfg["mainAirDuctCoeff"]) * TWO, 0)
        # 高压内表面来自主空道外周，外侧和油道与低压同构。
        mad_outer = self._circumference(p0.high_radii[0], candidate, p0.high_ellipse_deltas[0])
        hv_area = scale(mad_outer * D(hv_cfg["hvInnerSurfaceCoeff"]) * hv_effective, 0)
        for index in range(4):
            if candidate.high_voltage_duct.count >= index + 1 and hv_segments[index] > 0:
                perimeter = self._floor_to_multiple(self._half_oil_line(candidate, candidate.high_voltage_duct.duct_type,
                    p0.high_radii[index], p0.high_stacks[index], D(hv_cfg["hvOilDuctThickness"]),
                    p0.high_ellipse_deltas[index]), D(25))
                coeff = D(hv_cfg["hvOilDuctCoeffHalf"] if candidate.high_voltage_duct.duct_type == 1 else hv_cfg["hvOilDuctCoeffFull"])
                hv_area += scale(perimeter * hv_effective * coeff * D(4), 0)
        if hv_segments[4] > 0:
            perimeter = self._floor_to_multiple(self._half_oil_line(candidate, 2, p0.high_radii[4], p0.high_stacks[4], ZERO,
                                                                      p0.high_ellipse_deltas[4]), D(25))
            hv_area += scale(perimeter * hv_effective * D(hv_cfg["hvOuterSurfaceCoeff"]) * TWO, 0)
        return lv_area, hv_area

    @staticmethod
    def _oil_displacement_factor(wire_type: int, side: str) -> Decimal:
        """对应 Java calculateLow/HighVoltageProperties 返回值的第 3 项。"""
        return D("1.85") if ThreePhaseSingleDeviceEvaluator._is_aluminium(wire_type, side) else D("4.5")

    def _wire_price(self, wire_type: int, side: str) -> Decimal:
        """按 Java 的线材类别—单价映射取价；空单价与 Java 一样按 0 处理。"""
        prices = self.config["craft_unit_price"]
        keys = (("LM", "TM", "ZBL", "ZB") if side == "lv"
                else ("QZ", "QZL", "ZBL", "ZB", "QZB", "QZBL"))
        key = keys[wire_type] if 0 <= wire_type < len(keys) else keys[0]
        legacy_keys = {"QZL": "QLZ", "ZBL": "ZLB", "QZB": "QQB", "QZBL": "QQLB"}
        return D(prices.get(key) or prices.get(legacy_keys.get(key)) or 0)

    def _steel_price(self, brand: str) -> Decimal:
        """对应 ConfigCache.getSteelPriceByBrand；未知品牌不虚构价格。"""
        key_by_brand = {
            "18SQGD065": "steelPrice18SQGD65", "20SQGD070": "steelPrice20SQGD70",
            "23SQGD080": "steelPrice23SQGD80", "23ZDK90": "steelPrice23SQGD90",
            "27QG100": "steelPrice27QG100", "27QG110": "steelPrice27QG110",
            "30Q120": "steelPrice30Q120", "30Q130": "steelPrice30Q130",
            "30Q140": "steelPrice30Q140",
        }
        return D(self.config["craft_unit_price"].get(key_by_brand.get(brand, "")) or 0)

    def _tank_component_weights(self, tank_l: Decimal, tank_w: Decimal, tank_h: Decimal,
                                corr_nl: Decimal, corr_nw: Decimal, corr_w: Decimal,
                                long_faces: int, short_faces: int, tank_type: int) -> dict[str, Decimal]:
        """逐式迁移 ThreePhaseCommon.calculateTankComponentWeights。"""
        tank = self.config["craft_fuel_tank"]
        p, hvn = D(self.performance["capacity"]), D(self.performance["highVoltageRated"])
        # 新配置直接使用页面输入；旧历史配置缺项时仅按 Java 旧默认表补齐，便于回归读取。
        if hvn < D(35000):
            default_shell = (D(3), D(3), D(6)) if p < D(41) else (D(4), D(4), D(6)) if p < D(316) else (D(4), D(6), D(8)) if p < D(1001) else (D(6), D(8), D(10)) if p < D(1601) else (D(6), D(10), D(12))
        else:
            default_shell = (D(4), D(4), D(8)) if p < D(250) else (D(4), D(6), D(10)) if p < D(631) else (D(6), D(6), D(10)) if p < D(1001) else (D(6), D(8), D(12)) if p < D(1601) else (D(6), D(10), D(12))
        default_edge = (D(75), D(50), D(6)) if p < D(630) else (D(100), D(63), D(8)) if p <= D(1500) else (D(80), D(80), D(16))
        default_angle = ZERO if short_faces == 0 else (D(40) if p < D(1251) else D(50))
        default_foot = D(5) if p < D(51) else D("6.3") if p < D(251) else D(8) if p < D(401) else D(10) if p < D(631) else D(12) if p < D(1001) else D(14)
        u54, r55, w55 = (D(tank.get(key) if tank.get(key) is not None else default)
                          for key, default in zip(("tankWallThickness", "bottomCoverThickness", "tankCoverThickness"), default_shell))
        q56, t56, w56 = (D(tank.get(key) if tank.get(key) is not None else default)
                          for key, default in zip(("tankEdgeParam1", "tankEdgeParam2", "tankEdgeParam3"), default_edge))
        q57, s58 = D(tank.get("angleSteelParam1") if tank.get("angleSteelParam1") is not None else default_angle), D(tank.get("footPadThickness") if tank.get("footPadThickness") is not None else default_foot)
        unfold = TWO * (tank_l + tank_w)
        if tank_type == 1:
            wall = java_divide(unfold * tank_h * u54 * D("7.85"), D(1_000_000), 2)
        else:
            corr_cut = (corr_nl * D(long_faces) + corr_nw * D(short_faces) - D(long_faces + short_faces)) * D(45) * corr_w
            wall = java_divide((unfold * (tank_h - (ZERO if w55 == ZERO else q56)) - corr_cut) * u54 * D("7.85"), D(1_000_000), 2)
        bottom = scale(
            tank_l * tank_w * r55 * D("7.85") / D(1_000_000)
            + (tank_l + TWO * t56) * (tank_w + TWO * t56) * w56 * D("7.85") / D(1_000_000), 2)
        if w55 == D(6):
            edge = java_divide(D("5.721") * (unfold + FOUR * t56), D(1000), 2)
        elif w55 in (D(8), D(16)):
            edge = java_divide(D("7.469") * (unfold + FOUR * t56), D(1000), 2)
        else:
            edge = java_divide(java_divide(q56 * t56 * D("7.85"), D(1000), 0) * (unfold + FOUR * t56), D(1000), 2)
        if q57 == ZERO:
            angle = ZERO
        elif q57 == D(40):
            angle = java_divide(D("2.422") * (tank_h - D(20)) * FOUR, D(1000), 2)
        elif q57 == D(50):
            angle = java_divide(D("3.77") * (tank_h - D(20)) * FOUR, D(1000), 2)
        else:
            angle = java_divide(D("5.721") * (tank_h - D(20)) * FOUR, D(1000), 2)
        foot_1 = {D(5): D("5.44"), D("6.3"): D("6.64"), D(8): D("8.05"), D(10): D("10.1"), D(12): D("12.32")}.get(s58, D("16.74"))
        if p < D(631):
            foot_2 = {D(5): D("0.45"), D("6.3"): D("0.6"), D(8): D("0.72"), D(10): D("0.88"), D(12): D("1.14")}.get(s58, D("1.505"))
            foot = scale(foot_1 * foot_2 * TWO, 2)
        else:
            foot_2 = {D("6.3"): D("0.6"), D(8): D("0.72"), D(10): D("0.88"), D(12): D("1.14")}.get(s58, D("1.505"))
            foot = scale(foot_1 * foot_2 * FOUR, 2)
        return {"tank_wall_weight": wall, "tank_bottom_cover_weight": bottom,
                "tank_edge_weight": edge, "tank_angle_steel_weight": angle,
                "tank_foot_weight": foot}

    def _conservator(self, oil_weight: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """对应 TreeMap.higherEntry：严格选择油量阈值大于当前值的第一条储油柜。"""
        choices = sorted(self.catalog["storage_tank"], key=lambda row: D(row["transformer_oilweight"]))
        for row in choices:
            if D(row["transformer_oilweight"]) > oil_weight:
                return (D(row["storagetank_oilweight"]), D(row["storagetank_ironweight"]),
                        D(row["diameter"]), D(row["length"]))
        return ZERO, ZERO, ZERO, ZERO

    @staticmethod
    def _radiator_table_name(width_code: int) -> str:
        table_name = {0: "thermawide_310", 1: "thermawide_480", 2: "thermawide_520"}.get(width_code)
        if table_name is None:
            raise ValueError(f"散热片宽度编码不支持: {width_code}")
        return table_name

    @staticmethod
    def _row_id(row: dict[str, Any]) -> int:
        try:
            return int(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"目录记录缺少可追溯 ID: {row}") from exc

    def _legacy_corrugation_option(self) -> CoolingOption:
        """旧历史候选未保存型号时，按旧 Python/Java 的深度升序给出确定性回退。"""
        rows = [row for row in self.catalog["corrugation"] if row.get("corrugation_depth") is not None]
        rows = [row for row in rows if D(100) <= D(row["corrugation_depth"]) <= D(350)]
        if not rows:
            raise ValueError("原始波纹表中没有 100-350 mm 的可用散热档位")
        row = min(rows, key=lambda item: (D(item["corrugation_depth"]), self._row_id(item)))
        tank = self.config["craft_fuel_tank"]
        return CoolingOption(
            tank_type=0, corrugation_id=self._row_id(row),
            corrugation_long_axis_faces=int(tank.get("longAxisCorrugatedSurfaces") or 0),
            corrugation_short_axis_faces=int(tank.get("shortAxisCorrugatedSurfaces") or 0),
        )

    def _legacy_radiator_option(self, max_allowed: int) -> CoolingOption:
        """历史候选兼容：复刻旧逻辑的最大可装中心距、最小组数、最小片数。

        新 GA 候选不会走这里；其完整 ``CoolingOption`` 已在种群中被显式记录。
        """
        tank = self.config["craft_fuel_tank"]
        width_code = int(tank.get("heatSinkWidth") or 0)
        table_name = self._radiator_table_name(width_code)
        rows = self.catalog[table_name]
        centers = sorted({int(row["center_distance"]) for row in rows if int(row["center_distance"]) <= max_allowed})
        if not centers:
            raise ValueError(f"散热器目录中没有不大于 {max_allowed} mm 的中心距")
        center = centers[-1]
        slices = sorted({int(row["value_condition_slice_number"]) for row in rows if int(row["center_distance"]) == center})
        if not slices:
            raise ValueError("散热器目录没有可用片数")
        slice_count = slices[0]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if int(row["center_distance"]) != center or int(row["value_condition_slice_number"]) != slice_count:
                continue
            kind = str(row["parameter_type"]).upper()
            if kind in {"G", "Q", "SZ"}:
                by_type.setdefault(kind, []).append(row)
        if any(len(by_type.get(kind, [])) != 1 for kind in ("G", "Q", "SZ")):
            raise ValueError(f"{table_name} 历史兼容目标的 G/Q/SZ 参数行不唯一或缺失")
        group_min = tank.get("heatSinkGroupsMin")
        group_max = tank.get("heatSinkGroupsMax")
        if group_min is None and group_max is None and tank.get("heatSinkGroups") is not None:
            group_min = group_max = tank["heatSinkGroups"]
        groups = int(group_min or 0)
        if groups <= 0 or int(group_max or 0) < groups:
            raise ValueError("散热器油箱未配置有效的散热片组数范围")
        return CoolingOption(
            tank_type=1, radiator_width_code=width_code, radiator_center_distance=center,
            radiator_slice_count=slice_count, radiator_groups=groups,
            radiator_body_row_id=self._row_id(by_type["G"][0]),
            radiator_oil_row_id=self._row_id(by_type["Q"][0]),
            radiator_area_row_id=self._row_id(by_type["SZ"][0]),
        )

    def _corrugation_row_for_candidate(self, candidate: ThreePhaseDesignCandidate) -> tuple[CoolingOption, dict[str, Any], str]:
        """按候选 ID 取波纹型号；不再按深度排序替换成另一个合格记录。"""
        option = candidate.cooling_option
        source = "candidate"
        if option is None:
            option, source = self._legacy_corrugation_option(), "legacy_fallback"
        if option.tank_type != 0:
            raise ValueError("当前波纹油箱候选携带了散热器冷却型号")
        matches = [row for row in self.catalog["corrugation"] if self._row_id(row) == option.corrugation_id]
        if len(matches) != 1:
            raise ValueError(f"波纹冷却型号 ID 不存在或不唯一: {option.corrugation_id}")
        return option, matches[0], source

    def _radiator_rows_for_candidate(self, candidate: ThreePhaseDesignCandidate, max_allowed: int) -> tuple[CoolingOption, dict[str, Any], dict[str, Any], dict[str, Any], str]:
        """按候选三行真实 ID 取 G/Q/SZ，逐项校验同一中心距+片数的完整组合。"""
        option = candidate.cooling_option
        source = "candidate"
        if option is None:
            option, source = self._legacy_radiator_option(max_allowed), "legacy_fallback"
        if option.tank_type != 1:
            raise ValueError("当前散热器油箱候选未携带散热器完整型号")
        width_code = int(option.radiator_width_code)
        table_name = self._radiator_table_name(width_code)
        rows_by_id = {self._row_id(row): row for row in self.catalog[table_name]}
        selected: list[dict[str, Any]] = []
        for row_id, expected_type in (
            (option.radiator_body_row_id, "G"), (option.radiator_oil_row_id, "Q"), (option.radiator_area_row_id, "SZ"),
        ):
            row = rows_by_id.get(int(row_id))
            if row is None:
                raise ValueError(f"散热器冷却型号引用的 {expected_type} 行不存在: id={row_id}")
            if str(row["parameter_type"]).upper() != expected_type:
                raise ValueError(f"散热器冷却型号行类型不匹配: id={row_id}, expected={expected_type}")
            if (int(row["center_distance"]), int(row["value_condition_slice_number"])) != (
                option.radiator_center_distance, option.radiator_slice_count,
            ):
                raise ValueError(f"散热器冷却型号行与候选中心距/片数不一致: id={row_id}")
            selected.append(row)
        tank = self.config["craft_fuel_tank"]
        group_min, group_max = tank.get("heatSinkGroupsMin"), tank.get("heatSinkGroupsMax")
        if group_min is None and group_max is None and tank.get("heatSinkGroups") is not None:
            group_min = group_max = tank["heatSinkGroups"]
        # 若当前 Java 页面本身就是散热器并已填写组数范围，仍严格遵守；当本次
        # 研究从波纹页显式切换到散热器时，该范围可能不存在，此时只接受页面
        # 研究边界中已显式列出的正整数，不凭空补一个固定范围。
        if int(group_min or 0) > 0 and int(group_max or 0) >= int(group_min) and not (int(group_min) <= option.radiator_groups <= int(group_max)):
            raise ValueError(f"散热器型号组数超出页面工艺范围: {option.radiator_groups}")
        if option.radiator_groups <= 0:
            raise ValueError("散热器型号组数必须为正数")
        return option, selected[0], selected[1], selected[2], source

    def _thermal_type0(self, candidate: ThreePhaseDesignCandidate, core: CoreStage, p0: P0Stage,
                       pk: dict[str, Decimal], lv: WindingStage, hv: WindingStage) -> dict[str, Decimal]:
        """波纹油箱：逐档迁移 Java 的温升、排油、膨胀、重量与价格内层循环。"""
        tank, isolation = self.config["craft_fuel_tank"], self.config["craft_isolation"]
        lv_area, hv_area = self._thermal_areas(candidate, p0, lv, hv)
        option, row, selection_source = self._corrugation_row_for_candidate(candidate)
        # 新候选必须把面数带在冷却块中；仅旧历史兼容候选才回退到当时配置值。
        long_faces = int(option.corrugation_long_axis_faces if option.corrugation_long_axis_faces is not None else tank["longAxisCorrugatedSurfaces"])
        short_faces = int(option.corrugation_short_axis_faces if option.corrugation_short_axis_faces is not None else tank["shortAxisCorrugatedSurfaces"])
        corr_width = self._floor_to_multiple(pk["tankh"] - D(175) - D(tank["airGapHeight"]), D(50))
        corr_nl = java_divide(pk["tankl"] - D(80), D(45), 0) if long_faces else ZERO
        corr_nw = java_divide(pk["tankw"] - D(80), D(45), 0) if short_faces else ZERO
        corr_n = corr_nl * D(long_faces) + corr_nw * D(short_faces)
        tank_wall_area = java_divide(TWO * (pk["tankl"] + pk["tankw"]) * pk["tankh"] - D(45) * corr_n * corr_width, D(1_000_000), 3)
        tank_cover_area = java_divide(pk["tankl"] * pk["tankw"] * D("0.75"), D(1_000_000), 3)
        total_loss = p0.no_load_loss + pk["hvlvpk"]
        ax52 = java_divide((p0.window_height + D(p0.calculated_core_diameter) * TWO) / TWO + self._gasket_height(p0.calculated_core_diameter),
                           corr_width / TWO + (pk["tankh"] - corr_width - D(75)), 3)
        fixed_weights = self._tank_component_weights(pk["tankl"], pk["tankw"], pk["tankh"], corr_nl, corr_nw, corr_width,
                                                     long_faces, short_faces, 0)
        fixed_tank_weight = (sum(fixed_weights.values(), ZERO) * D("1.01"))
        lv_type, hv_type = self._wire_type(candidate, "lv"), self._wire_type(candidate, "hv")
        tank_oil = java_divide(D("0.9") * pk["tankl"] * pk["tankw"] * (pk["tankh"] - D(tank["airGapHeight"])), D(1_000_000), 0)
        base_oil = scale(tank_oil - java_divide(p0.core_weight, D("7.8"), 2)
                         - java_divide(pk["wlvkg"], self._oil_displacement_factor(lv_type, "lv"), 2)
                         - java_divide(pk["hvkg"], self._oil_displacement_factor(hv_type, "hv"), 2), 0)
        tank_price = D(self.config["craft_unit_price"].get("tankPrice") or 0)
        if int(tank["hasConservatorTank"] or 0) == 1:
            tank_price += ONE
        depth = D(row["corrugation_depth"])
        heat_coeff, expand_coeff = D(row["corrugation_heat_coeff"]), D(row["corrugation_expansion_coeff"])
        angle_area = java_divide(D((long_faces + short_faces) * 2) * depth * corr_width, D(1_000_000), 3)
        wall_area = java_divide(heat_coeff * (D(45) - D(6) - (D(3) if corr_width > D(950) else D("2.4")))
                                * (corr_n - D(long_faces + short_faces)) * corr_width, D(1_000_000), 3)
        wing_area = java_divide(heat_coeff * TWO * (corr_n - D(long_faces + short_faces)) * depth * corr_width, D(1_000_000), 3)
        area = angle_area + wall_area + wing_area + tank_wall_area + tank_cover_area
        if area <= ZERO:
            raise ValueError(f"候选波纹型号 {option.corrugation_id} 的散热面积无效")
        oil_average = scale(D("0.262") * self._pow(java_divide(total_loss, area, 0), .8), 2)
        oil_correction = scale(D("0.028") * Decimal(str(math.exp(float(D("3.5") * ax52)))) * self._pow(oil_average, .7), 3)
        oil_top = scale(D("1.2") * oil_average + oil_correction, 2)
        lv_tr = self._winding_temp_rise(pk["wlvpk"] + pk["lvecl"], lv_area, oil_average,
                                         lv.layer_insulation + lv.insulation_thickness, D(lv.layer_count), D("1.5") + TWO * D(candidate.low_voltage_duct.count))
        hv_tr = self._winding_temp_rise(pk["hvpk"] + pk["hvecl"], hv_area, oil_average,
                                         hv.layer_insulation + hv.insulation_thickness, D(hv.layer_count), D("1.5") + TWO * D(candidate.high_voltage_duct.count))
        corr_oil = java_divide(D(6) * corr_width * depth * corr_n * D("0.9"), D(1_000_000), 0)
        oil_before_conservator = self._ceil_to_multiple(base_oil + corr_oil, D(5))
        conservator_oil, conservator_iron, conservator_diameter, conservator_length = self._conservator(oil_before_conservator)
        if int(tank["hasConservatorTank"] or 0) == 0:
            conservator_oil = conservator_iron = conservator_diameter = conservator_length = ZERO
        final_oil = self._ceil_to_multiple(oil_before_conservator + conservator_oil, D(5))
        oil_expansion = final_oil * D("0.0007") * D(80)
        corr_expansion = corr_oil * expand_coeff
        heat_weight = java_divide(corr_n * (D(45) + TWO * depth) * corr_width
                                  * (D("1.5") if corr_width > D(950) else D("1.2")) * D("7.85"), D(1_000_000), 0)
        tank_weight = self._ceil_to_multiple(fixed_tank_weight + heat_weight + conservator_iron, D(5))
        price = (pk["wlvkg"] * self._wire_price(lv_type, "lv") + pk["hvkg"] * self._wire_price(hv_type, "hv")
                 + p0.core_weight * self._steel_price(candidate.steel_brand)
                 + final_oil * D(self.config["craft_unit_price"].get("oilPrice") or 0) + tank_weight * tank_price)
        tank_structure_weight = sum(fixed_weights.values(), ZERO)
        low_voltage_wire_total_price = pk["wlvkg"] * self._wire_price(lv_type, "lv")
        high_voltage_wire_total_price = pk["hvkg"] * self._wire_price(hv_type, "hv")
        silicon_steel_total_price = p0.core_weight * self._steel_price(candidate.steel_brand)
        oil_total_price = final_oil * D(self.config["craft_unit_price"].get("oilPrice") or 0)
        tank_and_accessory_total_price = tank_weight * tank_price
        return {"corrd": depth, "corrugation_id": option.corrugation_id,
                "corrugation_long_axis_faces": D(long_faces), "corrugation_short_axis_faces": D(short_faces),
                "cooling_option_key": str(option.key()),
                "cooling_selection_source": selection_source, "oatr": oil_average, "otr": oil_top, "lvtr": lv_tr, "hvtr": hv_tr,
                "thermal_area": area, "correxpc": expand_coeff, "corrn": corr_n, "corrw": corr_width,
                "corrfow": corr_oil, "oilweight": final_oil, "oil_expansion": oil_expansion,
                "corr_expansion": corr_expansion, "heat_dissipation_weight": heat_weight,
                "tank_weight": tank_weight, "price": price, "hsotow": conservator_oil,
                "hsotiw": conservator_iron, "hsotir": conservator_diameter, "hsotil": conservator_length,
                # 与 Java calculationSnapshot 的字段一一对应；这些均为精算当次参与运算的量。
                "tank_base_oil_weight": base_oil, "heat_dissipation_oil_weight": corr_oil,
                "conservator_oil_weight": conservator_oil, "total_oil_weight": final_oil,
                "tank_structure_weight": tank_structure_weight, "conservator_iron_weight": conservator_iron,
                "tank_and_accessory_weight": tank_weight, "low_voltage_wire_weight": pk["wlvkg"],
                "high_voltage_wire_weight": pk["hvkg"], "silicon_steel_weight": p0.core_weight,
                "low_voltage_wire_total_price": low_voltage_wire_total_price,
                "high_voltage_wire_total_price": high_voltage_wire_total_price,
                "silicon_steel_total_price": silicon_steel_total_price, "oil_total_price": oil_total_price,
                "tank_and_accessory_total_price": tank_and_accessory_total_price,
                "calculated_total_price": price,
                **fixed_weights}

    def _thermal_type1(self, candidate: ThreePhaseDesignCandidate, core: CoreStage, p0: P0Stage,
                       pk: dict[str, Decimal], lv: WindingStage, hv: WindingStage) -> dict[str, Decimal]:
        """散热器油箱：仅计算候选所指向的完整 G/Q/SZ 型号组合。"""
        tank = self.config["craft_fuel_tank"]
        lv_area, hv_area = self._thermal_areas(candidate, p0, lv, hv)
        max_allowed = int(pk["tankh"] - D(300) - D(tank.get("airGapHeight") or 0))
        option, body_row, oil_row, area_row, selection_source = self._radiator_rows_for_candidate(candidate, max_allowed)
        center, slices_count, groups = option.radiator_center_distance, option.radiator_slice_count, option.radiator_groups
        body, radiator_oil_per_group, radiator_area_per_group = D(body_row["value"] or 0), D(oil_row["value"] or 0), D(area_row["value"] or 0)
        if radiator_area_per_group <= ZERO:
            raise ValueError(f"候选散热器型号 {option.key()} 的 SZ 散热面积无效")
        fixed_weights = self._tank_component_weights(pk["tankl"], pk["tankw"], pk["tankh"], ZERO, ZERO, ZERO, 0, 0, 1)
        lv_type, hv_type = self._wire_type(candidate, "lv"), self._wire_type(candidate, "hv")
        tank_oil = java_divide(D("0.9") * pk["tankl"] * pk["tankw"] * (pk["tankh"] - D(tank.get("airGapHeight") or 0)), D(1_000_000), 0)
        base_oil = scale(tank_oil - java_divide(p0.core_weight, D("7.8"), 2)
                         - java_divide(pk["wlvkg"], self._oil_displacement_factor(lv_type, "lv"), 2)
                         - java_divide(pk["hvkg"], self._oil_displacement_factor(hv_type, "hv"), 2), 0)
        tank_wall_area = java_divide(TWO * (pk["tankl"] + pk["tankw"]) * pk["tankh"], D(1_000_000), 3)
        tank_cover_area = java_divide(pk["tankl"] * pk["tankw"] * D("0.75"), D(1_000_000), 3)
        ax52 = java_divide((p0.window_height + D(p0.calculated_core_diameter) * TWO) / TWO + self._gasket_height(p0.calculated_core_diameter),
                           D(center) / TWO + (pk["tankh"] - D(center) - D(75)), 3)
        total_loss = p0.no_load_loss + pk["hvlvpk"]
        tank_price = D(self.config["craft_unit_price"].get("tankPrice") or 0) + ONE
        if int(tank.get("hasConservatorTank") or 0) == 1:
            tank_price += ONE
        radiator_weight = scale(body * D(groups), 0)
        radiator_oil = scale(radiator_oil_per_group * D(groups), 0)
        oil_before_conservator = self._ceil_to_multiple(base_oil + radiator_oil, D(5))
        conservator_oil, conservator_iron, conservator_diameter, conservator_length = self._conservator(oil_before_conservator)
        if int(tank.get("hasConservatorTank") or 0) == 0:
            conservator_oil = conservator_iron = conservator_diameter = conservator_length = ZERO
        final_oil = self._ceil_to_multiple(oil_before_conservator + conservator_oil, D(5))
        area = tank_wall_area + tank_cover_area + radiator_area_per_group * D(groups)
        oil_average = scale(D("0.262") * self._pow(java_divide(total_loss, area, 0), .8), 2)
        oil_top = scale(D("1.2") * oil_average + scale(D("0.028") * Decimal(str(math.exp(float(D("3.5") * ax52)))) * self._pow(oil_average, .7), 3), 2)
        lv_tr = self._winding_temp_rise(pk["wlvpk"] + pk["lvecl"], lv_area, oil_average,
                                         lv.layer_insulation + lv.insulation_thickness, D(lv.layer_count), D("1.5") + TWO * D(candidate.low_voltage_duct.count))
        hv_tr = self._winding_temp_rise(pk["hvpk"] + pk["hvecl"], hv_area, oil_average,
                                         hv.layer_insulation + hv.insulation_thickness, D(hv.layer_count), D("1.5") + TWO * D(candidate.high_voltage_duct.count))
        tank_weight = self._ceil_to_multiple(sum(fixed_weights.values(), ZERO) + radiator_weight + conservator_iron, D(5))
        price = (pk["wlvkg"] * self._wire_price(lv_type, "lv") + pk["hvkg"] * self._wire_price(hv_type, "hv")
                 + p0.core_weight * self._steel_price(candidate.steel_brand)
                 + final_oil * D(self.config["craft_unit_price"].get("oilPrice") or 0) + tank_weight * tank_price)
        tank_structure_weight = sum(fixed_weights.values(), ZERO)
        low_voltage_wire_total_price = pk["wlvkg"] * self._wire_price(lv_type, "lv")
        high_voltage_wire_total_price = pk["hvkg"] * self._wire_price(hv_type, "hv")
        silicon_steel_total_price = p0.core_weight * self._steel_price(candidate.steel_brand)
        oil_total_price = final_oil * D(self.config["craft_unit_price"].get("oilPrice") or 0)
        tank_and_accessory_total_price = tank_weight * tank_price
        return {"hsp_width_code": option.radiator_width_code, "hsp_center_distance": D(center),
                "hsp_slice_count": D(slices_count), "hsp_groups": D(groups),
                "hsp_body_row_id": option.radiator_body_row_id, "hsp_oil_row_id": option.radiator_oil_row_id,
                "hsp_area_row_id": option.radiator_area_row_id, "cooling_option_key": str(option.key()),
                "cooling_selection_source": selection_source, "radiator_center_distance_max": D(max_allowed),
                "hsp_body_weight": body, "hsp_oil_per_group": radiator_oil_per_group, "hsp_area_per_group": radiator_area_per_group,
                "oatr": oil_average, "otr": oil_top, "lvtr": lv_tr, "hvtr": hv_tr, "thermal_area": area,
                "heat_dissipation_weight": radiator_weight, "oilweight": final_oil, "tank_weight": tank_weight,
                "price": price, "hsotow": conservator_oil, "hsotiw": conservator_iron,
                "hsotir": conservator_diameter, "hsotil": conservator_length,
                # 与 Java calculationSnapshot 的字段一一对应；散热器油重为 Q × 组数。
                "tank_base_oil_weight": base_oil, "heat_dissipation_oil_weight": radiator_oil,
                "conservator_oil_weight": conservator_oil, "total_oil_weight": final_oil,
                "tank_structure_weight": tank_structure_weight, "conservator_iron_weight": conservator_iron,
                "tank_and_accessory_weight": tank_weight, "low_voltage_wire_weight": pk["wlvkg"],
                "high_voltage_wire_weight": pk["hvkg"], "silicon_steel_weight": p0.core_weight,
                "low_voltage_wire_total_price": low_voltage_wire_total_price,
                "high_voltage_wire_total_price": high_voltage_wire_total_price,
                "silicon_steel_total_price": silicon_steel_total_price, "oil_total_price": oil_total_price,
                "tank_and_accessory_total_price": tank_and_accessory_total_price,
                "calculated_total_price": price,
                **fixed_weights}

    @staticmethod
    def _outside_violation(name: str, actual: Decimal, lower: Decimal, upper: Decimal) -> ConstraintViolation | None:
        if actual < lower:
            return ConstraintViolation(
                name, "lower", actual, lower,
                java_divide(lower - actual, max(abs(lower), ONE), 8), lower=lower, upper=upper,
            )
        if actual > upper:
            return ConstraintViolation(
                name, "upper", actual, upper,
                java_divide(actual - upper, max(abs(upper), ONE), 8), lower=lower, upper=upper,
            )
        return None

    @staticmethod
    def _append_optional_range(violations: list[ConstraintViolation], name: str, actual: Decimal,
                               lower: Any, upper: Any) -> None:
        """配置旧版本未提供范围时不伪造约束；新配置有值时按 Java 候选筛选范围记录。"""
        if lower is None or upper is None:
            return
        violation = ThreePhaseSingleDeviceEvaluator._outside_violation(name, actual, D(lower), D(upper))
        if violation:
            violations.append(violation)

    def evaluate(self, candidate: ThreePhaseDesignCandidate) -> EvaluationResult:
        """执行当前已迁入公式；完整公式迁移前不得用于 GA 成本排序。"""
        try:
            candidate = self._normalize_candidate(candidate)
            core = self._core_stage(candidate)
            lv = self._low_voltage_stage(candidate)
            hv = self._high_voltage_stage(candidate, core)
            violations = []
            violation = self._outside_violation(
                "flux_density", core.flux_density,
                D(self.scope["fluxDensityMin"]), D(self.scope["fluxDensityMax"]),
            )
            specific_loss = self._specific_loss(candidate.steel_brand, core.flux_density)
            if violation:
                violations.append(violation)
            p0_stage = self._window_geometry_and_p0(candidate, core, lv, hv, specific_loss)
            p0_violation = self._outside_violation(
                "no_load_loss", p0_stage.no_load_loss,
                scale(D(self.performance["noLoadStandard"]) * D(self.performance["noLoadLowerLimit"]), 0),
                scale(D(self.performance["noLoadStandard"]) * D(self.performance["noLoadUpperLimit"]), 0),
            )
            if p0_violation:
                violations.append(p0_violation)
            pk_uk = self._pk_uk_stage(candidate, core, p0_stage, lv, hv)
            thermal: dict[str, Decimal] = {}
            active_tank_type = (
                int(candidate.cooling_option.tank_type)
                if candidate.cooling_option is not None
                else int(self.config["craft_fuel_tank"]["tankType"])
            )
            if active_tank_type == 0:
                thermal = self._thermal_type0(candidate, core, p0_stage, pk_uk, lv, hv)
            elif active_tank_type == 1:
                thermal = self._thermal_type1(candidate, core, p0_stage, pk_uk, lv, hv)
            else:
                raise ValueError(f"候选冷却块中的油箱类型不支持: {active_tank_type}")
            if thermal:
                for name, actual, limit in (
                    ("oil_top_temp_rise", thermal["otr"], D(self.scope["oilLayerTempRiseLimit"])),
                    ("lv_temp_rise", thermal["lvtr"], D(self.scope["windingTempRiseLimit"])),
                    ("hv_temp_rise", thermal["hvtr"], D(self.scope["windingTempRiseLimit"])),
                ):
                    temperature_violation = self._outside_violation(name, actual, ZERO, limit)
                    if temperature_violation:
                        violations.append(temperature_violation)
                if "corr_expansion" in thermal and thermal["corr_expansion"] * THREE <= thermal["oil_expansion"]:
                    violations.append(ConstraintViolation(
                        "corrugation_expansion", "lower", thermal["corr_expansion"] * THREE,
                        thermal["oil_expansion"],
                        java_divide(thermal["oil_expansion"] - thermal["corr_expansion"] * THREE,
                                    max(thermal["oil_expansion"], ONE), 8),
                        lower=thermal["oil_expansion"], upper=None,
                        rule="3 × 波纹片可补偿油膨胀量应不小于油膨胀量",
                    ))
                if "radiator_center_distance_max" in thermal:
                    fit_violation = self._outside_violation(
                        "radiator_center_distance", thermal["hsp_center_distance"], ZERO,
                        thermal["radiator_center_distance_max"],
                    )
                    if fit_violation:
                        violations.append(fit_violation)
                thermal["total_assembly_weight"] = (
                    p0_stage.core_weight + pk_uk["wlvkg"] + pk_uk["hvkg"]
                    + thermal["tank_weight"] + thermal["oilweight"]
                )
            pk_violation = self._outside_violation(
                "load_loss", pk_uk["hvlvpk"],
                scale(D(self.performance["loadStandard"]) * D(self.performance["loadLowerLimit"]), 0),
                scale(D(self.performance["loadStandard"]) * D(self.performance["loadUpperLimit"]), 0),
            )
            uk_violation = self._outside_violation(
                "impedance", pk_uk["ukk"],
                scale(D(self.performance["impedanceStandard"]) * D(self.performance["impedanceLowerLimit"]), 2),
                scale(D(self.performance["impedanceStandard"]) * D(self.performance["impedanceUpperLimit"]), 2),
            )
            if pk_violation:
                violations.append(pk_violation)
            if uk_violation:
                violations.append(uk_violation)
            for side, stage, craft in (("lv", lv, self.config["craft_low_coil"]),
                                       ("hv", hv, self.config["craft_high_coil"])):
                wire_type = self._wire_type(candidate, side)
                material = "aluminum" if self._is_aluminium(wire_type, side) else "copper"
                density = self._outside_violation(
                    f"{side}_current_density", stage.current_density,
                    D(self.scope[f"{material}CurrentDensityMin"]), D(self.scope[f"{material}CurrentDensityMax"]),
                )
                height = self._outside_violation(
                    f"{side}_reactance_height", stage.reactance_height,
                    D(craft["minHeight"]), D(craft["maxHeight"]),
                )
                if density:
                    violations.append(density)
                if height:
                    violations.append(height)
                # 原 Java 在候选生成阶段过滤扁线的宽、厚和宽厚比；Python 保留个体并写入约束向量。
                if wire_type > 1:
                    self._append_optional_range(violations, f"{side}_wire_thickness", stage.bare_thickness,
                                                craft.get("flatWireThicknessMin"), craft.get("flatWireThicknessMax"))
                    self._append_optional_range(violations, f"{side}_wire_width", stage.bare_width,
                                                craft.get("flatWireWidthMin"), craft.get("flatWireWidthMax"))
                    min_ratio, max_ratio = craft.get("flatWireAspectRatioLimitMin"), craft.get("flatWireAspectRatioLimitMax")
                    if min_ratio is not None and max_ratio is not None:
                        ratio = java_divide(stage.bare_width, stage.bare_thickness, 6)
                        aspect = self._outside_violation(f"{side}_wire_aspect_ratio", ratio, D(min_ratio), D(max_ratio))
                        if aspect:
                            violations.append(aspect)
                if side == "lv":
                    if wire_type in (0, 1) and stage.layer_count != candidate.low_voltage_turns:
                        required_layers = D(candidate.low_voltage_turns)
                        violations.append(ConstraintViolation(
                            "lv_layer_count", "invalid", D(stage.layer_count), required_layers, ONE,
                            lower=required_layers, upper=required_layers,
                            rule="箔材低压层数必须等于低压匝数",
                        ))
                    elif wire_type > 1 and stage.layer_count not in (2, 4):
                        violations.append(ConstraintViolation(
                            "lv_layer_count", "invalid", D(stage.layer_count), None, ONE,
                            rule="扁线低压层数只能为 2 或 4",
                        ))
                else:
                    self._append_optional_range(violations, "hv_layer_count", D(stage.layer_count),
                                                craft.get("minLayerCount"), craft.get("maxLayerCount"))
            formula_complete = active_tank_type in (0, 1)
            return EvaluationResult(
                complete=formula_complete, calculable=True, feasible=formula_complete and not violations,
                metrics={
                    "ct": core.flux_density, "et": core.volts_per_turn,
                    "crgos": core.core_area, "core_angle_weight": core.angle_weight,
                    "crgokw": specific_loss, "conversion_c": core.conversion_c,
                    "crgoa": p0_stage.window_height, "lvydd": p0_stage.low_yoke_distance,
                    "hvydd": p0_stage.high_yoke_distance, "coil_diameter": p0_stage.coil_diameter,
                    "m0": p0_stage.core_center_distance, "crgog": p0_stage.core_weight,
                    "crgopo": p0_stage.no_load_loss,
                    **pk_uk,
                    **thermal,
                    "lvil_t": lv.bare_thickness, "lvil_w": lv.bare_width,
                    "lvma": lv.total_section, "lvcd": lv.current_density,
                    "lvrh": lv.reactance_height, "lvlt": lv.layer_insulation,
                    "lvwll": lv.lead_loss, "lvecl_coeff": lv.eddy_loss_coefficient,
                    "hvil_t": hv.bare_thickness, "hvil_w": hv.bare_width,
                    "hvqqb_sq": hv.total_section, "hvcd": hv.current_density,
                    "hvrh": hv.reactance_height, "hvlt": hv.layer_insulation,
                    "hvecl_coeff": hv.eddy_loss_coefficient,
                },
                violations=violations,
                diagnostic=[
                    "已完成铁芯、磁密、硅钢单位损耗以及高低压候选线规的基础绕组计算。",
                    "已完成五段油道半径链、铁芯重量和空载损耗 P0。",
                    "已完成圆形、长圆和椭圆铁芯的 PK、UK，以及波纹油箱/散热器油箱的温升、重量和成本公式迁移。",
                    "本次结果为严格合格/不合格判定的公式结果；新增热工、重量、成本字段仍待用同次 Java 结果逐项验证。",
                ],
            )
        except CandidateRejected as exc:
            # Java 主循环对这类候选一律 continue：它在本配置下**不可用**，
            # 既不是迁移缺口也不是数据缺失，因此不得与“首段精算失败”混淆。
            return EvaluationResult(
                complete=False, calculable=False, feasible=False,
                diagnostic=[f"候选被域规则拒绝: {exc}"],
            )
        except (ArithmeticError, KeyError, ValueError, NotImplementedError) as exc:
            return EvaluationResult(
                complete=False, calculable=False, feasible=False,
                diagnostic=[f"铁芯精算首段失败: {exc}"],
            )
