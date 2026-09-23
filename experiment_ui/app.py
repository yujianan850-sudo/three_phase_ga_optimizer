"""Streamlit 实验台：单设备精算、一致性验证与 GA 实验准备。

启动：
    streamlit run experiment_ui/app.py

页面只读 faladi 业务数据库。唯一的本地写入是用户主动执行“生成初始种群”时，
向 outputs/ 下的 SQLite 文件记录实验轨迹。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from calculation.database import DatabaseConnectionPool, FaladiRepository, load_database_settings  # noqa: E402
from calculation.consistency_check import SNAPSHOT_FIELD_MAP  # noqa: E402
from calculation.models import ConstraintViolation, CoolingOption, OilDuctScheme, ThreePhaseDesignCandidate  # noqa: E402
from calculation.record_adapter import resolve_record_candidate  # noqa: E402
from calculation.similar_history import select_similar_history_seeds  # noqa: E402
from calculation.single_device_evaluator import ThreePhaseSingleDeviceEvaluator  # noqa: E402
from ga.gene_codec import GeneDomain, load_optimizer_defaults  # noqa: E402
from ga.full_optimizer import (  # noqa: E402
    GASettings, apply_experiment_strategy, automatic_evolution_population_size, run_optimization,
)
from ga.optimizer_runtime import EvaluationCache, build_mixed_initial_population, trace_initial_population  # noqa: E402
from ga.trace_reader import discover_trace_files, read_trace, read_trace_overview  # noqa: E402


st.set_page_config(page_title="三相变压器优化研究操作台", page_icon=None, layout="wide")

st.markdown(
    """
    <style>
      :root { --navy:#1E3A5F; --blue:#2563EB; --green:#047857; --amber:#A16207; --danger:#B91C1C;
              --ink:#0F172A; --surface:#F8FAFC; --panel:#FFFFFF; --border:#CBD5E1; --muted:#52657A; --soft-blue:#EFF6FF; }
      .stApp, [data-testid="stAppViewContainer"] { background:var(--surface)!important; color:var(--ink)!important;
        font-family:"Microsoft YaHei UI","Microsoft YaHei","Noto Sans SC",Arial,sans-serif; }
      [data-testid="stHeader"] { background:rgba(248,250,252,.96)!important; border-bottom:1px solid #E2E8F0; }
      [data-testid="stSidebar"] { background:#FDFEFF!important; border-right:1px solid var(--border); }
      [data-testid="stSidebar"] * { color:var(--ink); }
      h1,h2,h3 { color:var(--navy)!important; font-family:"Microsoft YaHei UI","Microsoft YaHei","Noto Sans SC",Arial,sans-serif; letter-spacing:-.02em; }
      h1 { font-size:1.38rem!important; margin-bottom:.12rem!important; }
      h2 { font-size:1.16rem!important; margin-top:1rem!important; }
      h3 { font-size:1.08rem!important; }
      /* Streamlit 顶栏为固定定位；保留其高度，避免遮住应用工具栏。 */
      .block-container { max-width:1540px; padding-top:4.45rem; padding-bottom:2.25rem; }
      .lab-caption { color:var(--muted)!important; font-size:.9rem; margin-top:-.3rem; }
      .hero-strip { background:var(--navy); color:#fff!important; border-radius:7px;
        padding:.78rem 1rem; margin:.7rem 0 .8rem; border-left:5px solid #60A5FA; }
      .hero-strip * { color:#fff!important; }
      .hero-kicker { color:#BFDBFE!important; font-size:.72rem; }
      .hero-title { font-size:1.02rem; font-weight:700; margin:.12rem 0; }
      .hero-subtitle { color:#E0F2FE!important; font-size:.86rem; }
      .workspace-header { background:#FFFFFF; border:1px solid var(--border); border-radius:7px; padding:.62rem .78rem;
        box-shadow:none; margin-bottom:1.05rem; }
      .workspace-brand { display:flex; align-items:center; gap:.72rem; }
      .workspace-mark { display:flex; align-items:center; justify-content:center; width:2.1rem; height:2.1rem; border-radius:.4rem;
        background:#1D4ED8; color:#EFF6FF!important; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.75rem; font-weight:800; }
      .workspace-title { color:var(--navy)!important; font-size:1rem; font-weight:800; line-height:1.2; }
      .workspace-subtitle { color:var(--muted)!important; font-size:.73rem; line-height:1.35; margin-top:.15rem; }
      .status-pill { display:inline-flex; align-items:center; gap:.35rem; background:#ECFDF5; color:#047857!important;
        border:1px solid #A7F3D0; border-radius:999px; padding:.3rem .58rem; font-size:.72rem; font-weight:700; white-space:nowrap; }
      .status-pill.offline { background:#FFF7ED; border-color:#FED7AA; color:#9A3412!important; }
      .sidebar-kicker { color:#52657A!important; font-size:.72rem; font-weight:800; margin:.15rem 0 .35rem; }
      .sidebar-card { background:#F8FAFC; border:1px solid #D8E1EC; border-radius:7px; padding:.68rem .7rem; margin-bottom:.8rem; }
      .sidebar-caption { color:var(--muted)!important; font-size:.69rem; }
      .sidebar-value { color:#102A43!important; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.78rem; font-weight:750; }
      .side-safe-note { background:#FFF8E6; border:1px solid #F6D78C; border-radius:8px; padding:.65rem .7rem; color:#704D00!important; font-size:.74rem; line-height:1.5; }
      [data-testid="stSidebar"] [data-testid="stRadio"] { margin-top:.1rem; }
      [data-testid="stSidebar"] [data-testid="stRadio"] label { min-height:2.38rem; margin:0!important; padding:.48rem .58rem;
        border-left:3px solid transparent; border-radius:3px; font-weight:700; transition:background .18s ease,border-color .18s ease; }
      [data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background:#F1F5F9; }
      [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background:#EAF2FF; border-left-color:var(--blue); color:#174EA6!important; }
      [data-testid="stSidebar"] [data-testid="stRadio"] input { accent-color:var(--blue); }
      .step-heading { display:flex; align-items:center; gap:.55rem; margin:.2rem 0 .72rem; }
      .step-code { min-width:1.75rem; height:1.75rem; display:inline-flex; align-items:center; justify-content:center; border-radius:.28rem;
        background:#EFF6FF; color:#1D4ED8!important; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.72rem; font-weight:800; }
      .step-copy { color:var(--muted)!important; font-size:.78rem; margin-top:.1rem; }
      .status-note { border-left:4px solid var(--blue); background:#EFF6FF; padding:.75rem .92rem; border-radius:6px;
        color:#173B63!important; margin:.45rem 0 1rem; line-height:1.6; }
      .warning-note { border-left-color:var(--amber); background:#FFF8E6; color:#704D00!important; }
      .offline-note { border-left-color:#64748B; background:#F1F5F9; color:#334E68!important; }
      .page-header { display:flex; align-items:flex-end; justify-content:space-between; gap:1rem; border-bottom:2px solid var(--navy); padding:0 0 .75rem; margin:.05rem 0 1rem; }
      .page-header h2 { margin:0!important; font-size:1.32rem!important; }
      .page-header p { margin:.24rem 0 0; color:var(--muted)!important; font-size:.88rem; max-width:52rem; line-height:1.55; }
      .page-context { flex:0 0 auto; color:#174EA6!important; background:var(--soft-blue); border:1px solid #BFDBFE; border-radius:4px; padding:.3rem .55rem; font-size:.76rem; font-weight:700; }
      .section-card { background:var(--panel); border:1px solid #D8E1EC; border-radius:7px; padding:.8rem .85rem;
        margin:.3rem 0 .7rem; box-shadow:none; }
      .section-card-title { font-weight:700; color:var(--navy); margin:0 0 .25rem; }
      .section-card-copy { color:var(--muted); font-size:.88rem; line-height:1.55; }
      .work-card { background:var(--panel); border-top:3px solid #93C5FD; border-left:1px solid #D8E1EC; border-right:1px solid #D8E1EC; border-bottom:1px solid #D8E1EC; border-radius:5px; padding:.78rem .82rem; min-height:106px; }
      .work-card-number { color:var(--blue); font-size:.76rem; font-weight:700; }
      .work-card-title { color:var(--ink); font-weight:700; margin:.2rem 0; font-size:.95rem; }
      .work-card-copy { color:var(--muted); font-size:.83rem; line-height:1.5; }
      div[data-testid="stMetric"] { min-width:0; overflow:hidden; background:var(--panel); border:1px solid #D8E1EC; border-radius:5px; padding:.62rem .7rem; box-shadow:none; }
      div[data-testid="stMetric"] * { color:var(--ink)!important; }
      div[data-testid="stMetricLabel"] { min-width:0; color:var(--muted)!important; font-size:.78rem; line-height:1.35; }
      div[data-testid="stMetricLabel"] p { color:var(--muted)!important; font-size:.78rem!important; line-height:1.35!important; white-space:normal!important; overflow-wrap:anywhere; }
      div[data-testid="stMetricValue"], div[data-testid="stMetricValue"] > div { min-width:0!important; max-width:100%!important;
        overflow:hidden!important; color:var(--navy)!important; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace!important;
        font-size:clamp(1.05rem,1.25vw,1.35rem)!important; font-weight:650!important; line-height:1.2!important;
        letter-spacing:-.035em; font-variant-numeric:tabular-nums; text-overflow:ellipsis; white-space:nowrap!important; }
      [data-testid="stWidgetLabel"] p, [data-testid="stWidgetLabel"] label, label, .stCaption, small { color:var(--ink)!important; }
      [data-baseweb="input"] input, [data-baseweb="select"] > div, [data-baseweb="base-input"], textarea {
        background:#FFFFFF!important; color:var(--ink)!important; border-color:#CBD5E1!important; }
      [data-baseweb="select"] *, [data-baseweb="input"] * { color:var(--ink)!important; }
      [data-baseweb="slider"] [role="slider"] { background:var(--blue)!important; }
      .stButton > button,.stDownloadButton > button { background:#FFFFFF; color:var(--ink)!important; border:1px solid #CBD5E1;
        border-radius:5px; font-weight:650; min-height:2.35rem; transition:background .18s ease,border-color .18s ease,box-shadow .18s ease; }
      .stButton > button:focus,.stDownloadButton > button:focus { outline:3px solid rgba(29,78,216,.22); outline-offset:2px; }
      .stButton > button[kind="primary"] { background:var(--blue)!important; color:#FFFFFF!important; border-color:var(--blue)!important; }
      .stButton > button[kind="primary"]:hover { background:#1E40AF!important; border-color:#1E40AF!important; box-shadow:0 2px 6px rgba(29,78,216,.18); }
      .stTabs [data-baseweb="tab-list"] { background:transparent; border-bottom:1px solid var(--border); padding:0; gap:.2rem; }
      .stTabs [data-baseweb="tab"] { font-weight:700; color:var(--muted)!important; min-height:37px; border-radius:4px 4px 0 0; padding:0 .82rem; }
      .stTabs [aria-selected="true"] { color:var(--navy)!important; background:#EAF2FF!important; }
      .stTabs [data-baseweb="tab-highlight"] { background:var(--blue)!important; height:3px!important; }
      [data-testid="stExpander"] { background:var(--panel); border:1px solid var(--border); border-radius:6px; }
      [data-testid="stDataFrame"] { border:1px solid var(--border); border-radius:6px; overflow:hidden; }
      [data-testid="stForm"] { background:#FFFFFF; border:1px solid var(--border)!important; border-radius:6px; padding:.9rem!important; box-shadow:0 1px 2px rgba(15,23,42,.025); }
      [data-testid="stSidebar"] .stButton > button { min-height:2.2rem; }
      @media (max-width:760px) { .block-container { padding-top:4.1rem; padding-left:.85rem; padding-right:.85rem; } .hero-strip { padding:.85rem; } h1 { font-size:1.6rem!important; } }
      @media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition:none!important; animation:none!important; } }
    </style>
    """,
    unsafe_allow_html=True,
)


CORE_TYPE_NAME = {0: "圆形", 1: "长圆", 2: "椭圆"}
TANK_TYPE_NAME = {0: "波纹油箱", 1: "散热器油箱"}
RADIATOR_WIDTH_NAME = {0: "310 mm", 1: "480 mm", 2: "520 mm"}
DUCT_OPTIONS = (OilDuctScheme(0, 0), *(
    OilDuctScheme(count, duct_type) for duct_type in (1, 2) for count in range(1, 5)
))


def render_page_header(title: str, description: str, context: str) -> None:
    """统一一级模块标题：说明当前要完成的工作，而非重复展示系统术语。"""
    st.markdown(
        "<section class='page-header'>"
        f"<div><h2>{title}</h2><p>{description}</p></div>"
        f"<div class='page-context'>{context}</div>"
        "</section>",
        unsafe_allow_html=True,
    )
CHECKED_FIELDS = {
    "ct": "磁密 CT",
    "crgokw": "硅钢单位损耗",
    "lvrh": "低压绕组高度",
    "hvrh": "高压绕组高度",
    "lvydd": "低压轭距",
    "hvydd": "高压轭距",
    "crgopo": "空载损耗 P0",
    "hvlvpk": "负载损耗 PK",
    "ukk": "短路阻抗 UK",
    "price": "总成本",
}

SNAPSHOT_FIELD_LABELS = {
    "oilAverageTempRise": "油平均温升", "oilTopTempRise": "油顶温升",
    "lowVoltageWindingTempRise": "低压绕组温升", "highVoltageWindingTempRise": "高压绕组温升",
    "tankBaseOilWeight": "油箱基础油重", "heatDissipationOilWeight": "散热部件油重",
    "conservatorOilWeight": "储油柜油重", "totalOilWeight": "总油重",
    "tankStructureWeight": "油箱结构重量", "heatDissipationWeight": "散热部件重量",
    "conservatorIronWeight": "储油柜铁重", "tankAndAccessoryWeight": "油箱及附件重量",
    "lowVoltageWireWeight": "低压导线重量", "highVoltageWireWeight": "高压导线重量",
    "siliconSteelWeight": "硅钢重量", "calculatedAssemblyWeight": "总装配重量",
    "lowVoltageWireTotalPrice": "低压导线成本", "highVoltageWireTotalPrice": "高压导线成本",
    "siliconSteelTotalPrice": "硅钢成本", "oilTotalPrice": "油成本",
    "tankAndAccessoryTotalPrice": "油箱及附件成本", "calculatedTotalPrice": "快照总成本",
}

# 页面不直接暴露 Java 的字段名；但每个显示值均来自数据库中同一份三相配置 JSON。
# 未列出的字段仍会在“完整原始配置”中展示，便于追溯和核对。
CONFIG_FIELD_NAMES = {
    "capacity": "额定容量（kVA）", "frequency": "频率", "highVoltageRated": "高压额定值（V）",
    "lowVoltageRated": "低压额定值（V）", "windingConnection": "接线方式",
    "displayConnectionGroup": "接线组别", "noLoadStandard": "空载损耗标准（W）",
    "cspLoss": "CSP 损耗（单相字段）", "remark": "备注", "deviceQuantity": "台数",
    "deviationType": "偏差类型", "impedanceDeviationType": "阻抗偏差类型",
    "windingMethodType": "绕法", "loadStandardDeviation": "负载标准偏差（%）",
    "noLoadStandardDeviation": "空载标准偏差（%）",
    "impedanceStandardDeviation": "阻抗标准正偏差（%）",
    "impedanceStandardNegativeDeviation": "阻抗标准负偏差（%）",
    "loadLossCoefficient": "负载损耗系数", "impedanceCoefficient": "阻抗系数",
    "highVoltageSectionSpacing": "高压段间距（mm）",
    "lowVoltageSectionSpacing": "低压段间距（mm）",
    "noLoadLowerLimit": "空载损耗下限系数", "noLoadUpperLimit": "空载损耗上限系数",
    "loadStandard": "负载损耗标准（W）", "loadLowerLimit": "负载损耗下限系数",
    "loadUpperLimit": "负载损耗上限系数", "impedanceStandard": "阻抗标准（%）",
    "impedanceLowerLimit": "阻抗下限系数", "impedanceUpperLimit": "阻抗上限系数",
    "loadCalculationTemperature": "负载损耗计算温度（℃）", "tappingInterval": "分接间隔（%）",
    "tappingNegativePosition": "负分接位置", "tappingPositivePosition": "正分接位置",
    "fluxDensityMin": "磁密下限（T）", "fluxDensityMax": "磁密上限（T）",
    "lowVoltageTurnsMin": "低压匝数下限", "lowVoltageTurnsMax": "低压匝数上限",
    "windingTempRiseLimit": "绕组温升限值（K）", "oilLayerTempRiseLimit": "油顶层温升限值（K）",
    "copperCurrentDensityMin": "铜电流密度下限", "copperCurrentDensityMax": "铜电流密度上限",
    "aluminumCurrentDensityMin": "铝电流密度下限", "aluminumCurrentDensityMax": "铝电流密度上限",
    "coreDiameterMin": "铁芯直径下限（mm）", "coreDiameterMax": "铁芯直径上限（mm）",
    "siliconSteelGrade": "Java 既有硅钢片候选", "processCoefficient": "铁芯工艺系数",
    "laminationCoefficient": "叠片系数", "isRolledCore": "Java 当前铁芯形状",
    "wireSpecification": "导线类别", "minHeight": "绕组高度下限（mm）", "maxHeight": "绕组高度上限（mm）",
    "channelType": "油道类型", "channelCount": "油道数量", "lvOilDuctThickness": "低压油道厚度（mm）",
    "axialWindings": "轴向并绕", "radialWindings": "辐向并绕",
    "lvBusbarCoeff": "低压内表面散热系数", "mainAirDuctCoeff": "低压外表面散热系数",
    "lvOilDuctCoeffHalf": "半油道散热系数", "lvOilDuctCoeffFull": "全油道散热系数",
    "hvOilDuctThickness": "高压油道厚度（mm）", "hvInnerSurfaceCoeff": "高压内表面散热系数",
    "hvOuterSurfaceCoeff": "高压外表面散热系数", "hvOilDuctCoeffHalf": "半油道散热系数",
    "hvOilDuctCoeffFull": "全油道散热系数", "axialWindingCount": "轴向并绕",
    "radialWindingCount": "辐向并绕", "minLayerCount": "最小层（段）数",
    "maxLayerCount": "最大层（段）数", "axialWindingCoefficient": "轴向绕制系数",
    "radialWindingCoefficient": "径向绕制系数", "paperWrappedFlatThickness": "纸包扁线厚度（mm）",
    "lowVoltageLayerThickness": "低压层间绝缘厚度（mm）", "coilDistance": "高低压绕组距离（mm）",
    "flatWireWidthMin": "扁线宽度下限（mm）", "flatWireWidthMax": "扁线宽度上限（mm）",
    "flatWireThicknessMin": "扁线厚度下限（mm）", "flatWireThicknessMax": "扁线厚度上限（mm）",
    "flatWireAspectRatioLimitMin": "扁线宽厚比下限", "flatWireAspectRatioLimitMax": "扁线宽厚比上限",
    "coreToLowVoltageDistance": "铁芯至低压距离（mm）", "lowToHighMainAirway": "高低压主油道（mm）",
    "lowVoltageToYokeDistance": "低压至轭距离（mm）", "highVoltageToYokeDistance": "高压至轭距离（mm）",
    "tankType": "油箱类型", "tankHighGap": "油箱高度余量（mm）", "tankLongGap": "油箱长度余量（mm）",
    "tankWideGap": "油箱宽度余量（mm）", "tankWallThickness": "油箱壁厚（mm）",
    "tankCoverThickness": "油箱盖板厚度（mm）", "bottomCoverThickness": "底板厚度（mm）",
    "airGapHeight": "空气层高度（mm）", "heatSinkWidth": "散热器片宽",
    "heatSinkGroups": "散热器组数", "heatSinkGroupsMin": "散热器组数下限",
    "heatSinkGroupsMax": "散热器组数上限", "corrosionLevel": "防腐等级",
    "hasConservatorTank": "是否带储油柜", "tankEdgeParam1": "箱沿尺寸 1",
    "tankEdgeParam2": "箱沿尺寸 2", "tankEdgeParam3": "箱沿尺寸 3",
    "angleSteelParam1": "角钢尺寸 1", "angleSteelParam2": "角钢尺寸 2",
    "angleSteelParam3": "角钢尺寸 3", "footPadThickness": "垫脚厚度",
    "longAxisCorrugatedSurfaces": "长轴波纹面数", "shortAxisCorrugatedSurfaces": "短轴波纹面数",
}

SECTION_NAMES = {
    "performance_index": "产品性能需求与标准", "optimization_scope": "优化范围与约束",
    "craft_core": "铁芯工艺与计算参数", "craft_low_coil": "低压绕组工艺与计算参数",
    "craft_high_coil": "高压绕组工艺与计算参数", "craft_isolation": "绝缘与间距计算参数",
    "craft_fuel_tank": "油箱与散热计算参数", "craft_unit_price": "材料单价参数",
}


# 单设备页面的诊断面对工程使用者，不展示精算器和 GA 内部的英文约束编码。
# 单位与规则都跟随实际计算项；没有物理上限的规则明确写“仅设下限”，不伪造区间。
CONSTRAINT_PRESENTATION = {
    "flux_density": ("铁芯磁密", "T", "磁密必须处于页面设定的允许区间"),
    "no_load_loss": ("空载损耗 P0", "W", "按空载损耗标准及上下限系数判定"),
    "load_loss": ("负载损耗 PK", "W", "按负载损耗标准及上下限系数判定"),
    "impedance": ("短路阻抗 UK", "%", "按阻抗标准及上下限系数判定"),
    "oil_top_temp_rise": ("油顶温升", "K", "不应超过油顶层温升限值"),
    "lv_temp_rise": ("低压绕组温升", "K", "不应超过绕组温升限值"),
    "hv_temp_rise": ("高压绕组温升", "K", "不应超过绕组温升限值"),
    "corrugation_expansion": ("波纹片可补偿油膨胀量", "kg", "3 × 波纹片可补偿油膨胀量应不小于油膨胀量"),
    "radiator_center_distance": ("散热器中心距", "mm", "散热器中心距不得超过油箱可安装上限"),
    "lv_current_density": ("低压导线电流密度", "A/mm²", "按当前导线材料的电流密度区间判定"),
    "hv_current_density": ("高压导线电流密度", "A/mm²", "按当前导线材料的电流密度区间判定"),
    "lv_reactance_height": ("低压绕组高度", "mm", "按低压绕组高度上下限判定"),
    "hv_reactance_height": ("高压绕组高度", "mm", "按高压绕组高度上下限判定"),
    "lv_wire_thickness": ("低压扁线厚度", "mm", "按低压扁线厚度上下限判定"),
    "hv_wire_thickness": ("高压扁线厚度", "mm", "按高压扁线厚度上下限判定"),
    "lv_wire_width": ("低压扁线宽度", "mm", "按低压扁线宽度上下限判定"),
    "hv_wire_width": ("高压扁线宽度", "mm", "按高压扁线宽度上下限判定"),
    "lv_wire_aspect_ratio": ("低压扁线宽厚比", "", "按低压扁线宽厚比上下限判定"),
    "hv_wire_aspect_ratio": ("高压扁线宽厚比", "", "按高压扁线宽厚比上下限判定"),
    "lv_layer_count": ("低压层数", "层", "箔材层数等于低压匝数；扁线层数只能为 2 或 4"),
    "hv_layer_count": ("高压层（段）数", "层", "按高压最小/最大层（段）数判定"),
}


def duct_label(duct: OilDuctScheme) -> str:
    if duct.count == 0:
        return "0 · 无油道"
    return f"{duct.count} · {'半油道' if duct.duct_type == 1 else '全油道'}"


def decimal_text(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        number = Decimal(str(value))
        return f"{number:.{digits}f}"
    except Exception:
        return str(value)


def cooling_option_text(candidate: Any) -> str:
    """将不可拆分的冷却型号压缩成实验台可读摘要。"""
    option = candidate if isinstance(candidate, CoolingOption) else getattr(candidate, "cooling_option", None)
    if option is None:
        return "未选择冷却型号"
    if int(option.tank_type) == 0:
        return (
            f"波纹油箱：记录 #{option.corrugation_id}；"
            f"长轴 {option.corrugation_long_axis_faces} 面、短轴 {option.corrugation_short_axis_faces} 面"
        )
    return (
        f"散热器：片宽 {RADIATOR_WIDTH_NAME.get(int(option.radiator_width_code), option.radiator_width_code)}，"
        f"中心距 {option.radiator_center_distance} mm，"
        f"{option.radiator_slice_count} 片，{option.radiator_groups} 组"
    )


def candidate_result_row(candidate: Any, result: Any, rank: int, category: str) -> dict[str, str | int]:
    """严格档案、近可行档案和第 0 代预览共用的可读候选摘要。"""
    low_type = getattr(candidate, "low_voltage_wire_type", None)
    high_type = getattr(candidate, "high_voltage_wire_type", None)
    low_name = wire_type_name("lv", int(low_type)) if low_type is not None else "低压类别未还原"
    high_name = wire_type_name("hv", int(high_type)) if high_type is not None else "高压类别未还原"
    violation_names = "、".join(
        CONSTRAINT_PRESENTATION.get(item.name, (item.name, "", ""))[0]
        for item in getattr(result, "violations", [])
    ) or "无"
    return {
        "排名": rank,
        "类别": category,
        "总成本（元）": decimal_text(getattr(result, "metrics", {}).get("price"), 2),
        "总超限": decimal_text(getattr(result, "total_violation", None), 6),
        "超限项": violation_names,
        "铁芯": f"{CORE_TYPE_NAME.get(int(candidate.core_type), '未知形状')} · 记录 #{candidate.core_data_id}",
        "低压绕组": f"{low_name} · 记录 #{candidate.low_voltage_wire_id}；{candidate.low_voltage_turns} 匝 / {candidate.low_voltage_layers} 层",
        "高压绕组": f"{high_name} · 记录 #{candidate.high_voltage_wire_id}；{candidate.high_voltage_layers} 层（段）",
        "冷却型号": cooling_option_text(candidate),
    }


def archive_preview_rows(items: Any, category: str, limit: int = 5) -> list[dict[str, str | int]]:
    return [
        candidate_result_row(item.candidate, item.result, rank, category)
        for rank, item in enumerate(list(items)[:limit], start=1)
    ]


def render_final_archive_tables(strict_archive: Any, near_archive: Any) -> None:
    """运行结束后直接展示两类最终档案，避免必须跳转回放页才能看结果。"""
    st.markdown("##### 最终双档案候选")
    strict_tab, near_tab = st.tabs(["严格合格档案", "近可行档案"])
    with strict_tab:
        rows = archive_preview_rows(strict_archive, "严格合格", limit=20)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch", height=360)
        else:
            st.info("本次运行尚未找到严格合格候选。")
    with near_tab:
        rows = archive_preview_rows(near_archive, "近可行", limit=20)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch", height=360)
        else:
            st.info("本次运行没有可计算的近可行候选。")


def create_initial_progress_callback() -> Any:
    """第 0 代精算的即时进度板；不虚构尚未执行的双档案状态。"""
    with st.container(border=True):
        st.markdown("##### 第 0 代实时进度")
        stage_slot = st.empty()
        progress_bar = st.progress(0, text="正在准备第 0 代精算")
        metrics_slot = st.empty()

    def update(event: dict[str, Any]) -> None:
        current = int(event.get("已处理候选") or 0)
        total = max(1, int(event.get("本阶段候选总数") or 1))
        progress_bar.progress(min(100, round(current * 100 / total)), text=f"{event.get('阶段', '正在精算')}：{current} / {total}")
        stage_slot.caption("第 0 代仅构造并评价候选，尚未进入遗传选择，因此这里不显示双档案。")
        with metrics_slot.container():
            cols = st.columns(4)
            cols[0].metric("已完成候选", f"{current} / {total}")
            cols[1].metric("实际精算调用", event.get("实际精算调用", 0))
            cols[2].metric("缓存命中", event.get("缓存命中", 0))
            cols[3].metric("并行精算线程", event.get("并行工作线程", "准备中"))

    return update


def create_ga_progress_callback(max_generations: int, config_id: int) -> Any:
    """完整 GA 的实时状态板：展示已完成代、当前档案和档案前五名。"""
    with st.container(border=True):
        st.markdown("##### GA 实时运行状态")
        stage_slot = st.empty()
        progress_bar = st.progress(0, text="正在准备遗传优化")
        metrics_slot = st.empty()
        archive_slot = st.empty()

    def update(event: dict[str, Any]) -> None:
        # 实时面板中的 placeholder 会随 Streamlit 重新渲染而消失；同时保存一份
        # 不可变快照，用户切换页面标签或控件触发重绘后仍可恢复最后一次真实状态。
        snapshot = dict(event)
        snapshot["配置 ID"] = int(config_id)
        snapshot["最大代数"] = int(max_generations)
        snapshot["严格档案"] = tuple(event.get("严格档案", ()))
        snapshot["近可行档案"] = tuple(event.get("近可行档案", ()))
        snapshot["运行状态"] = str(event.get("运行状态") or "运行中")
        st.session_state["ga_live_progress"] = snapshot
        generation = int(event.get("当前代") or 0)
        population_size = max(1, int(event.get("本代种群规模") or 1))
        record_count = int(event.get("本代累计评价记录") or 0)
        completed = bool(event.get("本代完成"))
        run_status = str(event.get("运行状态") or "运行中")
        if run_status != "运行中":
            overall = 1.0
        elif generation <= 0:
            # 初始种群是 GA 的前置成本，单独给出 15% 的可见进度，避免长时间
            # 显示为“第 0 代 / 0%”。
            overall = 0.15 * min(1.0, record_count / population_size)
        elif generation > max_generations:
            overall = 1.0
        else:
            within_generation = 1.0 if completed else min(0.98, record_count / population_size)
            overall = 0.15 + 0.85 * min(1.0, ((generation - 1) + within_generation) / max(1, max_generations))
        stage = str(event.get("阶段") or "正在运行")
        progress_bar.progress(min(100, round(overall * 100)), text=f"{run_status}：{stage}（总体 {round(overall * 100)}%）")
        stop_reason = event.get("提前停止原因")
        if stop_reason:
            stage_slot.caption(f"本次在第 {generation} 代结束。提前停止原因：{stop_reason}。")
        else:
            stage_slot.caption(
                f"当前第 {generation} 代；本代累计评价记录 {record_count} 条。"
                "局部诊断探测也会计入评价记录，因此它可能多于本代种群规模。"
            )
        strict_archive = event.get("严格档案", ())
        near_archive = event.get("近可行档案", ())
        best_strict = event.get("当前最优严格方案")
        best_near = event.get("当前最优近可行方案")
        with metrics_slot.container():
            cols = st.columns(6)
            cols[0].metric("当前代", generation)
            cols[1].metric("实际精算调用", event.get("实际精算调用", 0))
            cols[2].metric("缓存命中", event.get("缓存命中", 0))
            cols[3].metric("严格档案数", len(strict_archive))
            cols[4].metric("近可行档案数", len(near_archive))
            cols[5].metric(
                "当前最低严格成本" if best_strict is not None else "当前最小近可行超限",
                decimal_text(best_strict.result.metrics.get("price"), 2)
                if best_strict is not None else decimal_text(best_near.result.total_violation, 6)
                if best_near is not None else "—",
            )
        # 生成中的档案是上一轮已完成归档的真实快照；每代结束后立即刷新为本代结果。
        with archive_slot.container():
            left, right = st.columns(2)
            with left:
                st.markdown("**严格合格档案前五名**")
                rows = archive_preview_rows(strict_archive, "严格合格")
                if rows:
                    st.dataframe(rows, hide_index=True, width="stretch", height=210)
                else:
                    st.caption("暂未找到严格合格候选")
            with right:
                st.markdown("**近可行档案前五名**")
                rows = archive_preview_rows(near_archive, "近可行")
                if rows:
                    st.dataframe(rows, hide_index=True, width="stretch", height=210)
                else:
                    st.caption("暂未形成可计算近可行候选")

    return update


def render_persisted_ga_progress(config_id: int) -> None:
    """在一次 Streamlit 重绘后恢复最近一次 GA 的真实状态快照。"""
    event = st.session_state.get("ga_live_progress")
    if not isinstance(event, dict) or int(event.get("配置 ID", -1)) != int(config_id):
        return
    max_generations = max(1, int(event.get("最大代数") or 1))
    generation = int(event.get("当前代") or 0)
    population_size = max(1, int(event.get("本代种群规模") or 1))
    record_count = int(event.get("本代累计评价记录") or 0)
    completed = bool(event.get("本代完成"))
    state = str(event.get("运行状态") or "已保存快照")
    if state != "运行中":
        overall = 1.0
    elif generation <= 0:
        overall = 0.15 * min(1.0, record_count / population_size)
    elif generation > max_generations:
        overall = 1.0
    else:
        within_generation = 1.0 if completed else min(0.98, record_count / population_size)
        overall = 0.15 + 0.85 * min(1.0, ((generation - 1) + within_generation) / max_generations)
    strict_archive = event.get("严格档案", ())
    near_archive = event.get("近可行档案", ())
    best_strict = event.get("当前最优严格方案")
    best_near = event.get("当前最优近可行方案")
    with st.container(border=True):
        st.markdown("##### 最近一次 GA 运行状态")
        stage = str(event.get("阶段") or "—")
        st.progress(min(100, round(overall * 100)), text=f"{state}：{stage}（总体 {round(overall * 100)}%）")
        stop_reason = event.get("提前停止原因")
        if stop_reason:
            st.caption(f"恢复的是已结束状态：第 {generation} 代结束；提前停止原因：{stop_reason}。")
        else:
            st.caption(
                f"恢复的是最后一次实际回调状态：第 {generation} 代，本代累计评价 {record_count} 条。"
                "切换页面不会把它重置为初始值。"
            )
        cols = st.columns(6)
        cols[0].metric("当前代", generation)
        cols[1].metric("实际精算调用", event.get("实际精算调用", 0))
        cols[2].metric("缓存命中", event.get("缓存命中", 0))
        cols[3].metric("严格档案数", len(strict_archive))
        cols[4].metric("近可行档案数", len(near_archive))
        cols[5].metric(
            "当前最低严格成本" if best_strict is not None else "当前最小近可行超限",
            decimal_text(best_strict.result.metrics.get("price"), 2)
            if best_strict is not None else decimal_text(best_near.result.total_violation, 6)
            if best_near is not None else "—",
        )
        archive_left, archive_right = st.columns(2)
        with archive_left:
            st.markdown("**严格合格档案前五名**")
            rows = archive_preview_rows(strict_archive, "严格合格")
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch", height=210)
            else:
                st.caption("暂未找到严格合格候选")
        with archive_right:
            st.markdown("**近可行档案前五名**")
            rows = archive_preview_rows(near_archive, "近可行")
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch", height=210)
            else:
                st.caption("暂未形成可计算近可行候选")


def constraint_value_text(value: Any, unit: str, absent_text: str) -> str:
    """以统一格式显示约束实际值和边界，避免由颜色或英文方向表达含义。"""
    if value is None:
        return absent_text
    digits = 0 if unit == "层" else 4
    shown = decimal_text(value, digits)
    return f"{shown} {unit}".rstrip()


def constraint_diagnostic_rows(violations: list[Any]) -> list[dict[str, str]]:
    """把内部违规对象转换为含完整上下界的中文诊断表。"""
    rows: list[dict[str, str]] = []
    for item in violations:
        label, unit, default_rule = CONSTRAINT_PRESENTATION.get(
            item.name, (f"未命名约束（{item.name}）", "", "请核对精算器约束定义"),
        )
        lower = getattr(item, "lower", None)
        upper = getattr(item, "upper", None)
        # 兼容此前 SQLite 轨迹中只有触发侧 limit 的违规记录。
        if lower is None and upper is None:
            if item.direction == "lower":
                lower = item.limit
            elif item.direction == "upper":
                upper = item.limit
        actual = item.actual
        rule = getattr(item, "rule", None) or default_rule
        if item.direction == "lower" and lower is not None and actual is not None:
            excess = constraint_value_text(lower - actual, unit, "—")
            status = "低于下限"
            deviation = f"少 {excess}"
        elif item.direction == "upper" and upper is not None and actual is not None:
            excess = constraint_value_text(actual - upper, unit, "—")
            status = "高于上限"
            deviation = f"多 {excess}"
        elif item.direction == "invalid" and lower is not None and upper == lower and actual is not None:
            deviation = f"应为 {constraint_value_text(lower, unit, '—')}，相差 {constraint_value_text(abs(actual - lower), unit, '—')}"
            status = "不符合规则"
        else:
            status = "不符合规则"
            deviation = "不符合离散取值规则"
        if lower is not None and upper is not None and lower == upper:
            allowed_range = f"必须等于 {constraint_value_text(lower, unit, '—')}"
        elif lower is None and upper is None:
            allowed_range = "离散取值，见判定依据"
        else:
            allowed_range = (
                f"下限 {constraint_value_text(lower, unit, '无下限')}；"
                f"上限 {constraint_value_text(upper, unit, '无上限')}"
            )
        rows.append({
            "约束指标": label,
            "允许范围": allowed_range,
            "当前实际值": constraint_value_text(actual, unit, "未计算"),
            "不符合情况": f"{status}：{deviation}",
            "判定依据": rule,
        })
    return rows


def render_constraint_diagnostics(violations: list[Any]) -> None:
    """以逐项诊断卡展示超限，避免宽表将关键结论挤到横向滚动区。"""
    for row in constraint_diagnostic_rows(violations):
        with st.container(border=True):
            st.markdown(f"**{row['约束指标']}**")
            range_col, actual_col, issue_col = st.columns([2.2, 1.25, 1.8])
            range_col.caption("允许范围")
            range_col.write(row["允许范围"])
            actual_col.caption("当前实际值")
            actual_col.write(row["当前实际值"])
            issue_col.caption("不符合情况")
            issue_col.write(row["不符合情况"])
            st.caption(f"判定依据：{row['判定依据']}")


def _trace_decimal(value: Any) -> Decimal | None:
    """将 SQLite 回放中的 JSON 数字安全恢复为诊断展示用数值。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def trace_constraint_diagnostic_rows(violations: list[dict[str, Any]]) -> list[dict[str, str]]:
    """复用单设备页的中文约束展示，不把 SQLite 内部字段直接暴露给读者。"""
    restored = [
        ConstraintViolation(
            name=str(item.get("name", "unknown")), direction=str(item.get("direction", "invalid")),
            actual=_trace_decimal(item.get("actual")), limit=_trace_decimal(item.get("limit")),
            normalized_excess=_trace_decimal(item.get("normalized_excess")) or Decimal("0"),
            lower=_trace_decimal(item.get("lower")), upper=_trace_decimal(item.get("upper")),
            rule=str(item["rule"]) if item.get("rule") else None,
        )
        for item in violations
        if isinstance(item, dict)
    ]
    return constraint_diagnostic_rows(restored)


def trace_candidate_structure_rows(candidate: dict[str, Any]) -> list[dict[str, str]]:
    """把回放轨迹的候选 JSON 压缩为工程人员可读的结构表。"""
    low_type = candidate.get("low_voltage_wire_type")
    high_type = candidate.get("high_voltage_wire_type")
    low_name = wire_type_name("lv", int(low_type)) if low_type is not None else "未还原"
    high_name = wire_type_name("hv", int(high_type)) if high_type is not None else "未还原"

    def duct(value: Any) -> str:
        if not isinstance(value, dict):
            return "未记录"
        count = int(value.get("count") or 0)
        if count == 0:
            return "无油道"
        return f"{count} 个{'半油道' if int(value.get('duct_type') or 0) == 1 else '全油道'}"

    cooling = candidate.get("cooling_option")
    if not isinstance(cooling, dict):
        cooling_text = "未记录"
    elif int(cooling.get("tank_type") or 0) == 0:
        cooling_text = f"波纹片记录 #{cooling.get('corrugation_id', '—')}"
    else:
        cooling_text = (
            f"散热器：中心距 {cooling.get('radiator_center_distance', '—')} mm，"
            f"{cooling.get('radiator_slice_count', '—')} 片，{cooling.get('radiator_groups', '—')} 组"
        )
    return [
        {"模块": "铁芯", "当前取值": f"{CORE_TYPE_NAME.get(int(candidate.get('core_type') or -1), '未知形状')} · 记录 #{candidate.get('core_data_id', '—')}；硅钢 {candidate.get('steel_brand', '—')}"},
        {"模块": "低压绕组", "当前取值": f"{low_name} · 记录 #{candidate.get('low_voltage_wire_id', '—')}；{candidate.get('low_voltage_turns', '—')} 匝 / {candidate.get('low_voltage_layers', '—')} 层；{duct(candidate.get('low_voltage_duct'))}"},
        {"模块": "高压绕组", "当前取值": f"{high_name} · 记录 #{candidate.get('high_voltage_wire_id', '—')}；{candidate.get('high_voltage_layers', '—')} 层（段）；{duct(candidate.get('high_voltage_duct'))}"},
        {"模块": "冷却型号", "当前取值": cooling_text},
        {"模块": "长圆铁芯直线段 C", "当前取值": "不适用" if candidate.get("conversion_c") is None else f"{candidate.get('conversion_c')} mm"},
    ]


def trace_domain_rows(domain: dict[str, Any]) -> list[dict[str, str]]:
    """回放页面只展示理解本次搜索范围所需的摘要，原始 JSON 留给调试折叠区。"""
    integer = domain.get("integer_domain") if isinstance(domain.get("integer_domain"), dict) else {}

    def count(name: str) -> str:
        return str(domain.get(name, "—"))

    def range_text(name: str, label: str) -> str:
        item = integer.get(name) if isinstance(integer, dict) else None
        if not isinstance(item, dict):
            return "未记录"
        if name == "lv_layers":
            return "按导线类别派生：箔材=低压匝数；扁线=2 或 4"
        return f"{item.get('minimum', '—')} ～ {item.get('maximum', '—')}，步长 {item.get('step', '—')}"

    return [
        {"搜索项": "硅钢牌号", "范围/数量": count("steel_brands") + " 种"},
        {"搜索项": "铁芯记录", "范围/数量": count("cores") + " 条"},
        {"搜索项": "低压完整线规", "范围/数量": count("lv_wires") + " 条"},
        {"搜索项": "高压完整线规", "范围/数量": count("hv_wires") + " 条"},
        {"搜索项": "低压匝数", "范围/数量": range_text("lv_turns", "低压匝数")},
        {"搜索项": "低压层数", "范围/数量": range_text("lv_layers", "低压层数")},
        {"搜索项": "高压层（段）数", "范围/数量": range_text("hv_layers", "高压层数")},
        {"搜索项": "低压/高压油道方案", "范围/数量": f"低压 {count('lv_oil_duct_schemes')} 种；高压 {count('hv_oil_duct_schemes')} 种"},
        {"搜索项": "完整冷却型号", "范围/数量": count("cooling_options") + " 种"},
    ]


def trace_setting_rows(settings: dict[str, Any]) -> list[dict[str, str]]:
    """将 GA 设置由内部键翻译为中文，避免回放默认出现大段 JSON。"""
    labels = {
        "population_size": "后续工作种群规模", "generations": "最大代数", "crossover_rate": "交叉率",
        "coupled_electromagnetic_crossover_rate": "结构电磁耦合交叉占比",
        "mutation_rate": "总变异率", "guided_mutation_rate": "定向变异占比",
        "history_ratio": "历史种子比例", "random_injection_ratio": "每代随机注入比例",
        "injection_mode": "随机注入来源模式", "guided_mutation_mode": "定向变异模式",
        "evaluation_budget": "总真实精算预算 B", "evolution_evaluation_budget": "演化精算预算 B′",
        "archive_size": "每类档案容量", "elite_count": "每代精英保留数",
        "local_probe_limit": "每次定向局部精算数", "local_refine_elites": "最终细化的严格精英数",
        "local_refine_limit": "最终邻域细化上限", "stagnation_limit": "停滞代数上限",
        "coverage_count": "第 0 代覆盖候选数", "history_count": "第 0 代历史种子数",
        "random_count": "第 0 代随机候选数", "effective_population_size": "第 0 代实际候选数",
        "automatic_target_size": "第 0 代自动目标规模", "parallel_workers": "第 0 代并行精算线程数",
        "initial_preview_reuse": "是否复用第 0 代预览结果",
        "initial_preview_reused_candidate_count": "复用的预览精算结果数",
        "experiment_strategy_label": "本次实验策略", "experiment_strategy": "策略内部标识",
        "execution_entry": "运行入口", "elapsed_seconds": "本次耗时（秒）",
    }
    value_labels = {
        "injection_mode": {"empirical_preferred": "经验有效域优先", "raw_only": "仅原始搜索域"},
        "guided_mutation_mode": {"single_primary": "单主导约束局部探测", "random_only": "仅普通随机变异"},
        "experiment_strategy": {
            "baseline": "消融基线", "coupled": "仅耦合交叉",
            "empirical_injection": "耦合交叉 + 经验有效域注入",
            "guided_mutation": "完整诊断反馈策略", "custom": "当前页面/CLI 自定义参数",
        },
        "execution_entry": {
            "streamlit_ga_page": "Streamlit GA 页面", "cli_ga_run": "命令行单次 GA",
            "cli_ga_ablation": "命令行消融批次", "initial_population_preview": "第 0 代预览",
        },
    }
    rows: list[dict[str, str]] = []
    for key, label in labels.items():
        if key not in settings:
            continue
        value = settings[key]
        if key == "elapsed_seconds" and isinstance(value, (int, float)):
            shown = f"{value:.3f}"
        else:
            shown = value_labels.get(key, {}).get(value, "是" if value is True else "否" if value is False else str(value))
        rows.append({"设置项": label, "当前值": shown})
    provenance = settings.get("provenance")
    if isinstance(provenance, dict):
        provenance_rows = (
            ("单设备精算公式版本", provenance.get("formula_revision")),
            ("核心代码指纹（SHA-256）", provenance.get("core_code_fingerprint")),
            ("运行证据采集入口", provenance.get("entrypoint")),
            ("运行证据采集时间（UTC）", provenance.get("captured_at_utc")),
        )
        for label, value in provenance_rows:
            if value is not None:
                rows.append({"设置项": label, "当前值": str(value)})
    return rows


def index_of(values: list[Any], value: Any) -> int:
    try:
        return values.index(value)
    except ValueError:
        return 0


def steel_brand_options(catalog: dict[str, list[dict[str, Any]]]) -> tuple[list[str], dict[int, str]]:
    """返回三相牌号选项，以及 Java 编码到牌号的映射。

    不可回退至完整硅钢片损耗目录：目录中存在的记录不等于三相页面允许选择的牌号。
    """
    existing = {str(row["brand"]) for row in catalog["steel"]}
    code_to_brand = {
        int(row["brand_code"]): str(row["brand"])
        for row in catalog.get("steel_brand_mapping", [])
        if str(row["brand"]) in existing
    }
    return [code_to_brand[code] for code in sorted(code_to_brand)], code_to_brand


def configured_steel_brands(config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]]) -> list[str]:
    """还原 Java 配置中的硅钢牌号编码，作为研究候选域的默认值。"""
    _, code_to_brand = steel_brand_options(catalog)
    grade_codes = config["craft_core"].get("siliconSteelGrade") or []
    result = [code_to_brand[int(code)] for code in grade_codes if int(code) in code_to_brand]
    return result


def config_table_rows(
    section: str, values: dict[str, Any], catalog: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, str]]:
    """将配置 JSON 转成可读表格，空值不隐藏以免误认为公式未使用该字段。"""
    rows: list[dict[str, str]] = []
    for key, value in values.items():
        field_name = CONFIG_FIELD_NAMES.get(key, key)
        if section == "craft_low_coil" and key == "wireSpecification":
            field_name = "低压线规"
        elif section == "craft_high_coil" and key == "wireSpecification":
            field_name = "高压线规"
        elif section == "craft_low_coil" and key == "channelType":
            field_name = "低压油道类型"
        elif section == "craft_high_coil" and key == "channelType":
            field_name = "高压油道类型"
        elif section == "craft_low_coil" and key == "channelCount":
            field_name = "低压油道数量"
        elif section == "craft_high_coil" and key == "channelCount":
            field_name = "高压油道数量"

        if section == "craft_core" and key == "siliconSteelGrade" and catalog is not None:
            _, code_to_brand = steel_brand_options(catalog)
            shown = "、".join(
                f"{code_to_brand.get(int(code), f'未知编码 {code}')}（编码 {code}）"
                for code in (value or [])
            ) or "未设置"
        elif section == "craft_core" and key == "isRolledCore":
            shown = CORE_TYPE_NAME.get(int(value), f"未知类型 {value}") if value is not None else "未设置"
        elif section == "performance_index" and key == "frequency":
            shown = {0: "50 Hz", 1: "60 Hz"}.get(value, str(value))
        elif section == "performance_index" and key == "deviationType":
            shown = {0: "正偏差", 1: "负偏差", 2: "美变正偏差"}.get(value, str(value))
        elif section == "performance_index" and key == "impedanceDeviationType":
            shown = {0: "阻抗标准 ±10%", 1: "阻抗标准 ±7.5%", 2: "阻抗标准＜100%"}.get(value, str(value))
        elif section == "performance_index" and key == "windingMethodType":
            shown = {0: "常规", 1: "高压分段", 2: "低高压双分"}.get(value, str(value))
        elif section == "craft_fuel_tank" and key == "tankType":
            shown = {0: "波纹翅片油箱", 1: "散热片油箱"}.get(value, str(value))
        elif section == "craft_fuel_tank" and key == "corrosionLevel":
            shown = {0: "C3", 1: "C4", 2: "C5"}.get(value, str(value))
        elif section == "craft_fuel_tank" and key == "heatSinkWidth":
            shown = {0: "310 mm", 1: "480 mm", 2: "520 mm"}.get(value, str(value))
        elif section == "craft_fuel_tank" and key == "hasConservatorTank":
            shown = {0: "否", 1: "是"}.get(value, str(value))
        elif section in ("craft_low_coil", "craft_high_coil") and key == "wireSpecification":
            wire_names = LOW_VOLTAGE_WIRE_TYPES if section == "craft_low_coil" else HIGH_VOLTAGE_WIRE_TYPES
            shown = wire_names.get(int(value), f"未知类别 {value}") if value is not None else "未设置"
        elif section in ("craft_low_coil", "craft_high_coil") and key == "channelType":
            duct_names = {0: "无油道", 1: "半油道", 2: "全油道"}
            options = value if isinstance(value, list) else [value]
            shown = "、".join(duct_names.get(int(item), f"未知类型 {item}") for item in options) if options else "未设置"
        elif isinstance(value, list):
            shown = "、".join(str(item) for item in value) if value else "未设置"
        elif value is None:
            shown = "未设置"
        else:
            shown = str(value)
        rows.append({"参数": field_name, "配置字段": key, "当前值": shown})
    return rows


LOW_VOLTAGE_WIRE_TYPES = {
    0: "LM（类别 0）",
    1: "TM（类别 1）",
    2: "ZBL（类别 2）",
    3: "ZB（类别 3）",
}
HIGH_VOLTAGE_WIRE_TYPES = {
    0: "QZ（类别 0）",
    1: "QZL / QQL（类别 1）",
    2: "ZBL（类别 2）",
    3: "ZB（类别 3）",
    4: "QZB（类别 4）",
    5: "QZBL / QQLB（类别 5）",
}


def wire_type_name(side: str, wire_type: int) -> str:
    """使用 Java 三相类别编码显示导线类别；QQL/QQLB 是现有价格字段兼容名。"""
    names = LOW_VOLTAGE_WIRE_TYPES if side == "lv" else HIGH_VOLTAGE_WIRE_TYPES
    return names.get(int(wire_type), f"未知类别 {wire_type}")


def wire_catalog_name(side: str, wire_type: int) -> str:
    """返回某侧、某完整导线类别所对应的真实基础目录。"""
    if side == "lv":
        return "箔材目录" if wire_type in (0, 1) else "扁线目录"
    return "圆线目录" if wire_type in (0, 1) else "扁线目录"


def wire_rows_in_search_boundary(
    side: str, wire_type: int, catalog: dict[str, list[dict[str, Any]]], craft: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回类别的原始目录行和实际可进搜索域的完整记录行。

    扁线过滤直接复用 GeneDomain 的同一规则，保证页面显示的数量与 GA 真正使用
    的数量一致；这里绝不把宽度、厚度重新排列组合成新规格。
    """
    family = "foil" if side == "lv" and wire_type in (0, 1) else (
        "round" if side == "hv" and wire_type in (0, 1) else "flat"
    )
    all_rows = list(catalog[family])
    eligible_rows = (
        GeneDomain._flat_rows_in_configured_range(all_rows, craft)
        if family == "flat" else all_rows
    )
    return all_rows, eligible_rows


def render_wire_boundary_summary(
    side: str, selected_types: list[int], catalog: dict[str, list[dict[str, Any]]], craft: dict[str, Any],
) -> None:
    """把每一类别的真实记录数和扁线边界过滤结果展示给操作者。"""
    rows: list[dict[str, Any]] = []
    for wire_type in selected_types:
        all_rows, eligible_rows = wire_rows_in_search_boundary(side, int(wire_type), catalog, craft)
        is_flat = wire_catalog_name(side, int(wire_type)) == "扁线目录"
        bounds = "不适用"
        if is_flat:
            bounds = (
                f"宽 {craft.get('flatWireWidthMin', '—')}–{craft.get('flatWireWidthMax', '—')}；"
                f"厚 {craft.get('flatWireThicknessMin', '—')}–{craft.get('flatWireThicknessMax', '—')}；"
                f"宽厚比 {craft.get('flatWireAspectRatioLimitMin', '—')}–{craft.get('flatWireAspectRatioLimitMax', '—')}"
            )
        rows.append({
            "类别": wire_type_name(side, int(wire_type)),
            "真实目录": wire_catalog_name(side, int(wire_type)),
            "目录完整记录数": len(all_rows),
            "进入搜索域的完整记录数": len(eligible_rows),
            "扁线范围过滤": bounds,
        })
    st.dataframe(rows, hide_index=True, width="stretch")


def wire_record_label(row: dict[str, Any], wire_type: int) -> str:
    """展示目录记录的真实规格，ID 只是保证离散编码可追溯的主键。"""
    record_id = row["id"]
    if "nominal_diameter" in row:
        return f"#{record_id} · 标称直径 {row.get('nominal_diameter')} mm · 截面 {row.get('calculate_section')} mm²"
    if "breadth" in row:
        return f"#{record_id} · 宽 {row.get('breadth')} mm × 厚 {row.get('thickness')} mm · 截面 {row.get('section')} mm²"
    return f"#{record_id} · 宽 {row.get('line_width')} mm × 厚 {row.get('line_thickness')} mm · 截面 {row.get('section')} mm²"


@dataclass(frozen=True)
class ResearchBoundary:
    """本次页面会话共享的、显式选择的 Python 候选边界。"""

    steels: tuple[str, ...]
    core_types: tuple[int, ...]
    low_wire_types: tuple[int, ...]
    high_wire_types: tuple[int, ...]
    tank_types: tuple[int, ...]
    corrugation_long_faces: tuple[int, ...]
    corrugation_short_faces: tuple[int, ...]
    radiator_width_codes: tuple[int, ...]
    radiator_groups: tuple[int, ...]

    def domain_kwargs(self) -> dict[str, Any]:
        return {
            "allowed_steel_brands": self.steels,
            "allowed_core_types": self.core_types,
            "allowed_low_voltage_wire_types": self.low_wire_types,
            "allowed_high_voltage_wire_types": self.high_wire_types,
            "allowed_tank_types": self.tank_types,
            "allowed_corrugation_long_faces": self.corrugation_long_faces,
            "allowed_corrugation_short_faces": self.corrugation_short_faces,
            "allowed_radiator_width_codes": self.radiator_width_codes,
            "allowed_radiator_groups": self.radiator_groups,
        }


def render_fixed_inputs_and_boundary(
    config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
) -> ResearchBoundary:
    """展示固定入参，并返回本次 Python 实验实际采用的材料/铁芯形状搜索域。"""
    performance = config["performance_index"]
    scope = config["optimization_scope"]
    core = config["craft_core"]
    low = config["craft_low_coil"]
    high = config["craft_high_coil"]
    tank = config["craft_fuel_tank"]

    st.info("此处只定义可变的结构设计域；产品规格、性能标准和工艺参数保持当前 Java 配置不变。这里的选择会同时用于单设备试算与 GA。")
    with st.expander("查看本次计算固定条件", expanded=False):
        base_card, standards_card, constraints_card = st.columns(3)
        with base_card:
            st.markdown("<div class='section-card'><div class='section-card-title'>产品基本需求</div>"
                        f"<div class='section-card-copy'>额定容量 <b>{decimal_text(performance.get('capacity'), 0)} kVA</b><br>"
                        f"频率 <b>{'50 Hz' if int(performance.get('frequency') or 0) == 0 else '60 Hz'}</b><br>"
                        f"高/低压 <b>{decimal_text(performance.get('highVoltageRated'), 0)} / {decimal_text(performance.get('lowVoltageRated'), 0)} V</b><br>"
                        f"接线组别 <b>{performance.get('displayConnectionGroup') or performance.get('windingConnection') or '—'}</b></div></div>", unsafe_allow_html=True)
        with standards_card:
            st.markdown("<div class='section-card'><div class='section-card-title'>性能标准</div>"
                        f"<div class='section-card-copy'>空载损耗 P0 <b>{decimal_text(performance.get('noLoadStandard'), 0)} W</b><br>"
                        f"负载损耗 PK <b>{decimal_text(performance.get('loadStandard'), 0)} W</b><br>"
                        f"短路阻抗 UK <b>{decimal_text(performance.get('impedanceStandard'), 2)} %</b><br>"
                        "具体上下限系数见完整配置表。</div></div>", unsafe_allow_html=True)
        with constraints_card:
            st.markdown("<div class='section-card'><div class='section-card-title'>严格可行约束</div>"
                        f"<div class='section-card-copy'>磁密 <b>{scope.get('fluxDensityMin')}–{scope.get('fluxDensityMax')} T</b><br>"
                        f"油顶温升 ≤ <b>{scope.get('oilLayerTempRiseLimit')} K</b><br>"
                        f"绕组温升 ≤ <b>{scope.get('windingTempRiseLimit')} K</b><br>"
                        "电流密度、几何和油道约束由精算器逐项判断。</div></div>", unsafe_allow_html=True)

    st.markdown("#### 本次允许变化的结构")
    st.caption("导线类别可多选，但每个候选始终引用“类别 + 完整目录记录 ID”；不会拼接目录中不存在的宽、厚或冷却部件。")
    all_steel_brands, code_to_brand = steel_brand_options(catalog)
    java_default_steels = configured_steel_brands(config, catalog)
    if not java_default_steels and config["craft_core"].get("siliconSteelGrade"):
        st.warning("当前 Java 配置中的硅钢片编码无法从三相牌号映射表还原；请核对 tb_silicon_steel_brand_mapping。")
    available_core_types = sorted({int(row["core_type"]) for row in catalog["core"]
                                   if int(core["coreDiameterMin"]) <= int(row["core_diameter"]) <= int(core["coreDiameterMax"])})
    # 旧版本曾将完整损耗目录写入此控件的 session state。更换 key，避免旧的
    # 非三相牌号残留在 Streamlit 多选框里，导致下拉菜单无法正常编辑。
    steel_state_key = f"research-steel-v2-{config.get('id', 'current')}"
    core_state_key = f"research-core-type-v2-{config.get('id', 'current')}"
    low_wire_state_key = f"research-low-wire-type-v1-{config.get('id', 'current')}"
    high_wire_state_key = f"research-high-wire-type-v1-{config.get('id', 'current')}"
    boundary_a, boundary_b = st.columns(2)
    with boundary_a:
        selected_steels = st.multiselect(
            "允许搜索的硅钢片牌号", all_steel_brands,
            default=java_default_steels or all_steel_brands,
            key=steel_state_key,
            help="实际牌号来自 tb_silicon_steel_sheets_config；默认沿用 Java 配置中的牌号编码。单设备精算和 GA 都只能从此处保留的牌号中选择。",
        )
    with boundary_b:
        selected_core_types = st.multiselect(
            "允许搜索的三相铁芯形状", available_core_types,
            default=[core_type for core_type in (0, 1, 2) if core_type in available_core_types],
            format_func=lambda value: CORE_TYPE_NAME.get(int(value), f"未知类型 {value}"),
            key=core_state_key,
            help="三相铁芯只有圆形、长圆形、椭圆形。这里的选择决定单设备候选与 GA 是否把该形状作为结构基因。",
        )
    wire_boundary_a, wire_boundary_b = st.columns(2)
    with wire_boundary_a:
        selected_low_wire_types = st.multiselect(
            "允许搜索的低压导线类别", list(LOW_VOLTAGE_WIRE_TYPES),
            default=[int(low["wireSpecification"])],
            format_func=lambda value: wire_type_name("lv", int(value)), key=low_wire_state_key,
            help="可同时保留 LM、TM、ZBL、ZB 等类别。GA 的低压导线基因是“类别 + 真实记录 ID”的完整选项，绝不拼接宽和厚。",
        )
        if selected_low_wire_types:
            render_wire_boundary_summary("lv", selected_low_wire_types, catalog, low)
    with wire_boundary_b:
        selected_high_wire_types = st.multiselect(
            "允许搜索的高压导线类别", list(HIGH_VOLTAGE_WIRE_TYPES),
            default=[int(high["wireSpecification"])],
            format_func=lambda value: wire_type_name("hv", int(value)), key=high_wire_state_key,
            help="可同时保留 QZ、QZL/QQL、ZBL、QZB、QZBL/QQLB 等类别。实际材料、绝缘和价格逻辑由候选类别与完整记录共同决定。",
        )
        if selected_high_wire_types:
            render_wire_boundary_summary("hv", selected_high_wire_types, catalog, high)
    st.markdown("##### 允许搜索的冷却方案")
    st.caption("一个候选只选择一种油箱冷却形式：波纹油箱或散热器油箱。可同时勾选两类，使 GA 比较两类方案；不会把两者混装到同一个候选。")
    cooling_a, cooling_b, cooling_c = st.columns(3)
    with cooling_a:
        selected_tank_types = st.multiselect(
            "允许搜索的油箱冷却形式", [0, 1], default=[int(tank["tankType"])],
            format_func=lambda value: TANK_TYPE_NAME[int(value)], key=f"research-tank-type-v1-{config.get('id', 'current')}",
        )
    with cooling_b:
        selected_width_codes = st.multiselect(
            "允许搜索的散热片片宽", [0, 1, 2], default=[int(tank.get("heatSinkWidth") or 0)],
            format_func=lambda value: RADIATOR_WIDTH_NAME[int(value)], key=f"research-radiator-width-v1-{config.get('id', 'current')}",
            disabled=1 not in selected_tank_types,
        )
    with cooling_c:
        group_min = tank.get("heatSinkGroupsMin")
        group_max = tank.get("heatSinkGroupsMax")
        if group_min is None and group_max is None:
            group_min = group_max = tank.get("heatSinkGroups")
        configured_groups = list(range(int(group_min or 0), int(group_max or -1) + 1)) if int(group_min or 0) > 0 else []
        group_text = st.text_input(
            "允许搜索的散热器组数（用逗号分隔）",
            value=",".join(str(value) for value in configured_groups),
            key=f"research-radiator-groups-v2-{config.get('id', 'current')}", disabled=1 not in selected_tank_types,
            help="有 Java 页面组数范围时默认列出该范围；当前页面若为波纹油箱而没有该范围，需在这里明确填写，例如 2,3,4。",
        )
    face_a, face_b = st.columns(2)
    with face_a:
        long_face_text = st.text_input(
            "允许搜索的波纹长轴面数（用逗号分隔）",
            value=str(int(tank.get("longAxisCorrugatedSurfaces") or 0)),
            key=f"research-corr-long-faces-v1-{config.get('id', 'current')}", disabled=0 not in selected_tank_types,
            help="Java 当前配置只保存一个面数，没有最小/最大候选范围。这里必须由实验者明确填写离散值，例如 1,2,3；不会擅自扩展为某个固定区间。",
        )
    with face_b:
        short_face_text = st.text_input(
            "允许搜索的波纹短轴面数（用逗号分隔）",
            value=str(int(tank.get("shortAxisCorrugatedSurfaces") or 0)),
            key=f"research-corr-short-faces-v1-{config.get('id', 'current')}", disabled=0 not in selected_tank_types,
            help="Java 当前配置只保存一个面数，没有最小/最大候选范围。这里必须由实验者明确填写离散值，例如 0,1,2。",
        )
    def parse_face_values(raw: str) -> tuple[int, ...]:
        try:
            values = tuple(sorted({int(piece.strip()) for piece in raw.replace("，", ",").split(",") if piece.strip()}))
        except ValueError:
            return ()
        return values if values and min(values) >= 0 else ()
    long_faces, short_faces = parse_face_values(long_face_text), parse_face_values(short_face_text)
    selected_groups = parse_face_values(group_text)
    if not selected_steels or not selected_core_types or not selected_low_wire_types or not selected_high_wire_types or not selected_tank_types:
        st.warning("硅钢片、铁芯形状、低压导线类别、高压导线类别和冷却形式均至少保留一种，才能运行单设备精算或 GA。")
    if 0 in selected_tank_types and (not long_faces or not short_faces):
        st.warning("波纹长轴和短轴面数必须填写非负整数，例如 2 或 1,2,3。")
    if 1 in selected_tank_types and not selected_groups:
        st.warning("散热器组数必须填写正整数，例如 2,3,4。")

    with st.expander("查看完整产品需求、损耗标准与约束字段", expanded=False):
        left, right = st.columns(2)
        with left:
            st.markdown("##### 产品需求与标准")
            st.dataframe(config_table_rows("performance_index", performance, catalog), hide_index=True, width="stretch", height=300)
        with right:
            st.markdown("##### 可行性约束")
            st.dataframe(config_table_rows("optimization_scope", scope, catalog), hide_index=True, width="stretch", height=300)

    with st.expander("固定输入 ② 工艺、绝缘、油箱与价格计算参数", expanded=False):
        for section in ("craft_core", "craft_low_coil", "craft_high_coil", "craft_isolation", "craft_fuel_tank", "craft_unit_price"):
            st.markdown(f"##### {SECTION_NAMES[section]}")
            st.dataframe(config_table_rows(section, config[section], catalog), hide_index=True, width="stretch", height=220)
            if section == "craft_unit_price":
                price_resolution = config.get("_unit_price_resolution", {})
                fallback_count = int(price_resolution.get("fallback_count") or 0)
                unresolved = price_resolution.get("unresolved_keys") or []
                if fallback_count:
                    st.caption(
                        f"单价来源：{fallback_count} 项方案配置为空，已通过 Faladi MCP 读取当前材料价格补齐；"
                        "其余项优先保留方案配置值。"
                    )
                if unresolved:
                    st.warning(
                        "以下单价在方案配置和当前材料价格接口中均未取得："
                        + "、".join(str(key) for key in unresolved)
                        + "。相关候选的成本不能视为有效比较结果。"
                    )
        st.caption("这些参数用于铁芯、绕组、绝缘、油箱/散热、重量和成本计算。其中“Java 既有硅钢片候选”和“Java 当前铁芯形状”只是旧配置字段；真正用于本次 Python 实验的选择以上方候选范围为准。")

    with st.expander("共享研究边界：单设备试算与 GA 使用同一离散来源", expanded=False):
        core_count = len([row for row in catalog["core"] if int(core["coreDiameterMin"]) <= int(row["core_diameter"]) <= int(core["coreDiameterMax"])])
        rows = [
            {"对象": "硅钢片牌号", "候选来源": "原始硅钢片目录", "本配置下的使用方式": "按原始牌号选择；不生成虚构材料"},
            {"对象": "铁芯形状与直径", "候选来源": f"原始铁芯目录（直径 {core['coreDiameterMin']}–{core['coreDiameterMax']} mm，共 {core_count} 条）", "本配置下的使用方式": "圆形、长圆、椭圆由目录记录决定"},
            {"对象": "低压线规", "候选来源": "所选低压类别对应的真实目录（箔材或扁线）", "本配置下的使用方式": "完整类别 + 原始记录 ID；扁线先按页面宽/厚/宽厚比过滤，不拆分或拼接宽厚"},
            {"对象": "高压线规", "候选来源": "所选高压类别对应的真实目录（圆线或扁线）", "本配置下的使用方式": "完整类别 + 原始记录 ID；扁线先按页面宽/厚/宽厚比过滤，不拆分或拼接宽厚"},
            {"对象": "油箱/冷却型号", "候选来源": "波纹片目录 + 长/短轴面数；或 310/480/520 散热器完整 G/Q/SZ 记录组", "本配置下的使用方式": "本次页面可多选波纹/散热器；每个候选二选一。波纹面数、散热片片宽和组数均进入冷却块"},
            {"对象": "两侧油道", "候选来源": "离散油道组合", "本配置下的使用方式": "无油道，或数量 1–4 的半油道/全油道"},
            {"对象": "低压匝数、两侧层数", "候选来源": "当前 Java 页面已保存的范围与导线类别规则", "本配置下的使用方式": "低压匝数取页面下限至上限；高压层（段）数取页面最小值至最大值；低压层数由导线类别决定：箔材等于低压匝数，扁线只能为 2 或 4"},
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
        st.info("这里定义的是可复现的搜索域来源，不调用旧 Java 的“先判断能否合格再进入计算”的候选前置筛选。候选是否合格仍由完整 Python 单设备精算器判定。")

    with st.expander("完整原始配置 JSON（核对 Java 入参用）", expanded=False):
        st.json({section: config[section] for section in SECTION_NAMES})
    return ResearchBoundary(
        steels=tuple(selected_steels), core_types=tuple(int(value) for value in selected_core_types),
        low_wire_types=tuple(int(value) for value in selected_low_wire_types), high_wire_types=tuple(int(value) for value in selected_high_wire_types),
        tank_types=tuple(int(value) for value in selected_tank_types),
        corrugation_long_faces=long_faces, corrugation_short_faces=short_faces,
        radiator_width_codes=tuple(int(value) for value in selected_width_codes),
        radiator_groups=tuple(int(value) for value in selected_groups),
    )


@st.cache_resource(show_spinner=False)
def page_repository() -> FaladiRepository:
    """创建页面生命周期内复用的只读仓储和连接池。

    Streamlit 每次控件交互都会重新执行脚本；此资源缓存保证不会因此反复建立
    MySQL TCP 连接。连接异常时连接池会在下一次借用时自动重连。
    """
    settings = load_database_settings()
    return FaladiRepository(settings, pool=DatabaseConnectionPool(settings, max_size=2))


@st.cache_data(ttl=300, show_spinner="正在加载三相配置与历史方案…")
def cached_page_context() -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """页面会话内切换配置不再访问数据库；点击刷新才重新读取。"""
    return page_repository().all_page_context()


@st.cache_data(ttl=1800, show_spinner="正在加载基础目录…")
def cached_catalog() -> dict[str, list[dict[str, Any]]]:
    return page_repository().catalog()


REPLAY_VIEW_SCHEMA_VERSION = "20260917-v2"


@st.cache_data(show_spinner=False)
def cached_trace(path_text: str, modified_at: float, view_schema_version: str) -> dict[str, Any]:
    """按文件更新时间缓存本地轨迹；避免大轨迹在控件交互时反复解析。"""
    del modified_at, view_schema_version
    return read_trace(Path(path_text))


@st.cache_data(show_spinner=False)
def cached_trace_overview(path_text: str, modified_at: float, view_schema_version: str) -> dict[str, Any]:
    """仅读取回放首屏摘要；候选级 JSON 留到用户主动打开时再解析。"""
    del modified_at, view_schema_version
    return read_trace_overview(Path(path_text))


def new_trace_path(prefix: str, config_id: int, seed: int) -> Path:
    """每次页面实验生成独立文件，避免同随机种子多次运行混写到一个 SQLite。"""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return PROJECT_ROOT / "outputs" / f"{prefix}-{config_id}-seed{seed}-{timestamp}.sqlite3"


def render_hero(db_available: bool, trace_count: int) -> None:
    mode = "在线精算模式" if db_available else "离线轨迹回放模式"
    description = (
        "从本机 faladi 数据库读取三相配置与基础目录，运行单设备精算与 GA；所有业务数据仅查询。"
        if db_available else "业务数据库当前不可用；仍可复盘本地 SQLite 实验轨迹，但不能运行新的精算或 GA。"
    )
    st.markdown(
        f"<div class='hero-strip'><div class='hero-kicker'>THREE-PHASE TRANSFORMER · RESEARCH WORKBENCH</div>"
        f"<div class='hero-title'>{mode}</div><div class='hero-subtitle'>{description} 当前可发现 {trace_count} 份本地轨迹。</div></div>",
        unsafe_allow_html=True,
    )


def render_workspace_header(
    configs: dict[int, dict[str, Any]], db_available: bool, trace_count: int,
) -> int | None:
    """参考研究控制台布局渲染顶部应用栏；真实配置选择仍由 Streamlit 控件完成。"""
    brand, state, selector, action = st.columns((4.2, 1.55, 2.25, 1.1), vertical_alignment="center")
    with brand:
        st.markdown(
            "<div class='workspace-header'><div class='workspace-brand'><div class='workspace-mark'>TP</div>"
            "<div><div class='workspace-title'>三相变压器优化研究操作台</div>"
            "<div class='workspace-subtitle'>单设备精算 · 一致性验证 · 离散遗传优化 · 可追溯实验</div>"
            "</div></div></div>",
            unsafe_allow_html=True,
        )
    if not db_available:
        with state:
            st.markdown("<span class='status-pill offline'>数据库未连接</span>", unsafe_allow_html=True)
        with selector:
            st.caption("当前仅可回放本地轨迹")
        with action:
            if st.button("重新连接", width="stretch"):
                st.cache_data.clear()
                st.cache_resource.clear()
                st.rerun()
        return None

    config_ids = sorted(configs, reverse=True)
    default_id = 445 if 445 in config_ids else config_ids[0]
    with state:
        st.markdown("<span class='status-pill'>● 本机数据库已连接（只读）</span>", unsafe_allow_html=True)
        st.caption(f"本地轨迹 {trace_count} 份")
    with selector:
        config_id = st.selectbox(
            "当前三相配置",
            config_ids,
            index=config_ids.index(default_id),
            format_func=lambda item: f"配置 {item} · {decimal_text(configs[item]['performance_index'].get('capacity'), 0)} kVA",
            key="workspace-config-id",
            help="切换后将重新绑定当前实验的固定产品需求、工艺参数、历史方案和基础目录范围。",
        )
    with action:
        st.caption("数据与目录")
        if st.button("刷新读取", width="stretch"):
            st.cache_data.clear()
            st.rerun()
    return int(config_id)


def render_experiment_sidebar(
    config: dict[str, Any] | None, records: list[dict[str, Any]], catalog: dict[str, list[dict[str, Any]]],
    trace_count: int, db_error: str | None,
) -> str:
    """渲染一级模块导航及实验上下文，返回当前选中的工作区。"""
    with st.sidebar:
        st.markdown("<div class='sidebar-kicker'>实验工作区</div>", unsafe_allow_html=True)
        active_page = st.radio(
            "实验工作区",
            ["单设备精算", "一致性验证", "GA 优化实验", "轨迹回放", "研究说明"],
            key="primary_workspace_page",
            label_visibility="collapsed",
        )
        st.divider()
        st.markdown("<div class='sidebar-kicker'>当前实验对象</div>", unsafe_allow_html=True)
        if config is None:
            st.markdown("<div class='sidebar-card'><div class='sidebar-caption'>当前处于离线轨迹回放模式</div>"
                        "<div class='sidebar-value'>未加载三相配置</div></div>", unsafe_allow_html=True)
            if db_error:
                st.caption(f"连接诊断：{db_error}")
        else:
            performance = config["performance_index"]
            st.markdown(
                "<div class='sidebar-card'><div class='sidebar-caption'>三相配置 ID</div>"
                f"<div class='sidebar-value'>{config.get('id', '—')}</div><hr style='border:0;border-top:1px solid #E6EDF5;margin:.55rem 0'>"
                "<div style='display:grid;grid-template-columns:1fr 1fr;gap:.52rem'>"
                f"<div><div class='sidebar-caption'>额定容量</div><div class='sidebar-value'>{decimal_text(performance.get('capacity'), 0)} kVA</div></div>"
                f"<div><div class='sidebar-caption'>接线组别</div><div class='sidebar-value'>{performance.get('displayConnectionGroup') or performance.get('windingConnection') or '—'}</div></div>"
                f"<div><div class='sidebar-caption'>高压额定</div><div class='sidebar-value'>{decimal_text(performance.get('highVoltageRated'), 0)} V</div></div>"
                f"<div><div class='sidebar-caption'>低压额定</div><div class='sidebar-value'>{decimal_text(performance.get('lowVoltageRated'), 0)} V</div></div>"
                "</div></div>",
                unsafe_allow_html=True,
            )
            st.markdown("<div class='sidebar-kicker'>数据源加载状态</div>", unsafe_allow_html=True)
            st.markdown(
                "<div class='sidebar-card'>"
                f"<div class='sidebar-caption'>基础目录</div><div class='sidebar-value'>已加载 {len(catalog)} 类</div>"
                f"<div style='height:.45rem'></div><div class='sidebar-caption'>当前配置历史记录</div><div class='sidebar-value'>已读取 {len(records)} 条</div>"
                f"<div style='height:.45rem'></div><div class='sidebar-caption'>本地 SQLite 轨迹</div><div class='sidebar-value'>{trace_count} 份</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        st.markdown(
            "<div class='side-safe-note'><b>数据隔离</b><br>业务数据库仅查询；候选、算子、代际和诊断记录只写入本地 SQLite 实验轨迹，不回写业务方案。</div>",
            unsafe_allow_html=True,
        )
    return active_page


def render_workflow_cards() -> None:
    cards = (
        ("01", "定义离散设计域", "以原始线规、铁芯、油道记录和整数候选集构造合法基因。"),
        ("02", "单设备精确评价", "固定一组基因后，计算损耗、阻抗、温升、重量、成本和约束诊断。"),
        ("03", "双档案遗传搜索", "严格合格方案按成本排序；近可行方案保留超限向量，参与跨边界演化。"),
        ("04", "轨迹复盘与对比", "固化随机种子、参数、父代和每代统计，为多随机种子与消融实验提供证据。"),
    )
    columns = st.columns(4)
    for column, (number, title, copy) in zip(columns, cards):
        with column:
            st.markdown(
                f"<div class='work-card'><div class='work-card-number'>{number}</div>"
                f"<div class='work-card-title'>{title}</div><div class='work-card-copy'>{copy}</div></div>",
                unsafe_allow_html=True,
            )


def render_database_required(action: str, db_error: str | None) -> None:
    detail = "当前未连接 Python 本机配置指定的 faladi 数据库。" if not db_error else f"当前未连接 Python 本机数据库：{db_error}"
    st.markdown(
        f"<div class='status-note offline-note'><b>{action}暂不可执行。</b>{detail}"
        "请检查 config/database.local.yml 与本地 MySQL，再点击左侧“重新连接并刷新”；在此之前仍可使用“轨迹回放”查看既有实验。</div>",
        unsafe_allow_html=True,
    )


def render_trace_replay(trace_files: list[Any]) -> None:
    """按“第 0 代预览 / 完整 GA”分别解释轨迹，避免暴露内部审计编码。"""
    st.caption("轨迹来自本地 outputs/*.sqlite3；只读取、不重新计算，也不写入 Java 业务数据库。先选定一份轨迹，再从下方页签阅读对应信息。")
    if not trace_files:
        st.info("尚未发现本地实验轨迹。连接数据库并运行“初始种群诊断”或“完整双档案 GA”后，结果会自动出现在这里。")
        return

    files_by_label = {
        f"{item.path.name} · {datetime.fromtimestamp(item.modified_at).strftime('%Y-%m-%d %H:%M:%S')} · {item.bytes_size / 1024:.0f} KB": item
        for item in trace_files
    }
    label = st.selectbox("选择本地轨迹", list(files_by_label), key="trace-file")
    selected = files_by_label[label]
    trace_key = f"{selected.path.resolve()}::{selected.modified_at}::{REPLAY_VIEW_SCHEMA_VERSION}"
    loaded_key = st.session_state.get("trace_loaded_key")
    if loaded_key != trace_key:
        st.info(
            f"已选中 {selected.bytes_size / 1024 / 1024:.1f} MB 轨迹。"
            "为避免切换到本页时等待大文件解析，请确认后再加载；加载完成后本会话内不会重复解析。"
        )
        if not st.button("加载选定轨迹", type="primary", key="load-selected-trace"):
            return
        try:
            with st.spinner("正在读取轨迹摘要（运行信息、代际与来源汇总）…"):
                trace = cached_trace_overview(str(selected.path), selected.modified_at, REPLAY_VIEW_SCHEMA_VERSION)
            st.session_state["trace_loaded_key"] = trace_key
            st.session_state["trace_loaded_overview"] = trace
            st.session_state.pop("trace_loaded_full_key", None)
            st.session_state.pop("trace_loaded_full_value", None)
            st.rerun()
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(f"无法读取该轨迹：{exc}")
            return
    else:
        trace = st.session_state.get("trace_loaded_overview")
        if not isinstance(trace, dict):
            try:
                with st.spinner("正在恢复已加载的轨迹摘要…"):
                    trace = cached_trace_overview(str(selected.path), selected.modified_at, REPLAY_VIEW_SCHEMA_VERSION)
                st.session_state["trace_loaded_overview"] = trace
            except (OSError, ValueError, RuntimeError) as exc:
                st.error(f"无法读取该轨迹：{exc}")
                return

    run = trace["run"]
    summary = trace["summary"]
    is_initial_preview = bool(trace.get("is_initial_preview"))
    if is_initial_preview:
        st.markdown(
            "<div class='status-note'><b>当前文件是“第 0 代覆盖与精算预览”，不是一次完整 GA。</b>"
            "它记录候选怎样构成以及每条候选的精算结果；没有执行父代选择、交叉、变异、代际迭代或双档案更新。"
            "因此页面不会把“未执行”的内容显示成未知状态。</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='status-note'><b>当前文件是完整双档案遗传优化。</b>"
            "它记录每代候选、父代、精算结果和双档案快照；回放只展示当次保存事实，不重新排序或改写原始记录。</div>",
            unsafe_allow_html=True,
        )
    metrics = st.columns(5)
    metrics[0].metric("轨迹类型", run.get("algorithm_label", "—"))
    metrics[1].metric("配置 ID", run.get("config_id", "—"))
    metrics[2].metric("随机种子", run.get("random_seed", "—"))
    metrics[3].metric("候选记录数", summary["individual_count"])
    metrics[4].metric("严格合格候选数", summary["feasible_count"] if summary.get("feasible_count") is not None else "待加载候选")

    def render_source_composition() -> None:
        st.markdown("#### 候选是怎样生成的")
        st.caption("“候选生成方式”说明一条方案进入本次实验的途径，不是工程计算指标，也不表示该方案已经合格。")
        rows = [
            {"生成方式": name, "候选数": count, "作用": summary.get("source_explanations", {}).get(name, "—")}
            for name, count in summary["source_counts"].items()
        ]
        st.dataframe(rows, hide_index=True, width="stretch")

    def render_best_result() -> None:
        best = summary.get("strict_best") or summary.get("near_best")
        st.markdown("#### 当前最优结果")
        if best is None and trace.get("overview_only"):
            st.info("候选级最优方案需要解析完整候选记录；请打开“候选与精算结果”加载。")
            return
        if best is None:
            st.warning("该轨迹没有完成精算的候选。")
            return
        is_strict = bool(best["result"].get("feasible") and best["result"].get("complete"))
        if is_strict:
            st.success("已找到严格合格候选；以下是在严格合格候选中成本最低的一条。")
        else:
            st.warning("尚未找到严格合格候选；以下是在可计算候选中总超限最小的一条。")
        best_metrics = best["result"].get("metrics", {})
        row = st.columns(3)
        row[0].metric("成本（元）", decimal_text(best_metrics.get("price"), 2))
        row[1].metric("总超限", f"{best['total_violation']:.6f}")
        row[2].metric("生成方式", best["source_label"])
        st.caption(f"结构：{best['candidate_structure']}")

    def load_full_trace_if_needed() -> dict[str, Any] | None:
        """仅在候选级视图才解析 individuals 中的大 JSON。"""
        if st.session_state.get("trace_loaded_full_key") == trace_key:
            cached_value = st.session_state.get("trace_loaded_full_value")
            if isinstance(cached_value, dict):
                return cached_value
        try:
            with st.spinner("正在解析候选结构、父代和完整精算结果。大文件首次打开候选视图需要一些时间…"):
                full_trace = cached_trace(str(selected.path), selected.modified_at, REPLAY_VIEW_SCHEMA_VERSION)
            st.session_state["trace_loaded_full_key"] = trace_key
            st.session_state["trace_loaded_full_value"] = full_trace
            return full_trace
        except (OSError, ValueError, RuntimeError) as exc:
            st.error(f"无法读取候选级轨迹数据：{exc}")
            return None

    if is_initial_preview:
        trace_view = st.radio(
            "第 0 代回放视图", ["第 0 代概览", "候选与精算结果", "本次预览设置"],
            horizontal=True, label_visibility="collapsed", key=f"trace-view-{trace_key}",
        )
        if trace_view == "第 0 代概览":
            left, right = st.columns((1.25, 1))
            with left:
                render_source_composition()
            with right:
                render_best_result()
            st.info("第 0 代预览没有父代、交叉、变异、代际曲线或双档案；这些内容只会在“完整双档案遗传优化”轨迹中出现。")
            st.caption(f"原始 SQLite 文件位于本机：{selected.path}。为避免把大文件复制进浏览器内存，页面不自动准备下载副本。")
        elif trace_view == "候选与精算结果":
            full_trace = load_full_trace_if_needed()
            if full_trace is not None:
                rows = full_trace["table_rows"]
                limit = min(300, len(rows))
                st.caption(f"下表按“严格合格优先 → 成本 / 总超限”排序。共 {len(rows)} 条候选，首屏只显示前 {limit} 条，避免大轨迹把全部数据传入浏览器。")
                st.dataframe(rows[:limit], hide_index=True, width="stretch", height=420)
        else:
            st.markdown("#### 搜索域摘要")
            st.dataframe(trace_domain_rows(run.get("domain", {})), hide_index=True, width="stretch")
            st.markdown("#### 第 0 代构成参数")
            st.dataframe(trace_setting_rows(run.get("settings", {})), hide_index=True, width="stretch")
            with st.expander("技术原始数据（仅排错使用）", expanded=False):
                st.json({"搜索域原始记录": run.get("domain", {}), "第 0 代参数原始记录": run.get("settings", {})})
    else:
        trace_view = st.radio(
            "完整 GA 回放视图", ["运行概览", "代际过程", "候选与演化关系", "实验设置"],
            horizontal=True, label_visibility="collapsed", key=f"trace-view-{trace_key}",
        )
        if trace_view == "运行概览":
            left, right = st.columns((1.25, 1))
            with left:
                render_source_composition()
            with right:
                render_best_result()
            st.markdown("#### 最终双档案")
            if summary.get("final_archive_available"):
                archive_counts = summary.get("final_archive_counts", {})
                archive = st.columns(2)
                archive[0].metric("最终严格档案候选", archive_counts.get("strict", len(summary.get("final_strict_archive", []))))
                archive[1].metric("最终近可行档案候选", archive_counts.get("near", len(summary.get("final_near_archive", []))))
            else:
                st.info("该完整 GA 轨迹未保存最终档案成员快照；可以查看每代档案数量，但不把历史处理事件误当作最终成员。新运行会保存最终成员快照。")
            st.caption(f"原始 SQLite 文件位于本机：{selected.path}。为避免把大文件复制进浏览器内存，页面不自动准备下载副本。")
        elif trace_view == "代际过程":
            generations = trace["generations"]
            archive_by_generation = {int(row["generation"]): row for row in trace.get("archive_snapshots", [])}
            if not generations or not archive_by_generation:
                st.info("该完整 GA 轨迹没有可用的代际双档案快照，无法可靠展示代际过程。请运行一次新版完整 GA。")
            else:
                display_generations = [{
                    "代": row["generation"], "累计实际精算调用": row["exact_evaluations"], "累计缓存命中": row["cache_hits"],
                    "本代严格合格数": row["feasible_count"],
                    "严格档案数": archive_by_generation.get(int(row["generation"]), {}).get("strict_count", "—"),
                    "近可行档案数": archive_by_generation.get(int(row["generation"]), {}).get("near_count", "—"),
                    "当前最佳严格成本": archive_by_generation.get(int(row["generation"]), {}).get("best_strict_cost", "—"),
                    "当前最小近可行超限": archive_by_generation.get(int(row["generation"]), {}).get("best_near_violation", "—"),
                    "本代说明": row.get("note") or "—",
                } for row in generations]
                st.line_chart(display_generations, x="代", y=["严格档案数", "近可行档案数", "本代严格合格数"], width="stretch")
                previous = None
                summaries = []
                for row in display_generations:
                    if previous is None:
                        change = "第 0 代完成全量覆盖并建立初始档案"
                    else:
                        strict_delta = int(row["严格档案数"]) - int(previous["严格档案数"])
                        near_delta = int(row["近可行档案数"]) - int(previous["近可行档案数"])
                        change = f"严格档案 {strict_delta:+d}；近可行档案 {near_delta:+d}"
                    summaries.append({"代": row["代"], "本代发生的事": change, "本代说明": row["本代说明"]})
                    previous = row
                st.caption("下表先用中文说明每一代的档案变化；数值明细仍保留在后面的代际统计表。")
                st.dataframe(summaries, hide_index=True, width="stretch", height=220)
                st.dataframe(display_generations, hide_index=True, width="stretch")
        elif trace_view == "候选与演化关系":
            full_trace = load_full_trace_if_needed()
            if full_trace is not None:
                all_rows = full_trace["table_rows"]
                ranked_individuals = full_trace.get("ranked_individuals", [])
                page_size = 200
                page_count = max(1, (len(all_rows) + page_size - 1) // page_size)
                page = st.number_input("候选结果页码", min_value=1, max_value=page_count, value=1, step=1, key=f"trace-candidate-page-{trace_key}")
                start = (int(page) - 1) * page_size
                end = min(start + page_size, len(all_rows))
                st.caption(f"按“严格合格优先 → 成本 / 总超限”排序。当前显示第 {int(page)} / {page_count} 页（第 {start + 1}–{end} 条，共 {len(all_rows)} 条）。父代仅对交叉或复制产生的候选有意义。")
                st.dataframe(all_rows[start:end], hide_index=True, width="stretch", height=420)
                page_individuals = ranked_individuals[start:end]
                rows = page_individuals
            else:
                rows = []
            if rows:
                ids = [item["individual_id"] for item in rows]
                selected_id = st.selectbox("查看单个候选的详细来源、父代和精算结果", ids, key="trace-individual")
                individual = next(item for item in rows if item["individual_id"] == selected_id)
                left, right = st.columns(2)
                with left:
                    st.markdown("#### 候选结构")
                    st.caption(individual.get("candidate_structure", "候选结构摘要未保存"))
                    st.dataframe(trace_candidate_structure_rows(individual["candidate"]), hide_index=True, width="stretch")
                with right:
                    st.markdown("#### 产生方式与约束诊断")
                    st.write(f"生成方式：{individual.get('source_label', '未记录')}")
                    st.write(f"父代 A：{individual['parent_a_id'] or '无（第 0 代或非交叉产生）'}")
                    st.write(f"父代 B：{individual['parent_b_id'] or '无（第 0 代或非交叉产生）'}")
                    st.write(f"本代去向：{individual.get('population_decision', '该轨迹未保存种群去向')}")
                    st.write(f"档案归属：{individual.get('archive_membership_label', '未进入档案')}")
                    st.write(f"档案处理结果：{individual.get('archive_reason_label', '该轨迹未保存档案去向')}")
                    st.write(f"入档前最后动作：{individual.get('archive_entry_last_action', '未记录')}")
                    st.caption("该字段只说明最后一次创建记录；不代表某个算子对最终入档结果的因果贡献。")
                    violation_rows = trace_constraint_diagnostic_rows(individual["result"].get("violations", []))
                    if violation_rows:
                        st.dataframe(violation_rows, hide_index=True, width="stretch", height=260)
                    else:
                        st.success("该候选没有记录到约束超限项。")
                with st.expander("查看完整精算结果", expanded=False):
                    st.json(individual["result"])
                with st.expander("查看技术原始记录（仅排错使用）", expanded=False):
                    st.json({"候选基因": individual["candidate"], "算子记录": individual["operator"]})
        else:
            left, right = st.columns(2)
            with left:
                st.markdown("#### 搜索域摘要")
                st.dataframe(trace_domain_rows(run.get("domain", {})), hide_index=True, width="stretch")
            with right:
                st.markdown("#### 算法设置")
                st.dataframe(trace_setting_rows(run.get("settings", {})), hide_index=True, width="stretch")
            with st.expander("技术原始数据（仅排错使用）", expanded=False):
                st.json({"搜索域原始记录": run.get("domain", {}), "算法设置原始记录": run.get("settings", {})})
    st.caption(f"轨迹创建时间：{run.get('created_at', '—')}；文件：{trace['file_path']}")

def candidate_from_widgets(
    config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], seed: ThreePhaseDesignCandidate | None,
    seed_key: str, boundary: ResearchBoundary,
) -> ThreePhaseDesignCandidate | None:
    """在表单中编辑离散结构基因；不编辑 Java 业务配置。"""
    core_min = int(config["craft_core"]["coreDiameterMin"])
    core_max = int(config["craft_core"]["coreDiameterMax"])
    cores = [row for row in catalog["core"] if core_min <= int(row["core_diameter"]) <= core_max
             and int(row["core_type"]) in boundary.core_types]
    if not cores:
        st.error("当前配置的铁芯直径范围内没有基础铁芯记录。")
        return None
    core_by_id = {int(row["id"]): row for row in cores}

    low_craft = config["craft_low_coil"]
    high_craft = config["craft_high_coil"]
    lv_options = [
        (int(wire_type), int(row["id"]))
        for wire_type in boundary.low_wire_types
        for row in wire_rows_in_search_boundary("lv", int(wire_type), catalog, low_craft)[1]
    ]
    hv_options = [
        (int(wire_type), int(row["id"]))
        for wire_type in boundary.high_wire_types
        for row in wire_rows_in_search_boundary("hv", int(wire_type), catalog, high_craft)[1]
    ]
    lv_by_option = {
        (int(wire_type), int(row["id"])): row
        for wire_type in boundary.low_wire_types
        for row in wire_rows_in_search_boundary("lv", int(wire_type), catalog, low_craft)[1]
    }
    hv_by_option = {
        (int(wire_type), int(row["id"])): row
        for wire_type in boundary.high_wire_types
        for row in wire_rows_in_search_boundary("hv", int(wire_type), catalog, high_craft)[1]
    }
    steel_names = sorted(set(boundary.steels) & {str(row["brand"]) for row in catalog["steel"]})
    if not lv_options or not hv_options or not steel_names:
        st.error("线规或硅钢目录为空，无法构造候选。")
        return None

    # 历史记录在兼容阶段可能尚未写入类别；它只能回退到该历史配置本身的
    # wireSpecification，不能因页面多选而猜成某个别的类别。
    default_lv_option = (
        (int(low_craft["wireSpecification"] if seed.low_voltage_wire_type is None else seed.low_voltage_wire_type),
         seed.low_voltage_wire_id)
        if seed is not None else None
    )
    default_hv_option = (
        (int(high_craft["wireSpecification"] if seed.high_voltage_wire_type is None else seed.high_voltage_wire_type),
         seed.high_voltage_wire_id)
        if seed is not None else None
    )
    if seed is not None and default_lv_option not in lv_options:
        st.warning("该历史方案的低压导线类别或完整线规不在当前研究范围内；不会替换为目录第一条。")
        return None
    if seed is not None and default_hv_option not in hv_options:
        st.warning("该历史方案的高压导线类别或完整线规不在当前研究范围内；不会替换为目录第一条。")
        return None
    if seed is None:
        st.error("请先选择一条可还原的 Java 历史方案，再进行 Python 复算。")
        return None
    if seed.steel_brand not in steel_names or seed.core_data_id not in core_by_id:
        st.warning("该历史方案不在当前研究候选范围内。请调整上方材料/铁芯范围后再复算，避免悄悄替换成另一个目录值。")
        return None
    try:
        cooling_options = GeneDomain.from_catalog(config, catalog, **boundary.domain_kwargs()).cooling_options
    except (KeyError, LookupError, ValueError) as exc:
        st.error(f"当前冷却搜索域无法构造：{exc}")
        return None
    if seed.cooling_option not in cooling_options:
        st.warning("该历史方案没有可还原的完整冷却快照，或其冷却型号不在当前允许范围内；不能把它伪装成同一 Java 方案复算。")
        return None

    with st.form("single-device-form", border=False):
        st.markdown(
            "<div class='step-heading'><span class='step-code'>B</span><div><b>可编辑离散结构基因</b>"
            "<div class='step-copy'>候选的唯一可变部分。手动精算、GA 交叉和变异均只改变这些离散记录或整数档位。</div></div></div>",
            unsafe_allow_html=True,
        )
        st.caption("导线基因是“导线类别 + 完整目录记录”而非宽、厚两个数字。它们可被手动试算，也将被 GA 的交叉、变异和局部精算改变；上方的产品需求、工艺系数和价格参数保持固定。")
        column_a, column_b, column_c, column_d = st.columns(4)
        with column_a:
            st.markdown("##### 铁芯模块")
            steel = st.selectbox("硅钢片牌号", steel_names, index=index_of(steel_names, seed.steel_brand), key=f"steel-{seed_key}", help="按原始硅钢牌号选择；价格与损耗曲线均由目录和固定配置读取。")
            core_types = sorted({int(row["core_type"]) for row in cores})
            default_core_type = seed.core_type if seed.core_type in core_types else core_types[0]
            selected_core_type = st.selectbox(
                "三相铁芯形状", core_types, index=index_of(core_types, default_core_type),
                format_func=lambda value: CORE_TYPE_NAME.get(int(value), f"未知类型 {value}"), key=f"core-type-{seed_key}",
                help="三相铁芯结构基因：圆形、长圆形或椭圆形。选择后只显示该形状的真实铁芯目录规格。",
            )
            core_ids = [int(row["id"]) for row in cores if int(row["core_type"]) == int(selected_core_type)]
            default_core_id = seed.core_data_id if seed.core_type == selected_core_type and seed.core_data_id in core_ids else core_ids[0]
            core_id = st.selectbox(
                "铁芯直径与目录规格", core_ids, index=index_of(core_ids, default_core_id),
                format_func=lambda value: f"#{value} · D={core_by_id[value]['core_diameter']} mm",
                key=f"core-{seed_key}", help="仅可选择当前铁芯形状下的原始目录记录，不能填写目录外的几何尺寸。",
            )
            low_turns = st.number_input("低压匝数", min_value=1, max_value=3000, value=int(seed.low_voltage_turns), step=1, key=f"lv-turns-{seed_key}", help="整数基因。手动精算可输入任意正整数；GA 搜索时只使用 Java 页面 optimizationScope.lowVoltageTurnsMin/Max 允许的整数范围。")
        with column_b:
            st.markdown("##### 低压绕组模块")
            low_wire = st.selectbox(
                "低压导线类别与线规（完整记录）", lv_options, index=index_of(lv_options, default_lv_option),
                format_func=lambda value: f"{wire_type_name('lv', value[0])} · {wire_record_label(lv_by_option[value], value[0])}",
                key=f"lv-wire-{seed_key}", help="选择完整的‘类别 + 线规记录 ID’；宽、厚和截面积不被拆开重组。",
            )
            low_layers = st.number_input("低压层数", min_value=1, max_value=200, value=int(seed.low_voltage_layers), step=1, key=f"lv-layers-{seed_key}", help="整数结构基因；与线规、油道共同影响绕组几何、损耗和温升。")
            low_duct = st.selectbox("低压油道方案", DUCT_OPTIONS, index=index_of(list(DUCT_OPTIONS), seed.low_voltage_duct), format_func=duct_label, key=f"lv-duct-{seed_key}", help="0 为无油道；1–4 可分别选择半油道或全油道。油道影响几何、热工和成本。")
        with column_c:
            st.markdown("##### 高压绕组模块")
            high_wire = st.selectbox(
                "高压导线类别与线规（完整记录）", hv_options, index=index_of(hv_options, default_hv_option),
                format_func=lambda value: f"{wire_type_name('hv', value[0])} · {wire_record_label(hv_by_option[value], value[0])}",
                key=f"hv-wire-{seed_key}", help="选择完整的‘类别 + 线规记录 ID’；圆线或扁线目录由所选类别决定，绝不跨类别拼接。",
            )
            high_layers = st.number_input("高压层数", min_value=1, max_value=300, value=int(seed.high_voltage_layers), step=1, key=f"hv-layers-{seed_key}", help="整数结构基因；GA 候选范围读取 Java 页面高压绕组的最小/最大层数，步长固定为 1。")
            high_duct = st.selectbox("高压油道方案", DUCT_OPTIONS, index=index_of(list(DUCT_OPTIONS), seed.high_voltage_duct), format_func=duct_label, key=f"hv-duct-{seed_key}", help="0 为无油道；1–4 可分别选择半油道或全油道。")
        with column_d:
            st.markdown("##### 冷却模块")
            cooling = st.selectbox(
                "油箱与完整冷却型号", cooling_options,
                index=index_of(list(cooling_options), seed.cooling_option),
                format_func=cooling_option_text, key=f"cooling-{seed_key}",
                help="波纹方案携带波纹记录和长/短轴面数；散热器方案携带片宽、中心距、片数、组数及完整 G/Q/SZ 记录。",
            )

        selected_core = core_by_id[int(core_id)]
        conversion: Decimal | None = None
        if int(selected_core["core_type"]) == 1:
            default_conversion = seed.conversion_c if seed.core_type == 1 and seed.conversion_c is not None else selected_core["stage1_thickness"]
            conversion = Decimal(str(st.number_input("长圆铁芯直线段 C 值（mm）", min_value=0.0, value=float(default_conversion), step=1.0, key=f"conversion-{seed_key}", help="仅在所选铁芯为长圆形时生效；初始值来自该铁芯目录记录。")))
        submitted = st.form_submit_button("运行单设备精算并给出约束诊断", type="primary", width="stretch")

    if not submitted:
        return None
    return ThreePhaseDesignCandidate(
        steel_brand=steel, core_type=int(selected_core["core_type"]), core_data_id=int(core_id),
        low_voltage_turns=int(low_turns), low_voltage_wire_id=int(low_wire[1]), low_voltage_layers=int(low_layers),
        low_voltage_duct=low_duct, high_voltage_wire_id=int(high_wire[1]), high_voltage_layers=int(high_layers),
        high_voltage_duct=high_duct, conversion_c=conversion,
        low_voltage_wire_type=int(low_wire[0]), high_voltage_wire_type=int(high_wire[0]),
        cooling_option=cooling,
    )


def render_result(result: Any) -> None:
    st.markdown(
        "<div class='step-heading'><span class='step-code'>C</span><div><b>单设备精算结果与约束诊断</b>"
        "<div class='step-copy'>由当前 Python 公式链对这一组固定需求与结构基因重新计算得到。</div></div></div>",
        unsafe_allow_html=True,
    )
    if not result.calculable:
        st.error("该候选未能完成精算。请查看下方诊断，确认是否为几何、目录或公式输入问题。")
        for item in result.diagnostic:
            st.write(f"- {item}")
        return
    state = "严格合格" if result.feasible else f"可计算，但有 {len(result.violations)} 项约束未满足"
    tone = "#166534" if result.feasible else "#A16207"
    st.markdown(f"<div class='status-note' style='border-left-color:{tone}'>精算完成：<b>{state}</b>。结果仅代表当前 Python 公式链，需结合一致性验证解释实验结论。</div>", unsafe_allow_html=True)
    metrics = result.metrics
    row_a = st.columns(4)
    row_a[0].metric("空载损耗 P0 (W)", decimal_text(metrics.get("crgopo"), 0))
    row_a[1].metric("负载损耗 PK (W)", decimal_text(metrics.get("hvlvpk"), 0))
    row_a[2].metric("短路阻抗 UK (%)", decimal_text(metrics.get("ukk"), 3))
    row_a[3].metric("总成本 (元)", decimal_text(metrics.get("price"), 2))
    row_b = st.columns(4)
    row_b[0].metric("油顶温升 (K)", decimal_text(metrics.get("otr"), 2))
    row_b[1].metric("低压温升 (K)", decimal_text(metrics.get("lvtr"), 2))
    row_b[2].metric("高压温升 (K)", decimal_text(metrics.get("hvtr"), 2))
    row_b[3].metric("总装配重量 (kg)", decimal_text(metrics.get("total_assembly_weight"), 1))
    if metrics.get("cooling_option_key") is not None:
        source = "历史兼容回退" if metrics.get("cooling_selection_source") == "legacy_fallback" else "候选中明确选择"
        st.caption(f"本次实际冷却型号：{metrics.get('cooling_option_key')}；来源：{source}。")

    if result.violations:
        st.subheader("约束诊断")
        st.warning(
            f"发现 {len(result.violations)} 项约束不满足。下方逐项给出允许下限、允许上限、"
            "当前实际值与超出量；“无上限”或“无下限”表示该规则本身是单侧约束，不是遗漏了参数。"
        )
        render_constraint_diagnostics(result.violations)
    else:
        st.success("没有检测到已建模约束超限。")

    with st.expander("查看完整精算指标", expanded=False):
        st.dataframe([
            {"指标": key, "数值": decimal_text(value, 6)}
            for key, value in sorted(metrics.items())
        ], width="stretch", hide_index=True, height=360)


def render_consistency(record: dict[str, Any], config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]]) -> None:
    try:
        candidate = resolve_record_candidate(record, catalog, config)
        result = ThreePhaseSingleDeviceEvaluator(config, catalog).evaluate(candidate)
    except (KeyError, LookupError, ValueError) as exc:
        st.error(f"无法还原该历史方案：{exc}")
        return
    if not result.calculable:
        st.error("Python 未完成该记录的精算，不能进行一致性结论。")
        st.write(result.diagnostic)
        return
    rows = []
    for key, label in CHECKED_FIELDS.items():
        java_value = record.get("price") if key == "price" else record["scheme_data"].get(key)
        python_value = result.metrics.get(key)
        if java_value is None or python_value is None:
            verdict = "Java 未保存"
        else:
            verdict = "一致" if Decimal(str(java_value)) == Decimal(str(python_value)) else "不一致"
        rows.append({"字段": label, "Java": decimal_text(java_value, 5), "Python": decimal_text(python_value, 5), "结论": verdict})
    snapshot_saved = record["scheme_data"].get("calculationSnapshotVersion") == 1
    if snapshot_saved:
        for java_key, python_key in SNAPSHOT_FIELD_MAP.items():
            java_value = record["scheme_data"].get(java_key)
            python_value = result.metrics.get(python_key)
            if java_value is None or python_value is None:
                verdict = "字段缺失"
            else:
                verdict = "一致" if Decimal(str(java_value)) == Decimal(str(python_value)) else "不一致"
            rows.append({
                "字段": SNAPSHOT_FIELD_LABELS.get(java_key, java_key),
                "Java": decimal_text(java_value, 5), "Python": decimal_text(python_value, 5), "结论": verdict,
            })
    consistent = all(row["结论"] in ("一致", "Java 未保存") for row in rows)
    comparable_count = sum(row["结论"] != "Java 未保存" for row in rows)
    if consistent:
        st.success(f"记录 {record['id']}：{comparable_count} 项可比较字段一致。")
    else:
        mismatch_count = sum(row["结论"] in ("不一致", "字段缺失") for row in rows)
        st.warning(f"记录 {record['id']}：{comparable_count} 项中有 {mismatch_count} 项差异，请结合后端公式版本与基础目录版本排查。")
    st.dataframe(rows, width="stretch", hide_index=True)
    if not snapshot_saved:
        st.caption("该记录未保存 calculationSnapshot；温升、油重、油箱重量和成本分项均标记为未验证，不计入上述对比。")
    st.caption(f"候选编码：{candidate.canonical_dict()}")


def history_candidates(config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]], domain: GeneDomain) -> tuple[list[ThreePhaseDesignCandidate], int]:
    """将当前配置下能还原的 Java 历史方案转为合法的 Python 种子。"""
    candidates: list[ThreePhaseDesignCandidate] = []
    skipped = 0
    allowed_core_ids = {int(row["id"]) for row in domain.cores}
    for record in records:
        try:
            candidate = resolve_record_candidate(record, catalog, config)
            if candidate.steel_brand in domain.steel_brands and candidate.core_data_id in allowed_core_ids:
                # 历史记录没有“多类别搜索域”这个概念；必须在此补齐/校验完整
                # 类别 + ID，不能让不在本次类别范围内的记录悄悄进入初始种群。
                candidates.append(domain.normalize_candidate_wire_types(candidate))
            else:
                skipped += 1
        except (KeyError, LookupError, ValueError):
            skipped += 1
    return candidates, skipped


def build_domain_and_history(
    config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
    boundary: ResearchBoundary,
) -> tuple[GeneDomain, list[ThreePhaseDesignCandidate | tuple[ThreePhaseDesignCandidate, str]], int]:
    domain = GeneDomain.from_catalog(config, catalog, **boundary.domain_kwargs())
    candidates, skipped = history_candidates(config, catalog, records, domain)
    # 当前配置的历史仍可作为种子；同时读取缓存的其它配置，加入能够映射到当前
    # 页面允许域的相似结构。旧价格只用于种子筛选，进入 GA 后仍按当前配置精算。
    configs_by_id, records_by_config = cached_page_context()
    similar = select_similar_history_seeds(config, configs_by_id, records_by_config, catalog, domain)
    sourced_candidates: list[ThreePhaseDesignCandidate | tuple[ThreePhaseDesignCandidate, str]] = [
        (candidate, "history_seed:same_config") for candidate in candidates
    ]
    sourced_candidates.extend(
        (
            seed.candidate,
            f"history_seed:similar_config={seed.source_config_id};record={seed.source_record_id};score={seed.config_similarity}",
            {
                "selection_priority": str(seed.config_similarity),
                "similarity_score": str(seed.config_similarity),
                "source_config_id": seed.source_config_id,
                "source_record_id": seed.source_record_id,
                "source_compliance_level": seed.source_compliance_level,
                "source_price": None if seed.source_price is None else str(seed.source_price),
                "similarity_feature_scores": {name: str(score) for name, score in seed.feature_scores},
            },
        )
        for seed in similar
    )
    return domain, sourced_candidates, skipped


def initial_preview_input_fingerprint(
    config_id: int, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]],
    boundary: ResearchBoundary,
) -> str:
    """为“预览结果可否复用”计算严格输入指纹。

    不只使用配置 ID：配置内容、完整目录和页面选择范围中任一变化，都会使旧的
    第 0 代结果失效，避免把旧价格或旧目录下的精算结果用于当前 GA。
    """
    payload = {
        "config_id": int(config_id), "config": config, "catalog": catalog,
        "research_boundary": boundary.domain_kwargs(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reusable_initial_preview_results(
    config_id: int, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]],
    boundary: ResearchBoundary,
    population: Any, formula_revision: str,
) -> dict[str, Any] | None:
    """只在同一会话、同输入、同一批完整候选下复用第 0 代精算缓存。"""
    cached = st.session_state.get("initial_preview_reuse")
    if not isinstance(cached, dict):
        return None
    if cached.get("config_id") != int(config_id) or cached.get("formula_revision") != formula_revision:
        return None
    fingerprint = initial_preview_input_fingerprint(
        config_id, config, catalog, boundary,
    )
    if cached.get("input_fingerprint") != fingerprint:
        return None
    results = cached.get("results_by_candidate")
    if not isinstance(results, dict):
        return None
    keys = tuple(EvaluationCache._key(candidate) for candidate in population.candidates)
    if set(keys) != set(results):
        return None
    # 复制字典而不复制 EvaluationResult：结果对象在 GA 内只读，避免修改预览缓存。
    return dict(results)


def render_coverage_plan(
    domain: GeneDomain, *, actual_size: int | None = None,
    actual_coverage_count: int | None = None,
) -> None:
    """展示初始化 A/B 覆盖计划；实际去重规模只在初始化后报告。"""
    plan = domain.coverage_plan_summary(include_key_pairs=True)
    planned_capacity = int(plan["coverage_candidate_capacity_before_dedup"])
    if actual_size is None:
        expansion = (
            f"当前 A/B 计划的候选容量（未去重）为 {planned_capacity}。不同覆盖项可能生成同一完整基因，"
            "因此页面预览不把它称为最小唯一候选数；实际去重覆盖数和有效种群规模将在初始化完成后记录。"
        )
        fourth_metric = "种群规模"
        fourth_value = "初始化后自动确定"
    else:
        expansion = (
            f"初始化后 A/B 去重覆盖候选为 {actual_coverage_count if actual_coverage_count is not None else '—'}，"
            f"实际有效种群为 {actual_size}；规模由 A/B 去重覆盖数和搜索域复杂度自动确定。"
        )
        fourth_metric = "实际有效种群"
        fourth_value = actual_size
    strategy_label = "均衡循环配对" if plan.get("pair_strategy") == "balanced" else "全组合枚举"
    st.markdown(f"##### 覆盖计划（A 单项 + B 关键两两：{strategy_label}）")
    coverage_cols = st.columns(4)
    coverage_cols[0].metric("A 单项候选（去重前）", plan["single_candidate_count_before_dedup"])
    coverage_cols[1].metric("B 关键两两候选（去重前）", plan["pair_candidate_count_before_dedup"])
    coverage_cols[2].metric("A/B 计划容量（未去重）", planned_capacity)
    coverage_cols[3].metric(fourth_metric, fourth_value)
    st.info(expansion)
    with st.expander("查看匝数、层数来源及长圆铁芯 C 值规则", expanded=False):
        integer_domain = plan.get("integer_domain", {})
        lv_turns = integer_domain.get("lv_turns", {})
        hv_layers = integer_domain.get("hv_layers", {})
        lv_turns_range = f"{lv_turns.get('minimum', '—')}～{lv_turns.get('maximum', '—')} 匝"
        # 低压层数不是一个连续整数区间。页面之前将所有合法值取并集后显示为
        # “2–70”，虽然候选生成时会再次合法化，但这个显示会误导用户以为 3、5
        # 或 25 层可随意用于任意低压导线，因此必须按 Java 的类别规则说明。
        st.dataframe([
            {
                "参数项": "低压匝数", "允许取值": lv_turns_range,
                "步长": "1 匝", "来源": "当前 Java 页面填写的低压匝数下限和上限",
            },
            {
                "参数项": "低压层数", "允许取值": "不设统一连续范围",
                "步长": "按导线类别自动确定", "来源": "见下方“低压层数生成规则”",
            },
            {
                "参数项": "高压层（段）数",
                "允许取值": f"{hv_layers.get('minimum', '—')}～{hv_layers.get('maximum', '—')} 层（段）",
                "步长": "1 层（段）", "来源": "当前 Java 页面填写的高压最小层（段）数和最大层（段）数",
            },
        ], hide_index=True, width="stretch")
        st.markdown("**低压层数生成规则**")
        st.markdown(
            f"- **低压箔材**：层数等于低压匝数，即当前为 {lv_turns_range}。\n"
            "- **低压扁线**：层数只能为 2 层或 4 层。\n"
            "- 生成候选时由所选导线类别自动确定，不允许把任意层数与任意导线记录自由组合。"
        )
        conversion = plan.get("conversion_c", {})
        st.caption(
            f"长圆铁芯直线段 C 值：{conversion.get('rule', '—')}；"
            f"步长 {conversion.get('step', '—')} mm；"
            f"当前兼容离散值数 {conversion.get('compatible_value_count', '—')}。"
        )
    with st.expander("查看 A/B 覆盖维度与两两组合", expanded=False):
        st.dataframe([
            {"层级": "A 单项", "维度": name, "允许值数量": count}
            for name, count in plan["single_dimension_counts"].items()
        ] + [
            {
                "层级": "B 关键两两", "维度": name,
                "配对数 / 全组合数": f"{detail['covered_pair_count']} / {detail['total_pair_count']}",
                "组合覆盖率": f"{float(detail['coverage_rate']):.2%}",
                "策略": detail["strategy"],
            }
            for name, detail in plan["pair_coverage"].items()
        ], hide_index=True, width="stretch")


def coverage_domain(
    config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]],
    boundary: ResearchBoundary,
) -> GeneDomain:
    """仅构造搜索域，用于运行前展示覆盖计划；不会精算或写入轨迹。"""
    return GeneDomain.from_catalog(config, catalog, **boundary.domain_kwargs())


def render_ga_prepare(
    config_id: int, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
    boundary: ResearchBoundary,
) -> None:
    st.markdown("#### 本次运行概况")
    st.caption("第 0 代负责覆盖并确认候选域；遗传迭代负责在这些候选附近继续搜索。所有结果均由同一 Python 精算器重新计算。")
    summary = st.columns(4)
    summary[0].metric("当前配置历史记录", len(records))
    summary[1].metric("搜索模块", "4 个")
    summary[2].metric("冷却候选", "、".join(TANK_TYPE_NAME[item] for item in boundary.tank_types))
    summary[3].metric("实验记录", "本地 SQLite")
    if not records:
        st.info("当前没有可用历史记录。仍可先生成第 0 代：系统会用 A/B 覆盖和随机合法候选构造搜索起点。")
    with st.expander("了解本次 GA 如何搜索", expanded=False):
        st.markdown(
            "- **搜索对象**：铁芯、低压绕组、高压绕组和冷却四个模块；导线与冷却都按完整目录记录选择。\n"
            "- **结果保留**：严格合格方案按总成本排序；尚未合格的方案按总超限、最大超限和成本排序。\n"
            "- **生成方式**：第 0 代做覆盖与历史/随机补充；后续通过块交叉、随机变异和诊断反馈变异产生候选。\n"
            "- **可追溯性**：候选来源、父代、算子和结果写入本地 SQLite 轨迹，可在“轨迹回放”查看。"
        )
        st.caption("冷却块整体替换：波纹方案包含波纹记录和长/短轴面数；散热器方案包含片宽、中心距、片数、组数和完整 G/Q/SZ 记录，不会拆开混搭。")
    tab_preview, tab_run = st.tabs(["第 0 代准备", "遗传迭代"])
    with tab_preview:
        render_initial_population_preview(config_id, config, catalog, records, boundary)
    with tab_run:
        render_full_ga(config_id, config, catalog, records, boundary)


def render_initial_population_preview(
    config_id: int, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
    boundary: ResearchBoundary,
) -> None:
    st.markdown("#### 先验证搜索起点，而不是直接运行遗传")
    st.caption("本步骤只构造并精算第 0 代：先生成多背景 A/B 覆盖，再加入可还原的同配置/相似配置历史结构，最后以随机完整合法候选补足。它不会发生交叉、变异或代际迭代。")
    defaults = load_optimizer_defaults()
    with st.form("initial-population-form"):
        st.markdown("##### 初始化设置")
        col_a, col_b = st.columns(2)
        with col_a:
            history_ratio = st.slider("历史种子比例", min_value=0.0, max_value=0.9, value=float(defaults["history_seed_ratio"]), step=0.05, help="默认来自 optimizer.yml；页面输入优先。A/B 覆盖后的剩余位置中，分配给同配置/相似配置历史结构的比例；其余由随机完整合法候选补足。")
        with col_b:
            seed = st.number_input("随机种子", min_value=0, max_value=2_147_483_647, value=20260907, step=1, help="相同配置、搜索域和随机种子下，初始化结果可复现。不同随机种子用于后续重复实验。")
        domain_input_error: str | None = None
        try:
            planned_domain = coverage_domain(config, catalog, boundary)
            with st.expander("查看第 0 代覆盖计划与候选范围", expanded=False):
                render_coverage_plan(planned_domain)
        except (KeyError, LookupError, ValueError) as exc:
            domain_input_error = str(exc)
            st.error(f"无法构造当前搜索域的覆盖计划：{exc}")
        st.info("生成后请先查看“结果分布”和“不可计算原因”。若大量候选不可计算，优先检查基础目录、当前固定配置和整数候选边界，而不是盲目增大 GA 代数。")
        submitted = st.form_submit_button("生成第 0 代并写入本地轨迹", type="primary", width="stretch")
    if not submitted:
        return
    if domain_input_error:
        st.error("请先在 Java 页面补齐产品范围后再生成第 0 代。")
        return
    try:
        domain, historical, skipped = build_domain_and_history(config, catalog, records, boundary)
        population = build_mixed_initial_population(domain, historical, None, float(history_ratio), int(seed))
        output = new_trace_path("initial-preview", config_id, int(seed))
        evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
        initial_progress = create_initial_progress_callback()
        run_id, cache = trace_initial_population(
            output, config_id, int(seed), population, evaluator.evaluate,
            {**domain.summary(), "coverage_plan": domain.coverage_plan_summary(include_key_pairs=True)},
            progress_callback=initial_progress,
        )
    except (KeyError, LookupError, ValueError, RuntimeError) as exc:
        st.error(f"初始种群生成失败：{exc}")
        return
    # 预览和完整 GA 均在同一页面会话、同一输入下执行时，允许后者复用已得到的
    # 精算结果。配置、目录、页面范围、公式版本或候选集合有任何变化都会失效。
    st.session_state["initial_preview_reuse"] = {
        "config_id": int(config_id),
        "formula_revision": evaluator.formula_revision,
        "input_fingerprint": initial_preview_input_fingerprint(config_id, config, catalog, boundary),
        "results_by_candidate": dict(cache._values),
        "preview_trace_file": output.name,
    }
    # Streamlit 热更新时已导入模块可能仍是上一版；兼容旧缓存只为读取实验统计。
    results = list(getattr(cache, "results", tuple(cache._values.values())))
    status = Counter("严格合格" if item.feasible else "近可行/不合格" if item.calculable else "不可计算" for item in results)
    non_calculable_reasons = Counter(
        item.diagnostic[0] if item.diagnostic else "未返回诊断信息"
        for item in results if not item.calculable
    )
    st.success("第 0 代已生成并写入本地 outputs 目录；可在“轨迹回放”查看每个候选。")
    render_coverage_plan(
        domain, actual_size=len(population.candidates),
        actual_coverage_count=population.coverage_count,
    )
    stat_cols = st.columns(6)
    stat_cols[0].metric("历史种子", population.history_count)
    stat_cols[1].metric("覆盖候选", population.coverage_count)
    stat_cols[2].metric("随机补充候选", population.random_count)
    stat_cols[3].metric("实际精算调用", cache.exact_evaluations)
    stat_cols[4].metric("缓存命中", cache.cache_hits)
    stat_cols[5].metric("自动目标规模", population.automatic_target_size)
    st.caption(
        f"搜索域估算合法候选数：{population.estimated_legal_candidate_count}；"
        f"A/B 覆盖目标占比：{population.coverage_target_ratio:.2%}；无法映射的当前配置历史记录：{skipped}。"
    )
    st.markdown("##### 第 0 代候选来源")
    source_counts = Counter(population.sources)
    st.dataframe([{"来源": source, "候选数": count} for source, count in sorted(source_counts.items())], hide_index=True, width="stretch")
    st.markdown("##### 第 0 代结果分布")
    st.dataframe([{"结果类型": key, "候选数": value} for key, value in status.items()], hide_index=True, width="stretch")
    # 第 0 代尚未进入双档案主循环，但仍直接给出当前评价结果中成本最低的严格
    # 候选和超限最小的近可行候选，避免用户只能看到计数而看不到实际方案。
    candidate_results = list(zip(population.candidates, results))
    strict_rows = sorted(
        (pair for pair in candidate_results if pair[1].feasible and pair[1].complete),
        key=lambda pair: Decimal("Infinity") if pair[1].metrics.get("price") is None
        else Decimal(str(pair[1].metrics["price"])),
    )
    near_rows = sorted(
        (pair for pair in candidate_results if pair[1].calculable and pair[1].complete and not pair[1].feasible),
        key=lambda pair: (
            pair[1].total_violation, pair[1].max_violation,
            Decimal("Infinity") if pair[1].metrics.get("price") is None
            else Decimal(str(pair[1].metrics["price"])),
        ),
    )
    st.markdown("##### 第 0 代当前结果前五名")
    strict_tab, near_tab = st.tabs(["严格合格候选", "近可行候选"])
    with strict_tab:
        rows = [candidate_result_row(candidate, result, rank, "严格合格") for rank, (candidate, result) in enumerate(strict_rows[:5], start=1)]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch", height=240)
        else:
            st.info("第 0 代尚未找到严格合格候选。")
    with near_tab:
        rows = [candidate_result_row(candidate, result, rank, "近可行") for rank, (candidate, result) in enumerate(near_rows[:5], start=1)]
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch", height=240)
        else:
            st.info("第 0 代没有可计算的近可行候选。")
    if non_calculable_reasons:
        st.markdown("##### 不可计算原因（前十项）")
        st.dataframe([{"原因": key, "候选数": value} for key, value in non_calculable_reasons.most_common(10)], hide_index=True, width="stretch")
    st.caption(
        f"轨迹文件：{output.name}；运行 ID：{run_id}。同一页面会话中，如后续以完全相同的"
        "配置、目录、研究范围、初始候选和公式版本启动 GA，将复用本次第 0 代精算结果。"
    )


def render_full_ga(
    config_id: int, config: dict[str, Any], catalog: dict[str, list[dict[str, Any]]], records: list[dict[str, Any]],
    boundary: ResearchBoundary,
) -> None:
    st.markdown("#### 运行参数")
    st.caption("建议先完成“第 0 代准备”。若本次未找到严格合格候选，只会展示超限最小的近可行候选，不能作为最终设计。")
    defaults = load_optimizer_defaults()
    with st.form("full-ga-form"):
        setup_tab, operator_tab, retention_tab = st.tabs(["运行规模", "交叉与变异", "档案与细化"])
        with setup_tab:
            basic_a, basic_b = st.columns(2)
            with basic_a:
                generations = st.number_input("最大代数", min_value=1, max_value=300, value=20, step=1, key="ga-generations", help="第 0 代后最多迭代的代数。严格/近可行档案连续 8 代没有改进时，算法会提前停止。")
            with basic_b:
                seed = st.number_input("随机种子", min_value=0, max_value=2_147_483_647, value=20260907, step=1, key="ga-seed", help="固定该值可重复本次实验；论文对比应使用 20–30 个不同种子独立运行。")
            history_ratio = st.slider("历史种子比例", min_value=0.0, max_value=0.9, value=float(defaults["history_seed_ratio"]), step=0.05, key="ga-history-ratio", help="A/B 覆盖后的剩余位置中，分配给同配置/相似配置历史结构的比例；其余由随机完整合法候选补足。")
            strategy_options = {
                "当前页面参数": None,
                "消融基线：关闭三项改进": "baseline",
                "仅启用结构电磁耦合交叉": "coupled",
                "当前推荐：耦合交叉 + 经验有效域注入": "empirical_injection",
                "完整诊断反馈策略": "guided_mutation",
            }
            strategy_label = st.selectbox(
                "本次实验策略",
                tuple(strategy_options),
                index=list(strategy_options).index("当前推荐：耦合交叉 + 经验有效域注入"),
                help="默认推荐仅来自配置 401、当前搜索域、B′=8000、10 个配对种子的 v9 对照：经验有效域注入显著有利；诊断反馈未检出额外收益。命名策略与 CLI 的 ga-run --experiment-strategy、ga-ablation 完全共用同一开关映射；它们是当前代码内的消融层级，不是历史旧版 GA。",
                key="ga-experiment-strategy-v9-default",
            )
            selected_strategy = strategy_options[strategy_label]
            if selected_strategy is not None:
                st.caption("选择命名策略后，页面中的交叉/注入/定向变异开关会按该策略统一覆盖；其它运行规模参数仍使用本页设置，并完整写入轨迹。")
            domain_input_error: str | None = None
            try:
                planned_domain = coverage_domain(config, catalog, boundary)
                with st.expander("查看本次搜索范围与第 0 代覆盖计划", expanded=False):
                    render_coverage_plan(planned_domain)
            except (KeyError, LookupError, ValueError) as exc:
                domain_input_error = str(exc)
                st.error(f"无法构造当前搜索域的覆盖计划：{exc}")
        with operator_tab:
            advanced_a, advanced_b = st.columns(2)
            with advanced_a:
                crossover_rate = st.slider("参数块交叉率", 0.0, 1.0, 0.85, 0.05, key="ga-crossover", help="父代之间进行结构电磁块与完整冷却型号的组合；候选始终保留完整导线和完整冷却目录记录。")
                coupled_crossover_rate = st.slider("结构电磁耦合交叉占比", 0.0, 1.0, 0.75, 0.05, key="ga-coupled-crossover", help="在发生交叉时，铁芯/硅钢牌号/低压匝数/高低压绕组整体继承同一父代的比例；其余比例保留四块探索重组，以避免过早锁死。")
                mutation_rate = st.slider("总变异率", 0.0, 1.0, 0.45, 0.05, key="ga-mutation", help="子代发生变异的概率。未发生变异时，子代只来自父代复制或块级交叉。")
            with advanced_b:
                guided_rate = st.slider("诊断反馈变异占比（仅完整诊断策略生效）", 0.0, 1.0, 0.65, 0.05, key="ga-guided", help="仅当选择“完整诊断反馈策略”时生效：在已决定变异的子代中，按“主导超限约束 → 一个优先模块 → 相邻离散方案精算”的比例；其余为普通合法随机变异。默认推荐策略不启用该探测。")
                probe_limit = st.number_input("每次诊断的局部精算数", min_value=1, max_value=10, value=3, step=1, key="ga-probe", help="定向变异时，只围绕一个主导约束和一个模块，最多精算多少个相邻离散候选；实际增减方向由精算结果确认。")
        with retention_tab:
            retention_a, retention_b = st.columns(2)
            with retention_a:
                archive_size = st.number_input("每类档案保留数", min_value=1, max_value=200, value=24, step=1, key="ga-archive", help="严格合格档案和近可行档案各自最多保留的去重候选数。严格档案按成本排序，近可行档案按超限向量排序。")
                elite_count = st.number_input("每代精英保留数", min_value=0, max_value=20, value=2, step=1, key="ga-elite", help="优先把严格档案精英带入下一代；尚无严格解时使用近可行精英。")
            with retention_b:
                injection_ratio = st.slider("每代有效域随机注入比例", min_value=0.0, max_value=1.0, value=float(defaults["random_injection_ratio"]), step=0.05, key="ga-random-injection", help="每代精英后，从已真实精算且可计算的经验候选池随机重组未见合法候选；经验池不足时才退回原始页面搜索域。")
                refine_limit = st.number_input("严格精英邻域精算上限", min_value=0, max_value=100, value=12, step=1, key="ga-refine", help="GA 主循环结束后，对严格精英邻近组合再次精算的最大次数。0 表示关闭。")
        st.info("运行结束后，建议到“轨迹回放”检查：每代严格合格数、实际精算调用、缓存命中、候选来源、父代 ID 与诊断记录。不要只看最后的最低成本。")
        submitted = st.form_submit_button("运行改进 GA，并保存全部实验轨迹", type="primary", width="stretch")
    if not submitted:
        render_persisted_ga_progress(config_id)
        return
    if domain_input_error:
        st.error("请先在 Java 页面补齐产品范围后再启动 GA。")
        return
    try:
        domain, historical, skipped = build_domain_and_history(config, catalog, records, boundary)
        population = build_mixed_initial_population(domain, historical, None, float(history_ratio), int(seed))
        working_population_size = automatic_evolution_population_size(len(population.candidates))
        manual_settings = GASettings(
            population_size=working_population_size, generations=int(generations), history_ratio=float(history_ratio),
            random_injection_ratio=float(injection_ratio),
            crossover_rate=float(crossover_rate), coupled_electromagnetic_crossover_rate=float(coupled_crossover_rate), mutation_rate=float(mutation_rate),
            guided_mutation_rate=float(guided_rate), archive_size=int(archive_size), elite_count=int(elite_count),
            local_probe_limit=int(probe_limit), local_refine_limit=int(refine_limit),
        )
        settings = apply_experiment_strategy(manual_settings, selected_strategy)
        st.caption(
            f"第 0 代将全量精算 {len(population.candidates)} 条覆盖/历史/随机候选；"
            f"后续遗传代最多采用 {working_population_size} 条工作种群。实际工作种群只从第 0 代“可计算且结果完整”的候选中选取，"
            "运行后会在第 0 代轨迹说明中记录实际数量。"
        )
        evaluator = ThreePhaseSingleDeviceEvaluator(config, catalog)
        precomputed_results = reusable_initial_preview_results(
            config_id, config, catalog, boundary, population, evaluator.formula_revision,
        )
        if precomputed_results is not None:
            st.info(
                f"已校验本次输入与第 0 代预览完全一致，将复用 {len(precomputed_results)} 条预览精算结果；"
                "本次 GA 轨迹仍会完整记录第 0 代候选，只是不重复调用精算器。"
            )
        output = new_trace_path("ga-run", config_id, int(seed))
        # 新一次提交不沿用上一次的状态；从第一条真实回调开始写入新的快照。
        st.session_state.pop("ga_live_progress", None)
        ga_progress = create_ga_progress_callback(int(generations), config_id)
        summary = run_optimization(
            output, config_id, int(seed), domain, population,
            evaluator.evaluate, settings, progress_callback=ga_progress,
            precomputed_results=precomputed_results,
            formula_revision=evaluator.formula_revision,
            run_metadata={
                "execution_entry": "streamlit_ga_page",
                "experiment_strategy": selected_strategy or "custom",
                "experiment_strategy_label": strategy_label,
            },
        )
    except (KeyError, LookupError, ValueError, RuntimeError) as exc:
        progress = st.session_state.get("ga_live_progress")
        if isinstance(progress, dict):
            progress["运行状态"] = "运行失败"
            progress["失败原因"] = str(exc)
        st.error(f"GA 运行失败：{exc}")
        return
    progress = st.session_state.get("ga_live_progress")
    if isinstance(progress, dict):
        progress["运行状态"] = (
            "已停止（无可计算起点）" if summary.termination_reason == "initial_recovery_exhausted"
            else "已完成（提前停止）" if summary.stopped_by_stagnation else "已完成"
        )
        if summary.stopped_by_stagnation:
            progress.setdefault("提前停止原因", f"连续 {settings.stagnation_limit} 代双档案无改进")
        progress["轨迹文件"] = output.name
    st.success("GA 已完成；完整候选、父代关系和代际统计已写入本地 outputs 目录。")
    summary_cols = st.columns(7)
    summary_cols[0].metric("完成代数", summary.generations_completed)
    summary_cols[1].metric("是否因停滞提前结束", "是" if summary.stopped_by_stagnation else "否")
    summary_cols[2].metric("实际精算调用", summary.exact_evaluations)
    summary_cols[3].metric("缓存命中", summary.cache_hits)
    summary_cols[4].metric("严格档案候选", len(summary.strict_archive))
    summary_cols[5].metric("近可行档案候选", len(summary.near_archive))
    summary_cols[6].metric("本次耗时", f"{summary.elapsed_seconds:.1f} 秒")
    st.caption(f"轨迹文件：{summary.trace_path.name}；运行 ID：{summary.run_id}；无法映射的历史记录：{skipped}")
    if summary.best_strict is not None:
        st.subheader("最终最低成本严格合格方案")
        render_result(summary.best_strict.result)
        st.caption(f"候选编码：{summary.best_strict.candidate.canonical_dict()}")
    elif summary.best_near is not None:
        st.warning("本次预算内没有严格合格方案。以下为近可行档案中约束超限最小的方案，不可作为最终设计方案。")
        render_result(summary.best_near.result)
        st.caption(f"候选编码：{summary.best_near.candidate.canonical_dict()}")
    else:
        st.error("本次运行没有获得可计算的近可行候选，请查看 SQLite 中的个体诊断并调整搜索域或初始种群。")
    render_final_archive_tables(summary.strict_archive, summary.near_archive)


def main() -> None:
    """实验台入口：数据库离线时退化为纯 SQLite 轨迹操作台。"""
    trace_files = discover_trace_files(PROJECT_ROOT / "outputs")
    db_error: str | None = None
    configs: dict[int, dict[str, Any]] = {}
    records_by_config: dict[int, list[dict[str, Any]]] = {}
    catalog: dict[str, list[dict[str, Any]]] = {}
    try:
        configs, records_by_config = cached_page_context()
        catalog = cached_catalog()
    except Exception as exc:
        db_error = str(exc)
    db_available = bool(configs and catalog)
    config_id = render_workspace_header(configs, db_available, len(trace_files))
    current_config = configs.get(config_id) if config_id is not None else None
    current_records = records_by_config.get(config_id, []) if config_id is not None else []
    active_page = render_experiment_sidebar(current_config, current_records, catalog, len(trace_files), db_error)

    research_boundary = st.session_state.get("research_boundary")
    if st.session_state.get("research_boundary_config_id") != config_id:
        research_boundary = None

    if active_page == "单设备精算":
        if not db_available:
            render_database_required("单设备精算", db_error)
        else:
            assert config_id is not None and current_config is not None
            config = current_config
            records = current_records
            render_page_header(
                "单设备精算", "按“范围设置、方案复算、结果诊断”完成一次可追溯的单设备计算。",
                "当前配置的离散候选",
            )
            boundary_tab, calculate_tab, result_tab = st.tabs(["1 · 设置本次搜索范围", "2 · 选择方案并复算", "3 · 查看计算结果"])
            with boundary_tab:
                research_boundary = render_fixed_inputs_and_boundary(config, catalog, records)
                st.session_state["research_boundary"] = research_boundary
                st.session_state["research_boundary_config_id"] = int(config_id)
                st.info("这里设置的材料、铁芯、导线和冷却范围会同时供本次单设备试算与后续 GA 使用。切换左侧模块后不会丢失。")
            with calculate_tab:
                st.markdown("#### 选择要复算的 Java 历史方案")
                record_map = {int(item["id"]): item for item in records}
                seed_ids = sorted(record_map, reverse=True)
                seed_id = st.selectbox("Java 历史方案记录", seed_ids, format_func=lambda value: f"记录 {value} · 保存价格 {decimal_text(record_map[value].get('price'), 2)}", help="选择后还原该记录保存的结构基因和冷却快照，再由 Python 重新精算。这里不是 GA 历史种子。") if seed_ids else None
                seed_candidate = None
                if seed_id is not None:
                    try:
                        seed_candidate = resolve_record_candidate(record_map[int(seed_id)], catalog, config)
                    except (KeyError, LookupError, ValueError) as exc:
                        st.warning(f"该历史方案不能映射为当前目录候选：{exc}")
                candidate = candidate_from_widgets(
                    config, catalog, seed_candidate, f"record-{seed_id}", research_boundary,
                )
                if candidate is not None:
                    result = ThreePhaseSingleDeviceEvaluator(config, catalog).evaluate(candidate)
                    st.session_state["last_candidate"] = candidate
                    st.session_state["last_result"] = result
                    st.session_state["last_result_config_id"] = int(config_id)
                    st.success("已完成 Python 单设备精算；请切换到“查看计算结果”阅读指标和约束诊断。")
            with result_tab:
                if "last_result" in st.session_state and st.session_state.get("last_result_config_id") == int(config_id):
                    render_result(st.session_state["last_result"])
                else:
                    st.info("尚未运行单设备精算。请在“选择方案并复算”页签中选择一条可还原的 Java 历史方案。")

    elif active_page == "一致性验证":
        if not db_available:
            render_database_required("Java—Python 一致性验证", db_error)
        else:
            assert config_id is not None and current_config is not None
            config = current_config
            records = current_records
            render_page_header(
                "一致性验证", "对同一条已保存方案分别读取 Java 结果与 Python 重算结果，定位字段差异。",
                "同方案、同字段口径",
            )
            compare_tab, guidance_tab = st.tabs(["字段对比", "对比口径说明"])
            with compare_tab:
                record_map = {int(item["id"]): item for item in records}
                options = sorted(record_map, reverse=True)
                default_record = 28856 if 28856 in record_map else (options[0] if options else None)
                if default_record is None:
                    st.info("当前配置没有历史方案记录。")
                else:
                    record_id = st.selectbox("方案记录", options, index=options.index(default_record), format_func=lambda value: f"记录 {value} · 阶段 {record_map[value].get('stage')} · 价格 {decimal_text(record_map[value].get('price'), 2)}")
                    if st.button("执行字段对比", type="primary"):
                        render_consistency(record_map[int(record_id)], config, catalog)
            with guidance_tab:
                st.info("此处比较的是同一条 Java 保存方案与 Python 重新精算的结果。应先经 Faladi MCP 请求后端完成最新计算，再读取该记录或快照进行比较；不能把旧保存数值当成新的 Java 计算结果。")
                st.markdown("- 有 calculationSnapshot 的记录可比较温升、油重、油箱重量和成本分项。\n- 没有快照的旧记录只比较可取得的字段，并明确标为未验证。\n- 字段存在差异时，应先确认输入快照和目录记录 ID 是否一致，再排查公式差异。")

    elif active_page == "GA 优化实验":
        if not db_available:
            render_database_required("GA 优化运行", db_error)
        else:
            assert config_id is not None and current_config is not None
            config = current_config
            records = current_records
            render_page_header(
                "GA 优化实验", "先确认搜索起点，再让遗传算法生成、评价并保留候选；每一步都写入本地轨迹。",
                "离散设计域搜索",
            )
            if research_boundary is None or not all((research_boundary.steels, research_boundary.core_types, research_boundary.low_wire_types, research_boundary.high_wire_types, research_boundary.tank_types)):
                st.warning("请先在“单设备精算”页保留至少一种材料、铁芯、两侧导线类别和冷却形式。")
            else:
                render_ga_prepare(int(config_id), config, catalog, records, research_boundary)

    elif active_page == "轨迹回放":
        render_page_header("轨迹回放", "不重新精算，直接复盘已保存实验的候选来源、代际过程与双档案结果。", "只读本地 SQLite")
        render_trace_replay(trace_files)

    else:
        render_page_header("研究说明", "说明操作台的数据边界、实验流程和当前验证结论；未验证内容不会作为工程结论展示。", "方法与验证边界")
        process_tab, boundary_tab = st.tabs(["实验流程", "数据与验证边界"])
        with process_tab:
            render_workflow_cards()
            if current_config is None:
                render_database_required("新的精算和 GA", db_error)
            else:
                performance = current_config["performance_index"]
                st.markdown("<div class='status-note'><b>当前实验上下文：</b>选定三相配置后，固定产品需求、工艺参数和原始目录；单设备精算评价一个候选，GA 只负责生成、交叉、变异和排序候选。每次实验独立写入 SQLite，供回放和多随机种子比较。</div>", unsafe_allow_html=True)
                overview = st.columns(5)
                overview[0].metric("额定容量（kVA）", decimal_text(performance.get("capacity"), 0))
                overview[1].metric("高压额定值（V）", decimal_text(performance.get("highVoltageRated"), 0))
                overview[2].metric("低压额定值（V）", decimal_text(performance.get("lowVoltageRated"), 0))
                overview[3].metric("历史方案数", len(current_records))
                overview[4].metric("已有轨迹数", len(trace_files))
        with boundary_tab:
            st.markdown("#### 当前页面能说明什么")
            st.markdown("- 业务数据库始终只读；候选、精算结果和算法轨迹只写入本地 `outputs/*.sqlite3`。\n- 线规、铁芯、油道和冷却都只引用真实离散记录，不生成虚构规格。\n- 严格合格候选按成本排序；近可行候选只用于搜索方向，不能视为最终设计。\n- GA 是否有效必须在固定搜索域下，使用多随机种子比较成功率、首次严格合格精算次数和最低严格成本后才能下结论。")

if __name__ == "__main__":
    # Streamlit 会将模块最外层的裸表达式自动渲染；若直接写 ``main()``，
    # 某些版本会把其返回的 DeltaGenerator 对象及方法说明显示在页面上。
    # 使用赋值调用可避免把框架内部对象误当实验结果输出。
    _main_result = main()

