from pathlib import Path
from copy import deepcopy
import re,json,argparse
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt,Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH,WD_BREAK
from docx.enum.section import WD_SECTION_START
from PIL import Image

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'ga').is_dir() and (p/'论文').is_dir())/'论文'
SRC=Path(__file__).resolve().parent.parent/'修订前原稿'/'目录约束双档案遗传优化及其在三相油浸式变压器离散设计中的基准测试研究.docx'
parser=argparse.ArgumentParser(description='Rebuild manuscript from archived source; no implicit overwrite.')
parser.add_argument('--output',required=True,type=Path)
OUT=parser.parse_args().output.resolve()
if OUT.exists():raise FileExistsError(f'Refusing to overwrite {OUT}')
ASSET=Path(__file__).parent/'figures'
d=Document(SRC); ps=d.paragraphs
equation6=deepcopy(ps[42]._p.find(qn('m:oMath')))
def font(r,size=9,bold=None):
    r.font.name='Times New Roman'; r.font.size=Pt(size)
    rf=r._r.get_or_add_rPr().rFonts;rf.set(qn('w:eastAsia'),'宋体')
    if bold is not None:r.bold=bold
def body(p):
    p.alignment=WD_ALIGN_PARAGRAPH.JUSTIFY
    pf=p.paragraph_format;pf.first_line_indent=Pt(18);pf.left_indent=Pt(0);pf.right_indent=Pt(0)
    pf.space_before=Pt(0);pf.space_after=Pt(0);pf.keep_with_next=False;pf.widow_control=True
    # Paragraphs containing long code identifiers can wrap instead of expanding Chinese spacing.
    pp=p._p.get_or_add_pPr(); wr=pp.find(qn('w:wordWrap'))
    if wr is None:wr=OxmlElement('w:wordWrap');pp.append(wr)
    wr.set(qn('w:val'),'1')
def text(p,s):
    p.clear();font(p.add_run(s));body(p)
def mr(s):
    e=OxmlElement('m:r'); pr=OxmlElement('w:rPr');rf=OxmlElement('w:rFonts')
    for key in ['ascii','hAnsi']:rf.set(qn('w:'+key),'Cambria Math')
    pr.append(rf);sz=OxmlElement('w:sz');sz.set(qn('w:val'),'18');pr.append(sz);e.append(pr)
    t=OxmlElement('m:t');t.text=s;e.append(t);return e
def sub(base,index):
    e=OxmlElement('m:sSub');b=OxmlElement('m:e');b.append(mr(base));s=OxmlElement('m:sub');s.append(mr(index));e.extend([b,s]);return e
mapping={'Af':('A','f'),'An':('A','n'),'A_f':('A','f'),'A_n':('A','n'),'Vmax':('V','max'),'V_max':('V','max'),'nv':('n','v'),'n_v':('n','v'),'P0':('P','0'),'P_0':('P','0'),'PK':('P','k'),'P_k':('P','k'),'UK':('U','k'),'U_k':('U','k'),'n_lv':('n','lv'),'d_H':('d','H'),'H_m':('H','m'),'N0':('N','0'),'N_0':('N','0'),'r_rb':('r','rb'),'p_single':('p','single'),'p_double':('p','double'),'p_cross':('p','cross'),'r_s':('r','s'),'Δ_s':('Δ','s'),'Δ_i':('Δ','i'),'Δ_j':('Δ','j'),'C_MGA,s':('C','MGA,s'),'C_BGA,s':('C','BGA,s'),'R_HL':('R','HL'),'l_i':('l','i'),'u_i':('u','i'),'y_i':('y','i'),'v_i':('v','i')}
pat=re.compile(r'(?<![A-Za-z0-9_])('+'|'.join(re.escape(x) for x in sorted(mapping,key=len,reverse=True))+r')(?![A-Za-z0-9_])')
def native_vars(p):
    # Operate on ordinary text runs only; existing OMML objects remain intact.
    for r in list(p.runs):
        if r._r.xpath('.//w:drawing'):continue
        s=r.text
        if not pat.search(s):continue
        cursor=0
        for m in pat.finditer(s):
            if m.start()>cursor:
                cp=deepcopy(r._r)
                for t in list(cp):
                    if t.tag!=qn('w:rPr'):cp.remove(t)
                t=OxmlElement('w:t');t.set(qn('xml:space'),'preserve');t.text=s[cursor:m.start()];cp.append(t);r._r.addprevious(cp)
            om=OxmlElement('m:oMath');om.append(sub(*mapping[m.group()]));r._r.addprevious(om);cursor=m.end()
        if cursor<len(s):
            cp=deepcopy(r._r)
            for t in list(cp):
                if t.tag!=qn('w:rPr'):cp.remove(t)
            t=OxmlElement('w:t');t.set(qn('xml:space'),'preserve');t.text=s[cursor:];cp.append(t);r._r.addprevious(cp)
        r._r.getparent().remove(r._r)

# Local prose edits. Experimental observations and reference entries are not rewritten.
ps[6].text='Catalogue-Constrained Dual-Archive Genetic Optimization and Benchmarking for Discrete Design of Three-Phase Oil-Immersed Transformers'
for r in ps[6].runs:font(r,10.5,True)
text(ps[32],'相关工作可分为三类。'+ps[32].text.split('相关工作可分为三类。',1)[1])
for r in ps[32].runs:r.text=r.text.replace('现行国家标准[12]','国家标准GB/T 1094.1—2013[12]')
text(ps[54],'本文主比较对象为目录约束基础遗传算法（BGA）和默认增强策略MGA-default（以下简称MGA）。'+ps[54].text.split('。',1)[1])
text(ps[38],'严格可行的判定和相应精算输出见表3。对任一具有下、上界[l_i,u_i]的约束，若实际值y_i低于下界，其归一化超限为(l_i−y_i)/max(|l_i|,1)；若高于上界，则为(y_i−u_i)/max(|u_i|,1)；在区间内为0。冷却波纹补偿项以油体积膨胀量减去3倍波纹补偿量，再除以油体积膨胀量与1的较大值，计算超限；满足补偿约束时该项为0。分母中的1用于避免约束基准值接近0时产生数值放大。该归一化量仅用于近可行候选的搜索排序和诊断优先级，不解释为不同物理约束严重程度的严格等价度量。对完整且可计算的候选，V(x)为各有效约束归一化超限之和，V_max(x)为其中最大值，n_v(x)为正超限项数；档案排序键见式（5）。complete与calculable分别刻画必要输出是否齐全、候选能否作为优化排序的计算结果，二者为不同门控维度，不以彼此互斥为前提；档案入口要求二者同时为True。complete=False表示候选未能得到P_0、P_k、U_k、温升、尺寸、重量和成本等完整必要输出，原因可包括目录或关联记录无法还原、条件派生失败或公式链中断；calculable=False表示候选不可作为优化排序的可计算结果，常见原因包括磁密表等计算数据缺口。两类候选均不进入A_n，完整性违规仅用于失败归因和审计。严格可行当且仅当候选完整可计算且V(x)=0。')
text(ps[61],'严格可行率的5个主比较场景构成一个Holm校正族；共同成功数不少于6的3个成本比较构成另一个Holm校正族。对同一种子s的共同严格成功配对，定义成本差Δ_s=C_MGA,s−C_BGA,s。双侧Wilcoxon符号秩检验与秩双列相关系数r_rb均基于成本差。Hodges–Lehmann（HL）位移估计H为所有(Δ_i+Δ_j)/2（i≤j）的中位数；文中报告的百分比效应为R_HL=100H/median(C_BGA,s)，即以共同成功子集的BGA成本中位数标准化，而非对逐种子成本变化率直接计算HL。95%置信区间由配对非参数bootstrap获得：以种子为单位重采样20 000次，每次重新计算成本差的HL，再以原共同成功子集的BGA成本中位数换算为百分比；各场景使用固定随机种子。各场景不按容量、候选数或共同成功样本量加权，跨场景叙述仅作描述性概括。所有参数在正式运行前冻结，未根据本轮结果回调。')
for p in ps:
    for r in p.runs:
        if not r._r.xpath('.//w:drawing|.//w:fldChar|.//w:instrText'):
            changed=r.text.replace('再经合法化函数合法化后','再经合法化函数处理后').replace('epsilon约束','ε约束')
            changed=changed.replace('Hodges-Lehmann成本变化率','标准化Hodges–Lehmann成本位移').replace('Hodges-Lehmann变化','标准化Hodges–Lehmann位移').replace('Hodges-Lehmann cost-change estimates','normalized Hodges–Lehmann cost-shift estimates')
            if changed!=r.text:r.text=changed

# Restore pictures after content edits by replacing the original image paragraphs in full.
figures={23:'fig1',29:'fig2',66:'fig4',70:'fig5',76:'fig6',102:'figA1'}
for idx,name in figures.items():
    p=ps[idx];p.clear();p.alignment=WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent=Pt(0);p.paragraph_format.keep_with_next=True
    p.paragraph_format.space_before=Pt(4);p.paragraph_format.space_after=Pt(2)
    p.paragraph_format.line_spacing=1
    p.add_run().add_picture(str(ASSET/(name+'.png')),width=Inches(3.15))
# The untouched Figure 8 drawing was preserved separately below.
original=Document(SRC)
ps[83]._p.getparent().replace(ps[83]._p,deepcopy(original.paragraphs[83]._p))
ps[42].text='图3 严格—近可行双档案的分流、父代抽样与循环更新。仅完整且可计算的候选参与档案排序。'
# Source paragraph 42 mixes equation (6), its picture and caption. Separate all three.
eq=ps[42].insert_paragraph_before();eq._p.append(equation6);eq.alignment=WD_ALIGN_PARAGRAPH.CENTER;eq.paragraph_format.first_line_indent=Pt(0)
pic=ps[42].insert_paragraph_before();pic.add_run().add_picture(str(ASSET/'fig3.png'),width=Inches(3.15));pic.alignment=WD_ALIGN_PARAGRAPH.CENTER;pic.paragraph_format.keep_with_next=True;pic.paragraph_format.first_line_indent=Pt(0)
ps[71].text='图5 共同成功配对的标准化HL成本位移与95%置信区间。粗竖线为估计值，横线为区间；n为共同成功种子数。'
ps[30].text='图2 完整目录记录整体继承与无匹配记录的属性混拼示意。A、B为示意记录，不代表具体业务数据。'
ps[24].text='图1 页面配置、冻结目录与价格驱动的工程优化闭环'
new=ps[78].insert_paragraph_before();new.add_run().add_picture(str(ASSET/'fig7.png'),width=Inches(3.15))
new.alignment=WD_ALIGN_PARAGRAPH.CENTER;new.paragraph_format.first_line_indent=Pt(0);new.paragraph_format.keep_with_next=True;new.paragraph_format.line_spacing=1
# Keep diversity figure inside its own subsection, immediately after the definition.
ps[82]._p.addnext(new._p);new._p.addnext(ps[78]._p)

for idx in [28,32,36,38,41,50,51,52,54,57,60,61,65,69,75,82,87,90,92,94,95,96,98,100,105,108]:
    body(ps[idx]);native_vars(ps[idx])
for p in d.paragraphs:
    t=p.text.strip();pf=p.paragraph_format
    for r in p.runs:
        rp=r._r.rPr
        if rp is not None:
            for tag in ['spacing','position','w','fitText']:
                for item in list(rp.findall(qn('w:'+tag))):rp.remove(item)
    if re.match(r'^(图\s*[1-8]|图\s*A1|表\s*\d+|表\s*[ABC]1|算法\s*[12])\s',t):
        p.alignment=WD_ALIGN_PARAGRAPH.CENTER;pf.first_line_indent=Pt(0);pf.space_before=Pt(3);pf.space_after=Pt(3);pf.keep_together=True
        pf.keep_with_next=t.startswith(('表','算法'))
        for r in p.runs:font(r,9,False)
    if p._p.xpath('.//w:drawing'):
        p.alignment=WD_ALIGN_PARAGRAPH.CENTER;pf.first_line_indent=Pt(0);pf.keep_with_next=True;pf.line_spacing=1
    if p.style.name.startswith('Heading'):
        pf.keep_with_next=True;pf.keep_together=True;pf.first_line_indent=Pt(0)
    if t=='参考文献':
        p.style='Heading 1';p.alignment=WD_ALIGN_PARAGRAPH.LEFT;pf.first_line_indent=Pt(0)
        for r in p.runs:font(r,10.5,True)
    if re.match(r'^\[\d+\]',t):
        p.alignment=WD_ALIGN_PARAGRAPH.LEFT;pf.left_indent=Pt(15);pf.first_line_indent=Pt(-15);pf.keep_with_next=False;pf.keep_together=True
        for r in p.runs:font(r,7.5)

for ti,tb in enumerate(d.tables):
    for ri,row in enumerate(tb.rows):
        trp=row._tr.get_or_add_trPr()
        if trp.find(qn('w:cantSplit')) is None:trp.append(OxmlElement('w:cantSplit'))
        if ri==0 and trp.find(qn('w:tblHeader')) is None:trp.append(OxmlElement('w:tblHeader'))
        for c in row.cells:
            for p in c.paragraphs:
                p.paragraph_format.keep_with_next=(ti in [0,1,2,5,7,8,9,10,11,13] and ri<len(tb.rows)-1)
                for r in p.runs:
                    font(r,7.5)
                    if 'epsilon' in r.text:r.text=r.text.replace('epsilon','ε')
                    if '成本HL变化率' in r.text:r.text=r.text.replace('成本HL变化率','标准化HL成本位移')
                # Match equation sizes inside 7.5 pt table cells.
                native_vars(p)
                for sz in p._p.xpath('.//m:oMath//w:sz'):sz.set(qn('w:val'),'15')

# Word's last justified lines ending in manual breaks must not be stretched.
compat=d.settings.element.find(qn('w:compat'))
if compat is None:compat=OxmlElement('w:compat');d.settings.element.append(compat)
if compat.find(qn('w:doNotExpandShiftReturn')) is None:compat.append(OxmlElement('w:doNotExpandShiftReturn'))
# Footer page numbering; preserve section geometry and existing headers.
for sec in d.sections:
    for p in sec.footer.paragraphs:
        if p.text.strip() or p._p.xpath('.//w:fldChar'):break
    else:
        p=sec.footer.paragraphs[0];p.alignment=WD_ALIGN_PARAGRAPH.CENTER
        r=p.add_run();font(r,7.5);field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'PAGE');p._p.append(field)
# A continuous trailing section balances the final reference columns without shrinking text.
d.add_section(WD_SECTION_START.CONTINUOUS)
alt=['工程优化闭环：输入、合法化、工程精算、约束诊断、双档案与搜索更新。','完整记录整体继承与无匹配记录的属性混拼对照。','双档案分流与父代抽样循环；完整且可计算候选才参与档案排序。','五个主比较场景的严格可行发现率及第0代后真实精算调用中位数。','B、C、D共同成功子集的标准化HL位移及95%置信区间；数值见表7与正文。','独立扩展批次A、B、D的预定义策略结果；完整五场景见表8。','五个主比较场景的工作种群汉明距离与严格档案签名数。','失败运行的主导约束构成；确切计数见表9。','13条记录、32字段，共416项Java–Python输出一致性核验。']
for shape,descr in zip(d.inline_shapes,alt):shape._inline.docPr.set('descr',descr)
d.save(OUT)
check=Document(OUT)
print(json.dumps({'output':str(OUT),'tables':len(check.tables),'images':len(check.inline_shapes),'references':len([p for p in check.paragraphs if re.match(r'^\[\d+\]',p.text)]),'math_objects':len(check.element.xpath('.//m:oMath'))},ensure_ascii=False))
assert len(check.inline_shapes)==9
assert len(check.tables)==14
assert len([p for p in check.paragraphs if re.match(r'^\[\d+\]',p.text)])==17
