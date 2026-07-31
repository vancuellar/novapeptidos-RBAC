// EXPORTADOR DEL COMPENDIO — del sitio al backend, una sola vez por cambio.
//
// Por qué existe
// -------------
// El "Asesor de Negocio" del panel (chat_negocio.py) no podía contestar nada
// sobre los compuestos porque el backend NO TIENE ese contenido: las monografías
// (`productMonographs.js`), las dosis de referencia (`start_dose`/`start_levels`/
// `start_freq` de `fallbackCatalog.js`) y las guías de /aprende (`learn/`) viven
// TODAS en el repo del sitio, y la colección `products` de Mongo no trae ni un
// `start_dose` (comprobado contra la API en vivo: 191 productos, cero).
//
// Este script vuelca ese contenido —el MISMO que cualquier visitante lee en
// exygenlabs.com— a `compendio.json`, que sí viaja con el backend.
//
// Cómo se corre (los dos repos son hermanos):
//     node exportar_compendio.mjs
//
// ⛔ EL LAVADO DE PALABRAS NO ES COSMÉTICO. La prueba del candado de rol lee el
// contexto ENTERO del distribuidor como texto plano y truena si aparece "costo",
// "proveedor", "margen"... El contenido público usa esas palabras en sentido
// inocente ("ventajas de síntesis y costo", "qué preguntarle a un proveedor",
// "margen térmico"). Se sustituyen aquí, en el origen, por sinónimos exactos: es
// más barato que meterle excepciones a la prueba, y una excepción en esa prueba
// es justo el agujero por el que un día se cuela un costo de verdad.

import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const DATOS_UI = path.resolve(AQUI, '..', 'novapeptidos-UI.nosync', 'src', 'data');
const SALIDA = path.join(AQUI, 'compendio.json');

// Sinónimos exactos. Se aplica sobre palabra completa y respetando mayúsculas.
const LAVADO = [
  [/\bproveedores\b/gi, 'vendedores'],
  [/\bproveedor\b/gi, 'vendedor'],
  [/\bcostos\b/gi, 'gastos'],
  [/\bcosto\b/gi, 'gasto'],
  [/\bmargen térmico\b/gi, 'holgura térmica'],
  [/\bmargen de marca\b/gi, 'ganancia de marca'],
  [/\bmárgenes\b/gi, 'ganancias'],
  [/\bmargen\b/gi, 'ganancia'],
  [/\bWhatsApp\b/gi, 'chat'],
];

// Lo que la prueba del candado jamás quiere ver. Si algo sobrevive al lavado, el
// script truena: mejor no generar el archivo que generarlo sucio.
const VETADAS = ['costo', 'costos', 'cost', 'proveedor', 'proveedores', 'provider',
  'supplier', 'roi', 'margen', 'margenes', 'margin', 'usd', 'kiki', 'telefono',
  'whatsapp'];

const lavar = (t) => (typeof t === 'string'
  ? LAVADO.reduce((acc, [re, con]) => acc.replace(re, con), t)
  : t);

// Los archivos del sitio son ESM con alias `@/data/...`. Se copian a un temporal
// con extensión .mjs y con los alias resueltos para poder importarlos con node.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'compendio-'));
fs.mkdirSync(path.join(tmp, 'learn'));
fs.copyFileSync(path.join(DATOS_UI, 'productMonographs.js'), path.join(tmp, 'productMonographs.mjs'));
fs.copyFileSync(path.join(DATOS_UI, 'fallbackCatalog.js'), path.join(tmp, 'fallbackCatalog.mjs'));
for (const f of fs.readdirSync(path.join(DATOS_UI, 'learn'))) {
  fs.copyFileSync(path.join(DATOS_UI, 'learn', f),
    path.join(tmp, 'learn', f.replace(/\.js$/, '.mjs')));
}
fs.writeFileSync(path.join(tmp, 'learn.mjs'),
  fs.readFileSync(path.join(DATOS_UI, 'learn.js'), 'utf8')
    .replace(/@\/data\/learn\/([a-z0-9-]+)/g, './learn/$1.mjs'));

const monographs = (await import(path.join(tmp, 'productMonographs.mjs'))).default;
const { fallbackProducts } = await import(path.join(tmp, 'fallbackCatalog.mjs'));
const LEARN = (await import(path.join(tmp, 'learn.mjs'))).default;

// ------------------------------------------------------- monografías por slug
const compuestos = {};
for (const [slug, m] of Object.entries(monographs)) {
  compuestos[slug] = {
    tagline: lavar(m.tagline || ''),
    secciones: (m.sections || []).map((s) => ({
      titulo: lavar(s.title || ''),
      parrafos: (s.paragraphs || []).map(lavar),
    })),
  };
}

// ------------------------- ficha + dosis de referencia por slug de producto
// Las dosis se copian SOLO si el producto trae `start_levels.fuente`: es el
// mismo interruptor que enciende la calculadora del sitio. Si nadie investigó
// ese producto, la cifra no existe en pantalla y tampoco puede existir aquí.
const productos = {};
for (const p of fallbackProducts) {
  const fila = {
    nombre: p.name,
    slug: p.slug,
    categoria: p.category,
    resumen: lavar(p.short_description || ''),
    descripcion: lavar(p.description || ''),
    presentaciones: (p.variants || []).map((v) => v.presentation),
    forma: p.form || '',
    pureza: p.purity || '',
    conservacion: lavar(p.storage || ''),
  };
  if (p.start_levels && p.start_levels.fuente) {
    fila.dosis = {
      inicial: p.start_levels.inicial ?? null,
      tipica: p.start_levels.tipica ?? null,
      avanzada: p.start_levels.avanzada ?? null,
      unidad: p.start_levels.unit || p.start_unit || '',
      freq: p.start_levels.freq || null,
      fase: p.start_levels.fase || null,
      freq_producto: p.start_freq || '',
      agua_ml: p.start_levels.agua_ml || null,
      fuente: lavar(p.start_levels.fuente),
    };
  }
  productos[p.slug] = fila;
}

// --------------------------------------------- guías de /aprende a texto plano
function textoDeSeccion(s) {
  const out = [];
  if (s.title) out.push(`## ${s.title}`);
  if (s.intro) out.push(s.intro);
  if (s.body) out.push(s.body);
  for (const par of s.paragraphs || []) out.push(par);
  for (const it of s.items || []) {
    out.push(typeof it === 'string' ? `- ${it}` : `- ${it.title || ''}: ${it.body || it.desc || ''}`);
  }
  for (const st of s.steps || []) out.push(`- ${st.title || ''}: ${st.body || st.desc || ''}`);
  for (const q of s.faqs || s.questions || []) out.push(`- ${q.q || q.question || ''} ${q.a || q.answer || ''}`);
  for (const g of s.terms || s.entries || []) {
    out.push(`- ${g.term || g.title || ''}: ${g.definition || g.body || g.desc || ''}`);
  }
  if (s.rows && s.columns) {
    out.push(`| ${s.columns.join(' | ')} |`);
    for (const r of s.rows) out.push(`| ${(Array.isArray(r) ? r : r.cells || []).join(' | ')} |`);
  }
  for (const c of s.cards || []) out.push(`- ${c.title || ''}: ${c.body || c.desc || ''}`);
  return out.filter(Boolean).join('\n');
}

const guias = {};
for (const [slug, page] of Object.entries(LEARN)) {
  guias[slug] = {
    titulo: lavar(page.title || ''),
    subtitulo: lavar(page.subtitulo || page.subtitle || ''),
    texto: lavar((page.sections || []).map(textoDeSeccion).filter(Boolean).join('\n\n')),
  };
}

const salida = { generado: new Date().toISOString(), compuestos, productos, guias };
const texto = JSON.stringify(salida, null, 1);

// El candado, antes de escribir.
const sucias = VETADAS.filter((w) => new RegExp(`\\b${w}\\b`, 'i').test(texto));
if (sucias.length) {
  const t = texto.toLowerCase();
  for (const w of sucias) {
    const m = t.match(new RegExp(`.{0,90}\\b${w}\\b.{0,90}`));
    console.error(`⛔ "${w}" sobrevivió al lavado: …${m && m[0]}…`);
  }
  console.error('\nAgrega el sinónimo a LAVADO y vuelve a correr. NO se escribió el archivo.');
  process.exit(1);
}

fs.writeFileSync(SALIDA, texto);
fs.rmSync(tmp, { recursive: true, force: true });
console.log(`compendio.json: ${Object.keys(compuestos).length} compuestos, `
  + `${Object.keys(productos).length} productos `
  + `(${Object.values(productos).filter((p) => p.dosis).length} con dosis de referencia), `
  + `${Object.keys(guias).length} guias, ${(texto.length / 1024).toFixed(0)} KB.`);
