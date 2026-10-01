# -*- coding: utf-8 -*-
"""单独输出的图 2 备选版；不写回论文。"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


OUT = Path(__file__).with_name("图2_目录记录整体继承_对照式重绘.png")
INK = "#263746"
NAVY = "#35627D"
RED = "#B5524B"
PALE_BLUE = "#F3F7FA"
PALE_RED = "#FCF4F3"

plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial Unicode MS"], "axes.unicode_minus": False})


def box(ax, x, y, w, h, text, *, face="white", edge=INK, fs=12, weight="normal", dashed=False):
    style = "round,pad=0.012,rounding_size=0.04"
    patch = FancyBboxPatch((x, y), w, h, boxstyle=style, facecolor=face, edgecolor=edge,
                           linewidth=1.15, linestyle="--" if dashed else "-")
    ax.add_patch(patch)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=INK if edge != RED else "#8D3E39", fontweight=weight, linespacing=1.45)


def arrow(ax, start, end, color=INK):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=12,
                                 linewidth=1.1, color=color))


fig, ax = plt.subplots(figsize=(7.4, 3.55))
ax.set_xlim(0, 14)
ax.set_ylim(0, 6)
ax.axis("off")

# 极简左右对照：避免字段枚举和多层大框在单栏缩放后溢出。
ax.text(0.45, 5.35, "不允许：字段拼接", color=RED, fontsize=14, fontweight="bold", va="center")
box(ax, 0.62, 3.55, 1.65, .88, "目录记录 A", edge="#8997A2", fs=11.5)
box(ax, 0.62, 2.05, 1.65, .88, "目录记录 B", edge="#8997A2", fs=11.5)
box(ax, 3.30, 2.52, 1.72, 1.35, "任意字段\n拼接", edge=RED, fs=12.8, weight="bold", dashed=True)
arrow(ax, (2.27, 3.98), (3.30, 3.38), RED)
arrow(ax, (2.27, 2.48), (3.30, 3.02), RED)
ax.text(5.28, 3.20, "×", fontsize=22, color=RED, fontweight="bold", ha="center", va="center")
ax.text(3.30, 1.35, "字段来自不同目录记录，无法回溯", fontsize=10.3, color="#8D3E39", ha="center")

ax.plot([6.95, 6.95], [1.10, 5.15], color="#D5DDE4", lw=1.0)
ax.text(7.35, 5.35, "允许：整条记录继承", color=NAVY, fontsize=14, fontweight="bold", va="center")
box(ax, 7.38, 2.73, 1.75, 1.00, "目录记录\nA 或 B", edge=NAVY, fs=11.5, weight="bold")
box(ax, 9.92, 2.73, 1.90, 1.00, "完整记录\n整体继承", edge=NAVY, fs=11.5, weight="bold")
box(ax, 12.42, 2.73, 1.15, 1.00, "合法化\n精算", edge=NAVY, fs=10.5, weight="bold")
arrow(ax, (9.13, 3.23), (9.92, 3.23), NAVY)
arrow(ax, (11.82, 3.23), (12.42, 3.23), NAVY)
ax.text(10.45, 1.35, "保留目录关联关系", fontsize=10.3, color="#4D6278", ha="center")

fig.subplots_adjust(left=.02, right=.99, top=.95, bottom=.08)
fig.savefig(OUT, dpi=600, facecolor="white")
plt.close(fig)
