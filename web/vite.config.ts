import { defineConfig, loadEnv, type Plugin } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { THEMES, DEFAULT_DARK, DEFAULT_LIGHT } from './src/lib/themes/themes.generated';

/* The saved theme is applied by a tiny inline script before the stylesheet paints, so there is no flash.
   It is built from the generated theme list, so a new palette needs no edit here. */
function themeBoot(): Plugin {
  const table: Record<string, [number, string]> = {};
  for (const t of THEMES) table[t.id] = [t.scheme === 'light' ? 1 : 0, t.canvas];
  const js = `(function(){try{var T=${JSON.stringify(table)},d=document.documentElement,p=new URLSearchParams(location.search),dk='${DEFAULT_DARK}',lt='${DEFAULT_LIGHT}',id=null,w=p.get('theme');
if(w){if(w==='dark')id=dk;else if(w==='light')id=lt;else if(T[w])id=w}
if(!id){var s=null;try{s=JSON.parse(localStorage.getItem('stashify.theme')||'null')}catch(e){}
if(s&&s.mode==='fixed'&&T[s.id])id=s.id;else{if(s&&T[s.dark]&&!T[s.dark][0])dk=s.dark;if(s&&T[s.light]&&T[s.light][0])lt=s.light;id=matchMedia('(prefers-color-scheme: light)').matches?lt:dk}}
d.dataset.theme=id;d.dataset.scheme=T[id][0]?'light':'dark';
var m=document.querySelector('meta[name=theme-color]');if(m)m.content=T[id][1];
var x=innerWidth;d.dataset.luProfile=x<600?'phone':x<900?'tablet':'desktop'}catch(e){}})();`;
  return {
    name: 'stashify-theme-boot',
    transformIndexHtml: { order: 'pre', handler: (html) => html.replace('<!--theme-boot-->', `<script>${js}</script>`) },
  };
}

/* Preload the one font file so the first paint already has it. */
function preloadFont(): Plugin {
  return {
    name: 'stashify-font-preload',
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        const name = Object.keys(ctx.bundle ?? {}).find((n) => /Manrope.*\.woff2$/.test(n));
        if (!name) return [];
        return [{ tag: 'link', injectTo: 'head', attrs: { rel: 'preload', as: 'font', type: 'font/woff2', crossorigin: '', href: `./${name}` } }];
      },
    },
  };
}

export default defineConfig(({ mode }) => ({
  base: './',
  plugins: [svelte(), themeBoot(), preloadFont()],
  build: { target: 'es2022', sourcemap: false, cssCodeSplit: false, assetsInlineLimit: 0, chunkSizeWarningLimit: 400 },
  /* dev only: forward api calls to the Python server (STASHIFY_API=http://host:port to point elsewhere) */
  server: { host: true, proxy: { '/api': loadEnv(mode, '.', '').STASHIFY_API || 'http://127.0.0.1:8710' } },
  preview: { host: true },
}));
