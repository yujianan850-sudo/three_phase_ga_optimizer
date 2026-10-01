"""Draw only the audited retained summary, not reconstructed population data."""
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont

BASE=Path(__file__).resolve().parent
report=json.loads((BASE/'指标口径复核.json').read_text(encoding='utf-8'))
W,H=1600,1549
im=Image.new('RGB',(W,H),'white'); draw=ImageDraw.Draw(im)
FONT=Path('C:/Windows/Fonts/msyh.ttc')
LATIN=Path('C:/Windows/Fonts/times.ttf')
BLACK='#222222'; GRID='#e8ebee'; BLUE='#285879'; GRAY='#788894'
font=ImageFont.truetype(str(FONT),52)
titlefont=ImageFont.truetype(str(FONT),55)
tickfont=ImageFont.truetype(str(LATIN),57)

def label(x,y,s,f=font,fill=BLACK,anchor='mm'):
    draw.text((x,y),s,font=f,fill=fill,anchor=anchor)

def marker(x,y,method,size=15):
    if method=='BGA':
        draw.ellipse((x-size,y-size,x+size,y+size),fill=GRAY)
    else:
        draw.rectangle((x-size,y-size,x+size,y+size),fill=BLUE)

def ylabel(text,cy):
    box=Image.new('RGBA',(750,100),(255,255,255,0))
    ImageDraw.Draw(box).text((375,50),text,font=font,fill=BLACK,anchor='mm')
    box=box.rotate(90,expand=True)
    im.paste(box,(22,int(cy-box.height/2)),box)

marker(1080,70,'BGA');label(1115,70,'BGA',tickfont,anchor='lm')
marker(1340,70,'MGA');label(1375,70,'MGA',tickfont,anchor='lm')
for top,bottom,maximum,ticks,key,title,yl in [
    (195,665,5,[0,1,2,3,4,5],'distance_median','(a) 五字段候选距离','平均汉明距离'),
    (905,1375,15,[0,5,10,15],'signature_median','(b) 末个工作代严格档案','完整记录签名数')]:
    label(225,top-58,title,titlefont,anchor='lm')
    left,right=225,1550
    for v in ticks:
        y=bottom-18-(v/maximum)*(bottom-top-36)
        draw.line((left,y,right,y),fill=GRID,width=3)
        label(left-24,y,str(v),tickfont,anchor='rm')
    draw.line((left,top,left,bottom),fill=BLACK,width=3)
    draw.line((left,bottom,right,bottom),fill=BLACK,width=3)
    for j,scene in enumerate('ABCDE'):
        x=left+95+j*(right-left-190)/4
        label(x,bottom+46,scene,tickfont)
        for method,off in [('BGA',-22),('MGA',22)]:
            rec=next(r for r in report['panels'] if r['scene']==scene and r['method']==method)
            y=bottom-18-(rec[key]/maximum)*(bottom-top-36)
            marker(x+off,y,method)
    ylabel(yl,(top+bottom)/2)
label(875,1488,'主比较场景')
im.save(BASE/'图7-五字段描述性统计.png',dpi=(508,508))
print('Figure saved:',BASE/'图7-五字段描述性统计.png')
