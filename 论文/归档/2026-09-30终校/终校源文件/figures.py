from pathlib import Path
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch

OUT=Path(__file__).parent/'figures'; OUT.mkdir(exist_ok=True)
REPO=next(p for p in Path(__file__).resolve().parents if (p/'ga').is_dir() and (p/'论文').is_dir())
DATA=REPO/'测试归档/2026-09-24测试/论文真实数据'
stats=json.loads((DATA/'05_统计汇总与作图数据/论文主比较统一统计复核-2026-09-27.json').read_text(encoding='utf-8'))['results']
plt.rcParams.update({'font.family':'sans-serif','font.sans-serif':['Microsoft YaHei'],'font.size':8,'axes.labelsize':8,'xtick.labelsize':8,'ytick.labelsize':8,'axes.linewidth':.6,'axes.unicode_minus':False,'mathtext.fontset':'stix','savefig.facecolor':'white'})
INK='#263746'; BLUE='#315B78'; GRAY='#A1ACB4'; GRID='#E5E9EC'
def save(fig,name):
    fig.savefig(OUT/(name+'.png'),dpi=600)
    plt.close(fig)
def canvas(h):
    fig=plt.figure(figsize=(3.15,h)); ax=fig.add_axes([0,0,1,1]); ax.set(xlim=(0,1),ylim=(0,1)); ax.axis('off'); return fig,ax
def box(ax,x,y,w,h,text,fs=8):
    ax.add_patch(Rectangle((x,y),w,h,facecolor='white',edgecolor=INK,linewidth=.7))
    ax.text(x+w/2,y+h/2,text,ha='center',va='center',fontsize=fs,linespacing=1.3,color=INK)
def line(ax,points):
    if len(points)>2: ax.plot(*zip(*points[:-1]),color=INK,lw=.7)
    ax.add_patch(FancyArrowPatch(points[-2],points[-1],arrowstyle='-|>',mutation_scale=6,lw=.7,color=INK,shrinkA=0,shrinkB=0))
def clean(ax):
    ax.spines[['top','right']].set_visible(False); ax.grid(axis='y',color=GRID,lw=.5); ax.set_axisbelow(True); ax.tick_params(length=2,pad=3)

# Engineering-level cycle, kept separate from the archive-level mechanism.
fig,ax=canvas(1.55)
for x,y,t in [(.025,.65,'页面配置\n冻结目录与价格'),(.385,.65,'候选合法化'),(.745,.65,'工程精算'),(.745,.17,'约束诊断'),(.385,.17,'双档案保留'),(.025,.17,'搜索更新')]: box(ax,x,y,.23,.23,t,7.8)
for points in [[(.255,.765),(.385,.765)],[(.615,.765),(.745,.765)],[(.86,.65),(.86,.40)],[(.745,.285),(.615,.285)],[(.385,.285),(.255,.285)],[(.14,.40),(.14,.51),(.50,.51),(.50,.65)]]: line(ax,points)
save(fig,'fig1')

# Schematic identifiers A/B denote examples, not fabricated business records.
fig,ax=canvas(2.05)
ax.text(.03,.95,'完整目录记录',weight='bold',va='top',fontsize=8.5)
box(ax,.03,.64,.94,.20,'A：型号 A · 规格 A · 绝缘 A · 价格 A\nB：型号 B · 规格 B · 绝缘 B · 价格 B',8.3)
box(ax,.03,.33,.94,.21,'无匹配记录的属性混拼\n规格 A ＋ 绝缘 B',8.3)
ax.text(.93,.435,'×',ha='right',va='center',fontsize=15,color=INK)
box(ax,.03,.04,.94,.20,'整体继承：完整记录 A 或 B\n保留记录关联，再合法化与精算',8.3)
save(fig,'fig2')

fig,ax=canvas(2.7)
box(ax,.28,.80,.55,.15,'工程精算与门控\n完整且可计算的候选',8)
box(ax,.14,.52,.36,.15,'严格可行档案 $A_f$\n$V(x)=0$',8)
box(ax,.60,.52,.36,.15,'近可行档案 $A_n$\n$V(x)>0$',8)
box(ax,.28,.28,.55,.13,'父代抽样',8.5)
box(ax,.28,.06,.55,.13,'交叉、变异与合法化',8.5)
line(ax,[(.43,.80),(.32,.67)]); line(ax,[(.67,.80),(.78,.67)])
line(ax,[(.32,.52),(.43,.41)]); line(ax,[(.78,.52),(.67,.41)])
line(ax,[(.555,.28),(.555,.19)])
line(ax,[(.28,.125),(.045,.125),(.045,.875),(.28,.875)])
save(fig,'fig3')

# Primary outcomes: actual archived data, vertically stacked for single-column print.
fig,axs=plt.subplots(2,1,figsize=(3.15,3.45)); x=np.arange(5); width=.34
for ax,keys,scale,ylabel,ylim,title in [(axs[0],('bga_success','mga_success'),100/30,'严格可行发现率（%）',113,'(a) 严格可行发现'),(axs[1],('bga_evolution_median','mga_evolution_median'),1,'真实精算调用中位数（次）',11000,'(b) 第 0 代后调用量')]:
    for off,key,col,name in [(-width/2,keys[0],GRAY,'BGA'),(width/2,keys[1],BLUE,'MGA')]: ax.bar(x+off,[r[key]*scale for r in stats],width,color=col,label=name,zorder=3)
    ax.set_xticks(x,list('ABCDE')); ax.set_ylim(0,ylim); ax.set_ylabel(ylabel); ax.set_title(title,loc='left',fontsize=8.5,pad=7); clean(ax)
axs[0].legend(frameon=False,ncol=2,fontsize=7.8,loc='upper center',bbox_to_anchor=(.67,1.27),columnspacing=.9,handlelength=1)
axs[0].set_yticks([0,25,50,75,100]); axs[1].set_yticks([0,2500,5000,7500,10000]); axs[1].set_xlabel('主比较场景')
fig.subplots_adjust(left=.23,right=.98,bottom=.11,top=.90,hspace=.70);save(fig,'fig4')

fig=plt.figure(figsize=(3.15,2.0)); ax=fig.add_axes([.23,.25,.72,.65]); ys=[2,1,0]
for y,r in zip(ys,stats[1:4]):
    lo,hi,est=[r[k] for k in ['hl_ci_lo_pct','hl_ci_hi_pct','hl_pct']]
    ax.hlines(y,lo,hi,color=BLUE,lw=1.1);ax.vlines([lo,hi],y-.06,y+.06,color=BLUE,lw=.7)
    # Small point, never shifted away from the true estimate to fake an interior CI.
    ax.vlines(est,y-.09,y+.09,color=BLUE,lw=1.7,zorder=4)
    ax.text(-12.3,y+.23,f'{est:.2f}%  [{lo:.2f}%, {hi:.2f}%]',fontsize=7.7,va='center',color=INK)
ax.axvline(0,color='#858F97',lw=.7); ax.set_xlim(-12.8,1.8);ax.set_ylim(-.35,2.52)
ax.set_yticks(ys,['B (n=14)','C (n=30)','D (n=12)']); ax.set_xticks([-12,-9,-6,-3,0]);ax.set_xlabel('标准化 HL 成本位移（%）',labelpad=5)
ax.spines[['top','right','left']].set_visible(False);ax.tick_params(length=2);ax.grid(axis='x',color=GRID,lw=.45);ax.set_axisbelow(True)
save(fig,'fig5')

fig,ax=plt.subplots(figsize=(3.15,1.95));x=np.arange(5)
for offset,vals,name,marker,color in [(-.055,[16.7,20,20,43.3,63.3],'场景 A','o','#9C6B3F'),(0,[60,63.3,80,96.7,70],'场景 B','s',BLUE),(.055,[40,100,100,100,100],'场景 D','^','#507D70')]: ax.scatter(x+offset,vals,s=17,label=name,marker=marker,color=color,zorder=3)
ax.set_xticks(x,['S0','S1','S2','S3','S4']);ax.set_ylim(-4,110);ax.set_yticks([0,25,50,75,100]);ax.set_ylabel('严格可行率（%）');clean(ax)
ax.legend(frameon=False,ncol=3,fontsize=7.3,loc='lower center',bbox_to_anchor=(.5,1.10),columnspacing=.75,handletextpad=.2,handlelength=.9)
fig.subplots_adjust(left=.19,right=.98,bottom=.16,top=.75);save(fig,'fig6')

rows=[]
for f in ['diversity_failure_summary.json','diversity_failure_runs_t2rad_runs_t2r429.json']: rows+=json.loads((DATA/'03_主比较_BGA_MGA'/f).read_text(encoding='utf-8'))['diversity']
ids=['S-429','S-387','S-409','S-469','S-487']
fig,axs=plt.subplots(2,1,figsize=(3.15,3.05))
for ax,field,title,ylabel,ylim in [(axs[0],'final_distance','(a) 最终工作种群','平均汉明距离',4.2),(axs[1],'signature_median','(b) 严格档案','完整记录签名数',15)]:
    for off,method,marker,color in [(-.08,'BGA','o',GRAY),(.08,'MGA','s',BLUE)]:
        vals=[next(r[field] for r in rows if r['scene']==s and r['method']==method and field in r) for s in ids]
        ax.scatter(np.arange(5)+off,vals,s=18,color=color,label=method,marker=marker,zorder=3)
    ax.set_xticks(np.arange(5),list('ABCDE'));ax.set_ylim(0,ylim);ax.set_ylabel(ylabel);ax.set_title(title,loc='left',fontsize=8.5,pad=6);clean(ax)
axs[0].legend(frameon=False,ncol=2,fontsize=7.8,loc='upper right',bbox_to_anchor=(1.02,1.36));axs[1].set_xlabel('主比较场景')
fig.subplots_adjust(left=.19,right=.98,bottom=.13,top=.87,hspace=.80);save(fig,'fig7')

fig,ax=canvas(1.1)
for x,y,w,t in [(.02,.54,.44,'13 条记录 × 32 字段'),(.54,.54,.44,'416 / 416 项一致'),(.02,.06,.96,'冻结输入下的 Java–Python 输出一致性')]:box(ax,x,y,w,.30,t,8)
line(ax,[(.46,.69),(.54,.69)]);save(fig,'figA1')
print(OUT)
