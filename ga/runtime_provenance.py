"""为 GA 轨迹和消融批次生成可比较的运行版本证据。"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def runtime_provenance(*, formula_revision: str | None, entrypoint: str) -> dict[str, Any]:
    """返回本次运行的公式版本、核心代码指纹与采集时间。

    指纹只覆盖会改变候选域、精算结果或 GA 行为的核心 Python 文件；它不是 Git
    提交号的替代品，但在当前工作区没有可用 Git 历史时，足以阻止不同公式/算子版本
    的轨迹被误并入同一批统计。
    """
    relative_paths = (
        "calculation/single_device_evaluator.py",
        "ga/gene_codec.py",
        "ga/optimizer_runtime.py",
        "ga/full_optimizer.py",
        "ga/trace_store.py",
        "main.py",
        "experiment_ui/app.py",
    )
    digest = sha256()
    files: list[dict[str, str]] = []
    for relative in relative_paths:
        path = PROJECT_ROOT / relative
        content = path.read_bytes()
        file_hash = sha256(content).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        files.append({"path": relative, "sha256": file_hash})
    return {
        "formula_revision": formula_revision or "unknown",
        "core_code_fingerprint": digest.hexdigest(),
        "core_source_files": files,
        "entrypoint": entrypoint,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    }
