"""V6.3 送外审前样式统一。

保留 V6.2 正文、图表与实验结论，只统一字号、缩进、标题、表格和参考文献，
并将若干被 ASCII 化的正式数学符号恢复为 Cambria Math 字符运行。
"""
from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt


ROOT = Path(r"D:\IdeaProject\faladi\three_phase_ga_optimizer")
SOURCE = ROOT / "论文" / "目录约束双档案遗传优化三相变压器_版式与引用优化版V6.2-送审前图表与字符终校版.docx"
OUTPUT = ROOT / "论文" / "目录约束双档案遗传优化三相变压器_版式与引用优化版V6.3-送外审样式与数学符号统一版.docx"

BODY_SIZE = 9.0
TABLE_SIZE = 7.5
REF_SIZE = 7.5
FIRST_LINE = Pt(18)


def set_fonts(run, size, east_asia="宋体", latin="Times New Roman", bold=None, math=False):
    """显式设置中西文字体，避免 Normal 样式继承导致的 10.5 pt 残留。"""
    run.font.size = Pt(size)
    run.font.name = "Cambria Math" if math else latin
    if bold is not None:
        run.bold = bold
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.rFonts
    if fonts is None:
        from docx.oxml import OxmlElement
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    fonts.set(qn("w:ascii"), "Cambria Math" if math else latin)
    fonts.set(qn("w:hAnsi"), "Cambria Math" if math else latin)
    fonts.set(qn("w:eastAsia"), "Cambria Math" if math else east_asia)
    fonts.set(qn("w:cs"), "Cambria Math" if math else latin)
    if math:
        rpr.set(qn("w:hint"), "default")
    else:
        rpr.set(qn("w:hint"), "eastAsia")


def set_para_body(paragraph, indent=True, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    paragraph.alignment = align
    fmt = paragraph.paragraph_format
    fmt.line_spacing = 1.15
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.first_line_indent = FIRST_LINE if indent else Pt(0)
    for run in paragraph.runs:
        set_fonts(run, BODY_SIZE)


def clear_content(paragraph):
    """清除段落内容但保留段落属性。"""
    p = paragraph._p
    for child in list(p):
        if child.tag != qn("w:pPr"):
            p.remove(child)


def add_text(paragraph, text, size=BODY_SIZE, bold=None):
    run = paragraph.add_run(text)
    set_fonts(run, size, bold=bold)
    return run


def add_math(paragraph, text, size=BODY_SIZE, subscript_parts=()):
    """以 Cambria Math run 插入正式数学字符；下标单独使用 Word 下标格式。"""
    run = paragraph.add_run(text)
    set_fonts(run, size, math=True)
    for start, end in subscript_parts:
        # 仅用于新建的单一公式文本；由调用方传入需下标的字符区间。
        pass
    return run


def add_math_parts(paragraph, parts, size=BODY_SIZE):
    """parts: [('text', text), ('math', text), ('sub', text)]。"""
    for kind, text in parts:
        run = paragraph.add_run(text)
        set_fonts(run, size, math=(kind != "text"))
        if kind == "sub":
            run.font.subscript = True


def rewrite(paragraph, parts, indent=True, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    clear_content(paragraph)
    set_para_body(paragraph, indent=indent, align=align)
    add_math_parts(paragraph, parts)


def text_parts(value):
    return [("text", value)]


def set_normal_style(doc):
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(BODY_SIZE)
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Times New Roman")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Times New Roman")
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.first_line_indent = FIRST_LINE


def normalize_headings(doc):
    for i, p in enumerate(doc.paragraphs):
        if p.style.name == "Heading 1":
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(5)
            p.paragraph_format.space_after = Pt(3)
            for r in p.runs:
                set_fonts(r, 10.5, east_asia="黑体", latin="Times New Roman", bold=True)
        elif p.style.name == "Heading 2":
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(2)
            for r in p.runs:
                set_fonts(r, 9.5, east_asia="黑体", latin="Times New Roman", bold=True)
        elif p.style.name == "Heading 3":
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(1)
            for r in p.runs:
                set_fonts(r, 9.0, east_asia="黑体", latin="Times New Roman", bold=True)


def normalize_tables(doc):
    def is_math_run(run):
        rpr = run._element.rPr
        if rpr is None or rpr.rFonts is None:
            return False
        return rpr.rFonts.get(qn("w:ascii")) == "Cambria Math"

    for table in doc.tables:
        for row_index, row in enumerate(table.rows):
            for cell in row.cells:
                cell.vertical_alignment = 1  # center
                for p in cell.paragraphs:
                    p.paragraph_format.space_before = Pt(0)
                    p.paragraph_format.space_after = Pt(0)
                    p.paragraph_format.line_spacing = 1.0
                    p.paragraph_format.first_line_indent = Pt(0)
                    # 单元格中的段落不强制横向居中，保留原有对齐以免破坏长文本列。
                    for r in p.runs:
                        set_fonts(
                            r,
                            TABLE_SIZE,
                            bold=(True if row_index == 0 else None),
                            math=is_math_run(r),
                        )


def normalize_captions_and_references(doc):
    for idx, p in enumerate(doc.paragraphs):
        text = p.text.strip()
        if text.startswith("图") or text.startswith("表") or text.startswith("算法"):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(1)
            for r in p.runs:
                set_fonts(r, 9.0)
        if text.startswith("注："):
            set_para_body(p, indent=False)
            for r in p.runs:
                set_fonts(r, TABLE_SIZE)
        if text.startswith("[") and text[1:2].isdigit():
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.left_indent = Pt(16)
            p.paragraph_format.first_line_indent = Pt(-16)
            for r in p.runs:
                set_fonts(r, REF_SIZE)


def normalize_body(doc):
    # 不含首页元数据、图表题、注释、参考文献和公式导语的正文段落。
    exempt = {0, 1, 2, 3, 4, 5, 6, 7, 8, 24, 26, 28, 29, 32, 38, 43, 45, 46,
              59, 63, 68, 72, 73, 74, 78, 79, 80, 81, 85, 86, 87, 102, 104, 107, 110, 111}
    for idx, p in enumerate(doc.paragraphs):
        if idx in exempt or p.style.name.startswith("Heading"):
            continue
        text = p.text.strip()
        if not text or (text.startswith("[") and text[1:2].isdigit()):
            continue
        set_para_body(p, indent=True)

    # 首页摘要与关键词：同为小五、无首行缩进，避免继承 Normal 的五号。
    for idx in (2, 4, 5, 7, 8):
        p = doc.paragraphs[idx]
        set_para_body(p, indent=False)
    doc.paragraphs[3].alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in doc.paragraphs[3].runs:
        set_fonts(r, 9.0, east_asia="黑体", bold=True)
    for r in doc.paragraphs[0].runs:
        set_fonts(r, 18.0, east_asia="黑体", bold=True)
    for r in doc.paragraphs[1].runs:
        set_fonts(r, 9.0)
    for r in doc.paragraphs[6].runs:
        set_fonts(r, 12.0, latin="Times New Roman", bold=True)


def restore_math_text(doc):
    # 第 1.1 节：Phi/Omega 恢复为 Cambria Math 字符运行。
    rewrite(doc.paragraphs[18], [
        ("text", "任一可搜索模块"), ("math", "m"),
        ("text", "的完整记录定义见式（1），其中"), ("math", "t_m"),
        ("text", "为导线或结构类别，"), ("math", "id_m"),
        ("text", "为目录记录标识，"), ("math", "a_m"),
        ("text", "为随记录绑定的几何、材料、绝缘、价格和工艺属性。候选"), ("math", "x"),
        ("text", "由铁芯、低压绕组、高压绕组、冷却记录及条件依赖变量构成，其中"),
        ("math", "r_c、r_l、r_h 和 r_k"), ("text", "为四个完整记录，"),
        ("math", "z"), ("text", "为由规则派生的变量向量。令"),
        ("math", "Φ(x,q,D)"), ("text", "表示合法化过程；只有当完整记录可由目录"),
        ("math", "D"), ("text", "唯一还原、页面范围"), ("math", "q"),
        ("text", "满足且"), ("math", "z=Φ(x,q,D)"), ("text", "时，候选才属于合法域"),
        ("math", "Ω(q,D)"), ("text", "。工程精算器"), ("math", "E(x;q,p)"),
        ("text", "在给定价格与计算参数快照"), ("math", "p"),
        ("text", "下返回性能、温升、重量和成本，完整优化问题由式（3）给出。"),
    ])

    # 表 3：集合成员关系恢复符号。
    cell = doc.tables[2].cell(3, 0)
    p = cell.paragraphs[0]
    rewrite(p, [
        ("text", "层数与结构依赖：低压箔材层数=匝数；低压扁线层数"),
        ("math", " n_lv∈{2,4}"),
        ("text", "；高压层数、油道均为页面许可档位。"),
    ], indent=False, align=p.alignment or WD_ALIGN_PARAGRAPH.LEFT)

    # 表 5：保留页面布局，恢复 N0、根号与上取整符号。
    cell = doc.tables[6].cell(4, 1)
    p = cell.paragraphs[0]
    rewrite(p, [
        ("math", "N=⌈6√N₀⌉"),
        ("text", "：347、319、666、747、703；锦标赛规模3；每代精英2条；严格档案优先。"),
    ], indent=False, align=p.alignment or WD_ALIGN_PARAGRAPH.LEFT)

    # 表 1 的符号列补回符号，避免“符号表”变成文字标签。
    symbol_updates = {
        (0, 1, 0): "Φ",
        (0, 1, 2): "κ",
        (0, 2, 0): "Ω",
    }
    for (table_i, row_i, cell_i), symbol in symbol_updates.items():
        c = doc.tables[table_i].cell(row_i, cell_i)
        if c.paragraphs:
            p = c.paragraphs[0]
            clear_content(p)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            add_math_parts(p, [("math", symbol)], size=TABLE_SIZE)


def main():
    doc = Document(SOURCE)
    set_normal_style(doc)
    normalize_headings(doc)
    normalize_body(doc)
    normalize_tables(doc)
    normalize_captions_and_references(doc)
    restore_math_text(doc)
    # 恢复公式/表格写入后，确保字体统一。
    normalize_tables(doc)
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
