from pathlib import Path
import sys
import pypdfium2 as pdfium
from PIL import Image, ImageOps, ImageDraw
p=Path(sys.argv[1]); out=Path(sys.argv[2]); out.mkdir(exist_ok=True,parents=True)
doc=pdfium.PdfDocument(p)
thumbs=[]
for i,page in enumerate(doc):
    image=page.render(scale=1.65).to_pil().convert('RGB')
    image.save(out/f'page-{i+1:02}.png')
    page.get_textpage().get_text_range()
    th=image.copy(); th.thumbnail((420,594))
    tile=Image.new('RGB',(440,624),'#ddd'); tile.paste(th,((440-th.width)//2,25)); ImageDraw.Draw(tile).text((10,5),str(i+1),fill='black'); thumbs.append(tile)
    (out/f'page-{i+1:02}.txt').write_text(page.get_textpage().get_text_range(),encoding='utf-8')
for j in range(0,len(thumbs),6):
    sheet=Image.new('RGB',(1320,1248),'white')
    for k,t in enumerate(thumbs[j:j+6]): sheet.paste(t,((k%3)*440,(k//3)*624))
    sheet.save(out/f'overview-{j//6+1}.png')
print('PDF pages:',len(doc))
