"use strict";

// Evidence source: 2026-09-28 testing summary.
// 13 reproducible three-phase records × 32 comparable fields = 416 direct
// Java--Python comparisons. All values matched exactly.
const fs = require("fs");
const path = require("path");
const sharp = require("sharp");

const outDir = __dirname;
const svgPath = path.join(outDir, "图4_JavaPython一致性核验_13记录.svg");
const pngPath = path.join(outDir, "图4_JavaPython一致性核验_13记录.png");

const groups = [
  { label: "Java 快照字段", value: 286 },
  { label: "方案字段", value: 117 },
  { label: "记录级总价", value: 13 },
];

function esc(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function card(x, title, value, note) {
  return `
    <rect x="${x}" y="70" width="230" height="270" rx="18" fill="#F0FDFA" stroke="#0F766E" stroke-width="2"/>
    <text x="${x + 115}" y="132" text-anchor="middle" class="card-title">${esc(title)}</text>
    <text x="${x + 115}" y="225" text-anchor="middle" class="card-value">${esc(value)}</text>
    <text x="${x + 115}" y="280" text-anchor="middle" class="card-note">${esc(note)}</text>`;
}

const bars = groups.map((g, i) => {
  const y = 102 + i * 75;
  const width = (g.value / 286) * 420;
  return `
    <text x="610" y="${y + 20}" class="label">${esc(g.label)}</text>
    <rect x="780" y="${y}" width="420" height="32" rx="7" fill="#D1FAE5"/>
    <rect x="780" y="${y}" width="${width.toFixed(1)}" height="32" rx="7" fill="#0F766E"/>
    <text x="${Math.min(1222, 780 + width + 18)}" y="${y + 22}" class="bar-value">${g.value}/${g.value}</text>`;
}).join("\n");

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="460" viewBox="0 0 1400 460">
  <style>
    .card-title { font-family: 'Microsoft YaHei', sans-serif; font-size: 28px; font-weight: 700; fill: #334155; }
    .card-value { font-family: 'Microsoft YaHei', sans-serif; font-size: 58px; font-weight: 700; fill: #0F766E; }
    .card-note { font-family: 'Microsoft YaHei', sans-serif; font-size: 21px; fill: #475569; }
    .section-title { font-family: 'Microsoft YaHei', sans-serif; font-size: 29px; font-weight: 700; fill: #0F172A; }
    .label { font-family: 'Microsoft YaHei', sans-serif; font-size: 23px; fill: #334155; }
    .bar-value { font-family: 'Arial', 'Microsoft YaHei', sans-serif; font-size: 22px; font-weight: 700; fill: #0F172A; }
    .footer { font-family: 'Microsoft YaHei', sans-serif; font-size: 19px; fill: #64748B; }
  </style>
  <rect width="1400" height="460" fill="#FFFFFF"/>
  ${card(48, "可逐位复现记录", "13 条", "每条 32 个可比字段")}
  ${card(306, "直接字段比较", "416 项", "字段差异 0 项")}
  <line x1="565" y1="55" x2="565" y2="355" stroke="#CBD5E1" stroke-width="2"/>
  <text x="610" y="60" class="section-title">字段组核验汇总</text>
  ${bars}
  <line x1="780" y1="334" x2="1200" y2="334" stroke="#CBD5E1" stroke-width="2"/>
  <text x="780" y="370" class="footer">22 个 Java 快照字段 + 9 个方案字段 + 1 个记录级总价</text>
  <text x="48" y="420" class="footer">注：统计范围仅为当前可逐位复现记录；该核验属于软件计算链一致性，不等同于实测或生产定型验证。</text>
</svg>`;

async function main() {
  fs.writeFileSync(svgPath, svg, "utf8");
  await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toFile(pngPath);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
