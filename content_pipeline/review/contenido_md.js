/* contenido_md: parser del contrato de contenido de lecciones (docs/contenido_lecciones.md).
 * Port 1:1 de content_pipeline/processors/content_blocks.py (parse_blocks, parse_inline).
 * Fuente única en JS: la Brújula lo lleva embebido entre los marcadores contenido_md:inicio/fin
 * (un test verifica que sea idéntico) y la webapp y la app Expo lo copian.
 * Debe dar exactamente los bloques/tokens de content_pipeline/review/fixtures_contenido.json.
 */
const ContenidoMd = (() => {
  const TIPOS = { NORMA: "Norma y sanción", ATENCION: "Atención", DEBES_SABER: "Debes saber", DATO: "Dato", CONSEJO: "Consejo" };
  const FIG = /^\{\{\s*figura\s*:\s*([A-Za-z0-9_-]+)\s*\}\}$/;
  const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR = /^(?:-{3,}|\*{3,}|_{3,})$/;
  const FENCE = /^```/;
  const QUOTE = /^>\s?(.*)$/;
  const CALLOUT = /^\[!([A-Za-z_]+)\]\s*(.*)$/;
  const UL = /^[-*+]\s+(.*)$/;
  const OL = /^\d+[.)]\s+(.*)$/;
  const TABLE_TITLE = /^Tabla:\s*(.+)$/;
  const SEP_CELL = /^:?-+:?$/;

  const splitRow = (line) => {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  };
  const isRow = (line) => line.trim().startsWith("|");
  const isSep = (line) => {
    if (!isRow(line)) return false;
    const cells = splitRow(line);
    return cells.length > 0 && cells.every((c) => SEP_CELL.test(c.replace(/ /g, "")));
  };
  const align = (cell) => {
    const c = cell.replace(/ /g, "");
    if (c.startsWith(":") && c.endsWith(":")) return "center";
    if (c.endsWith(":")) return "right";
    if (c.startsWith(":")) return "left";
    return null;
  };
  const startsTable = (lines, i) => i + 1 < lines.length && isRow(lines[i]) && isSep(lines[i + 1]);
  const startsBlock = (lines, i) => {
    const s = lines[i].trim();
    return !s || FIG.test(s) || HEADING.test(s) || HR.test(s) || FENCE.test(s) || s.startsWith(">")
      || UL.test(s) || OL.test(s) || startsTable(lines, i) || (TABLE_TITLE.test(s) && startsTable(lines, i + 1));
  };

  function parseBlocks(md, enRecuadro = false) {
    const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    const n = lines.length;
    while (i < n) {
      const s = lines[i].trim();
      let m;
      if (!s) { i++; continue; }
      if (FENCE.test(s)) {
        let j = i + 1;
        while (j < n && !FENCE.test(lines[j].trim())) j++;
        blocks.push({ t: "code", text: lines.slice(i + 1, j).join("\n") });
        i = j + 1;
        continue;
      }
      if ((m = FIG.exec(s))) { blocks.push({ t: "figura", id: m[1] }); i++; continue; }
      if ((m = HEADING.exec(s))) { blocks.push({ t: "heading", level: m[1].length, text: m[2] }); i++; continue; }
      if (HR.test(s)) { blocks.push({ t: "hr" }); i++; continue; }
      if (s.startsWith(">")) {
        const body = [];
        while (i < n && lines[i].trim().startsWith(">")) { body.push(QUOTE.exec(lines[i].trim())[1]); i++; }
        m = enRecuadro ? null : CALLOUT.exec(body[0].trim());
        if (m) {
          blocks.push({ t: "callout", tipo: m[1].toUpperCase(), titulo: m[2].trim() || null,
            blocks: parseBlocks(body.slice(1).join("\n"), true) });
        } else {
          blocks.push({ t: "quote", blocks: parseBlocks(body.join("\n"), true) });
        }
        continue;
      }
      let titulo = null;
      if ((m = TABLE_TITLE.exec(s)) && startsTable(lines, i + 1)) { titulo = m[1].trim(); i++; }
      if (startsTable(lines, i)) {
        const header = splitRow(lines[i]);
        const al = splitRow(lines[i + 1]).map(align);
        const rows = [];
        i += 2;
        while (i < n && isRow(lines[i])) { rows.push(splitRow(lines[i])); i++; }
        blocks.push({ t: "table", titulo, align: al, header, rows });
        continue;
      }
      if (UL.test(s) || OL.test(s)) {
        const ordered = OL.test(s);
        const itemRe = ordered ? OL : UL;
        const items = [];
        while (i < n) {
          const raw = lines[i];
          const st = raw.trim();
          if ((m = itemRe.exec(st))) items.push(m[1].trim());
          else if (st && (raw[0] === " " || raw[0] === "\t") && items.length && !startsBlock(lines, i)) items[items.length - 1] += " " + st;
          else break;
          i++;
        }
        blocks.push({ t: "list", ordered, items });
        continue;
      }
      const para = [s];
      i++;
      while (i < n && !startsBlock(lines, i)) { para.push(lines[i].trim()); i++; }
      blocks.push({ t: "paragraph", text: para.join(" ") });
    }
    return blocks;
  }

  const INLINE = new RegExp(
    "`(?<code>[^`]+)`"
    + "|\\[(?<ltext>[^\\]]+)\\]\\((?<href>https?://[^)\\s]+)\\)"
    + "|\\*\\*(?<strong>.+?)\\*\\*|__(?<strong2>.+?)__"
    + "|\\*(?<em>[^*\\s][^*]*?)\\*|(?<![\\p{L}\\p{N}_])_(?<em2>[^_\\s][^_]*?)_(?![\\p{L}\\p{N}_])",
    "gu");

  function parseInline(text) {
    const s = String(text || "");
    const out = [];
    let pos = 0;
    for (const m of s.matchAll(INLINE)) {
      const g = m.groups;
      if (m.index > pos) out.push({ t: "text", v: s.slice(pos, m.index) });
      if (g.code !== undefined) out.push({ t: "code", v: g.code });
      else if (g.href !== undefined) out.push({ t: "link", v: g.ltext, href: g.href });
      else if ((g.strong ?? g.strong2) !== undefined) out.push({ t: "strong", v: g.strong ?? g.strong2 });
      else out.push({ t: "em", v: g.em ?? g.em2 });
      pos = m.index + m[0].length;
    }
    if (pos < s.length) out.push({ t: "text", v: s.slice(pos) });
    return out;
  }

  return { TIPOS, parseBlocks, parseInline };
})();
if (typeof module !== "undefined") module.exports = ContenidoMd;
