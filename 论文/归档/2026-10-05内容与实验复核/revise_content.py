"""Small evidence-bounded revisions; retain equations, fields, figures and template."""
from pathlib import Path
import json
from docx import Document
from lxml import etree
BASE=Path(__file__).resolve().parent
PAPER=BASE.parents[1]
src=next((BASE/'历史稿件').glob('*_作者占位优化版.docx'))
out=PAPER/src.name.replace('_作者占位优化版','_内容复核版')
assert out != src
doc=Document(src)
changes=[
('本文主比较结果来源于正式冻结后的运行轨迹；主实验冻结后独立开展Java与Python一致性核验及泛化边界检查，二者仅用于界定计算链一致性与适用边界，不参与主比较统计。','本文主比较依据归档的逐运行JSON汇总进行统计复核；原始候选级轨迹的保存限制见附录B。另行开展的Java与Python一致性核验及泛化边界检查，仅用于界定计算链一致性与适用边界，不参与主比较统计。'),
('只有满足后二者的候选','只有同时满足目录合法性和工程可行性的候选'),
('2.1 双档案与多样性维护','2.1 双档案管理与多样性统计'),
('因而不向目标配置引入结果标签信息。','不直接复用目标配置的结果标签；但排除目标配置ID并不保证相似历史方案与目标配置完全独立，历史信息迁移的影响仍需无历史种子对照检验。'),
('各场景使用固定随机种子。各场景不按容量','各场景使用固定随机种子。上述区间为未作多重性校正的逐场景区间，百分比换算分母固定为原样本中位数；显著性判断采用前述Holm校正p值。各场景不按容量'),
('不能据此单独推断首次发现严格解更快。','不能据此单独推断首次发现严格解更快或实际运行更快。归档wall_seconds中位数在场景D为BGA 51.44 s、MGA 67.74 s，在场景E为BGA 50.53 s、MGA 74.77 s；该记录未充分控制硬件负载、并发与计时范围，只作历史耗时描述，不作速度优势推断。'),
('不将严格成功率与成本中位数混合为统一性能指标。','不将严格成功率与成本中位数混合为统一性能指标。另按各方法全部严格成功运行作描述性补充：场景D中BGA与MGA的最低严格成本中位数分别为77 167.25元和72 131.00元，四分位区间分别为[74 837.88,81 041.63]元和[72 131.00,72 253.50]元；场景E中MGA的30次成功运行中位数为116 882.50元，四分位区间为[116 012.50,117 543.75]元，而BGA仅1次成功，成本为118 405.00元。此处样本集合不同，不作非配对优劣推断，也不替代表7的共同成功配对分析。'),
('后续工作将优先补记首次发现严格解所需真实精算次数、严格可行发现率-真实精算次数曲线，以及各方法全部严格成功运行的描述性成本分布；','后续工作将优先保存完整输入、版本与非空候选轨迹，补记首次发现严格解所需真实精算次数，并在相同实际精算预算下绘制严格可行发现率曲线；'),
('共416项逐项字段比较全部通过。','共416项逐项字段比较全部通过。416项为13条记录内的重复字段核对，不等于416个独立样本。'),
]
def replace(p,old,new):
    nodes=p._p.xpath('.//w:t'); text=''.join(n.text or '' for n in nodes)
    start=text.find(old)
    if start<0:return False
    end=start+len(old);pos=0;first=True
    for node in nodes:
        s=node.text or '';left=pos;right=pos+len(s);pos=right
        if right<=start or left>=end:continue
        node.text=s[:max(0,start-left)]+(new if first else '')+(s[max(0,end-left):] if end<right else '')
        first=False
    return True
for old,new in changes:
    old=old.replace('统一性能指标','统一优劣指标').replace('后续工作将优先补记首次发现严格解所需真实精算次数、严格可行发现率-真实精算次数曲线，以及各方法全部严格成功运行的描述性成本分布；','后续将优先补充首次发现严格解所需真实精算次数及严格可行发现率-真实精算次数曲线、各方法全部严格成功运行的描述性成本分布；').replace('共416项逐项字段比较全部通过。','共416项直接字段比较全部通过。')
    count=sum(replace(p,old,new) for p in doc.paragraphs)
    assert count==1,(old,count)
doc.save(out)
original=Document(src); final=Document(out)
assert [t._tbl.xml for t in final.tables]==[t._tbl.xml for t in original.tables]
assert [etree.tostring(e) for e in final.element.xpath('.//m:oMath')]==[etree.tostring(e) for e in original.element.xpath('.//m:oMath')]
assert [r.blob for r in final.part.related_parts.values() if hasattr(r,'image')]==[r.blob for r in original.part.related_parts.values() if hasattr(r,'image')]
assert [p.text for p in final.paragraphs if p.text.startswith('[')]==[p.text for p in original.paragraphs if p.text.startswith('[')]
(BASE/'文字修改核验.json').write_text(json.dumps({'output':str(out),'changes':changes,'tables_equations_images_references_unchanged':True},ensure_ascii=False,indent=2),encoding='utf-8')
print(out)
