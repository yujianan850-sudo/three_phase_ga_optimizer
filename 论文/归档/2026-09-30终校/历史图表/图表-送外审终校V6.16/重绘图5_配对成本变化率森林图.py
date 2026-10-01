# -*- coding: utf-8 -*-
"""单独输出的图 5 备选版；数据沿用论文表 7，不写回论文。"""
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


OUT = Path(__file__).with_name("图5_配对成本变化率_森林图重绘.png")
INK = "#263746"
BLUE = "#2C5B7D"
GRID = "#E5EBF0"
ZERO = "#98A5B1"

plt.rcParams.update({
    "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial Unicode MS"],
    "axes.unicode_minus": False,
    "font.size": 10.5,
})

# 表 7：HL 成本变化率与 95% CI；仅展示共同成功配对的 B/C/D 场景。
scenes = ["B  (n=14)", "C  (n=30)", "D  (n=12)"]
estimate = np.array([0.00, -0.83, -7.02])
lower = np.array([-2.83, -1.03, -11.96])
upper = np.array([1.27, -0.83, -3.75])
y = np.array([2, 1, 0])

fig, ax = plt.subplots(figsize=(7.0, 3.35))
fig.patch.set_facecolor("white")

# 轻量横向辅助线，不干扰置信区间。
for yi in y:
    ax.axhline(yi, color=GRID, lw=.8, zorder=0)
ax.axvline(0, color=ZERO, lw=1.0, ls="--", zorder=0)

# 森林图：横线为 95% CI，实心圆为 HL 点估计。
# 场景 C 的上界与点估计重合；该侧不画端帽，避免出现“点跑到区间外”的假象。
cap_half_height = .105
for yi, point, lo, hi in zip(y, estimate, lower, upper):
    ax.hlines(yi, lo, hi, color=BLUE, lw=1.65, zorder=2)
    if abs(lo - point) > 1e-9:
        ax.vlines(lo, yi - cap_half_height, yi + cap_half_height, color=BLUE, lw=1.65, zorder=2)
    if abs(hi - point) > 1e-9:
        ax.vlines(hi, yi - cap_half_height, yi + cap_half_height, color=BLUE, lw=1.65, zorder=2)
    ax.plot(point, yi, "o", ms=7.0, mfc=BLUE, mec=BLUE, zorder=3)

# 数值标签统一置于右侧，不与点估计、CI 或零线重叠。
for yi, value in zip(y, estimate):
    ax.text(1.65, yi, f"{value:.2f}%", va="center", ha="left", color=INK, fontsize=10.5)

ax.set_yticks(y, scenes, color=INK)
ax.set_ylim(-.55, 2.55)
ax.set_xlim(-13.0, 3.15)
ax.set_xticks([-12, -9, -6, -3, 0, 3])
ax.set_xlabel("Hodges–Lehmann 成本变化率（%）", color=INK, labelpad=7)

ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.spines["left"].set_color(INK)
ax.spines["bottom"].set_color(INK)
ax.tick_params(axis="x", colors=INK, length=3)
ax.tick_params(axis="y", colors=INK, length=0)

fig.subplots_adjust(left=.16, right=.97, top=.94, bottom=.22)
fig.savefig(OUT, dpi=600, facecolor="white")
plt.close(fig)
