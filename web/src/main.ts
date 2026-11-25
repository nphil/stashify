import './styles/fonts.css';
import './styles/tokens.css';
import './styles/themes.generated.css';
import './styles/base.css';
import './styles/library.css';
import './styles/options.css';
import './styles/picker.css';
import './styles/browser.css';
import './styles/chrome.css';
import './styles/jobs.css';
import './styles/review.css';
import './styles/settings.css';
import './styles/overlays.css';
import './styles/shell.css';
import './styles/tray.css';
import './styles/app.css';
import './styles/themes.css';
import { mount } from 'svelte';
import App from './App.svelte';
import { MOCK } from './lib/api/client';

const root = document.documentElement;
const params = new URLSearchParams(location.search);
if (params.get('shot') === '1') root.dataset.shot = '';
if (params.get('ts') === '2') root.dataset.ts = '2';
if (MOCK) root.dataset.mock = '';

/* phone / tablet / desktop decides how much chrome there is; the boot script in index.html set it for first paint */
let frame = 0;
function profile(): void {
  frame = 0;
  const w = innerWidth;
  const p = w < 600 ? 'phone' : w < 900 ? 'tablet' : 'desktop';
  if (root.dataset.luProfile !== p) root.dataset.luProfile = p;
}
addEventListener('resize', () => { if (!frame) frame = requestAnimationFrame(profile); }, { passive: true });
profile();

const target = document.getElementById('app');
if (!target) throw new Error('missing #app');
mount(App, { target });
