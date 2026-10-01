"""Order existing OOXML properties without changing content or formatting values."""
from pathlib import Path
from zipfile import ZipFile
from lxml import etree
import sys

def ordered(parent, names):
    ranks={name:i for i,name in enumerate(names)}
    elements=list(parent)
    for child in sorted(elements,key=lambda c:ranks.get(etree.QName(c).localname,1000)):
        parent.append(child)

def normalize(path):
    parts={}
    with ZipFile(path) as archive:
        records=archive.infolist()
        for item in records:parts[item.filename]=archive.read(item.filename)
    root=etree.fromstring(parts['word/styles.xml'])
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    for style in root.findall('w:style',ns):
        ordered(style,['name','aliases','basedOn','next','link','autoRedefine','hidden',
            'uiPriority','semiHidden','unhideWhenUsed','qFormat','locked','personal',
            'personalCompose','personalReply','rsid','pPr','rPr','tblPr','trPr','tcPr'])
    parts['word/styles.xml']=etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
    root=etree.fromstring(parts['word/document.xml'])
    for props in root.findall('.//w:tblPr',ns):
        ordered(props,['tblStyle','tblpPr','tblOverlap','bidiVisual','tblStyleRowBandSize',
            'tblStyleColBandSize','tblW','jc','tblCellSpacing','tblInd','tblBorders',
            'shd','tblLayout','tblCellMar','tblLook','tblCaption','tblDescription','tblPrChange'])
    parts['word/document.xml']=etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
    root=etree.fromstring(parts['word/settings.xml'])
    pictures=root.find('w:doNotAutoCompressPictures',ns)
    colors=root.find('w:clrSchemeMapping',ns)
    if pictures is not None and colors is not None:
        root.remove(pictures);colors.addnext(pictures)
    parts['word/settings.xml']=etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
    temporary=path.with_suffix('.normalized.tmp')
    with ZipFile(temporary,'w') as archive:
        for item in records:archive.writestr(item,parts[item.filename])
    temporary.replace(path)

if __name__=='__main__':
    normalize(Path(sys.argv[1]).resolve())
    print('Existing properties ordered; content and values unchanged.')
