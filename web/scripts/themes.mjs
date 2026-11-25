#!/usr/bin/env node
/* Stashify theme generator.
 *
 * Input : themes/source/*.json  (official palette hexes + a slot mapping; or literal house roles)
 * Output: src/styles/themes.generated.css   one --pal-* block per theme, applied with [data-theme="<id>"]
 *         src/lib/themes/themes.generated.ts tiny metadata for the picker and the inline boot script
 *         themes/contrast-report.txt         theme x role-pair contrast table (composited surfaces)
 *
 * A palette's hexes are never edited. Only the ROLE MAPPING is tuned: which official colour fills which
 * Lucent role, and, where no official colour can reach the contrast floor, a lightness-only shift (OKLCH L,
 * hue and chroma kept) that is logged as "tuned" in the report.
 *
 * Contrast floors (Lucent): 4.5:1 text, 3:1 non-text (glyphs, accent, switch tracks). Ratios are computed on
 * COMPOSITED surfaces: canvas, accent glow, sheet (also over the worst-case backdrop), glass and raised glass
 * (over canvas and over sheet). `--check` exits non-zero when any floor fails.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SRC_DIR = path.join(ROOT, 'themes/source');
const OUT_CSS = path.join(ROOT, 'src/styles/themes.generated.css');
const OUT_TS = path.join(ROOT, 'src/lib/themes/themes.generated.ts');
const OUT_REPORT = path.join(ROOT, 'themes/contrast-report.txt');
const CHECK = process.argv.includes('--check');
const ALL = process.argv.includes('--all');

/* Gallery order. `lucent-*` are the house defaults; the rest are popular published palettes. */
const ORDER = {
  dark: ['lucent-dark', 'rose-pine', 'rose-pine-moon', 'catppuccin-mocha', 'catppuccin-macchiato', 'catppuccin-frappe',
    'tokyo-night', 'tokyo-night-storm', 'dracula', 'nord', 'gruvbox-dark', 'everforest-dark', 'solarized-dark',
    'one-dark', 'monokai', 'night-owl', 'ayu-mirage', 'github-dark', 'kanagawa-wave', 'nightfox',
    'material-palenight', 'synthwave-84', 'oxocarbon-dark', 'flexoki-dark', 'modus-vivendi', 'selenized-dark',
    'ayu-dark', 'kanagawa-dragon', 'papercolor-dark'],
  light: ['lucent-light', 'rose-pine-dawn', 'catppuccin-latte', 'tokyo-night-day', 'alucard', 'gruvbox-light',
    'everforest-light', 'solarized-light', 'one-light', 'night-owl-light', 'ayu-light', 'github-light',
    'kanagawa-lotus', 'dayfox', 'dawnfox', 'material-lighter', 'oxocarbon-light', 'flexoki-light',
    'modus-operandi', 'papercolor-light', 'selenized-light', 'nord-light'],
};
/* Shipped set: the house theme + up to 20 popular palettes per scheme. nord-light is a Snow Storm mapping of the
 * Nord colours, not a published light theme, so it is left out. */
const SHIP_MAX = 21;
const DROP = new Set(['nord-light']);

/* ---------------------------------------------------------------- colour maths */
const WHITE = [255, 255, 255], BLACK = [0, 0, 0];
const M = 0.03; // safety margin on every floor so 8-bit rounding in the browser cannot tip a pass into a fail
const clamp = (v, a = 0, b = 255) => Math.min(b, Math.max(a, v));
const rnd = (c) => c.map((v) => Math.round(clamp(v)));
const q = (a) => Math.round(a * 255) / 255;
const hexRgb = (h) => { h = h.replace('#', ''); return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)); };
const hexAlpha = (h) => { h = h.replace('#', ''); return h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1; };
const toHex = (c, a = 1) => '#' + c.map((v) => Math.round(clamp(v)).toString(16).padStart(2, '0')).join('').toUpperCase()
  + (a < 1 ? Math.round(a * 255).toString(16).padStart(2, '0').toUpperCase() : '');
const mix = (a, b, t) => rnd(a.map((v, i) => v + (b[i] - v) * t));
const over = (fg, a, bg) => fg.map((v, i) => v * a + bg[i] * (1 - a));
const lin = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
const lum = (c) => 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
const cr = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const minCR = (c, list) => Math.min(...list.map((s) => cr(c, s)));

function rgb2oklab(c) {
  const [r, g, b] = c.map(lin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}
function oklab2lin([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3, m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3,
    s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s];
}
const gam = (v) => (v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055);
function oklabToRgb(L, a, b) {
  const inGamut = (k) => oklab2lin([L, a * k, b * k]).every((v) => v >= -1e-4 && v <= 1 + 1e-4);
  let k = 1;
  if (!inGamut(1)) { let lo = 0, hi = 1; for (let i = 0; i < 22; i++) { const mid = (lo + hi) / 2; if (inGamut(mid)) lo = mid; else hi = mid; } k = lo; }
  return rnd(oklab2lin([L, a * k, b * k]).map((v) => gam(clamp(v, 0, 1)) * 255));
}
/* Smallest lightness shift (OKLCH L; hue and chroma kept) that satisfies `ok`. dir +1 = lighter, -1 = darker. */
function tune(c, ok, dir) {
  if (ok(c)) return { c, d: 0 };
  const [L, a, b] = rgb2oklab(c);
  for (let d = 0.005; d <= 1; d += 0.005) {
    const L2 = L + dir * d;
    if (L2 < 0 || L2 > 1) break;
    const c2 = oklabToRgb(L2, a, b);
    if (ok(c2)) return { c: c2, d: Math.round(d * 1000) / 1000 };
  }
  return { c: dir > 0 ? WHITE : BLACK, d: null };
}
function rgb2lab(c) {
  const [r, g, b] = c.map(lin);
  const X = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047, Y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b,
    Z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / 1.08883;
  const f = (t) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
  return [116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))];
}
function dE00([L1, a1, b1], [L2, a2, b2]) { // CIEDE2000 (Sharma, Wu, Dalal)
  const rad = Math.PI / 180, deg = 180 / Math.PI, p7 = 25 ** 7;
  const C1 = Math.hypot(a1, b1), C2 = Math.hypot(a2, b2), Cb = (C1 + C2) / 2;
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + p7)));
  const a1p = (1 + G) * a1, a2p = (1 + G) * a2, C1p = Math.hypot(a1p, b1), C2p = Math.hypot(a2p, b2);
  const h1p = C1p === 0 ? 0 : (Math.atan2(b1, a1p) * deg + 360) % 360, h2p = C2p === 0 ? 0 : (Math.atan2(b2, a2p) * deg + 360) % 360;
  const dLp = L2 - L1, dCp = C2p - C1p;
  let dhp = 0;
  if (C1p * C2p !== 0) { dhp = h2p - h1p; if (dhp > 180) dhp -= 360; else if (dhp < -180) dhp += 360; }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp * rad) / 2), Lbp = (L1 + L2) / 2, Cbp = (C1p + C2p) / 2;
  let hbp = h1p + h2p;
  if (C1p * C2p !== 0) hbp = Math.abs(h1p - h2p) <= 180 ? (h1p + h2p) / 2 : (h1p + h2p + (h1p + h2p < 360 ? 360 : -360)) / 2;
  const T = 1 - 0.17 * Math.cos((hbp - 30) * rad) + 0.24 * Math.cos(2 * hbp * rad) + 0.32 * Math.cos((3 * hbp + 6) * rad) - 0.2 * Math.cos((4 * hbp - 63) * rad);
  const dth = 30 * Math.exp(-(((hbp - 275) / 25) ** 2)), Rc = 2 * Math.sqrt(Cbp ** 7 / (Cbp ** 7 + p7));
  const Sl = 1 + (0.015 * (Lbp - 50) ** 2) / Math.sqrt(20 + (Lbp - 50) ** 2), Sc = 1 + 0.045 * Cbp, Sh = 1 + 0.015 * Cbp * T;
  const Rt = -Math.sin(2 * dth * rad) * Rc;
  return Math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh));
}

/* ---------------------------------------------------------------- surfaces */
function surfaces(R, accent, dark) {
  const C = R.canvas[0], back = dark ? WHITE : BLACK;
  const sheet = over(R.sheet[0], R.sheet[1], C);
  return {
    canvas: C, glow: mix(C, accent, 0.15), sheet, sheetW: over(R.sheet[0], R.sheet[1], back),
    glass: over(R.glass[0], R.glass[1], C), glassS: over(R.glass[0], R.glass[1], sheet),
    raised: over(R.glassRaised[0], R.glassRaised[1], C), raisedS: over(R.glassRaised[0], R.glassRaised[1], sheet),
  };
}

function searchInk(fg, ext, list, comfort, pre) {
  const minOver = (ink, a) => Math.min(...list.map((s) => cr(over(ink, q(a), s), s)));
  const a2s = [...(pre ? [pre.a2] : []), 0.6, 0.66, 0.72, 0.78, 0.84, 0.9];
  const a3s = [...(pre ? [pre.a3] : []), 0.36, 0.42, 0.48, 0.54, 0.6, 0.66];
  for (let t = 0; t <= 0.95; t += 0.025) {
    const ink = t ? mix(fg, ext, t) : fg;
    if (minCR(ink, comfort) < 7 && t < 0.95) continue; // comfort target: 7:1 on the everyday surfaces, 4.5:1 floor everywhere
    if (Math.min(...comfort.map((s) => cr(over(ink, q(0.6), s), s))) < 3 + M && t < 0.95) continue; // switch tracks are ink at 60%: 3:1
    const a2 = a2s.find((a) => minOver(ink, a) >= 5.2) ?? a2s.find((a) => minOver(ink, a) >= 4.5 + M);
    if (a2 === undefined) continue;
    const a3 = a3s.find((a) => a <= a2 - 0.08 && minOver(ink, a) >= 3 + M);
    if (a3 === undefined) continue;
    return { ink, t: Math.round(t * 1000) / 1000, a2: q(a2), a3: q(a3) };
  }
  return null;
}

/* ---------------------------------------------------------------- derive roles for one palette */
function derive(p) {
  const dark = p.scheme === 'dark', ext = dark ? WHITE : BLACK, dir = dark ? 1 : -1;
  const notes = [];
  const lit = p.roles ? Object.fromEntries(Object.entries(p.roles).map(([k, v]) => [k, [hexRgb(v), hexAlpha(v)]])) : null;
  const S = p.slots ? Object.fromEntries(Object.entries(p.slots).map(([k, n]) => [k, n == null ? null : hexRgb(p.official[n])])) : null;
  const R = {};
  let bg;
  if (lit) {
    for (const k of ['canvas', 'sheet', 'scrim', 'glass', 'glassRaised', 'edge', 'edgeRaised', 'trackOff', 'focus']) R[k] = lit[k];
    bg = lit.canvas[0];
  } else {
    bg = S.bg;
    const deep = S.bgDeep || S.bg, raised = S.bgRaised || S.bg, high = S.bgHigh || raised;
    R.canvas = [bg, 1];
    if (dark) {
      const base = mix(deep, BLACK, 0.28);
      Object.assign(R, { sheet: [raised, q(0.91)], scrim: [base, q(0.42)], glass: [base, q(0.4)], glassRaised: [mix(raised, WHITE, 0.06), q(0.56)],
        edge: [WHITE, q(0.12)], edgeRaised: [WHITE, q(0.2)], trackOff: [WHITE, q(0.16)] });
    } else {
      Object.assign(R, { sheet: [raised, q(0.92)], scrim: [bg, q(0.3)], glass: [WHITE, q(0.52)], glassRaised: [WHITE, q(0.72)],
        edge: [WHITE, q(0.85)], edgeRaised: [WHITE, q(0.9)] });
    }
  }
  const S0 = R.canvas[0], sheetC = over(R.sheet[0], R.sheet[1], S0);

  /* accent: non-text floor 3:1 on canvas and sheet */
  let accent = lit ? lit.accent[0] : S.accent;
  { const r = tune(accent, (c) => minCR(c, [S0, sheetC]) >= 3 + M, dir); if (r.d) note(notes, 'accent', `L${dir > 0 ? '+' : '-'}${r.d}`); accent = r.c; }
  const surf = surfaces(R, accent, dark), list = Object.values(surf);

  /* ink, ink2, ink3 */
  const fg = lit ? lit.ink[0] : S.fg;
  const found = searchInk(fg, ext, list, [surf.canvas, surf.sheet, surf.glass, surf.raised], lit ? { a2: lit.ink2[1], a3: lit.ink3[1] } : null);
  if (!found) throw new Error(`${p.id}: no ink satisfies the floors`);
  const ink = found.ink;
  if (found.t) note(notes, 'ink', `fg ${Math.round(found.t * 100)}% toward ${dark ? 'white' : 'black'}`);
  R.ink = [ink, 1]; R.ink2 = [ink, found.a2]; R.ink3 = [ink, found.a3];
  if (!dark) R.trackOff = [ink, q(0.12)];
  if (!lit) {
    if (dark) { R.focus = [mix(ink, WHITE, 0.5), q(0.98)]; } else { R.focus = [WHITE, q(0.95)]; }
  }
  R.focusInk = lit ? lit.focusInk : [dark ? mix(bg, BLACK, 0.1) : ink, 1];

  /* accentInk on the raw accent; button fill (accent-fill) and accent-coloured text */
  const darkInk = dark ? mix(bg, BLACK, 0.1) : ink;
  let aInk = lit ? lit.accentInk[0] : null;
  if (!aInk || cr(aInk, accent) < 4.5 + M) {
    const pick = [darkInk, WHITE, BLACK].map((c) => [c, cr(c, accent)]).find((x) => x[1] >= 4.5 + M);
    if (pick) { if (lit && aInk) note(notes, 'accentInk', `was ${toHex(aInk)} (${cr(aInk, accent).toFixed(1)}:1)`); aInk = pick[0]; }
    else aInk = [darkInk, WHITE].sort((a, b) => cr(b, accent) - cr(a, accent))[0];
  }
  R.accent = [accent, 1]; R.accentInk = [aInk, 1];
  let fill = accent, fillInk = aInk;
  if (!dark) { // light: white label on a deepened fill reads as the mockup's primary button
    const r = tune(accent, (c) => cr(WHITE, c) >= 4.5 + M, -1);
    if (r.d !== null && r.d <= 0.22) { fill = r.c; fillInk = WHITE; if (r.d) note(notes, 'accentFill', `L-${r.d}`); }
  }
  if (cr(fillInk, fill) < 4.5 + M) {
    const a = tune(accent, (c) => cr(darkInk, c) >= 4.5 + M, 1), b = tune(accent, (c) => cr(WHITE, c) >= 4.5 + M, -1);
    if (a.d !== null && (b.d === null || a.d <= b.d)) { fill = a.c; fillInk = darkInk; } else { fill = b.c; fillInk = WHITE; }
    note(notes, 'accentFill', 'tuned');
  }
  const accTint = [over(accent, 0.16, S0), over(accent, 0.16, sheetC)];
  const at = tune(accent, (c) => minCR(c, [...list, ...accTint]) >= 4.5 + M, dir);
  if (at.d) note(notes, 'accentText', `L${dir > 0 ? '+' : '-'}${at.d}`);

  /* status colours: pick the combination of official hues that is most distinguishable, then tune lightness */
  const cand = lit
    ? { positive: [lit.positive[0]], warning: [lit.warning[0]], danger: [lit.danger[0]], info: [lit.info[0], ...(p.alt?.info || []).map(hexRgb)] }
    : {
      positive: [S.green, S.cyan].filter(Boolean), warning: [S.yellow, S.orange].filter(Boolean), danger: [S.red, S.pink].filter(Boolean),
      info: [S.blue, S.cyan, S.purple, S.pink].filter(Boolean),
    };
  const keys = ['positive', 'warning', 'danger', 'info'];
  for (const k of keys) if (!cand[k].length) throw new Error(`${p.id}: no official ${k} colour`);
  const tuned = Object.fromEntries(keys.map((k) => [k, cand[k].map((c) => tune(c, (x) => minCR(x, [S0, sheetC]) >= 3 + M, dir))]));
  let best = null;
  const walk = (i, cur) => {
    if (i === 4) {
      const labs = cur.map((o) => rgb2lab(o.c));
      let min = Infinity;
      for (let a = 0; a < 4; a++) for (let b = a + 1; b < 4; b++) min = Math.min(min, dE00(labs[a], labs[b]));
      const cost = cur.reduce((s, o, k) => s + tuned[keys[k]].indexOf(o), 0);
      const score = min >= 14 ? 1000 - cost : min;
      if (!best || score > best.score) best = { cur: [...cur], min, score };
      return;
    }
    for (const o of tuned[keys[i]]) walk(i + 1, [...cur, o]);
  };
  walk(0, []);
  const status = {}, statusText = {};
  keys.forEach((k, i) => {
    const o = best.cur[i];
    status[k] = o.c;
    if (o.d) note(notes, k, `L${dir > 0 ? '+' : '-'}${o.d}`);
    if (tuned[k].indexOf(o) > 0) note(notes, k, 'remapped for distinctness');
    const tints = [over(o.c, 0.18, S0), over(o.c, 0.18, sheetC)];
    const t = tune(o.c, (x) => minCR(x, [...list, ...tints]) >= 4.5 + M, dir);
    statusText[k] = t.c;
    if (t.d) note(notes, k + 'Text', `L${dir > 0 ? '+' : '-'}${t.d}`);
  });
  for (const k of keys) R[k] = [status[k], 1];
  R.live = [status.danger, 1];

  const X = { accentFill: fill, accentFillInk: fillInk, accentText: at.c, positiveText: statusText.positive, warningText: statusText.warning,
    dangerText: statusText.danger, infoText: statusText.info };
  return { R, X, notes };
}
function note(notes, k, m) { notes.push(`${k} ${m}`); }

/* ---------------------------------------------------------------- independent evaluation of the final values */
function evaluate(t, dark) {
  const { R, X } = t;
  const surf = surfaces(R, R.accent[0], dark), list = Object.values(surf), S0 = surf.canvas, S1 = surf.sheet;
  const comp = (role, s) => over(R[role][0], R[role][1], s);
  const row = {};
  row.ink = minCR(R.ink[0], list);
  row.ink2 = Math.min(...list.map((s) => cr(comp('ink2', s), s)));
  row.ink3 = Math.min(...list.map((s) => cr(comp('ink3', s), s)));
  row.accent = minCR(R.accent[0], [S0, S1]);
  row.accentInk = cr(R.accentInk[0], R.accent[0]);
  row.fillInk = cr(X.accentFillInk, X.accentFill);
  const accTint = [over(R.accent[0], 0.16, S0), over(R.accent[0], 0.16, S1)];
  row.accentText = minCR(X.accentText, [...list, ...accTint]);
  const keys = ['positive', 'warning', 'danger', 'info'];
  row.glyph = Math.min(...keys.map((k) => minCR(R[k][0], [S0, S1])));
  row.text = Math.min(...keys.map((k) => minCR(X[k + 'Text'], [...list, over(R[k][0], 0.18, S0), over(R[k][0], 0.18, S1)])));
  row.track = Math.min(...[S0, surf.glass, S1].map((s) => cr(over(R.ink[0], 0.6, s), s)));
  row.trackOff = Math.min(...[S0, S1].map((s) => cr(comp('trackOff', s), s)));
  row.focus = cr(R.focusInk[0], over(R.focus[0], R.focus[1], S0));
  const labs = keys.map((k) => rgb2lab(R[k][0]));
  let de = Infinity;
  for (let a = 0; a < 4; a++) for (let b = a + 1; b < 4; b++) de = Math.min(de, dE00(labs[a], labs[b]));
  row.de = de;
  const fails = [];
  if (row.ink < 4.5) fails.push('ink'); if (row.ink2 < 4.5) fails.push('ink2'); if (row.ink3 < 3) fails.push('ink3');
  if (row.accent < 3) fails.push('accent'); if (row.accentInk < 4.5) fails.push('accentInk'); if (row.fillInk < 4.5) fails.push('fillInk');
  if (row.accentText < 4.5) fails.push('accentText'); if (row.glyph < 3) fails.push('glyph'); if (row.text < 4.5) fails.push('statusText');
  if (row.track < 3) fails.push('track'); if (row.focus < 4.5) fails.push('focusInk'); if (row.de < 12) fails.push('statusDistinct');
  return { row, fails };
}

/* ---------------------------------------------------------------- run */
const palettes = [];
for (const f of fs.readdirSync(SRC_DIR).filter((n) => n.endsWith('.json')).sort()) palettes.push(...JSON.parse(fs.readFileSync(path.join(SRC_DIR, f), 'utf8')).palettes);
const byId = new Map(palettes.map((p) => [p.id, p]));
const pick = (scheme) => {
  const ids = ORDER[scheme].filter((id) => byId.has(id) && !DROP.has(id));
  return (ALL ? ids : ids.slice(0, SHIP_MAX)).map((id) => byId.get(id));
};
const shipped = [...pick('dark'), ...pick('light')];

const PAL = ['canvas', 'sheet', 'scrim', 'glass', 'glassRaised', 'edge', 'edgeRaised', 'ink', 'ink2', 'ink3', 'focus', 'focusInk', 'trackOff',
  'accent', 'accentInk', 'positive', 'warning', 'danger', 'info', 'live'];
const kebab = (s) => s.replace(/([A-Z])/g, (m) => '-' + m.toLowerCase()).replace(/([a-z])(\d)/, '$1-$2');
const EXTRA = [['accentFill', 'accent-fill'], ['accentFillInk', 'accent-fill-ink'], ['accentText', 'accent-text'], ['positiveText', 'positive-text'],
  ['warningText', 'warning-text'], ['dangerText', 'danger-text'], ['infoText', 'info-text']];

let css = '/* GENERATED by scripts/themes.mjs from themes/source/*.json. Do not edit.\n * Palette values only; structure stays Lucent. Apply with <html data-theme="<id>">. */\n';
const lightIds = [];
const results = [];
for (const p of shipped) {
  let t;
  try { t = derive(p); } catch (e) { if (ALL) { console.log('skip ' + e.message); continue; } throw e; }
  const ev = evaluate(t, p.scheme === 'dark');
  results.push({ p, t, ev });
  const decl = PAL.map((k) => `--pal-${kebab(k)}:${toHex(t.R[k][0], t.R[k][1])}`).concat(EXTRA.map(([k, n]) => `--pal-${n}:${toHex(t.X[k])}`)).join(';');
  const sel = p.id === 'lucent-dark' ? `:root,[data-theme="${p.id}"]` : `[data-theme="${p.id}"]`;
  css += `/* ${p.name} (${p.scheme}) - ${p.sources.join(' ; ')} */\n${sel}{${decl}}\n`;
  if (p.scheme === 'light') lightIds.push(p.id);
}
css += `/* Light schemes: Lucent's lighter shadows and washes. */\n:is(${lightIds.map((i) => `[data-theme="${i}"]`).join(',')}){color-scheme:light;`
  + '--lu-shadow-rest:0 24px 60px rgba(0,0,0,.18);--lu-shadow-raised:0 34px 80px rgba(0,0,0,.28),0 10px 24px rgba(0,0,0,.14);'
  + '--lu-shadow-pressed:0 6px 14px rgba(0,0,0,.16);--lu-highlight-rest:inset 0 1px 0 rgba(255,255,255,.60);'
  + '--lu-highlight-raised:inset 0 1px 0 rgba(255,255,255,.90),inset 0 -1px 0 rgba(0,0,0,.10);--lu-neumorphic-light:rgba(255,255,255,.86);'
  + '--lu-neumorphic-shade:rgba(0,0,0,.10);--lu-focus-wash:color-mix(in srgb,var(--lu-ink) 6%,transparent);'
  + '--lu-material-edge:color-mix(in srgb,var(--lu-ink) 16%,transparent)}\n';

const ts = `// GENERATED by scripts/themes.mjs. Do not edit.\n`
  + `export interface ThemeMeta { id: string; name: string; family: string; scheme: 'dark' | 'light'; canvas: string }\n`
  + `export const THEMES: readonly ThemeMeta[] = ${JSON.stringify(results.map(({ p, t }) => ({ id: p.id, name: p.name, family: p.family, scheme: p.scheme,
    canvas: toHex(t.R.canvas[0]) })), null, 1)};\n`
  + `export const DEFAULT_DARK = 'lucent-dark';\nexport const DEFAULT_LIGHT = 'lucent-light';\n`;

/* report */
const f1 = (x) => x.toFixed(1).padStart(5);
const cols = [['ink', 'ink'], ['ink2', 'ink2'], ['ink3', 'ink3'], ['accent', 'acc'], ['accentInk', 'accI'], ['fillInk', 'fill'], ['accentText', 'accT'],
  ['glyph', 'glyf'], ['text', 'stTx'], ['track', 'trck'], ['focus', 'focI'], ['de', 'dE00']];
let rep = 'Stashify themes: contrast on composited surfaces (min over canvas, accent glow, sheet, glass, raised glass)\n'
  + 'Floors: ink/ink2/accentInk/fillInk/accentText/status text >= 4.5, ink3/accent/status glyph/track >= 3, status dE00 >= 12.\n'
  + 'ink = body text, ink2 = secondary, ink3 = muted tier, acc = accent glyph on canvas+sheet, accI = accentInk on accent, fill = fill label on button fill,\n'
  + 'accT = accent-coloured text, glyf = raw status colour as glyph (min of 4), stTx = status text (min of 4, also on 18% tints), trck = switch track,\n'
  + 'focI = focusInk on focus fill, dE00 = smallest CIEDE2000 between the four status colours.\n\n';
rep += 'theme'.padEnd(22) + 'sch  ' + cols.map((c) => c[1].padStart(5)).join(' ') + '  result  tuned\n';
let failCount = 0;
for (const { p, t, ev } of results) {
  const bad = ev.fails.length ? 'FAIL ' + ev.fails.join(',') : 'pass';
  if (ev.fails.length) failCount++;
  rep += p.id.padEnd(22) + p.scheme.padEnd(5) + cols.map((c) => f1(ev.row[c[0]])).join(' ') + '  ' + bad.padEnd(6) + '  ' + (t.notes.join('; ') || '-') + '\n';
}
const nd = results.filter((r) => r.p.scheme === 'dark').length, nl = results.length - nd;
rep += `\n${results.length} themes (${nd} dark, ${nl} light); ${failCount} failing.\n`;

if (!CHECK) {
  fs.writeFileSync(OUT_CSS, css);
  fs.mkdirSync(path.dirname(OUT_TS), { recursive: true });
  fs.writeFileSync(OUT_TS, ts);
  fs.writeFileSync(OUT_REPORT, rep);
}
console.log(rep.split('\n').filter((l, i) => i >= 6 && (l.includes('FAIL') || l.includes('themes ('))).join('\n') || rep.slice(-120));
if (CHECK && failCount) { console.error(`themes: ${failCount} failing`); process.exit(1); }
