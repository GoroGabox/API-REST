// Extrae los datos de un curso HTML interactivo (Cursos-Licencias-C-y-D) sin DOM.
//
// Uso: node curso_html_extract.js <archivo.html>   → JSON por stdout
//   {modules: [{id, group, title, html}], svg: {clave: "<svg…>"}, signs: {n: dataURI}, q: [...]}
//
// Solo se evalúa la parte de datos del <script> (hasta «Estado»), en un contexto
// vm vacío (sin require, process ni red) y con tiempo máximo. `fig(k, pie)` se
// reemplaza por un marcador <figure data-svg="k"> para que el conversor Python
// rasterice cada lámina una sola vez.
"use strict";
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync(process.argv[2], "utf8");
const m = html.match(/<script[^>]*>([\s\S]*?)<\/script>/);
if (!m) {
  console.error("sin <script>");
  process.exit(2);
}
let code = m[1];
const corte = code.indexOf("/* ---------- Estado");
if (corte < 0) {
  console.error("no se encontró el bloque «Estado» del script");
  process.exit(2);
}
code = code.slice(0, corte);
code = code.replace(
  /const fig=\(k,cap\)=>`[^\n]*`;/,
  "const fig=(k,cap)=>`<figure data-svg=\"${k}\"><figcaption>${cap}</figcaption></figure>`;"
);
code +=
  "\n;globalThis.__out = {modules: MODULES.map(x=>({id:x.id, group:x.group, title:x.title, html:x.html})), " +
  "svg: SVG, signs: SIGNS, q: Q};";

const ctx = vm.createContext({});
vm.runInContext(code, ctx, { timeout: 10000 });
process.stdout.write(JSON.stringify(ctx.__out));
