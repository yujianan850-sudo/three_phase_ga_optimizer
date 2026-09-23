"""三相遗传优化器的领域数据结构。

这里的 ``Candidate`` 只保存离散基因的原始记录 ID 或有序候选索引，绝不保存
由交叉得到的任意浮点线宽/线厚。派生几何量、性能量和价格必须由精算器产生。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class OilDuctScheme:
    """一个绕组侧的有效油道方案；0 必须与“无油道”成对出现。"""

    count: int
    duct_type: int  # 0: 无油道, 1: 半油道, 2: 全油道

    def __post_init__(self) -> None:
        if self.count == 0 and self.duct_type == 0:
            return
        if not (1 <= self.count <= 4 and self.duct_type in (1, 2)):
            raise ValueError(f"非法油道组合: ({self.count}, {self.duct_type})")


VALID_OIL_DUCT_SCHEMES: tuple[OilDuctScheme, ...] = (
    OilDuctScheme(0, 0),
    *(OilDuctScheme(count, duct_type) for duct_type in (1, 2) for count in range(1, 5)),
)


@dataclass(frozen=True)
class CoolingOption:
    """一个不可拆分的油箱散热型号选择。

    波纹油箱以 ``tb_corrugation`` 的真实记录 ID 表示；散热器表没有单独的
    “型号主表”记录，Java 实际以同一 ``中心距 + 片数`` 的 ``G/Q/SZ`` 三行
    参数共同计算。因此散热器选项必须同时保存三行 ID，不能把某一行的重量、
    另一行的油量或面积任意混搭。``radiator_width_code`` 是散热片
    大类（0=310、1=480、2=520）。波纹油箱还必须保存长、短轴波纹面数；
    它们是油箱结构的一部分，不能继续从固定配置中隐式读取。
    """

    tank_type: int  # 0: 波纹油箱, 1: 散热器油箱
    corrugation_id: int | None = None
    corrugation_long_axis_faces: int | None = None
    corrugation_short_axis_faces: int | None = None
    radiator_width_code: int | None = None
    radiator_center_distance: int | None = None
    radiator_slice_count: int | None = None
    radiator_groups: int | None = None
    radiator_body_row_id: int | None = None  # parameter_type=G
    radiator_oil_row_id: int | None = None   # parameter_type=Q
    radiator_area_row_id: int | None = None  # parameter_type=SZ/Sz

    def __post_init__(self) -> None:
        if self.tank_type == 0:
            if self.corrugation_id is None:
                raise ValueError("波纹油箱冷却选项必须指定 corrugation_id")
            if self.corrugation_long_axis_faces is not None and self.corrugation_long_axis_faces < 0:
                raise ValueError("波纹长轴面数不能为负数")
            if self.corrugation_short_axis_faces is not None and self.corrugation_short_axis_faces < 0:
                raise ValueError("波纹短轴面数不能为负数")
            if any(value is not None for value in (
                self.radiator_width_code, self.radiator_center_distance, self.radiator_slice_count,
                self.radiator_groups, self.radiator_body_row_id, self.radiator_oil_row_id,
                self.radiator_area_row_id,
            )):
                raise ValueError("波纹油箱冷却选项不能混入散热器字段")
            return
        if self.tank_type == 1:
            values = (
                self.radiator_width_code, self.radiator_center_distance, self.radiator_slice_count,
                self.radiator_groups, self.radiator_body_row_id, self.radiator_oil_row_id,
                self.radiator_area_row_id,
            )
            if (self.corrugation_id is not None or self.corrugation_long_axis_faces is not None
                    or self.corrugation_short_axis_faces is not None or any(value is None for value in values)):
                raise ValueError("散热器冷却选项必须完整指定宽度、中心距、片数、组数及 G/Q/SZ 行 ID")
            if self.radiator_width_code not in (0, 1, 2):
                raise ValueError(f"非法散热片宽度编码: {self.radiator_width_code}")
            if self.radiator_center_distance <= 0 or self.radiator_slice_count <= 0 or self.radiator_groups <= 0:
                raise ValueError("散热器中心距、片数和组数必须为正数")
            return
        raise ValueError(f"非法油箱类型: {self.tank_type}")

    def key(self) -> tuple[int, int | None, int | None, int | None, int | None, int | None, int | None, int | None, int | None, int | None, int | None]:
        """缓存、交叉审计与输出展示共用的稳定完整键。"""
        return (
            self.tank_type, self.corrugation_id, self.corrugation_long_axis_faces,
            self.corrugation_short_axis_faces, self.radiator_width_code,
            self.radiator_center_distance, self.radiator_slice_count, self.radiator_groups,
            self.radiator_body_row_id, self.radiator_oil_row_id, self.radiator_area_row_id,
        )


@dataclass(frozen=True)
class ThreePhaseDesignCandidate:
    """一台三相变压器的全部可变结构基因。

    ``*_wire_id`` 指向原始圆线、扁线或箔材规格记录，``*_wire_type`` 与记录 ID
    共同构成一个不可拆分的完整导线选项。两者不能分别来自不同父代或目录记录。

    ``*_wire_type`` 为 ``None`` 时表示历史兼容候选：调用方必须按本次配置中的
    固定 ``wireSpecification`` 补齐它，不能猜测导线类别。``conversion_c`` 只在
    长圆铁芯中使用。
    """

    steel_brand: str
    core_type: int  # 0: 圆形, 1: 长圆, 2: 椭圆
    core_data_id: int
    low_voltage_turns: int
    low_voltage_wire_id: int
    low_voltage_layers: int
    low_voltage_duct: OilDuctScheme
    high_voltage_wire_id: int
    high_voltage_layers: int
    high_voltage_duct: OilDuctScheme
    conversion_c: Decimal | None = None
    low_voltage_wire_type: int | None = None
    high_voltage_wire_type: int | None = None
    # None 只服务于历史记录兼容；新的 GA 域候选必须携带完整实际冷却选项。
    cooling_option: CoolingOption | None = None

    def canonical_dict(self) -> dict[str, Any]:
        """用于结果缓存和运行追踪的稳定、可 JSON 序列化编码。"""
        value = asdict(self)
        value["conversion_c"] = None if self.conversion_c is None else str(self.conversion_c)
        return value


@dataclass(frozen=True)
class ConstraintViolation:
    name: str
    direction: str  # lower / upper / invalid
    actual: Decimal | None
    limit: Decimal | None
    normalized_excess: Decimal
    # ``limit`` 保留为触发违规的单侧阈值，兼容既有轨迹；页面和新轨迹同时保留完整区间，
    # 使诊断能够说明“允许范围、实际值与超出量”，而不只显示触发侧。
    lower: Decimal | None = None
    upper: Decimal | None = None
    rule: str | None = None


@dataclass
class EvaluationResult:
    """精算结果及其可解释约束诊断。

    ``complete`` 只能在 P0、PK、UK、温升、尺寸、重量和总成本完整计算后为 True。
    优化器据此拒绝使用未完成公式链的结果做 GA 排序。
    """

    complete: bool
    calculable: bool
    feasible: bool
    metrics: dict[str, Decimal | str | int | None] = field(default_factory=dict)
    violations: list[ConstraintViolation] = field(default_factory=list)
    diagnostic: list[str] = field(default_factory=list)

    @property
    def total_violation(self) -> Decimal:
        return sum((item.normalized_excess for item in self.violations), Decimal("0"))

    @property
    def max_violation(self) -> Decimal:
        return max((item.normalized_excess for item in self.violations), default=Decimal("0"))

    def as_json_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, ConstraintViolation):
                return {key: convert(item) for key, item in asdict(value).items()}
            if isinstance(value, Mapping):
                return {str(key): convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        return convert(asdict(self))
