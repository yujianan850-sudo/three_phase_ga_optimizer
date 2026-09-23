"""三相材料价格的只读来源解析。

方案 ``craft_unit_price`` 是首选来源。仅当其中某项为空时，才通过同仓库
``faladi_mcp`` 的只读 MCP 工具读取 Java 当前材料价格；禁止在此模块直接请求
Java HTTP 接口，也不向业务数据库写入补齐后的价格。
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from typing import Any


OPTIMIZER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MCP_ROOT = OPTIMIZER_ROOT.parent / "faladi_mcp"


class MaterialPriceUnavailable(RuntimeError):
    """方案单价缺失且本地 MCP 价格服务不能提供回退价格。"""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _mcp_paths() -> tuple[Path, Path]:
    mcp_root = Path(os.environ.get("FALADI_MCP_HOME", DEFAULT_MCP_ROOT))
    python_path = Path(os.environ.get("FALADI_MCP_PYTHON", mcp_root / ".venv" / "Scripts" / "python.exe"))
    return mcp_root, python_path


def read_current_three_phase_prices(timeout_seconds: int = 20) -> dict[str, Any]:
    """经 Faladi MCP 调用 Java 的 ``/fuelTankPriceConfig``，返回原始价格字典。"""
    mcp_root, python_path = _mcp_paths()
    bridge = mcp_root / "price_bridge.py"
    if not bridge.is_file() or not python_path.is_file():
        raise MaterialPriceUnavailable(
            "方案存在空单价，但未找到 faladi_mcp 的 price_bridge.py 或其 .venv Python；"
            "可设置 FALADI_MCP_HOME、FALADI_MCP_PYTHON 后重试。"
        )
    try:
        completed = subprocess.run(
            [str(python_path), str(bridge), "three_phase"], cwd=str(mcp_root),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaterialPriceUnavailable(f"经 MCP 读取当前材料价格失败：{exc}") from exc
    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise MaterialPriceUnavailable(
            f"MCP 价格桥未返回可解析 JSON（退出码 {completed.returncode}）：{completed.stderr.strip()}"
        ) from exc
    if completed.returncode != 0 or not payload.get("ok") or not isinstance(payload.get("data"), dict):
        raise MaterialPriceUnavailable(payload.get("error") or completed.stderr.strip() or "MCP 价格接口没有返回价格数据")
    return dict(payload["data"])


def _normalized_fallback_prices(raw_prices: dict[str, Any]) -> dict[str, Any]:
    """对齐 Java 接口的小写导线编码与 Python/Java 计算侧历史别名。"""
    normalized: dict[str, Any] = {}
    lower_wire_keys = {"lm", "tm", "qz", "qlz", "qqb", "qqlb", "zb", "zlb"}
    for key, value in raw_prices.items():
        if _is_blank(value) or key == "transformerId":
            continue
        normalized[key.upper() if key in lower_wire_keys else key] = value
    return normalized


def resolve_unit_prices(
    config: dict[str, Any], fallback_prices: dict[str, Any] | None = None,
    fallback_error: str | None = None,
) -> dict[str, Any]:
    """返回价格已补齐的配置副本，并记录每项价格来自方案还是 MCP。

    数值 0 不被当作“未填写”：它会保留方案配置的明确值，符合“方案配置优先”的
    口径。只有 ``None`` 或空字符串才会触发 MCP 回退。
    """
    resolved = deepcopy(config)
    configured = dict(resolved.get("craft_unit_price") or {})
    missing = [key for key, value in configured.items() if _is_blank(value)]
    sources = {key: "方案配置" for key, value in configured.items() if not _is_blank(value)}
    if missing:
        if fallback_prices is None:
            try:
                fallback = _normalized_fallback_prices(read_current_three_phase_prices())
            except MaterialPriceUnavailable as exc:
                fallback = {}
                fallback_error = str(exc)
        else:
            fallback = _normalized_fallback_prices(fallback_prices)
        for key in missing:
            if key in fallback:
                configured[key] = fallback[key]
                sources[key] = "当前材料价格接口（经 MCP）"
            else:
                sources[key] = "未取得价格"

    resolved["craft_unit_price"] = configured
    resolved["_unit_price_resolution"] = {
        "configured_count": sum(value == "方案配置" for value in sources.values()),
        "fallback_count": sum(value == "当前材料价格接口（经 MCP）" for value in sources.values()),
        "unresolved_keys": [key for key, value in sources.items() if value == "未取得价格"],
        "fallback_error": fallback_error,
        "sources": sources,
    }
    return resolved
