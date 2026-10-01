# -*- coding: utf-8 -*-
"""单独输出的图 3 备选版；不写回论文。"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


OUT = Path(__file__).with_name("图3_双档案闭环_单独重绘.png")
INK = "#263746"
NAVY = "#35627D"
EDGE = "#637789"

plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial Unicode MS"], "axes.unicode_minus": False})


def box(ax, x, y, w, h, text, fs=12.2, linespacing=1.25):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.035",
                                facecolor="white", edgecolor=NAVY, linewidth=1.2))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=INK, linespacing=linespacing)


def arrow(ax, start, end, *, color=EDGE):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=12,
                                 linewidth=1.15, color=color))


fig, ax = plt.subplots(figsize=(7.4, 3.55))
ax.set_xlim(0, 14)
ax.set_ylim(0, 6)
ax.axis("off")

 # 期刊化的单色流程：判定后分流，汇合后产生新候选；没有菱形或交叉线。
box(ax, .45, 2.65, 2.18, .80, "工程精算候选")
box(ax, 3.35, 2.65, 1.52, .80, "约束判定", 12.0)
box(ax, 5.62, 4.12, 2.28, 1.06, "严格档案  A_f\nV(x)=0", 11.8)
box(ax, 5.62, .82, 2.28, 1.06, "近可行档案  A_n\nV(x)>0", 11.8)
box(ax, 9.15, 2.65, 1.82, .80, "父代抽样")
box(ax, 11.82, 2.65, 1.72, .80, "生成候选")

arrow(ax, (2.63, 3.05), (3.35, 3.05), color=NAVY)
arrow(ax, (4.87, 3.30), (5.62, 4.65), color=NAVY)
arrow(ax, (4.87, 2.80), (5.62, 1.35), color=NAVY)
arrow(ax, (7.90, 4.65), (9.15, 3.22), color=NAVY)
arrow(ax, (7.90, 1.35), (9.15, 2.88), color=NAVY)
arrow(ax, (10.97, 3.05), (11.82, 3.05), color=NAVY)

# 回路绕过主体，且不添加图内说明文字。
arrow(ax, (12.68, 2.65), (12.68, .38), color=EDGE)
arrow(ax, (12.68, .38), (1.48, .38), color=EDGE)
arrow(ax, (1.48, .38), (1.48, 2.65), color=EDGE)

fig.subplots_adjust(left=.02, right=.99, top=.95, bottom=.07)
fig.savefig(OUT, dpi=600, facecolor="white")
plt.close(fig)
