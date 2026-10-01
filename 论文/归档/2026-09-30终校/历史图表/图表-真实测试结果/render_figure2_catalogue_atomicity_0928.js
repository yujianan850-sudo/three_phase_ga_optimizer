const fs = require('fs');
const path = require('path');

const out = path.join(__dirname, '图2_完整目录记录与非法属性拼接.svg');
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const text = (x, y, lines, cls = 'body', anchor = 'middle') =>
  lines.map((line, i) => `<text x="${x}" y="${y + i * 50}" class="${cls}" text-anchor="${anchor}">${esc(line)}</text>`).join('');
const card = (x, y, w, h, fill, stroke, title, lines) => `
  <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" fill="${fill}" stroke="${stroke}" stroke-width="2"/>
  ${text(x + w / 2, y + 33, [title], 'title')}
  ${text(x + w / 2, y + 64, lines, 'body')}`;

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="900" height="500" viewBox="0 0 900 500">
  <style>
    .title{font:700 52px 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:#15263A}
    .body{font:40px 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:#31445A}
    .small{font:25px 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:#5C6E80}
    .ok{font:700 48px 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:#0F766E}
    .bad{font:700 46px 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;fill:#B42318}
  </style>
  <rect width="900" height="500" fill="#FFFFFF"/>
  <!-- 图题置于正文图注，图内只保留必要的可读标签。 -->
  ${card(45, 35, 250, 150, '#F5F9FD', '#5B7C99', '记录 A', ['规格·绝缘', '价格'])}
  ${card(605, 35, 250, 150, '#F5F9FD', '#5B7C99', '记录 B', ['规格·绝缘', '价格'])}
  <path d="M295 110 H340" stroke="#B42318" stroke-width="5" marker-end="url(#arrowRed)"/>
  <path d="M605 110 H560" stroke="#B42318" stroke-width="5" marker-end="url(#arrowRed)"/>
  <rect x="320" y="48" width="260" height="125" rx="12" fill="#FFF6F4" stroke="#C24138" stroke-width="3" stroke-dasharray="9 6"/>
  ${text(450, 105, ['拼接 ×'], 'bad')}
  ${text(450, 155, ['A宽 + B厚'], 'body')}
  <path d="M450 185 V260" stroke="#0F766E" stroke-width="5" marker-end="url(#arrowGreen)"/>
  <rect x="70" y="275" width="760" height="125" rx="14" fill="#F0FDFA" stroke="#0F766E" stroke-width="3"/>
  ${text(450, 335, ['整体继承  ✓'], 'ok')}
  ${text(450, 385, ['A / B  →  Φ  →  E'], 'body')}
  <text x="450" y="465" class="small" text-anchor="middle">图示仅说明编码原则：整条目录记录不可拆分。</text>
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#6B7A8B"/></marker>
    <marker id="arrowRed" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#B42318"/></marker>
    <marker id="arrowGreen" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#0F766E"/></marker>
  </defs>
</svg>`;
fs.writeFileSync(out, svg, 'utf8');
