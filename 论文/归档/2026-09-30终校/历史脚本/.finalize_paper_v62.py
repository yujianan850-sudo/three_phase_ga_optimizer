"""送审前图 3 / 图 5 重绘与 DOCX 内嵌图替换。

仅替换论文中的两张已有图，不改变正文数据、图注、表格或页面结构。
图内一律采用 ASCII 科学计数法，避免 PDF 字体回退导致上标或负号缺字。
"""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from docx import Document


ROOT = Path(r"D:\IdeaProject\faladi\three_phase_ga_optimizer")
SOURCE = ROOT / "论文" / "目录约束双档案遗传优化三相变压器_版式与引用优化版V6.1-送审前字符与引用终校版.docx"
OUTPUT = ROOT / "论文" / "目录约束双档案遗传优化三相变压器_版式与引用优化版V6.2-送审前图表与字符终校版.docx"
ASSET_DIR = ROOT / "论文" / "图表-送审终校V6.2"

INK = "#19324D"
BLUE = "#2E5F89"
TEAL = "#167C74"
ORANGE = "#BE7A2F"
PALE_BLUE = "#F4F8FC"
PALE_TEAL = "#EEF8F6"
PALE_ORANGE = "#FFF7EE"
GRID = "#D9E3EC"

plt.rcParams.update({
    "font.sans-serif": ["Microsoft YaHei", "SimHei", "Arial Unicode MS"],
    "axes.unicode_minus": False,
    "font.size": 12,
})


def draw_box(ax, xy, width, height, title, subtitle, face, edge):
    x, y = xy
    patch = FancyBboxPatch(
        (x, y), width, height,
        boxstyle="round,pad=0.025,rounding_size=0.04",
        linewidth=2.0, edgecolor=edge, facecolor=face,
    )
    ax.add_patch(patch)
    ax.text(x + width / 2, y + height * 0.62, title,
            ha="center", va="center", fontsize=16, color=INK, fontweight="bold")
    ax.text(x + width / 2, y + height * 0.33, subtitle,
            ha="center", va="center", fontsize=10.5, color="#42576B")


def arrow(ax, start, end, color=BLUE, rad=0.0):
    ax.add_patch(FancyArrowPatch(
        start, end, arrowstyle="-|>", mutation_scale=15,
        linewidth=2.2, color=color,
        connectionstyle=f"arc3,rad={rad}",
    ))


def render_figure3(path: Path):
    """清晰、无交叉箭头的双档案循环图。"""
    fig, ax = plt.subplots(figsize=(12.2, 6.8))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 7)
    ax.axis("off")

    # 左：精算；中：严格/近可行分流；右：父代与新候选；下：更新回路。
    draw_box(ax, (0.45, 3.05), 2.05, 1.25,
             "候选工程精算", "性能、成本、约束向量", PALE_BLUE, BLUE)
    draw_box(ax, (3.45, 4.75), 2.50, 1.18,
             "严格可行档案  A_f", "V(x)=0；按成本保留", PALE_TEAL, TEAL)
    draw_box(ax, (3.45, 1.45), 2.50, 1.18,
             "近可行档案  A_n", "V(x)>0；按超限排序", PALE_ORANGE, ORANGE)
    draw_box(ax, (7.05, 3.05), 1.90, 1.25,
             "父代抽样", "A_f 优先；A_n 可参与", "#FFFFFF", BLUE)
    draw_box(ax, (10.00, 3.05), 1.55, 1.25,
             "新候选", "交叉、变异", PALE_BLUE, BLUE)
    draw_box(ax, (6.35, 0.35), 3.30, 0.90,
             "评价并更新", "档案更新、候选池注入与种群截断", "#F8FAFC", TEAL)

    arrow(ax, (2.50, 3.82), (3.45, 5.32))
    arrow(ax, (2.50, 3.52), (3.45, 2.02))
    arrow(ax, (5.95, 5.32), (7.05, 4.02))
    arrow(ax, (5.95, 2.02), (7.05, 3.34))
    arrow(ax, (8.95, 3.68), (10.00, 3.68))
    # 新候选先进入“评价并更新”；更新完成后从该框左侧返回精算入口。
    # 两段回路均从框边缘出入，避免箭头穿过文本框。
    arrow(ax, (10.78, 3.05), (9.65, 1.25), color=TEAL, rad=-0.14)
    arrow(ax, (6.35, 0.80), (2.50, 0.80), color=TEAL)
    arrow(ax, (2.50, 0.80), (2.50, 3.05), color=TEAL)

    ax.text(6.0, 6.55, "严格-近可行双档案的分流、父代抽样与循环更新",
            ha="center", va="center", fontsize=19, fontweight="bold", color=INK)
    ax.text(0.45, 6.05,
            "注：近可行档案仅保存完整且可计算的候选；记录不完整或不可计算候选不进入任何档案。",
            ha="left", va="center", fontsize=10, color="#586B7E")
    fig.tight_layout(pad=0.3)
    fig.savefig(path, dpi=360, facecolor="white")
    plt.close(fig)


def render_figure5(path: Path):
    """HL 成本变化率与 95% CI；避免使用无法稳定渲染的上标字符。"""
    rows = [
        ("场景 B", 14, 0.00, -2.83, 1.27, "0.00%", "Holm p=0.5176"),
        ("场景 C", 30, -0.83, -1.03, -0.83, "-0.83%", "Holm p=3.84e-5"),
        ("场景 D", 12, -7.02, -11.96, -3.75, "-7.02%", "Holm p=9.77e-4"),
    ]
    fig, ax = plt.subplots(figsize=(10.6, 6.3))
    y = [2, 1, 0]
    ax.axvline(0, color="#9BAEC1", linewidth=1.4, zorder=0)
    ax.text(0.12, 2.75, "无变化", color="#63768A", fontsize=11)
    for yi, (name, n, est, lo, hi, label, p) in zip(y, rows):
        ax.hlines(yi, lo, hi, color=TEAL, linewidth=3.2, zorder=2)
        ax.vlines([lo, hi], yi - .08, yi + .08, color=TEAL, linewidth=2.5, zorder=2)
        ax.scatter([est], [yi], s=95, color="#0D5C78", zorder=3)
        # 将文字置于点的上方或右方，避免与区间端点叠压。
        if name == "场景 D":
            x_text, ha = est + .35, "left"
        else:
            x_text, ha = est + .24, "left"
        ax.text(x_text, yi + .15, label, ha=ha, va="center",
                fontsize=14, color=INK, fontweight="bold")
        ax.text(x_text, yi - .16, p, ha=ha, va="center",
                fontsize=10.5, color="#536578")
        ax.text(-12.8, yi + .04, name, ha="left", va="center",
                fontsize=13, color=INK, fontweight="bold")
        ax.text(-12.8, yi - .25, f"共同严格成功 n={n}", ha="left", va="center",
                fontsize=10.5, color="#607388")

    ax.set_xlim(-13.2, 2.0)
    ax.set_ylim(-.55, 3.05)
    ax.set_yticks([])
    ax.set_xticks([-12, -9, -6, -3, 0, 2])
    ax.set_xlabel("Hodges-Lehmann 成本变化率（%）", fontsize=13, labelpad=10)
    ax.set_title("共同严格成功配对的成本变化率", loc="left",
                 fontsize=17, fontweight="bold", color=INK, pad=15)
    ax.text(-13.05, 2.98, "负值表示 MGA 的最低严格成本较低；点为 HL 估计，横线为 95% CI。",
            fontsize=10.5, color="#586B7E", va="top")
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#62768B")
    ax.tick_params(axis="x", labelsize=10.5, colors="#455A70")
    ax.grid(axis="x", color=GRID, linewidth=.75, zorder=0)
    fig.tight_layout(pad=.8)
    fig.savefig(path, dpi=360, facecolor="white")
    plt.close(fig)


def replace_image_blob(doc: Document, paragraph_index: int, image_path: Path):
    paragraph = doc.paragraphs[paragraph_index]
    rel_ids = paragraph._p.xpath(".//a:blip/@r:embed")
    if len(rel_ids) != 1:
        raise RuntimeError(f"paragraph {paragraph_index} expected one image, got {rel_ids}")
    part = doc.part.related_parts[rel_ids[0]]
    part._blob = image_path.read_bytes()


def main():
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    fig3 = ASSET_DIR / "图3_双档案循环更新_送审终校.png"
    fig5 = ASSET_DIR / "图5_HL成本变化率_送审终校.png"
    render_figure3(fig3)
    render_figure5(fig5)

    doc = Document(SOURCE)
    # 图3、图5分别位于正文段落 42、71；保留原有图片尺寸以稳定双栏分页。
    replace_image_blob(doc, 42, fig3)
    replace_image_blob(doc, 71, fig5)
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
