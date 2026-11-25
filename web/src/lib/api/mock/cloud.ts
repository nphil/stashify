/* Mock server for the GPU rental market. It speaks the REAL wire shape (hireling's offer record plus `estimate`,
   `facets`, `hardware_presets`, `sorts`, `providers`), so the adapter is exercised exactly as it will be against the
   real server. Like the real one it refuses a query parameter it does not know (400 `bad_query`), which keeps the
   request builder honest. The 29 machines and the price maths are the approved mockup's.

   URL switches: `?offers=<n>` adds n machines (the 500-row scroll test), `?legacy=1` plays today's server (it does not
   know `scene_ids` or `rented`), `?refuse=<code>` makes the price check refuse (no_licence, no_key, ...), `?prov=2`
   connects RunPod as well (see shared.ts). The limits come from BUDGET, which the settings mock edits. */
import { NET, OUT_F, baseSpeed, jobReq, modelsGB, sceneInfo } from '../../choices';
import type { OpId, QualityId, Scene, SceneInfo } from '../types';
import { BASE_SCENES } from './scenes';
import { BUDGET, PROVIDERS } from './shared';
import { fail, route } from './router';

const URL_FLAGS = new URLSearchParams(location.search);
const EXTRA = Math.min(2000, Math.max(0, Number(URL_FLAGS.get('offers')) || 0));
const LEGACY = URL_FLAGS.get('legacy') === '1';
const REFUSE = URL_FLAGS.get('refuse');

/* ---------- the machines ---------- */

interface Gpu { name: string; vram: number; bw: number; tf: number; perf: number; g: number; arch: string }
/** public specs, rounded. perf = the marketplace score per GPU, g = speed against the RTX 3080 at home */
const GPUS: Readonly<Record<string, Gpu>> = {
  '5090': { name: 'RTX 5090', vram: 32, bw: 1792, tf: 104.8, perf: 58, g: 3.15, arch: 'Blackwell' },
  '5080': { name: 'RTX 5080', vram: 16, bw: 960, tf: 56.3, perf: 36, g: 1.95, arch: 'Blackwell' },
  '5070ti': { name: 'RTX 5070 Ti', vram: 16, bw: 896, tf: 43.9, perf: 30, g: 1.6, arch: 'Blackwell' },
  '5070': { name: 'RTX 5070', vram: 12, bw: 672, tf: 30.9, perf: 24, g: 1.25, arch: 'Blackwell' },
  '5060ti': { name: 'RTX 5060 Ti', vram: 16, bw: 448, tf: 23.7, perf: 17, g: 0.95, arch: 'Blackwell' },
  '4090': { name: 'RTX 4090', vram: 24, bw: 1008, tf: 82.6, perf: 42, g: 2.25, arch: 'Ada' },
  '4080s': { name: 'RTX 4080 SUPER', vram: 16, bw: 736, tf: 52.2, perf: 31, g: 1.8, arch: 'Ada' },
  '4070tis': { name: 'RTX 4070 Ti SUPER', vram: 16, bw: 672, tf: 44.1, perf: 26, g: 1.5, arch: 'Ada' },
  '3090': { name: 'RTX 3090', vram: 24, bw: 936, tf: 35.6, perf: 26, g: 1.15, arch: 'Ampere' },
  '3080': { name: 'RTX 3080', vram: 10, bw: 760, tf: 29.8, perf: 21, g: 1.0, arch: 'Ampere' },
  '3060': { name: 'RTX 3060', vram: 12, bw: 360, tf: 12.7, perf: 10, g: 0.55, arch: 'Ampere' },
  a6000: { name: 'RTX A6000', vram: 48, bw: 768, tf: 38.7, perf: 28, g: 1.5, arch: 'Ampere' },
  l40s: { name: 'L40S', vram: 48, bw: 864, tf: 91.6, perf: 44, g: 2.6, arch: 'Ada' },
  h100: { name: 'H100 PCIe', vram: 80, bw: 2000, tf: 51.2, perf: 52, g: 4.4, arch: 'Hopper' },
  h100s: { name: 'H100 SXM', vram: 80, bw: 3350, tf: 66.9, perf: 60, g: 4.6, arch: 'Hopper' },
  '1080ti': { name: 'GTX 1080 Ti', vram: 11, bw: 484, tf: 11.3, perf: 8, g: 0.3, arch: 'Pascal' },
};

/** the fixture rows keep the mockup's short names; `Machine` is what the rest of this file works with */
interface Row {
  id: string; prov: 'vast' | 'runpod'; gpu: string; n: number; tier: 'dc' | 'comm'; price: number; perGB: number; rel: number;
  ver: 0 | 1; days: number; loc: string; cpu: string; cores: number; ram: number; pcie: string; disk: string; dmbs: number;
  free: number; down: number; up: number; ports: number; cuda: number; drv: number; stor: number; vram?: number;
}
const ROWS: readonly Row[] = [
  { id: 'v1', prov: 'vast', gpu: '5090', n: 1, tier: 'dc', price: 0.51, perGB: 0.005, rel: 99.6, ver: 1, days: 30, loc: 'Frankfurt, DE', cpu: 'AMD EPYC 9354', cores: 32, ram: 128, pcie: '5.0 x16', disk: 'Micron 7450', dmbs: 5200, free: 500, down: 2400, up: 1800, ports: 24, cuda: 12.8, drv: 570, stor: 0.15 },
  { id: 'v2', prov: 'vast', gpu: '5090', n: 1, tier: 'comm', price: 0.65, perGB: 0.012, rel: 97.9, ver: 1, days: 14, loc: 'Texas, US', cpu: 'AMD Ryzen 9 7950X', cores: 16, ram: 96, pcie: '5.0 x16', disk: 'Samsung 990 PRO', dmbs: 6800, free: 300, down: 940, up: 620, ports: 12, cuda: 12.8, drv: 570, stor: 0.2 },
  { id: 'v3', prov: 'vast', gpu: '5090', n: 1, tier: 'comm', price: 0.57, perGB: 0.009, rel: 96.8, ver: 0, days: 7, loc: 'Seoul, KR', cpu: 'Intel Core i9-14900K', cores: 24, ram: 64, pcie: '5.0 x16', disk: 'WD Black SN850X', dmbs: 6300, free: 220, down: 700, up: 520, ports: 8, cuda: 12.8, drv: 570, stor: 0.18 },
  { id: 'v4', prov: 'vast', gpu: '5080', n: 1, tier: 'comm', price: 0.29, perGB: 0.008, rel: 98.1, ver: 1, days: 14, loc: 'Paris, FR', cpu: 'AMD Ryzen 9 7900X', cores: 12, ram: 64, pcie: '5.0 x16', disk: 'Crucial T500', dmbs: 7000, free: 260, down: 860, up: 600, ports: 10, cuda: 12.8, drv: 570, stor: 0.16 },
  { id: 'v5', prov: 'vast', gpu: '5070ti', n: 1, tier: 'comm', price: 0.2, perGB: 0.007, rel: 97.5, ver: 1, days: 14, loc: 'Madrid, ES', cpu: 'AMD Ryzen 7 7700', cores: 8, ram: 32, pcie: '5.0 x16', disk: 'Samsung 990 PRO', dmbs: 6800, free: 180, down: 520, up: 410, ports: 8, cuda: 12.8, drv: 570, stor: 0.16 },
  { id: 'v6', prov: 'vast', gpu: '5070', n: 1, tier: 'comm', price: 0.14, perGB: 0.006, rel: 97.9, ver: 1, days: 30, loc: 'Warsaw, PL', cpu: 'AMD Ryzen 5 7600', cores: 6, ram: 32, pcie: '5.0 x16', disk: 'Kingston KC3000', dmbs: 6200, free: 400, down: 600, up: 450, ports: 10, cuda: 12.8, drv: 570, stor: 0.14 },
  { id: 'v7', prov: 'vast', gpu: '5060ti', n: 1, tier: 'comm', price: 0.11, perGB: 0.006, rel: 98.4, ver: 1, days: 30, loc: 'Vienna, AT', cpu: 'Intel Core i5-13600K', cores: 14, ram: 32, pcie: '5.0 x8', disk: 'WD Black SN770', dmbs: 4900, free: 350, down: 480, up: 300, ports: 6, cuda: 12.8, drv: 570, stor: 0.12 },
  { id: 'v8', prov: 'vast', gpu: '5060ti', n: 1, tier: 'comm', price: 0.08, perGB: 0.006, rel: 96.0, ver: 0, days: 7, loc: 'Prague, CZ', cpu: 'AMD Ryzen 5 5600', cores: 6, ram: 16, pcie: '4.0 x8', disk: 'Crucial P3', dmbs: 3500, free: 120, down: 300, up: 220, ports: 4, cuda: 12.8, drv: 570, stor: 0.12, vram: 8 },
  { id: 'v9', prov: 'vast', gpu: '4090', n: 1, tier: 'comm', price: 0.43, perGB: 0.01, rel: 98.7, ver: 1, days: 30, loc: 'Warsaw, PL', cpu: 'AMD Ryzen 9 5950X', cores: 16, ram: 64, pcie: '4.0 x16', disk: 'Samsung 980 PRO', dmbs: 6500, free: 200, down: 620, up: 480, ports: 12, cuda: 12.6, drv: 560, stor: 0.2 },
  { id: 'v10', prov: 'vast', gpu: '4090', n: 1, tier: 'dc', price: 0.69, perGB: 0.004, rel: 99.9, ver: 1, days: 90, loc: 'Amsterdam, NL', cpu: 'AMD EPYC 7543', cores: 32, ram: 128, pcie: '4.0 x16', disk: 'Micron 7450', dmbs: 5000, free: 800, down: 3000, up: 3000, ports: 40, cuda: 12.6, drv: 560, stor: 0.12 },
  { id: 'v11', prov: 'vast', gpu: '4090', n: 1, tier: 'dc', price: 0.74, perGB: 0.004, rel: 99.4, ver: 1, days: 90, loc: 'Chicago, US', cpu: 'Intel Xeon Gold 6430', cores: 32, ram: 128, pcie: '4.0 x16', disk: 'Kioxia CD7', dmbs: 6200, free: 30, down: 2800, up: 2600, ports: 40, cuda: 12.6, drv: 560, stor: 0.12 },
  { id: 'v12', prov: 'vast', gpu: '4090', n: 2, tier: 'comm', price: 0.86, perGB: 0.007, rel: 98.8, ver: 1, days: 14, loc: 'Prague, CZ', cpu: 'AMD Threadripper 3960X', cores: 24, ram: 128, pcie: '4.0 x16', disk: 'Samsung 980 PRO', dmbs: 6400, free: 900, down: 1600, up: 1400, ports: 20, cuda: 12.6, drv: 560, stor: 0.15 },
  { id: 'v13', prov: 'vast', gpu: '4090', n: 4, tier: 'comm', price: 1.62, perGB: 0.007, rel: 97.6, ver: 1, days: 14, loc: 'Montreal, CA', cpu: 'AMD EPYC 7452', cores: 32, ram: 256, pcie: '4.0 x16', disk: 'Intel P4510', dmbs: 3200, free: 1800, down: 1400, up: 1200, ports: 20, cuda: 12.6, drv: 560, stor: 0.15 },
  { id: 'v14', prov: 'vast', gpu: '4080s', n: 1, tier: 'comm', price: 0.19, perGB: 0.008, rel: 97.4, ver: 1, days: 14, loc: 'Sofia, BG', cpu: 'Intel Core i7-13700', cores: 16, ram: 48, pcie: '4.0 x16', disk: 'WD Black SN850X', dmbs: 6200, free: 150, down: 480, up: 310, ports: 8, cuda: 12.6, drv: 560, stor: 0.18 },
  { id: 'v15', prov: 'vast', gpu: '4070tis', n: 1, tier: 'comm', price: 0.15, perGB: 0.007, rel: 97.2, ver: 1, days: 14, loc: 'Bucharest, RO', cpu: 'AMD Ryzen 7 5800X', cores: 8, ram: 32, pcie: '4.0 x16', disk: 'Samsung 970 EVO', dmbs: 3400, free: 200, down: 400, up: 280, ports: 6, cuda: 12.4, drv: 550, stor: 0.14 },
  { id: 'v16', prov: 'vast', gpu: '3090', n: 1, tier: 'comm', price: 0.16, perGB: 0.006, rel: 96.1, ver: 1, days: 30, loc: 'Lisbon, PT', cpu: 'AMD Ryzen 5 5600X', cores: 6, ram: 64, pcie: '4.0 x16', disk: 'Crucial P5 Plus', dmbs: 5000, free: 250, down: 350, up: 280, ports: 8, cuda: 12.4, drv: 550, stor: 0.15 },
  { id: 'v17', prov: 'vast', gpu: '3090', n: 2, tier: 'comm', price: 0.3, perGB: 0.006, rel: 97.0, ver: 1, days: 30, loc: 'Dublin, IE', cpu: 'AMD Ryzen 9 3950X', cores: 16, ram: 128, pcie: '4.0 x8', disk: 'Samsung 970 PRO', dmbs: 3300, free: 600, down: 800, up: 600, ports: 12, cuda: 12.4, drv: 550, stor: 0.14 },
  { id: 'v18', prov: 'vast', gpu: '3090', n: 4, tier: 'comm', price: 0.55, perGB: 0.006, rel: 96.5, ver: 1, days: 30, loc: 'Helsinki, FI', cpu: 'AMD EPYC 7302P', cores: 16, ram: 256, pcie: '4.0 x16', disk: 'Samsung PM983', dmbs: 3000, free: 1500, down: 1200, up: 900, ports: 16, cuda: 12.4, drv: 550, stor: 0.13 },
  { id: 'v19', prov: 'vast', gpu: '3080', n: 1, tier: 'comm', price: 0.11, perGB: 0.005, rel: 95.8, ver: 0, days: 7, loc: 'Athens, GR', cpu: 'Intel Core i7-10700K', cores: 8, ram: 32, pcie: '3.0 x16', disk: 'WD Blue SN550', dmbs: 2400, free: 160, down: 300, up: 150, ports: 4, cuda: 12.2, drv: 535, stor: 0.12 },
  { id: 'v20', prov: 'vast', gpu: '3060', n: 1, tier: 'comm', price: 0.07, perGB: 0.005, rel: 94.9, ver: 0, days: 7, loc: 'Belgrade, RS', cpu: 'AMD Ryzen 5 3600', cores: 6, ram: 16, pcie: '3.0 x16', disk: 'Kingston A2000', dmbs: 2200, free: 90, down: 220, up: 100, ports: 4, cuda: 12.4, drv: 550, stor: 0.1 },
  { id: 'v21', prov: 'vast', gpu: 'a6000', n: 1, tier: 'comm', price: 0.38, perGB: 0.008, rel: 98.2, ver: 1, days: 30, loc: 'Montreal, CA', cpu: 'AMD EPYC 7313P', cores: 16, ram: 96, pcie: '4.0 x16', disk: 'Micron 9300', dmbs: 3100, free: 400, down: 1200, up: 900, ports: 16, cuda: 12.6, drv: 560, stor: 0.18 },
  { id: 'v22', prov: 'vast', gpu: 'l40s', n: 1, tier: 'dc', price: 0.78, perGB: 0.004, rel: 99.8, ver: 1, days: 90, loc: 'Stockholm, SE', cpu: 'AMD EPYC 9254', cores: 24, ram: 192, pcie: '5.0 x16', disk: 'Kioxia CM7', dmbs: 6800, free: 700, down: 3000, up: 2500, ports: 40, cuda: 12.8, drv: 570, stor: 0.12 },
  { id: 'v23', prov: 'vast', gpu: 'h100', n: 1, tier: 'dc', price: 0.97, perGB: 0.003, rel: 99.9, ver: 1, days: 90, loc: 'Oslo, NO', cpu: 'Intel Xeon Platinum 8480', cores: 56, ram: 256, pcie: '5.0 x16', disk: 'Kioxia CM7', dmbs: 6800, free: 1000, down: 5000, up: 5000, ports: 40, cuda: 12.8, drv: 570, stor: 0.1 },
  { id: 'v24', prov: 'vast', gpu: 'h100s', n: 8, tier: 'dc', price: 15.2, perGB: 0.003, rel: 99.9, ver: 1, days: 90, loc: 'Dallas, US', cpu: 'Intel Xeon Platinum 8480', cores: 112, ram: 2048, pcie: '5.0 x16', disk: 'Kioxia CM7', dmbs: 6800, free: 8000, down: 10000, up: 10000, ports: 40, cuda: 12.8, drv: 570, stor: 0.1 },
  { id: 'v25', prov: 'vast', gpu: '1080ti', n: 1, tier: 'comm', price: 0.04, perGB: 0.004, rel: 93.5, ver: 0, days: 3, loc: 'Kyiv, UA', cpu: 'Intel Core i5-8400', cores: 6, ram: 16, pcie: '3.0 x16', disk: 'WD Green SATA', dmbs: 520, free: 80, down: 150, up: 60, ports: 4, cuda: 11.8, drv: 525, stor: 0.08 },
  { id: 'r1', prov: 'runpod', gpu: '4090', n: 1, tier: 'dc', price: 0.59, perGB: 0, rel: 99.5, ver: 1, days: 90, loc: 'Reykjavik, IS', cpu: 'AMD EPYC 7413', cores: 24, ram: 62, pcie: '4.0 x16', disk: 'Network volume', dmbs: 2000, free: 80, down: 1500, up: 1500, ports: 10, cuda: 12.6, drv: 560, stor: 0.1 },
  { id: 'r2', prov: 'runpod', gpu: '5090', n: 1, tier: 'dc', price: 0.89, perGB: 0, rel: 99.7, ver: 1, days: 90, loc: 'Prague, CZ', cpu: 'AMD EPYC 9124', cores: 16, ram: 92, pcie: '5.0 x16', disk: 'Network volume', dmbs: 2000, free: 80, down: 1500, up: 1500, ports: 10, cuda: 12.8, drv: 570, stor: 0.1 },
  { id: 'r3', prov: 'runpod', gpu: 'l40s', n: 1, tier: 'dc', price: 0.86, perGB: 0, rel: 99.6, ver: 1, days: 90, loc: 'Amsterdam, NL', cpu: 'AMD EPYC 9124', cores: 16, ram: 94, pcie: '5.0 x16', disk: 'Network volume', dmbs: 2000, free: 80, down: 1500, up: 1500, ports: 10, cuda: 12.8, drv: 570, stor: 0.1 },
  { id: 'r4', prov: 'runpod', gpu: '3090', n: 1, tier: 'dc', price: 0.22, perGB: 0, rel: 99.2, ver: 1, days: 90, loc: 'Stockholm, SE', cpu: 'AMD EPYC 7313', cores: 16, ram: 62, pcie: '4.0 x16', disk: 'Network volume', dmbs: 2000, free: 80, down: 1500, up: 1500, ports: 10, cuda: 12.4, drv: 550, stor: 0.1 },
];

interface Machine extends Row { g: Gpu; vramPer: number; country: string; rented: boolean }
const toMachine = (r: Row, rented: boolean): Machine => {
  const g = GPUS[r.gpu];
  return { ...r, g, vramPer: r.vram ?? g.vram, country: r.loc.slice(-2), rented };
};

/** extra neutral machines for the long-list test: the same shape, deterministic */
function synth(count: number): Machine[] {
  const keys = Object.keys(GPUS);
  const cities = ['Brno, CZ', 'Lyon, FR', 'Turin, IT', 'Malmo, SE', 'Ohio, US', 'Oregon, US', 'Toronto, CA', 'Tokyo, JP', 'Osaka, JP', 'Sydney, AU', 'Sao Paulo, BR', 'Singapore, SG', 'Mumbai, IN', 'Lagos, NG'];
  let x = 20260;
  const rnd = (): number => { x = (x * 1103515245 + 12345) & 0x7fffffff; return x / 0x7fffffff; };
  const out: Machine[] = [];
  for (let i = 0; i < count; i++) {
    const key = keys[Math.floor(rnd() * keys.length)];
    const g = GPUS[key];
    const n = [1, 1, 1, 2, 4, 8][Math.floor(rnd() * 6)];
    const dc = rnd() < 0.2;
    const row: Row = {
      id: `x${i + 1}`, prov: PROVIDERS === 2 && i % 5 === 0 ? 'runpod' : 'vast', gpu: key, n, tier: dc ? 'dc' : 'comm',
      price: Math.max(0.03, Math.round((g.perf / 60 + 0.08) * n * (0.7 + rnd() * 0.6) * 100) / 100),
      perGB: Math.round(rnd() * 12) / 1000, rel: Math.round((91 + rnd() * 8.9) * 10) / 10, ver: rnd() < 0.8 ? 1 : 0,
      days: [3, 7, 14, 30, 90][Math.floor(rnd() * 5)], loc: cities[Math.floor(rnd() * cities.length)],
      cpu: 'AMD EPYC 7B13', cores: 8 * (1 + Math.floor(rnd() * 8)), ram: 32 * (1 + Math.floor(rnd() * 8)), pcie: '4.0 x16',
      disk: 'NVMe SSD', dmbs: 2000 + Math.floor(rnd() * 5000), free: [60, 120, 250, 500, 1000][Math.floor(rnd() * 5)],
      down: 200 + Math.floor(rnd() * 2800), up: 100 + Math.floor(rnd() * 1900), ports: 4 + Math.floor(rnd() * 20),
      cuda: [12.2, 12.4, 12.6, 12.8][Math.floor(rnd() * 4)], drv: [535, 550, 560, 570][Math.floor(rnd() * 4)], stor: 0.1 + Math.round(rnd() * 10) / 100,
    };
    out.push(toMachine(row, i % 37 === 36));
  }
  return out;
}

const MACHINES: readonly Machine[] = [...ROWS.map((r) => toMachine(r, r.id === 'v12' || r.id === 'v17')), ...synth(EXTRA)];

const PROVIDER_LIST = [
  { name: 'vast', display_name: 'Vast.ai', implemented: true, capabilities: ['offers', 'rent'], enabled: true },
  ...(PROVIDERS === 2 ? [{ name: 'runpod', display_name: 'RunPod', implemented: true, capabilities: ['offers', 'rent'], enabled: true }] : []),
];

/* ---------- the job and its price (the mockup's maths) ---------- */

interface Job { op: OpId; q: QualityId; eff: number; gb: number; n: number; info: SceneInfo; ids: string[] }
const OP_FROM_WIRE: Readonly<Record<string, OpId>> = { decensor: 'decensor', 'decensor+upscale': 'decensor_up', upscale: 'upscale' };
const Q_FROM_PRESET: Readonly<Record<string, QualityId>> = { 'close-ups': 'fast', detail: 'balanced', 'detail-close-ups': 'high' };
const TYPICAL: Scene = BASE_SCENES[0];

/** The job a request describes; null = browsing without one. An unknown scene id is priced as a typical scene. */
function jobOf(idsRaw: string | null, opRaw: string | null, presetRaw: string | null): Job | null {
  const ids = (idsRaw ?? '').split(',').map((s) => s.trim()).filter((s) => s !== '');
  if (ids.length === 0) return null;
  const scenes = ids.map((id) => BASE_SCENES.find((s) => s.id === id) ?? TYPICAL);
  const info = sceneInfo(scenes);
  const op = OP_FROM_WIRE[(opRaw ?? 'decensor').replace(' ', '+')] ?? 'decensor';
  return { op, q: Q_FROM_PRESET[presetRaw ?? 'detail'] ?? 'balanced', eff: info.eff, gb: info.gb, n: info.n, info, ids };
}

const r2 = (x: number): number => Math.round(x * 100) / 100;
const usd = (n: number): string => '$' + n.toFixed(2);

interface Priced {
  minutes: number; hours: number; runMin: number; setupMin: number; transferMin: number;
  compute: number; transfer: number; storage: number; total: number;
  gbIn: number; gbOut: number; diskGb: number; bwDown: number;
}
function priceOn(m: Machine, job: Job): Priced {
  const speed = baseSpeed(job.op, job.q) * m.g.g;
  const runMin = job.eff / speed / 60;
  const gbUp = job.gb, gbDown = job.gb * OUT_F[job.op], gbModels = modelsGB(job.op, job.q);
  const upMin = (gbUp * 8000) / Math.min(NET.ourUp, m.down) / 60;
  const downMin = (gbDown * 8000) / Math.min(NET.ourDown, m.up) / 60;
  const modelMin = (gbModels * 8000) / m.down / 60;
  const setupMin = 2.5 + modelMin;
  const minutes = setupMin + upMin + runMin + downMin;
  const hours = minutes / 60;
  const gbTotal = gbUp + gbDown + gbModels;
  const diskGb = Math.max(40, Math.ceil(job.gb * 3 + gbModels + 10));
  const compute = r2(hours * m.price);
  const transfer = r2(gbTotal * m.perGB);
  const storage = Math.max(0.01, r2((diskGb * m.stor) / 720 * hours));
  return {
    minutes, hours, runMin, setupMin, transferMin: upMin + downMin, compute, transfer, storage,
    total: r2(compute + transfer + storage), gbIn: gbUp + gbModels, gbOut: gbDown, diskGb, bwDown: r2((gbUp + gbModels) * m.perGB),
  };
}

/** what keeps this machine from running the job, in plain words (the real server says it for hardware) */
function fitReason(m: Machine, job: Job): string | null {
  const need = jobReq(job.op, job.q, job.info);
  if (m.g.arch === 'Pascal') return 'Needs an RTX 20-series or newer';
  if (m.vramPer < need.vram) return `Needs ${need.vram} GB+ video memory`;
  if (m.free < need.diskGB) return `Disk too small (needs ${need.diskGB} GB)`;
  if (m.cuda < need.cuda) return `Needs CUDA ${need.cuda} or newer`;
  return null;
}
/** the owner's limits; the real server leaves these to the app, the mock uses them to order and to pick */
function limitReason(m: Machine, total: number): string | null {
  if (m.price > BUDGET.maxHour + 1e-9) return `Over your ${usd(BUDGET.maxHour)}/h limit`;
  if (total > BUDGET.jobCap) return `Over your ${usd(BUDGET.jobCap)} stop per rental`;
  if (total > BUDGET.month - BUDGET.spent + 1e-9) return 'Over this month’s limit';
  if (total > BUDGET.credit + 1e-9) return 'Not enough credit';
  return null;
}

interface Entry { m: Machine; p: Priced | null; fit: string | null; why: string | null }
function entryOf(m: Machine, job: Job | null): Entry {
  if (!job) return { m, p: null, fit: null, why: null };
  const p = priceOn(m, job);
  const fit = fitReason(m, job);
  return { m, p, fit, why: fit ?? limitReason(m, p.total) };
}

function estimateWire(m: Machine, p: Priced, job: Job): Record<string, unknown> {
  return {
    setup_min: p.setupMin, transfer_min: p.transferMin, processing_min: p.runMin, total_min: p.minutes, minutes: p.minutes,
    hours: p.hours, est_usd: p.total, disk_gb: p.diskGb,
    assumptions: [`GPU time is an estimate for ${m.g.name}.`, `Uploading from home is assumed to run at ${Math.round(NET.ourUp / 8)} MB/s.`, `${job.n} scene${job.n === 1 ? '' : 's'} on one rental.`],
    breakdown: {
      hours: p.hours, compute: p.compute, storage: p.storage, bandwidth: p.transfer, bw_down: p.bwDown, bw_up: r2(p.transfer - p.bwDown),
      gb_down: p.gbIn, gb_up: p.gbOut, total: p.total, notes: [],
    },
  };
}

/** PCIe lanes to GB/s, roughly (the real record carries a number, not a label) */
function pcieBw(label: string): number {
  const [gen, lanes] = label.split(' x');
  const per = Number(gen) >= 5 ? 3.94 : Number(gen) >= 4 ? 1.97 : 0.985;
  return Math.round(per * Number(lanes) * 10) / 10;
}

function offerWire(e: Entry, job: Job | null): Record<string, unknown> {
  const m = e.m;
  return {
    id: m.id, provider: m.prov, gpu: m.g.name, num_gpus: m.n, vram_gb: m.vramPer, total_vram_gb: m.vramPer * m.n,
    price_per_hour: m.price, reliability: m.rel / 100, datacenter: m.tier === 'dc', host_type: m.tier === 'dc' ? 'datacenter' : 'community',
    verified: m.ver === 1, driver: m.drv, cuda: m.cuda, cpu_name: m.cpu, cpu_cores: m.cores, ram_gb: m.ram,
    disk_name: m.disk, disk_gb: m.free, disk_bw: m.dmbs, gpu_mem_bw: m.g.bw, pcie_bw: pcieBw(m.pcie),
    inet_down: m.down, inet_up: m.up, open_ports: m.ports, location: m.loc, country: m.country, max_duration_hours: m.days * 24,
    dlperf: m.g.perf * m.n, total_flops: Math.round(m.g.tf * m.n * 10) / 10, perf_per_dollar: (m.g.perf * m.n) / m.price,
    storage_per_gb_hr: m.stor / 720, bw_up_per_gb: m.perGB, bw_down_per_gb: m.perGB,
    compatible: e.fit === null, incompatible_reason: e.fit,
    estimate: e.p && job ? estimateWire(m, e.p, job) : null,
    ...(!LEGACY && { rented: m.rented }),
  };
}

/* ---------- recommendations (the mockup's rules; each machine is used once) ---------- */

interface Pick { id: 'best_value' | 'fastest' | 'cheapest' | 'most_reliable'; label: string; description: string }
const PICKS: readonly Pick[] = [
  { id: 'best_value', label: 'Best value', description: 'The best balance of price and time for this job.' },
  { id: 'fastest', label: 'Fastest', description: 'Finishes first.' },
  { id: 'cheapest', label: 'Cheapest', description: 'Costs the least for this job.' },
  { id: 'most_reliable', label: 'Most reliable', description: 'The steadiest host.' },
];
function presetsOf(pool: readonly Entry[], job: Job): Record<string, unknown>[] {
  const ok = pool.filter((e) => e.m.n === 1 && e.p !== null && e.why === null);
  const minutes = (e: Entry): number => e.p?.minutes ?? 0;
  const total = (e: Entry): number => e.p?.total ?? 0;
  const by = (f: (e: Entry) => number, asc = true): Entry[] => ok.slice().sort((a, b) => (asc ? f(a) - f(b) : f(b) - f(a)));
  const lists: Record<Pick['id'], Entry[]> = {
    best_value: by((e) => total(e) * Math.pow(e.p?.hours ?? 1, 0.6)).filter((e) => e.m.rel >= 98.5),
    fastest: by(minutes),
    cheapest: by(total),
    most_reliable: by((e) => e.m.rel * 1000 - total(e), false),
  };
  const used = new Set<string>();
  const out: Record<string, unknown>[] = [];
  for (const pick of PICKS) {
    const e = lists[pick.id].find((x) => !used.has(x.m.id));
    if (!e || !e.p) continue;
    used.add(e.m.id);
    out.push({ id: pick.id, label: pick.label, description: pick.description, offer_id: e.m.id, provider: e.m.prov, gpu: e.m.g.name, price_per_hour: e.m.price, note: null, estimate: estimateWire(e.m, e.p, job) });
  }
  return out;
}

/* ---------- the endpoint ---------- */

const ALLOWED = new Set([
  'sort', 'limit', 'offset', 'scene', 'preset', 'op', 'upscale', 'upscale_height', 'hours',
  'gpus', 'gpu', 'num_gpus', 'max_gpus', 'min_vram_gb', 'max_vram_gb', 'min_cpu_cores', 'min_ram_gb', 'min_disk_gb', 'min_inet_down',
  'min_inet_up', 'min_reliability', 'min_driver', 'min_cuda', 'min_open_ports', 'min_duration_hours', 'datacenter_only', 'verified',
  'prefer', 'countries', 'provider', 'offer_id', 'rental_type', 'max_price', 'max_bw_cost',
  ...(LEGACY ? [] : ['scene_ids', 'rented']),
]);

const SORTS = [
  { id: 'auto', label: 'Auto', needs_estimate: false },
  { id: 'price_asc', label: 'Price: low to high', needs_estimate: false },
  { id: 'price_desc', label: 'Price: high to low', needs_estimate: false },
  { id: 'perf_per_dollar', label: 'Performance per $/h', needs_estimate: false },
  { id: 'perf', label: 'Performance', needs_estimate: false },
  { id: 'flops_per_dollar', label: 'TFLOPS per $/h', needs_estimate: false },
  { id: 'flops', label: 'TFLOPS', needs_estimate: false },
  { id: 'vram', label: 'Video RAM', needs_estimate: false },
  { id: 'vram_per_dollar', label: 'Video RAM per $/h', needs_estimate: false },
  { id: 'reliability', label: 'Reliability', needs_estimate: false },
  { id: 'est_total', label: 'Estimated total', needs_estimate: true },
  { id: 'est_time', label: 'Estimated time', needs_estimate: true },
];

const norm = (s: string): string => s.replace(/[\s_]+/g, ' ').trim().toLowerCase();
function flag(v: string | null): boolean | null {
  if (v === null || v === '') return null;
  const s = v.toLowerCase();
  if (['1', 'true', 'yes', 'on'].includes(s)) return true;
  if (['0', 'false', 'no', 'off'].includes(s)) return false;
  return null;
}
function numParam(q: URLSearchParams, name: string): number | null {
  const v = q.get(name);
  if (v === null || v.trim() === '') return null;
  const n = Number(v);
  if (!Number.isFinite(n)) fail(400, `${name} must be a number`, { code: 'bad_query' });
  return n;
}

route('GET', 'api/cloud/offers', ({ query }) => {
  for (const key of query.keys()) if (!ALLOWED.has(key)) fail(400, `unknown parameter '${key.slice(0, 40)}'`, { code: 'bad_query' });
  const sort = query.get('sort') ?? 'auto';
  if (!SORTS.some((s) => s.id === sort)) fail(400, 'sort must be one of: ' + SORTS.map((s) => s.id).join(', '), { code: 'bad_query' });
  const limit = numParam(query, 'limit') ?? 50;
  const offset = numParam(query, 'offset') ?? 0;
  if (limit < 1 || limit > 200) fail(400, 'limit must be between 1 and 200', { code: 'bad_query' });
  if (offset < 0) fail(400, 'offset must be 0 or more', { code: 'bad_query' });

  const job = jobOf(query.get('scene_ids') ?? query.get('scene'), query.get('op'), query.get('preset'));
  if (!job && (sort === 'est_total' || sort === 'est_time')) fail(400, `sort '${sort}' needs a job to estimate`, { code: 'bad_query' });

  // What the owner has connected, then the one provider a chip picked.
  const only = query.get('provider');
  const connected = MACHINES.filter((m) => PROVIDERS === 2 || m.prov === 'vast');
  const visible = only ? connected.filter((m) => m.prov === only) : connected;
  const all = visible.length;

  const dc = flag(query.get('datacenter_only')) ?? BUDGET.dcOnly;
  const verified = flag(query.get('verified'));
  const wantRented = flag(query.get('rented')) === true;
  const offerId = query.get('offer_id');
  const gpus = [query.get('gpu'), ...(query.get('gpus') ?? '').split(',')].filter((g): g is string => !!g && g.trim() !== '').map(norm);
  const countries = (query.get('countries') ?? '').split(',').map((c) => c.trim().toUpperCase()).filter((c) => c !== '');
  const numGpus = numParam(query, 'num_gpus');
  const maxGpus = numParam(query, 'max_gpus');
  const minDisk = numParam(query, 'min_disk_gb') ?? 0;
  const minRel = numParam(query, 'min_reliability') ?? 0;
  const minHours = numParam(query, 'min_duration_hours') ?? 0;
  const minCuda = numParam(query, 'min_cuda') ?? 0;
  const minDriver = numParam(query, 'min_driver') ?? 0;
  const maxPrice = numParam(query, 'max_price');
  const rental = query.get('rental_type') ?? 'on-demand';

  const matches = (m: Machine): boolean => {
    if (offerId !== null) return m.id === offerId;
    if (rental !== 'on-demand') return false;
    if (m.rented && !wantRented) return false;
    if (dc && m.tier !== 'dc') return false;
    if (verified === true && m.ver !== 1) return false;
    if (verified === false && m.ver !== 0) return false;
    if (maxPrice !== null && m.price > maxPrice) return false;
    if (m.free < minDisk || m.rel / 100 < minRel || m.days * 24 < minHours || m.cuda < minCuda || m.drv < minDriver) return false;
    if (countries.length && !countries.includes(m.country)) return false;
    if (gpus.length && !gpus.includes(norm(m.g.name))) return false;
    if (numGpus !== null) {
      const hi = maxGpus !== null && maxGpus > numGpus ? maxGpus : numGpus;
      if (m.n < numGpus || m.n > hi) return false;
    }
    return true;
  };

  const entries = visible.filter(matches).map((m) => entryOf(m, job));
  const perfOf = (e: Entry): number => e.m.g.perf * e.m.n;
  const tfOf = (e: Entry): number => e.m.g.tf * e.m.n;
  const vramOf = (e: Entry): number => e.m.vramPer * e.m.n;
  const sink = (e: Entry): number => (e.why ? 1e6 : 0);
  const key: Record<string, (e: Entry) => number> = {
    auto: job ? (e) => sink(e) + (e.p ? e.p.total * Math.pow(e.p.hours, 0.6) : 0) : (e) => -perfOf(e) / e.m.price,
    est_total: (e) => sink(e) + (e.p?.total ?? 0),
    est_time: (e) => sink(e) + (e.p?.minutes ?? 0),
    perf_per_dollar: (e) => -perfOf(e) / e.m.price,
    perf: (e) => -perfOf(e),
    flops_per_dollar: (e) => -tfOf(e) / e.m.price,
    flops: (e) => -tfOf(e),
    price_asc: (e) => e.m.price,
    price_desc: (e) => -e.m.price,
    vram: (e) => -vramOf(e),
    vram_per_dollar: (e) => -vramOf(e) / e.m.price,
    reliability: (e) => -e.m.rel,
  };
  entries.sort((a, b) => key[sort](a) - key[sort](b));
  const page = entries.slice(offset, offset + limit);

  // Facets count what matches the filters, as the real server's do.
  const gpuTally = new Map<string, { name: string; count: number; min_price: number; vram_gb: number }>();
  const countTally = new Map<number, number>();
  const countryTally = new Map<string, number>();
  const providerTally = new Map<string, number>();
  for (const { m } of entries) {
    const g = gpuTally.get(m.g.name) ?? { name: m.g.name, count: 0, min_price: m.price, vram_gb: m.vramPer };
    g.count += 1;
    g.min_price = Math.min(g.min_price, m.price);
    gpuTally.set(m.g.name, g);
    countTally.set(m.n, (countTally.get(m.n) ?? 0) + 1);
    countryTally.set(m.country, (countryTally.get(m.country) ?? 0) + 1);
    providerTally.set(m.prov, (providerTally.get(m.prov) ?? 0) + 1);
  }
  const hasRented = visible.filter((m) => m.rented).length;

  // Recommendations are for the owner's own switches only (providers, data centres), never the browser's other filters.
  const poolFor = job ? visible.filter((m) => !(dc && m.tier !== 'dc') && !m.rented).map((m) => entryOf(m, job)) : [];

  return {
    ok: true,
    offers: page.map((e) => offerWire(e, job)),
    total: entries.length, count: page.length, offset, limit, sort, all,
    sorts: SORTS.map((s) => ({ ...s, available: !s.needs_estimate || job !== null })),
    facets: {
      gpus: [...gpuTally.values()].sort((a, b) => b.count - a.count),
      num_gpus: [...countTally].sort((a, b) => a[0] - b[0]).map(([value, count]) => ({ value, count })),
      countries: [...countryTally].map(([code, count]) => ({ code, count })),
      providers: [...providerTally].map(([name, count]) => ({ name, count })),
      host_types: [{ id: 'datacenter', count: entries.filter((e) => e.m.tier === 'dc').length }, { id: 'community', count: entries.filter((e) => e.m.tier !== 'dc').length }],
      rental_types: [{ id: 'on-demand', available: true }, { id: 'interruptible', available: false }, { id: 'reserved', available: false }],
      ...(!LEGACY && { rented: hasRented }),
    },
    providers: PROVIDER_LIST,
    errors: {},
    truncated: false,
    filters: { datacenter_only: dc, verified, max_price: maxPrice },
    hardware_presets: job ? presetsOf(poolFor, job) : [],
    for: job ? { scene_id: job.ids[0], title: null, op: job.op, preset: job.q, label: null, estimated: true } : null,
  };
});

/* ---------- the price check before a paid run ---------- */

route('GET', 'api/cloud/quote', ({ query }) => {
  const job = jobOf(query.get('scenes'), query.get('op'), query.get('preset'));
  if (!job) fail(400, 'Pick at least one scene.', { code: 'bad_query' });
  const offerId = query.get('offer_id');
  const m = MACHINES.find((x) => x.id === offerId && (query.get('provider') === null || x.prov === query.get('provider')));
  const month = { job_cap: BUDGET.jobCap, month_cap: BUDGET.month, max_price: BUDGET.maxHour, max_job_hours: 3, month_to_date: BUDGET.spent, month_remaining: r2(Math.max(0, BUDGET.month - BUDGET.spent)) };
  const head = { op: job.op, preset: job.q, label: null, plan: { op: job.op }, queued_usd: 0 };
  const line = (extra: Record<string, unknown>): Record<string, unknown> => ({ scene_id: job.ids.join(','), title: null, estimate: true, ...month, ...extra });

  if (REFUSE) return { ...head, quotes: [line({ ok: false, est_usd: null, refusal: { code: REFUSE, message: REFUSE === 'no_licence' ? 'Add your Jasna licence code in Cloud settings first.' : 'This rental was refused.' } })], total_usd: 0 };
  if (!m) return { ...head, quotes: [line({ ok: false, est_usd: null, refusal: { code: 'offer_gone', message: 'The machine you picked is no longer available at this price. Nothing was rented; pick another.' } })], total_usd: 0 };

  const p = priceOn(m, job);
  const brief = { provider: m.prov, id: m.id, gpu: m.g.name, num_gpus: m.n, vram_gb: m.vramPer, price_per_hour: m.price, reliability: m.rel / 100, datacenter: m.tier === 'dc', location: m.loc, country: m.country };
  let refusal: { code: string; message: string } | null = null;
  if (p.total > BUDGET.jobCap) refusal = { code: 'over_job_cap', message: `Estimated ${usd(p.total)} is more than your per-job cap of ${usd(BUDGET.jobCap)}.` };
  else if (p.total > BUDGET.month - BUDGET.spent + 1e-9) refusal = { code: 'over_month_cap', message: `Estimated ${usd(p.total)} would take this month past your ${usd(BUDGET.month)} cap.` };
  return {
    ...head,
    quotes: [line({ ...estimateWire(m, p, job), ok: refusal === null, refusal, gpu: m.g.name, price_per_hour: m.price, offer: brief })],
    total_usd: p.total,
  };
});
