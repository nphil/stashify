# Stashify dashboard: three workflow mockups

Static mockups only: one HTML file per idea (inline CSS and JS), fake data, grey placeholder thumbnails.
Nothing in `webui/` or the Python was touched. Open a file in a browser; it starts on the hero state
(3 scenes picked, Cloud chosen, card picker open). Dark by default.

## The three ideas

### A · Tray (`concept-a-tray.html`) · recommended
- Idea: one sentence stays at the bottom while you browse: `3 scenes · Decensor · High detail · RTX 5090 · Frankfurt · Run ~$0.96`. Tap any part to change it. Cloud opens the card picker right above it.
- Suits you: one line to read and one button to press. A repeat job is two taps. The library never goes away.
- Trade-off: choices live in popovers, so less is visible at once. It is the least familiar pattern of the three.

### B · Inspector (`concept-b-inspector.html`)
- Idea: library on the left, and a right-hand panel that IS the job: What, Quality and Where open one at a time, with the summary and Run pinned at its foot. Cloud swaps the library for the card picker.
- Suits you: everything about the selection is visible together, and Jobs and Review reuse the same panel.
- Trade-off: the library gets narrower, the panel is tall on short screens, and on a phone it becomes a bottom sheet.

### C · Steps (`concept-c-stepped.html`)
- Idea: pick scenes, press Process, and a focus sheet asks one question at a time: What, Quality, Where, Go. The paid confirm is the last step.
- Suits you: the calmest and most guided; the money check cannot be skipped.
- Trade-off: the most taps for a repeat job (the "Same as last time" shortcut softens it), and the library is hidden while you decide.

## Recommendation: A
The one-sentence summary is always on screen, so there is nothing to remember and the job is never half-hidden.
A repeat job is: pick scenes, Run. The card picker opens where you are already looking, with 4 one-click
recommendations (card, host type, time, total) and the full marketplace browser one click away under "Advanced".
The paid confirm is a focused dialog with Cancel as the default and the full receipt (compute, data transfer,
storage) next to the caps and remaining credit.
If you want more at a glance, borrow B's right-hand panel for Jobs and Review only.

## What every concept shows
- States: scenes selected and home job, cloud with card picker, advanced marketplace browser, paid confirm with full cost breakdown, jobs running (one home, one cloud), review with before/after, Cloud settings, Runners, and edge states (no runner online, cloud not set up, no offers under the cap, over budget, loading, stale or error).
- Cloud is offered for Decensor, Decensor + upscale and Upscale, and disabled with a reason for Transcode.
- The marketplace browser is provider-neutral and self-contained. Each row names its provider. With no job attached
  (`?s=standalone`) it is a plain rental browser: no estimate column, and the action says "Rent". With a job it adds
  time and total (compute + transfer + storage) per row, sorts by them, and dims offers that cannot run the job with a short reason.
- Open any state with `?s=<state>`: `compose`, `quality`, `where`, `cloud` (hero), `breakdown`, `browse`, `details`, `sortmenu`,
  `filters`, `standalone`, `confirm`, `jobs`, `review`, `replace`, `runners`, `cloudset`, `edge` (C also has `ready`).
  Add `&theme=light`, `&prov=2` (two providers), `&job=0` (no job attached), `&ts=2` (200% text).

## Screenshots (hero `cloud` state)
For each of `a-tray`, `b-inspector`, `c-stepped`: `-1440-dark.png`, `-1440-light.png`, `-390-dark.png`,
plus `-browse-1440-dark.png` and `-browse-390-dark.png` (the advanced marketplace browser).

## Not verified
- The cost model, the Quality-to-settings mapping and the "one machine per run, safety stop per rental" rule are proposals. Every number, card and price is made up. No provider or hardware was tested.
- Checked in my own headless Chromium (no shared browser was available). A was reviewed at 1440 and 390, dark and light, at 200% text and in its key states. B and C were reviewed at their hero states and main flows, not every state at every size.
- The design interview was replaced by inference from the brief; no PRODUCT.md was created.
- Accessibility needs a proper pass when built: the "i" cost buttons sit inside selectable rows and tiles (nested interactive controls), and keyboard and screen-reader behaviour were not tested.
- Impeccable ran on A only. The detector reported one warning (small 4 px gaps dominate the spacing). They are the tight title and caption pairs inside rows and tiles, so I left them. My own review pass found two real problems, both fixed in A: tray chips overlapped at 200% text, and the picker's position was measured only once.
