/// <reference types="svelte" />
/// <reference types="vite/client" />

interface Window {
  /** Worker token, written into index.html by the Python server (`__WORKER_TOKEN__`). */
  __WT?: string;
}
interface ImportMetaEnv {
  readonly VITE_MOCK?: string;
}
