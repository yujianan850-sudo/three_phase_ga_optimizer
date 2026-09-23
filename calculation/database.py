"""只读访问本地 faladi 数据库。

Python 实验工程只读取自身 ``config/database.local.yml`` 中的连接信息，完全不依赖
Java 工程的开发配置。该文件已被 Git 忽略，允许保存本机开发数据库口令，但不得提交。
"""

from __future__ import annotations

import json
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pymysql
import yaml

from .material_price_provider import MaterialPriceUnavailable, read_current_three_phase_prices, resolve_unit_prices


# database.py 位于 calculation/；配置仍固定在项目根目录的 config/ 下。
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    schema: str
    username: str
    password: str
    charset: str = "utf8mb4"
    connect_timeout: int = 5


class DatabaseConnectionPool:
    """轻量只读连接池：复用长连接，失效时才重连。

    页面请求可能由 Streamlit 的不同线程执行，因此不能把单个 PyMySQL 连接裸露给所有
    请求。池中连接通过 ``ping(reconnect=True)`` 保活；数据库主动断开空闲连接时，下一次
    借用会自动恢复，而不是让页面报错。
    """

    def __init__(self, settings: DatabaseSettings, max_size: int = 4) -> None:
        if max_size < 1:
            raise ValueError("连接池大小必须大于 0")
        self.settings = settings
        self.max_size = max_size
        self._idle: queue.LifoQueue[pymysql.connections.Connection] = queue.LifoQueue(maxsize=max_size)
        self._created = 0
        self._lock = threading.Lock()

    def _new_connection(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.settings.host, port=self.settings.port, user=self.settings.username,
            password=self.settings.password, database=self.settings.schema, charset=self.settings.charset,
            connect_timeout=self.settings.connect_timeout, cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def _discard(self, conn: pymysql.connections.Connection) -> None:
        try:
            conn.close()
        finally:
            with self._lock:
                self._created = max(0, self._created - 1)

    def _acquire(self) -> pymysql.connections.Connection:
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self.max_size:
                    self._created += 1
                    create_new = True
                else:
                    create_new = False
            if create_new:
                try:
                    return self._new_connection()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise
            conn = self._idle.get(timeout=self.settings.connect_timeout)
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            self._discard(conn)
            return self._acquire()

    def _release(self, conn: pymysql.connections.Connection) -> None:
        if not conn.open:
            self._discard(conn)
            return
        try:
            self._idle.put_nowait(conn)
        except queue.Full:
            self._discard(conn)

    @contextmanager
    def connection(self) -> Iterator[pymysql.connections.Connection]:
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    def close(self) -> None:
        """关闭空闲连接；应用退出时可选调用，正常页面运行无需频繁关闭。"""
        while True:
            try:
                self._discard(self._idle.get_nowait())
            except queue.Empty:
                return


def _read_local_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_database_settings(config_path: Path | None = None) -> DatabaseSettings:
    """读取 Python 工程自身的本地数据库配置。

    ``config_path`` 只为测试注入保留；生产页面固定读取
    ``three_phase_ga_optimizer/config/database.local.yml``。YAML 中纯数字口令会被
    解析为 ``int``，因此此处统一转为字符串。
    """
    local_path = config_path or ROOT / "config" / "database.local.yml"
    if not local_path.exists():
        raise FileNotFoundError(
            f"未找到 Python 数据库配置：{local_path}。请复制 config/database.local.yml.example 并填写本机连接信息。"
        )
    data = _read_local_config(local_path) or {}
    db = data.get("database") or {}
    required = ("host", "schema", "username", "password")
    missing = [key for key in required if db.get(key) in (None, "")]
    if missing:
        raise ValueError(f"Python 数据库配置缺少字段：{', '.join(missing)}")
    return DatabaseSettings(
        host=str(db["host"]),
        port=int(db.get("port", 3306)),
        schema=str(db["schema"]),
        username=str(db["username"]),
        password=str(db["password"]),
        charset=str(db.get("charset", "utf8mb4")),
        connect_timeout=int(db.get("connect_timeout_seconds", 5)),
    )


class FaladiRepository:
    """三相优化所需的原始数据和历史方案读取器；不向业务库写任何数据。"""

    def __init__(self, settings: DatabaseSettings, pool: DatabaseConnectionPool | None = None):
        self.settings = settings
        self.pool = pool

    @contextmanager
    def connection(self) -> Iterator[pymysql.connections.Connection]:
        if self.pool is not None:
            with self.pool.connection() as conn:
                yield conn
            return
        conn = pymysql.connect(
            host=self.settings.host, port=self.settings.port, user=self.settings.username,
            password=self.settings.password, database=self.settings.schema, charset=self.settings.charset,
            connect_timeout=self.settings.connect_timeout, cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        try:
            yield conn
        finally:
            conn.close()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
        rows = self._all(sql, params)
        if not rows:
            raise LookupError("未找到请求的数据")
        return rows[0]

    @staticmethod
    def _decode_json_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        value = dict(row)
        for field in fields:
            raw = value.get(field)
            value[field] = json.loads(raw) if isinstance(raw, str) else raw
        return value

    @staticmethod
    def _has_blank_unit_price(config: dict[str, Any]) -> bool:
        prices = config.get("craft_unit_price") or {}
        return any(value is None or (isinstance(value, str) and not value.strip()) for value in prices.values())

    @staticmethod
    def _resolve_unit_prices_for_configs(configs: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """同一批三相配置最多经 MCP 读取一次全局材料价格。"""
        need_fallback = any(FaladiRepository._has_blank_unit_price(config) for config in configs.values())
        fallback_prices: dict[str, Any] | None = None
        fallback_error: str | None = None
        if need_fallback:
            try:
                fallback_prices = read_current_three_phase_prices()
            except MaterialPriceUnavailable as exc:
                fallback_prices = {}
                fallback_error = str(exc)
        return {
            config_id: resolve_unit_prices(config, fallback_prices, fallback_error)
            for config_id, config in configs.items()
        }

    def scheme_config(self, config_id: int) -> dict[str, Any]:
        row = self._one(
            "SELECT * FROM tb_scheme_config_three_phase WHERE id = %s AND is_deleted = 0", (config_id,)
        )
        config = self._decode_json_fields(row, (
            "performance_index", "optimization_scope", "craft_core", "craft_high_coil",
            "craft_low_coil", "craft_isolation", "craft_fuel_tank", "craft_unit_price",
        ))
        return self._resolve_unit_prices_for_configs({int(config_id): config})[int(config_id)]

    def scheme_record(self, record_id: int) -> dict[str, Any]:
        row = self._one(
            "SELECT * FROM tb_scheme_record_three_phase WHERE id = %s AND is_deleted = 0", (record_id,)
        )
        return self._decode_json_fields(row, ("scheme_data",))

    def active_records(self, config_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM tb_scheme_record_three_phase WHERE scheme_config_id = %s AND is_deleted = 0",
            (config_id,),
        )
        return [self._decode_json_fields(row, ("scheme_data",)) for row in rows]

    def scheme_context(self, config_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """一次连接读取页面切换配置所需的配置与历史方案。"""
        config_fields = (
            "performance_index", "optimization_scope", "craft_core", "craft_high_coil",
            "craft_low_coil", "craft_isolation", "craft_fuel_tank", "craft_unit_price",
        )
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM tb_scheme_config_three_phase WHERE id = %s AND is_deleted = 0", (config_id,)
                )
                config_row = cursor.fetchone()
                if not config_row:
                    raise LookupError("未找到请求的数据")
                cursor.execute(
                    "SELECT * FROM tb_scheme_record_three_phase WHERE scheme_config_id = %s AND is_deleted = 0",
                    (config_id,),
                )
                record_rows = list(cursor.fetchall())
        config = self._decode_json_fields(config_row, config_fields)
        return (
            self._resolve_unit_prices_for_configs({int(config_id): config})[int(config_id)],
            [self._decode_json_fields(row, ("scheme_data",)) for row in record_rows],
        )

    def all_active_records(self) -> list[dict[str, Any]]:
        """读取全部三相历史方案，仅用于 Python—Java 一致性回归，不修改业务数据。"""
        rows = self._all("SELECT * FROM tb_scheme_record_three_phase WHERE is_deleted = 0")
        return [self._decode_json_fields(row, ("scheme_data",)) for row in rows]

    def all_scheme_configs(self) -> dict[int, dict[str, Any]]:
        """批量读取三相配置，供全量一致性回归复用，避免每条历史方案单独建连接。"""
        rows = self._all("SELECT * FROM tb_scheme_config_three_phase WHERE is_deleted = 0")
        fields = (
            "performance_index", "optimization_scope", "craft_core", "craft_high_coil",
            "craft_low_coil", "craft_isolation", "craft_fuel_tank", "craft_unit_price",
        )
        configs = {int(row["id"]): self._decode_json_fields(row, fields) for row in rows}
        return self._resolve_unit_prices_for_configs(configs)

    def active_scheme_config_ids(self) -> list[int]:
        """只读取页面配置下拉框所需的 ID，避免交互时加载全部 JSON 配置。"""
        rows = self._all("SELECT id FROM tb_scheme_config_three_phase WHERE is_deleted = 0")
        return [int(row["id"]) for row in rows]

    def all_page_context(self) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
        """以一条连接读取实验页面的全部配置和历史方案，供页面内切换时走内存缓存。"""
        config_fields = (
            "performance_index", "optimization_scope", "craft_core", "craft_high_coil",
            "craft_low_coil", "craft_isolation", "craft_fuel_tank", "craft_unit_price",
        )
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM tb_scheme_config_three_phase WHERE is_deleted = 0")
                config_rows = list(cursor.fetchall())
                cursor.execute("SELECT * FROM tb_scheme_record_three_phase WHERE is_deleted = 0")
                record_rows = list(cursor.fetchall())
        configs = {int(row["id"]): self._decode_json_fields(row, config_fields) for row in config_rows}
        configs = self._resolve_unit_prices_for_configs(configs)
        records_by_config: dict[int, list[dict[str, Any]]] = {config_id: [] for config_id in configs}
        for row in record_rows:
            record = self._decode_json_fields(row, ("scheme_data",))
            records_by_config.setdefault(int(record["scheme_config_id"]), []).append(record)
        return configs, records_by_config

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        """一次读取精算与编码使用的基础目录，查询不复用旧 Java 的性能筛选。"""
        queries = {
            "core": "SELECT * FROM tb_core_data WHERE is_deleted = 0",
            "steel": "SELECT * FROM tb_silicon_steel_sheets_config WHERE is_deleted = 0",
            # 三相配置中的 siliconSteelGrade 保存的是牌号编码，不是牌号文本；
            # 必须通过该映射表还原后再构造 Python 的离散候选域。
            "steel_brand_mapping": "SELECT brand_code, brand FROM tb_silicon_steel_brand_mapping WHERE transformer_id = 2 AND is_deleted = 0",
            "flat": "SELECT * FROM tb_flat_line_config WHERE is_deleted = 0",
            "foil": "SELECT * FROM tb_foil_config WHERE is_deleted = 0",
            "round": "SELECT * FROM tb_round_line_config WHERE is_deleted = 0",
            "corrugation": "SELECT * FROM tb_corrugation WHERE is_deleted = 0",
            "storage_tank": "SELECT * FROM tb_storage_tank WHERE is_deleted = 0",
            "thermawide_310": "SELECT * FROM tb_thermawide_310 WHERE is_deleted = 0",
            "thermawide_480": "SELECT * FROM tb_thermawide_480 WHERE is_deleted = 0",
            "thermawide_520": "SELECT * FROM tb_thermawide_520 WHERE is_deleted = 0",
        }
        with self.connection() as conn:
            with conn.cursor() as cursor:
                result: dict[str, list[dict[str, Any]]] = {}
                for name, sql in queries.items():
                    cursor.execute(sql)
                    result[name] = list(cursor.fetchall())
                return result
