"""运行级、代级、个体级、算子级可追踪 SQLite 日志。"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from calculation.models import EvaluationResult, ThreePhaseDesignCandidate


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, random_seed INTEGER NOT NULL,
  config_id INTEGER NOT NULL, algorithm TEXT NOT NULL, domain_json TEXT NOT NULL, settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generations (
  run_id TEXT NOT NULL, generation INTEGER NOT NULL, exact_evaluations INTEGER NOT NULL,
  cache_hits INTEGER NOT NULL, feasible_count INTEGER NOT NULL, note TEXT,
  PRIMARY KEY (run_id, generation)
);
CREATE TABLE IF NOT EXISTS archive_snapshots (
  run_id TEXT NOT NULL, generation INTEGER NOT NULL, strict_count INTEGER NOT NULL,
  near_count INTEGER NOT NULL, best_strict_cost TEXT, best_near_violation TEXT,
  PRIMARY KEY (run_id, generation)
);
CREATE TABLE IF NOT EXISTS individuals (
  individual_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, generation INTEGER NOT NULL,
  parent_a_id TEXT, parent_b_id TEXT, source TEXT NOT NULL, operator_json TEXT,
  candidate_json TEXT NOT NULL, result_json TEXT NOT NULL, cache_hit INTEGER NOT NULL,
  selected_archive TEXT, created_at TEXT NOT NULL
);
-- 以下三张表是 v2 轨迹语义的增量迁移。它们故意不改写旧 individuals 表，
-- 因此历史 sqlite 文件在首次打开时只会新增表，不会丢失已有记录。
CREATE TABLE IF NOT EXISTS candidate_lifecycle (
  run_id TEXT NOT NULL, candidate_key TEXT NOT NULL,
  first_strict_generation INTEGER NOT NULL, first_strict_individual_id TEXT NOT NULL,
  PRIMARY KEY (run_id, candidate_key)
);
CREATE TABLE IF NOT EXISTS population_outcomes (
  run_id TEXT NOT NULL, individual_id TEXT NOT NULL, generation INTEGER NOT NULL,
  population_status TEXT NOT NULL, population_reason TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (run_id, individual_id)
);
CREATE TABLE IF NOT EXISTS archive_memberships (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, individual_id TEXT NOT NULL, generation INTEGER NOT NULL,
  stage TEXT NOT NULL, candidate_key TEXT NOT NULL,
  archive_membership TEXT NOT NULL, archive_reason TEXT NOT NULL,
  evaluation_status TEXT NOT NULL, first_strict_generation INTEGER,
  recorded_at TEXT NOT NULL,
  UNIQUE (run_id, individual_id, generation, stage)
);
CREATE TABLE IF NOT EXISTS run_terminations (
  run_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, reason TEXT NOT NULL,
  exact_evaluations INTEGER NOT NULL, completed_generation INTEGER NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_memberships_run_individual
  ON archive_memberships (run_id, individual_id, event_id);
"""


class TraceStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        # 完整候选结果的 JSON 较大；按代积累后用 executemany 写入，避免每一条
        # 都让 SQLite 重新准备一次 INSERT。缓冲内容只在调用 commit() 时落盘。
        self._pending_individual_rows: list[tuple[Any, ...]] = []

    @staticmethod
    def candidate_key(candidate: ThreePhaseDesignCandidate) -> str:
        """候选的稳定摘要键；避免在关系表重复存放大段 candidate_json。"""
        raw = json.dumps(candidate.canonical_dict(), ensure_ascii=False, sort_keys=True, default=str)
        return sha256(raw.encode("utf-8")).hexdigest()

    def close(self) -> None:
        # 故意不在 close 时自动 flush：异常中断时，未完成代的缓冲记录不应伪装成
        # 已完成、可复盘的一代；已完成代由调用方的 commit() 保证落盘。
        self.conn.close()

    def flush_individuals(self) -> None:
        """把当前代累计的个体记录批量写入当前事务。"""
        if not self._pending_individual_rows:
            return
        self.conn.executemany(
            "INSERT INTO individuals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._pending_individual_rows,
        )
        self._pending_individual_rows.clear()

    def commit(self) -> None:
        """先批量落个体记录，再提交一个已完成阶段的全部审计事实。"""
        self.flush_individuals()
        self.conn.commit()

    def create_run(self, run_id: str, seed: int, config_id: int, algorithm: str,
                   domain: dict[str, Any], settings: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, datetime.now(timezone.utc).isoformat(), seed, config_id, algorithm,
             json.dumps(domain, ensure_ascii=False, default=str), json.dumps(settings, ensure_ascii=False, default=str)),
        )
        self.commit()

    def merge_run_settings(self, run_id: str, updates: dict[str, Any]) -> None:
        """在运行结束后补写耗时等运行事实，不覆盖开始时的策略参数。"""
        row = self.conn.execute("SELECT settings_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"找不到需要更新的运行记录: {run_id}")
        try:
            settings = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"运行设置 JSON 无法读取: {run_id}") from exc
        settings.update(updates)
        self.conn.execute(
            "UPDATE runs SET settings_json = ? WHERE run_id = ?",
            (json.dumps(settings, ensure_ascii=False, default=str), run_id),
        )
        self.commit()

    def record_individual(self, individual_id: str, run_id: str, generation: int,
                          candidate: ThreePhaseDesignCandidate, result: EvaluationResult,
                          source: str, operator: dict[str, Any] | None = None,
                          parent_a_id: str | None = None, parent_b_id: str | None = None,
                          cache_hit: bool = False, selected_archive: str | None = None) -> None:
        self._pending_individual_rows.append(
            (individual_id, run_id, generation, parent_a_id, parent_b_id, source,
             json.dumps(operator, ensure_ascii=False, default=str) if operator else None,
             json.dumps(candidate.canonical_dict(), ensure_ascii=False, default=str),
             json.dumps(result.as_json_dict(), ensure_ascii=False), int(cache_hit), selected_archive,
             datetime.now(timezone.utc).isoformat()),
        )
        # 个体记录由调用方在“第 0 代完成”或“每代完成”时统一 commit。此前每条
        # 记录都 commit 一次，数千个候选会把大部分时间耗在 SQLite 文件同步上。

    def record_archive_snapshot(
        self, run_id: str, generation: int, strict_count: int, near_count: int,
        best_strict_cost: str | None, best_near_violation: str | None,
    ) -> None:
        """记录当代双档案状态，供多随机种子实验回放和收敛过程分析。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO archive_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, generation, strict_count, near_count, best_strict_cost, best_near_violation),
        )

    def record_population_outcomes(self, run_id: str, generation: int, outcomes: list[dict[str, Any]]) -> None:
        """写入本代真实进入种群或被算子分支舍弃的结果。

        当前 GA 并没有“先生成大批候选、再按容量截断”的步骤；因此只有真的发生
        容量截断时才允许写 ``population_capacity_eliminated``，不能把诊断探测点
        伪造为种群截断。
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR REPLACE INTO population_outcomes "
            "(run_id, individual_id, generation, population_status, population_reason, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, str(item["individual_id"]), generation, str(item["population_status"]),
                 str(item["population_reason"]), now)
                for item in outcomes
            ],
        )

    def mark_first_strict(
        self, run_id: str, candidate: ThreePhaseDesignCandidate, generation: int, individual_id: str,
    ) -> int:
        """原子地记录候选首次得到严格可行精算结果的代数，并返回该首次代数。"""
        key = self.candidate_key(candidate)
        self.conn.execute(
            "INSERT OR IGNORE INTO candidate_lifecycle "
            "(run_id, candidate_key, first_strict_generation, first_strict_individual_id) VALUES (?, ?, ?, ?)",
            (run_id, key, generation, individual_id),
        )
        row = self.conn.execute(
            "SELECT first_strict_generation FROM candidate_lifecycle WHERE run_id = ? AND candidate_key = ?",
            (run_id, key),
        ).fetchone()
        if row is None:
            raise RuntimeError("严格可行候选生命周期记录失败")
        return int(row[0])

    def record_archive_memberships(
        self, run_id: str, generation: int, stage: str, decisions: list[dict[str, Any]],
    ) -> None:
        """记录档案更新当时的真实成员资格和淘汰原因，不能由回放排序倒推。"""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            "INSERT OR REPLACE INTO archive_memberships "
            "(run_id, individual_id, generation, stage, candidate_key, archive_membership, archive_reason, "
            "evaluation_status, first_strict_generation, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, str(item["individual_id"]), generation, stage, str(item["candidate_key"]),
                    str(item["archive_membership"]), str(item["archive_reason"]),
                    str(item["evaluation_status"]), item.get("first_strict_generation"), now,
                )
                for item in decisions
            ],
        )

    def record_termination(
        self, run_id: str, generation: int, reason: str, exact_evaluations: int, *, completed_generation: bool,
    ) -> None:
        """写入唯一的运行终止事实，区分代数完成、停滞和预算硬截止。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO run_terminations "
            "(run_id, generation, reason, exact_evaluations, completed_generation, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id, generation, reason, exact_evaluations, int(completed_generation),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
