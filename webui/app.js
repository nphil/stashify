// Stashify dashboard — a standalone WebUI for the Stashify worker.
// Served same-origin with Stash (via the reverse-proxy path), so it talks to
// Stash's own GraphQL/media over the session cookie and to the worker for jobs.
(function () {
  "use strict";

  var PER = 36;
  var TOKEN = "";
  var state = { page: 1, count: 0, sel: new Set(), scenes: {}, dismissed: new Set(), pollTimer: null };

  var $ = function (id) { return document.getElementById(id); };
  var conn = $("conn"), grid = $("grid"), joblist = $("joblist"), jobsEmpty = $("jobs-empty");

  // ---- helpers ----------------------------------------------------------- //
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  var toastTimer;
  function toast(msg, isErr) {
    var t = $("toast"); t.textContent = msg; t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.className = "toast"; }, 3200);
  }
  function fmtDur(s) {
    s = Math.round(s || 0); var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return h ? h + ":" + p(m) + ":" + p(x) : m + ":" + p(x);
  }
  function fmtSize(b) {
    if (!b) return ""; var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return b.toFixed(b < 10 && i > 0 ? 1 : 0) + " " + u[i];
  }
  function resLabel(h) { return h >= 2160 ? "4K" : h ? h + "p" : ""; }

  // ---- backends ---------------------------------------------------------- //
  function workerUrl(p) { return new URL("api/" + p, document.baseURI).toString(); }
  async function workerFetch(p, opts) {
    opts = opts || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (TOKEN) headers["X-Decensor-Token"] = TOKEN;
    var r = await fetch(workerUrl(p), Object.assign({}, opts, { headers: headers }));
    var text = await r.text(), body;
    try { body = text ? JSON.parse(text) : {}; }
    catch (e) { throw new Error("worker HTTP " + r.status + (r.ok ? " (non-JSON)" : "")); }
    if (!r.ok) throw new Error(body.error || ("worker HTTP " + r.status));
    return body;
  }
  async function stashGQL(query, variables) {
    var r = await fetch("/graphql", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify({ query: query, variables: variables || {} }),
    });
    if (r.status === 401 || r.status === 403) throw new Error("Not logged in to Stash");
    var j = await r.json();
    if (j.errors && j.errors.length) throw new Error(j.errors[0].message);
    return j.data;
  }

  async function loadToken() {
    // The worker injects its token into the served page, so the dashboard is
    // self-contained and works from any origin (e.g. a dedicated subdomain).
    if (typeof window.__WT === "string" && window.__WT !== "__WORKER_TOKEN__") { TOKEN = window.__WT; return; }
    try {
      var d = await stashGQL("query { configuration { plugins } }");
      TOKEN = (((d.configuration || {}).plugins || {}).stashify || {}).workerToken || "";
    } catch (e) { /* worker may accept unauthenticated */ }
  }
  var health = null;
  var ENGINE_LABEL = { "lada": "decensor", "lada+up": "decensor + upscale", "upscale": "upscale 2x", "transcode": "transcode" };
  function connLabel() {
    // reflect the SELECTED operation + pinned engine, not an internal backend id
    var lbl = ENGINE_LABEL[$("engine").value] || (health ? health.backend : "?");
    var ep = $("enginePin");
    if (ep && !ep.hidden && ep.value && ep.value !== "auto") lbl += " · " + engNice(ep.value);
    return lbl;
  }
  function renderConn() {
    if (!health) return;
    conn.className = "conn ok";
    conn.textContent = "● " + connLabel() + " · GPU " + health.gpu;
  }
  async function updateConn() {
    try {
      health = await workerFetch("health");
      renderConn();
    } catch (e) {
      conn.className = "conn err"; conn.textContent = "worker unreachable";
    }
    loadRunnerCaps();   // independent of health; drives the engine/runner pickers
  }

  // ---- engine + runner pickers (built from live runner capabilities) ----- //
  var ENGINE_NICE = { lada: "Lada", jasna: "Jasna", span: "SPAN 2×" };
  function engNice(e) { return ENGINE_NICE[e] || e || ""; }
  function opForOperation(op) {
    return op === "lada" ? "decensor"
      : op === "lada+up" ? "decensor+upscale"
      : op === "upscale" ? "upscale"
      : op === "transcode" ? "transcode" : op;
  }
  state.runnerCaps = [];                 // online runners: [{name, ops, engines, ...}]
  var _pinRestore = { engine: null, runner: null };   // localStorage values, applied once options exist

  async function loadRunnerCaps() {
    try {
      var rs = await workerFetch("runners");
      state.runnerCaps = (rs || []).filter(function (r) { return r.online; });
    } catch (e) { /* keep last-known caps */ }
    rebuildPins();
    if (typeof updateRtx === "function") updateRtx();
    var any = state.runnerCaps.length > 0;   // gate the controls on real availability, not env
    ["engine", "enginePin", "runnerPin"].forEach(function (id) { var s = $(id); if (s) s.disabled = !any; });
    if ($("engine")) $("engine").title = any ? "Operation" : "no compute runner online";
  }
  function runnersForOp(op) {
    return state.runnerCaps.filter(function (r) { return (r.ops || []).indexOf(op) >= 0; });
  }
  function enginesForOp(op) {
    var set = [];
    runnersForOp(op).forEach(function (r) {
      var e = (r.engines || {})[op];
      if (e && set.indexOf(e) < 0) set.push(e);
    });
    return set;
  }
  function fillSelect(sel, autoLabel, values, labelFn, restore) {
    var want = restore || sel.value;       // preserve the current choice across rebuilds
    sel.innerHTML = "";
    var o0 = document.createElement("option");
    o0.value = "auto"; o0.textContent = autoLabel; sel.appendChild(o0);
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = labelFn ? labelFn(v) : v; sel.appendChild(o);
    });
    sel.value = Array.prototype.some.call(sel.options, function (o) { return o.value === want; }) ? want : "auto";
  }
  function rebuildPins() {
    var epin = $("enginePin"), rpin = $("runnerPin");
    if (!epin || !rpin) return;
    // One-shot restore: consume the saved pins on the first rebuild that has real
    // capabilities, then null them so subsequent rebuilds preserve the LIVE
    // selection (fillSelect falls back to sel.value). Without this, a saved pin
    // that's never valid for the current op would clobber the user's choice on
    // every periodic refresh.
    var haveCaps = state.runnerCaps.length > 0;
    var wantE = _pinRestore.engine, wantR = _pinRestore.runner;
    if (haveCaps) { _pinRestore.engine = null; _pinRestore.runner = null; }
    var op = opForOperation($("engine").value);
    // engine pin — only where there's a real engine choice (never for transcode)
    var engines = op === "transcode" ? [] : enginesForOp(op);
    fillSelect(epin, "Auto engine", engines, engNice, wantE);
    epin.hidden = op === "transcode" || engines.length === 0;
    // runner pin — online runners that can do op and match the chosen engine
    var chosenEng = epin.value;
    var runners = runnersForOp(op).filter(function (r) {
      return chosenEng === "auto" || (r.engines || {})[op] === chosenEng;
    }).map(function (r) { return r.name; });
    fillSelect(rpin, "Auto runner", runners, null, wantR);
  }
  // RTX Super Res is a jasna-only secondary detail pass. Offer it only for a
  // decensor-family op where a jasna runner is actually online, and never when
  // the engine pin is forced to a non-jasna engine (e.g. lada).
  function updateRtx() {
    var wrap = $("rtxWrap");
    if (!wrap) return;
    var op = opForOperation($("engine").value);
    var isDecensor = op === "decensor" || op === "decensor+upscale";
    var jasnaAvail = enginesForOp(op).indexOf("jasna") >= 0;
    var epin = $("enginePin");
    var pin = epin ? epin.value : "auto";
    var pinOk = pin === "auto" || pin === "jasna";
    wrap.hidden = !(isDecensor && jasnaAvail && pinOk);
    // denoise/deblur pickers only matter once a mode is chosen
    var mode = $("rtxMode") ? $("rtxMode").value : "off";
    var on = !wrap.hidden && mode !== "off";
    if ($("rtxDenoise")) $("rtxDenoise").hidden = !on;
    if ($("rtxDeblur")) $("rtxDeblur").hidden = !on;
    // live segment preview shares the same gate (jasna decensor on the 3080)
    if ($("previewWrap")) $("previewWrap").hidden = !(isDecensor && jasnaAvail && pinOk);
  }
  var _previewTimer;
  function updatePreview() { clearTimeout(_previewTimer); _previewTimer = setTimeout(doPreview, 150); }
  async function doPreview() {
    var will = $("routeWill");
    if (!will) return;
    var op = opForOperation($("engine").value);
    var eng = $("enginePin") ? $("enginePin").value : "auto";
    var run = $("runnerPin") ? $("runnerPin").value : "auto";
    try {
      var res = await workerFetch("route-preview?op=" + encodeURIComponent(op) +
        "&engine=" + encodeURIComponent(eng) + "&runner=" + encodeURIComponent(run));
      if (res.error) { will.className = "routewill err"; will.textContent = "→ " + res.error; }
      else {
        will.className = "routewill";
        will.textContent = "→ " + (res.runner || "?") + (res.engine ? " · " + engNice(res.engine) : "");
      }
    } catch (e) { will.className = "routewill"; will.textContent = ""; }
  }

  // ---- scene browser ----------------------------------------------------- //
  var SCENE_Q = "query($filter: FindFilterType) {" +
    " findScenes(filter: $filter) { count scenes {" +
    " id title date files { path width height duration size } studio { name } tags { name } } } }";

  function isDone(scene) {
    // shows the DONE badge on cards for processed scenes
    return (scene.tags || []).some(function (t) { return /^(Decensored|Upscaled)/i.test(t.name); });
  }

  var TAG_POPULAR = 18;   // "most used" tags shown when the picker opens without a query

  async function loadTags() {
    // live tag list from Stash (busiest first) backing the searchable tag picker
    try { state.tags = (await workerFetch("tags")) || []; }
    catch (e) { state.tags = []; }
    var saved = "";
    try { saved = localStorage.getItem("dc_tag") || ""; } catch (e) {}
    var cur = (state.tags || []).filter(function (t) { return String(t.id) === String(saved); })[0];
    $("tagf").value = cur ? cur.id : "";
    $("tagSearch").value = cur ? cur.name : "";
  }

  function tagPick(t) {
    // t = tag object, or null to clear to "All tags"
    $("tagf").value = t ? t.id : "";
    $("tagSearch").value = t ? t.name : "";
    try { localStorage.setItem("dc_tag", $("tagf").value); } catch (e) {}
    if ($("tagMenu")) $("tagMenu").hidden = true;
    state.page = 1;
    loadScenes();
  }

  function tagMenuRender(q) {
    var menu = $("tagMenu");
    if (!menu) return;
    var tags = state.tags || [];
    q = (q || "").trim().toLowerCase();
    var list = q
      ? tags.filter(function (t) { return t.name.toLowerCase().indexOf(q) >= 0; }).slice(0, 40)
      : tags.slice(0, TAG_POPULAR);   // already busiest-first from the API
    var sel = String($("tagf").value || "");
    menu.innerHTML = "";
    var all = el("div", "tagmenu-item all" + (sel ? "" : " on"));
    all.appendChild(el("span", "tagmenu-name", "All tags"));
    all.onmousedown = function (e) { e.preventDefault(); tagPick(null); };
    menu.appendChild(all);
    if (!q) menu.appendChild(el("div", "tagmenu-hdr", "Most used"));
    else if (!list.length) menu.appendChild(el("div", "tagmenu-empty", "No matching tags"));
    list.forEach(function (t) {
      var row = el("div", "tagmenu-item" + (sel === String(t.id) ? " on" : ""));
      var nm = el("span", "tagmenu-name");
      nm.textContent = t.name;                 // textContent (not el's innerHTML arg): XSS-safe
      row.appendChild(nm);
      row.appendChild(el("span", "tagmenu-count", String(t.scene_count)));
      // mousedown (not click) so it fires before the input's blur closes the menu
      row.onmousedown = function (e) { e.preventDefault(); tagPick(t); };
      menu.appendChild(row);
    });
    menu.hidden = false;
  }

  async function loadScenes() {
    grid.innerHTML = "<div class='empty'>Loading…</div>";
    var sort = $("sort").value;
    var qs = "scenes?q=" + encodeURIComponent($("search").value.trim()) +
      "&page=" + state.page + "&per_page=" + PER + "&sort=" + encodeURIComponent(sort) +
      "&tag=" + encodeURIComponent($("tagf").value);   // server-side tag filter
    var res;
    try { res = await workerFetch(qs); }
    catch (e) {
      grid.innerHTML = "<div class='empty'>Couldn't load scenes: " + esc(e.message) +
        "<br>Check the worker can reach Stash (STASH_URL / API key).</div>";
      return;
    }
    state.count = res.count;
    var minres = parseInt($("minres").value, 10) || 0;
    grid.innerHTML = "";
    var shown = 0;
    res.scenes.forEach(function (s) {
      var f = (s.files || [])[0] || {};
      if (minres && (f.height || 0) < minres) return;
      state.scenes[s.id] = { title: s.title || (f.path || "").split(/[\\/]/).pop() };
      shown++;
      grid.appendChild(sceneCard(s, f));
    });
    if (!shown) grid.innerHTML = "<div class='empty'>No matching scenes on this page.</div>";
    var pages = Math.max(1, Math.ceil(state.count / PER));
    $("pageinfo").textContent = "Page " + state.page + " / " + pages + " · " + state.count + " scenes";
    $("prev").disabled = state.page <= 1; $("next").disabled = state.page >= pages;
    refreshSelBtn();
  }

  function sceneCard(s, f) {
    var title = state.scenes[s.id].title;
    var card = el("div", "card" + (state.sel.has(s.id) ? " sel" : ""));
    card.dataset.id = s.id;
    var thumb = el("div", "thumb");
    thumb.style.backgroundImage = "url(" + workerUrl("img/" + s.id) + ")";
    card.appendChild(thumb);
    if (f.height) card.appendChild(el("span", "badge", resLabel(f.height)));
    card.appendChild(el("div", "tick", "✓"));
    if (isDone(s)) card.appendChild(el("span", "done", "DONE"));
    var meta = el("div", "meta");
    meta.appendChild(el("div", "t", esc(title)));
    var sub = "";
    if (f.duration) sub += "<span>" + fmtDur(f.duration) + "</span>";
    if (f.size) sub += "<span>" + fmtSize(f.size) + "</span>";
    if (s.studio && s.studio.name) sub += "<span>" + esc(s.studio.name) + "</span>";
    meta.appendChild(el("div", "sub", sub));
    card.appendChild(meta);
    card.onclick = function () { toggleSel(s.id, card); };
    return card;
  }

  function toggleSel(id, card) {
    if (state.sel.has(id)) { state.sel.delete(id); card.classList.remove("sel"); }
    else { state.sel.add(id); card.classList.add("sel"); }
    refreshSelBtn();
  }
  function refreshSelBtn() {
    var b = $("decensorSel");
    var e = $("engine").value;
    var verb = e === "upscale" ? "Upscale" : (e === "transcode" ? "Transcode" : "Decensor");
    b.textContent = verb + " selected (" + state.sel.size + ")";
    b.disabled = state.sel.size === 0;
  }

  async function decensorSelected() {
    var ids = Array.from(state.sel);
    if (!ids.length) return;
    var eng = $("engine").value;
    var backend = eng === "upscale" ? "upscale" : (eng === "transcode" ? "transcode" : "decensor");
    var extra = { backend: backend };
    if (backend === "decensor") extra.detection_model = $("ladaq").value;
    if (eng === "lada+up") extra.post_upscale = true;
    if (eng === "transcode" && $("txq").value) extra.transcode_height = $("txq").value;
    // per-job overrides: pin a specific engine and/or runner (Auto = coordinator decides)
    var epin = $("enginePin"), rpin = $("runnerPin");
    if (epin && epin.value !== "auto" && !epin.hidden) extra.engine = epin.value;
    if (rpin && rpin.value !== "auto") extra.runner = rpin.value;
    // RTX Super Res: jasna-only secondary detail pass. Preset maps to quality+scale;
    // denoise/deblur are sent only when overridden ("auto" = engine default). Force
    // the jasna engine so it can't route to a runner that ignores it.
    var rtxMode = $("rtxMode");
    if (rtxMode && !$("rtxWrap").hidden && rtxMode.value !== "off" && backend === "decensor") {
      extra.secondary = "rtx-super-res";
      if (rtxMode.value === "high2") { extra.rtx_quality = "high"; extra.rtx_scale = "2"; }
      else { extra.rtx_quality = "ultra"; extra.rtx_scale = "4"; }
      var dn = $("rtxDenoise"); if (dn && dn.value !== "auto") extra.rtx_denoise = dn.value;
      var db = $("rtxDeblur"); if (db && db.value !== "auto") extra.rtx_deblur = db.value;
      if (!extra.engine) extra.engine = "jasna";
    }
    // Live segment preview: jasna-only smart mode; force jasna so it can't route away.
    var pv = $("previewToggle");
    if (pv && pv.checked && !$("previewWrap").hidden && backend === "decensor") {
      extra.preview = true;
      if (!extra.engine) extra.engine = "jasna";
    }
    var ok = 0;
    for (var i = 0; i < ids.length; i++) {
      try {
        await workerFetch("decensor", { method: "POST", body: JSON.stringify(Object.assign({ scene_id: ids[i] }, extra)) });
        ok++;
      }
      catch (e) { toast("Failed to queue scene " + ids[i] + ": " + e.message, true); }
    }
    state.sel.clear();
    document.querySelectorAll(".card.sel").forEach(function (c) { c.classList.remove("sel"); });
    refreshSelBtn();
    if (ok) toast("Queued " + ok + " scene" + (ok > 1 ? "s" : ""));
    pollJobs();
  }

  // ---- jobs -------------------------------------------------------------- //
  var RUNNING = { queued: 1, running: 1, replacing: 1, discarding: 1 };
  var jobEls = {};                 // jobId -> { el, sig }; persisted across polls
  state.logs = {};                 // jobId -> { cursor, open, follow, body, count, lines }

  function jobName(j) { return (state.scenes[j.scene_id] || {}).title || ("Scene " + j.scene_id); }

  // Structural signature: rebuild a card only when this changes; otherwise update
  // it in place so the live-log's DOM and scroll position survive each poll.
  function jobSig(j) {
    if (j.state === "review_ready") return "review:" + (j.review_scene_id ? 1 : 0);
    if (j.state === "replaced" || j.state === "discarded" || j.state === "cancelled") return "done:" + j.state;
    if (j.state === "error") return "error";
    return "active:" + j.state;    // queued | running | replacing | discarding
  }
  function showsLog(j) { return j.state === "running" || j.state === "error"; }

  async function pollJobs() {
    var jobs;
    try { jobs = await workerFetch("jobs"); }
    catch (e) { return; }
    jobs = jobs.filter(function (j) { return !state.dismissed.has(j.id); });
    jobs.reverse();                                 // newest first
    jobsEmpty.style.display = jobs.length ? "none" : "block";

    var seen = {};
    jobs.forEach(function (j) {
      seen[j.id] = 1;
      var sig = jobSig(j), entry = jobEls[j.id];
      if (!entry) {
        jobEls[j.id] = { el: buildJobCard(j), sig: sig };
      } else if (entry.sig !== sig) {
        var fresh = buildJobCard(j);
        if (entry.el.parentNode) entry.el.parentNode.replaceChild(fresh, entry.el);
        entry.el = fresh; entry.sig = sig;
      }
      updateJobCard(jobEls[j.id].el, j);
    });
    Object.keys(jobEls).forEach(function (id) { if (!seen[id]) removeJob(id); });
    reorderJobs(jobs.map(function (j) { return j.id; }));   // attach new + order; no-op when stable
    jobs.forEach(function (j) { if (showsLog(j)) fetchLog(j); });
  }

  function removeJob(id) {
    var e = jobEls[id];
    if (e && e.el.parentNode) e.el.parentNode.removeChild(e.el);
    delete jobEls[id]; delete state.logs[id];
  }
  // Minimal DOM moves: only touches nodes whose position is actually wrong, so a
  // running job's scrolled log is left untouched in the steady state.
  function reorderJobs(order) {
    order.forEach(function (id, i) {
      var want = jobEls[id] && jobEls[id].el;
      if (!want) return;
      if (joblist.children[i] !== want) joblist.insertBefore(want, joblist.children[i] || null);
    });
  }

  // ---- stats + log rendering --------------------------------------------- //
  function statChip(g, label, val) {
    if (val == null || val === "") return;
    var s = el("div", "stat");
    s.appendChild(el("span", "sl", label));
    var v = el("span", "sv");
    v.textContent = val;      // val can carry runner/engine/stage from a runner - never innerHTML (XSS)
    s.appendChild(v);
    g.appendChild(s);
  }
  function fillStats(g, j, pct) {
    g.innerHTML = "";
    statChip(g, "progress", pct + "%");
    if (j.runner) statChip(g, "runner", j.runner + (j.engine ? " · " + engNice(j.engine) : ""));
    if (j.stage) statChip(g, "stage", j.stage);
    if (j.frame != null && j.total_frames) statChip(g, "frame", j.frame + " / " + j.total_frames);
    if (j.fps != null) statChip(g, "fps", Math.round(j.fps * 10) / 10);
    if (j.eta != null) statChip(g, "eta", fmtDur(j.eta));
    if (j.elapsed != null) statChip(g, "elapsed", fmtDur(j.elapsed));
    var gp = j.gpu_stats || {};
    if (gp.util != null) statChip(g, "gpu", Math.round(gp.util) + "%");
    if (gp.mem_used != null && gp.mem_total != null)
      statChip(g, "vram", Math.round(gp.mem_used) + " / " + Math.round(gp.mem_total) + " MB");
    if (gp.temp != null) statChip(g, "temp", Math.round(gp.temp) + "°C");
    if (gp.power != null) statChip(g, "power", Math.round(gp.power) + " W");
  }

  function logLine(ln) {
    var d = el("div", "ln lv-" + (ln.level || "proc"));
    d.textContent = ln.text;
    return d;
  }
  function applyOpen(wrap, caret, open) {
    wrap.classList.toggle("collapsed", !open);
    caret.textContent = open ? "▾" : "▸";
  }
  function logBox(j, live) {
    var wrap = el("div", "logwrap");
    var head = el("div", "loghead");
    var caret = el("span", "caret");
    head.appendChild(caret);
    head.appendChild(el("span", "loglbl", "live log"));
    if (live) head.appendChild(el("span", "live"));
    head.appendChild(el("span", "spacer"));
    var count = el("span", "logcount");
    head.appendChild(count);
    var body = el("pre", "joblog");
    wrap.appendChild(head); wrap.appendChild(body);

    var store = state.logs[j.id];
    if (!store) store = state.logs[j.id] = {
      cursor: 0, open: (j.state === "running" || j.state === "error"), follow: true, lines: [],
    };
    store.body = body; store.count = count;
    if (store.lines.length) store.lines.forEach(function (ln) { body.appendChild(logLine(ln)); });
    else body.appendChild(el("div", "empty-log", "waiting for output…"));
    count.textContent = store.lines.length ? store.lines.length + " lines" : "";
    applyOpen(wrap, caret, store.open);
    if (store.follow) body.scrollTop = body.scrollHeight;

    head.onclick = function () {
      store.open = !store.open;
      applyOpen(wrap, caret, store.open);
      if (store.open) { store.follow = true; body.scrollTop = body.scrollHeight; }
    };
    body.addEventListener("scroll", function () {
      store.follow = (body.scrollHeight - body.scrollTop - body.clientHeight) < 24;
    });
    return wrap;
  }
  async function fetchLog(j) {
    var store = state.logs[j.id];
    if (!store || !store.body) return;
    if (j.log_cursor != null && j.log_cursor === store.cursor) return;   // nothing new
    var res;
    try { res = await workerFetch("jobs/" + j.id + "/log?after=" + store.cursor); }
    catch (e) { return; }
    if (res.cursor != null) store.cursor = res.cursor;
    if (!res.lines || !res.lines.length) return;
    var body = store.body, empty = body.querySelector(".empty-log");
    if (empty) body.removeChild(empty);
    res.lines.forEach(function (ln) {
      store.lines.push(ln);
      body.appendChild(logLine(ln));
    });
    while (store.lines.length > 500) { store.lines.shift(); if (body.firstChild) body.removeChild(body.firstChild); }
    if (store.count) store.count.textContent = store.lines.length + " lines";
    if (store.follow) body.scrollTop = body.scrollHeight;
  }

  // ---- job cards --------------------------------------------------------- //
  function dismissRow(id) {
    var d = el("div", "row"), b = el("button", "btn", "Dismiss");
    b.onclick = function () {
      state.dismissed.add(id); removeJob(id);
      jobsEmpty.style.display = Object.keys(jobEls).length ? "none" : "block";
    };
    d.appendChild(b);
    return d;
  }

  function buildJobCard(j) {
    var c = el("div", "job");
    c._refs = {};
    var title = el("div", "jt");
    if (j.state === "running") title.appendChild(el("span", "livedot"));
    title.appendChild(document.createTextNode(jobName(j)));
    c._refs.titleText = title.lastChild;
    c.appendChild(title);

    if (j.state === "review_ready") {
      c.appendChild(el("div", "jmsg ok", "Preview ready — review it:"));
      if (j.review_scene_id) {
        var v = el("video"); v.controls = true; v.preload = "metadata";
        v.src = workerUrl("vid/" + j.review_scene_id); c.appendChild(v);
      } else {
        c.appendChild(el("div", "jmsg", "(preview not indexed yet — Stash was busy; you can still replace)"));
      }
      var row = el("div", "row");
      var rep = el("button", "btn btn-danger", "Replace original");
      var dis = el("button", "btn", "Discard");
      rep.onclick = function () { rep.disabled = dis.disabled = true; jobAction(j.id, "replace"); };
      dis.onclick = function () { rep.disabled = dis.disabled = true; jobAction(j.id, "discard"); };
      row.appendChild(rep); row.appendChild(dis); c.appendChild(row);
      return c;
    }
    if (j.state === "replaced" || j.state === "discarded" || j.state === "cancelled") {
      var txt = j.state === "replaced" ? "Original replaced ✓"
        : (j.state === "cancelled" ? "Cancelled" : "Preview discarded");
      c.appendChild(el("div", "jmsg" + (j.state === "cancelled" ? "" : " ok"), txt));
      c.appendChild(dismissRow(j.id));
      return c;
    }
    if (j.state === "error") {
      c._refs.err = el("div", "jmsg err", esc(j.error || j.message || "Failed"));
      c.appendChild(c._refs.err);
      c.appendChild(logBox(j, false));
      c.appendChild(dismissRow(j.id));
      return c;
    }
    // active: queued | running | replacing | discarding
    if (j.state === "running") c.classList.add("active");
    c._refs.msg = el("div", "jmsg");
    c.appendChild(c._refs.msg);
    var bar = el("div", "bar");
    c._refs.fill = el("div", "fill");
    bar.appendChild(c._refs.fill);
    c._refs.bar = bar;
    c.appendChild(bar);
    c._refs.stats = el("div", "stats");
    c.appendChild(c._refs.stats);
    if (j.state === "running") {
      // Live compare (lada): the decensored feed (tail of the file being
      // written) next to the CENSORED ORIGINAL, seek-locked to its playhead.
      var duo = el("div", "duo");
      duo.hidden = true;
      var caps = {};
      var mkv = function (key, label, live) {
        var f = el("figure");
        var v = el("video");
        v.muted = true; v.playsInline = true;
        if (live) { v.autoplay = true; v.controls = true; }
        else { v.preload = "auto"; }
        f.appendChild(v);
        var cap = el("figcaption", live ? "lbl-live" : null, label);
        cap.onclick = function () { duo.classList.toggle("stacked"); };
        cap.title = "toggle large view";
        f.appendChild(cap);
        duo.appendChild(f);
        caps[key] = cap;
        return v;
      };
      var cens = mkv("cens", "Censored (original)", false);
      var lv = mkv("live", "Decensored · live", true);
      c._refs.capCens = caps.cens; c._refs.capLive = caps.live;
      lv.addEventListener("loadeddata", function () { duo.hidden = false; });
      // The original PLAYS alongside the live feed (smooth), paired to its
      // play/pause/seek and drift-corrected only when >1s out of step —
      // constant seek-snapping made it jitter.
      var follow = function () { cens.play && cens.play().catch(function () {}); };
      var hold = function () { cens.pause(); };
      var snap = function () {
        if (cens.readyState >= 1) cens.currentTime = lv.currentTime;
      };
      lv.addEventListener("play", follow);
      lv.addEventListener("playing", follow);
      lv.addEventListener("pause", hold);
      lv.addEventListener("waiting", hold);
      lv.addEventListener("seeked", snap);
      lv.addEventListener("loadeddata", snap);
      lv.addEventListener("timeupdate", function () {
        if (cens.readyState >= 1 && Math.abs(cens.currentTime - lv.currentTime) > 1.0) snap();
      });
      c._refs.live = lv; c._refs.cens = cens; c._refs.duo = duo;
      c.appendChild(duo);
      // still-frame before/after (DeepMosaics jobs; lada uses the duo above)
      var pv = el("div", "pv");
      pv.hidden = true;
      var mk = function (which, label) {
        var f = el("figure");
        var img = el("img");
        img.alt = label;
        f.appendChild(img);
        f.appendChild(el("figcaption", null, label));
        pv.appendChild(f);
        return img;
      };
      c._refs.pvBefore = mk("before", "Censored");
      c._refs.pvAfter = mk("after", "Decensored");
      c._refs.pv = pv;
      pv._last = 0;
      c.appendChild(pv);
      c.appendChild(logBox(j, true));
    }
    // Live segment preview (jasna 0.8.0 smart mode): per mosaic segment, the
    // decensored clip next to the original, seek-locked, with a segment picker.
    // Built for every card so it also shows on done/review jobs.
    var segw = el("div", "segprev");
    segw.hidden = true;
    var segLbl = el("div", "seglbl", "Mosaic segments — original vs decensored");
    segw.appendChild(segLbl);
    var segSel = el("div", "segsel");
    segw.appendChild(segSel);
    var sduo = el("div", "duo segduo");
    var mkSeg = function (label) {
      var f = el("figure");
      var v = el("video");
      v.muted = true; v.playsInline = true; v.loop = true; v.controls = true; v.preload = "auto";
      f.appendChild(v);
      f.appendChild(el("figcaption", null, label));
      sduo.appendChild(f);
      return v;
    };
    var segB = mkSeg("Censored (original)");
    var segA = mkSeg("Decensored");
    segw.appendChild(sduo);
    var segsnap = function () { if (segB.readyState >= 1) segB.currentTime = segA.currentTime; };
    segA.addEventListener("play", function () { segB.play().catch(function () {}); });
    segA.addEventListener("pause", function () { segB.pause(); });
    segA.addEventListener("seeked", segsnap);
    segA.addEventListener("loadeddata", segsnap);
    segA.addEventListener("timeupdate", function () {
      if (segB.readyState >= 1 && Math.abs(segB.currentTime - segA.currentTime) > 0.2) segsnap();
    });
    c._refs.segw = segw; c._refs.segSel = segSel; c._refs.segA = segA; c._refs.segB = segB;
    c._refs.segCount = 0; c._refs.segN = -1;
    c.appendChild(segw);
    // Decensored-only sample reel (built at job end): every restored segment back to
    // back, so you can judge overall quality smoothly without seeking the full video.
    var sampw = el("div", "sampleprev");
    sampw.hidden = true;
    sampw.appendChild(el("div", "seglbl", "Decensored sample — restored portions only"));
    var sampV = el("video");
    sampV.muted = true; sampV.playsInline = true; sampV.loop = true; sampV.controls = true; sampV.preload = "auto";
    sampw.appendChild(sampV);
    c._refs.sampw = sampw; c._refs.sampV = sampV; c._refs.sampSet = false;
    c.appendChild(sampw);
    var ctl = el("div", "row");
    if (j.state === "running") {
      var pr = el("button", "btn", "Pause");
      pr.onclick = function () { pr.disabled = true; jobAction(j.id, pr.dataset.act || "pause"); };
      c._refs.pause = pr; ctl.appendChild(pr);
    }
    if (j.state === "running" || j.state === "queued") {
      var cx = el("button", "btn btn-danger", "Cancel");
      cx.onclick = function () { cx.disabled = true; jobAction(j.id, "cancel"); };
      ctl.appendChild(cx);
    }
    if (ctl.children.length) c.appendChild(ctl);
    return c;
  }

  function selectSeg(c, j, i) {
    var r = c._refs || {};
    if (!r.segA) return;
    r.segN = i;
    var base = workerUrl("jobs/" + j.id + "/seg/" + i + "/");
    r.segA.src = base + "after.mp4";
    r.segB.src = base + "before.mp4";
    r.segA.load(); r.segB.load();
    r.segA.play && r.segA.play().catch(function () {});
    [].forEach.call(r.segSel.children, function (ch, k) {
      ch.className = "segchip" + (k === i ? " on" : "");
    });
  }

  function updateJobCard(c, j) {
    var r = c._refs || {};
    if (r.segw && j.segments && j.segments.length && j.segments.length !== r.segCount) {
      r.segCount = j.segments.length;
      r.segSel.innerHTML = "";
      j.segments.forEach(function (s, i) {
        var chip = el("button", "segchip", "seg " + (i + 1));
        chip.title = (s.start != null ? s.start + "–" + s.end + "s" : "");
        chip.onclick = function () { selectSeg(c, j, i); };
        r.segSel.appendChild(chip);
      });
      r.segw.hidden = false;
      if (r.segN < 0) selectSeg(c, j, 0);            // auto-show the first as it arrives
      else [].forEach.call(r.segSel.children, function (ch, k) {
        ch.className = "segchip" + (k === r.segN ? " on" : "");
      });
    }
    if (r.sampw && j.sample && !r.sampSet) {         // the concatenated sample reel is ready
      r.sampSet = true;
      r.sampV.src = workerUrl("jobs/" + j.id + "/sample.mp4");
      r.sampV.load();
      r.sampw.hidden = false;
    }
    if (r.titleText) r.titleText.nodeValue = jobName(j);
    if (j.state === "error") { if (r.err) r.err.textContent = j.error || j.message || "Failed"; return; }
    if (!r.bar) return;                              // review_ready / done: nothing live
    var pct = Math.round((j.progress || 0) * 100);
    if (r.msg) {
      r.msg.textContent = j.paused ? "Paused" : (j.message || j.state);
      r.msg.className = "jmsg" + (j.paused ? " paused" : "");
    }
    r.bar.className = "bar" + (j.paused ? " is-paused" : "");
    r.fill.style.width = pct + "%";
    if (r.stats) fillStats(r.stats, j, pct);
    if (r.pv && j.preview && j.backend !== "decensor" && j.backend !== "lada" && j.backend !== "upscale") {
      // still-frame pair (legacy/command backends): runner ops use the video duo
      var now = Date.now();
      if (now - r.pv._last > 2000) {          // refresh pace ~ the extractor's
        r.pv._last = now;
        // preload off-DOM, swap only on success: no broken icons, no flicker
        [[r.pvBefore, "before"], [r.pvAfter, "after"]].forEach(function (p) {
          var url = workerUrl("jobs/" + j.id + "/preview/" + p[1] + ".jpg") + "?t=" + now;
          var tmp = new Image();
          tmp.onload = function () { p[0].src = url; r.pv.hidden = false; };
          tmp.src = url;
        });
      }
    }
    if (r.capCens && j.backend) {
      var up = j.backend === "upscale";
      r.capCens.textContent = up ? "Original" : "Censored (original)";
      r.capLive.textContent = up ? "Upscaled · live" : "Decensored · live";
    }
    if (r.live && (j.backend === "decensor" || j.backend === "lada" || j.backend === "upscale") && j.preview && !r.live.src) {
      // attach once, after the first fragments exist (preview implies output);
      // 'loadeddata' on the live feed unhides the whole duo
      r.live.src = workerUrl("jobs/" + j.id + "/live.mp4");
      if (r.cens) r.cens.src = workerUrl("vid/" + j.scene_id);
      r.live.play && r.live.play().catch(function () {});
    }
    if (r.pause) {
      r.pause.textContent = j.paused ? "Resume" : "Pause";
      r.pause.dataset.act = j.paused ? "resume" : "pause";
      r.pause.disabled = false;
    }
  }

  async function jobAction(id, kind) {
    try { await workerFetch("jobs/" + id + "/" + kind, { method: "POST" }); }
    catch (e) { toast(kind + " failed: " + e.message, true); }
    pollJobs();
  }

  // ---- live GPU meter (topbar) ------------------------------------------- //
  async function pollGpu() {
    var box = $("gpumeter"), txt = $("gpumeter-text");
    if (!box) return;
    try {
      var g = await (await fetch(workerUrl("gpu"))).json();
      if (g && g.util != null) {
        var vram = (g.mem_used != null && g.mem_total != null)
          ? " · " + (g.mem_used / 1024).toFixed(1) + "/" + Math.round(g.mem_total / 1024) + " GB" : "";
        txt.textContent = "GPU " + Math.round(g.util) + "%" + vram + (g.temp != null ? " · " + Math.round(g.temp) + "°C" : "");
        box.hidden = false;
        box.classList.toggle("busy", g.util >= 5);
      } else { box.hidden = true; }
    } catch (e) { box.hidden = true; }
  }

  // ---- runners management ------------------------------------------------ //
  function openRunners() { $("runnersModal").hidden = false; loadRunners(); }
  function closeRunners() { $("runnersModal").hidden = true; $("addRunnerForm").hidden = true; }

  async function loadRunners() {
    var box = $("runnersList");
    box.innerHTML = "<div class='empty'>Checking runners…</div>";
    var list;
    try { list = await workerFetch("runners"); }
    catch (e) { box.innerHTML = "<div class='empty'>Couldn't load runners: " + esc(e.message) + "</div>"; return; }
    box.innerHTML = "";
    if (!list.length) { box.innerHTML = "<div class='empty'>No runners yet. Add or discover one.</div>"; }
    list.forEach(function (r) { box.appendChild(runnerCard(r)); });
    state.runnerCaps = list.filter(function (r) { return r.online; });   // keep the pickers fresh
    rebuildPins();
  }

  function runnerCard(r) {
    var c = el("div", "rc " + (r.online ? "online" : "offline"));
    c.appendChild(el("span", "dot"));
    var main = el("div", "rc-main");
    var name = el("div", "rc-name", esc(r.name || r.url));
    if (r.kind) name.appendChild(el("span", "rc-kind", esc(r.kind)));
    main.appendChild(name);
    main.appendChild(el("div", "rc-url", esc(r.url)));
    if (r.ops && r.ops.length) {
      var ops = el("div", "rc-ops");
      r.ops.forEach(function (op) {
        var prefer = (r.prefer || []).indexOf(op) >= 0;
        var eng = (r.engines || {})[op];
        ops.appendChild(el("span", "op" + (prefer ? " prefer" : ""), esc(op + (eng ? " · " + engNice(eng) : ""))));
      });
      main.appendChild(ops);
    }
    if (r.source) main.appendChild(el("div", "rc-src", "source: " + r.source + (r.note ? " · " + r.note : "")));
    c.appendChild(main);
    var st = el("div", "rc-status" + (r.busy ? " busy" : ""),
      r.online ? (r.paused ? "paused" : (r.busy ? "busy" : "idle")) : "offline");
    c.appendChild(st);
    if (r.source !== "env") {
      var rm = el("button", "linkbtn", "remove");
      rm.onclick = function () { removeRunner(r.url); };
      c.appendChild(rm);
    }
    return c;
  }

  function formRunner() {
    var prefer = [];
    if ($("rfPrefUp").checked) prefer.push("upscale");
    if ($("rfPrefTx").checked) prefer.push("transcode");
    if ($("rfPrefDe").checked) prefer.push("decensor");
    return { name: $("rfName").value.trim(), url: $("rfUrl").value.trim(),
             token: $("rfToken").value.trim(), prefer: prefer };
  }
  function rfMsg(t, cls) { var m = $("rfMsg"); m.textContent = t; m.className = "rf-msg" + (cls ? " " + cls : ""); }

  async function removeRunner(url) {
    try { await workerFetch("runners/remove", { method: "POST", body: JSON.stringify({ url: url }) }); }
    catch (e) { toast("remove failed: " + e.message, true); }
    loadRunners();
  }

  // ---- init -------------------------------------------------------------- //
  function bind() {
    $("runnersBtn").onclick = openRunners;
    $("runnersClose").onclick = closeRunners;
    $("runnersModal").addEventListener("click", function (e) { if (e.target === $("runnersModal")) closeRunners(); });
    $("addRunnerBtn").onclick = function () { $("addRunnerForm").hidden = false; rfMsg(""); };
    $("rfCancel").onclick = function () { $("addRunnerForm").hidden = true; };
    $("rfTest").onclick = async function () {
      var r = formRunner();
      if (!r.url) { rfMsg("enter a URL", "err"); return; }
      rfMsg("testing…");
      try {
        var res = await workerFetch("runners/test", { method: "POST", body: JSON.stringify(r) });
        rfMsg(res.online ? ("online: " + (res.node || "?") + " [" + (res.ops || []).join(",") + "]") : "offline / unreachable", res.online ? "ok" : "err");
      } catch (e) { rfMsg("test failed: " + e.message, "err"); }
    };
    $("addRunnerForm").onsubmit = async function (e) {
      e.preventDefault();
      var r = formRunner();
      if (!r.url) { rfMsg("enter a URL", "err"); return; }
      try {
        await workerFetch("runners", { method: "POST", body: JSON.stringify(r) });
        $("addRunnerForm").hidden = true; $("rfName").value = $("rfUrl").value = $("rfToken").value = "";
        loadRunners();
      } catch (err) { rfMsg("save failed: " + err.message, "err"); }
    };
    $("discoverBtn").onclick = async function () {
      var b = $("discoverBtn"); b.disabled = true; b.textContent = "Scanning…";
      try {
        var found = await workerFetch("runners/discover", { method: "POST", body: "{}" });
        var nu = found.filter(function (f) { return !f.registered; });
        if (!found.length) toast("No runners found on the network");
        else if (!nu.length) toast("Found " + found.length + " — all already registered");
        else {
          for (var i = 0; i < nu.length; i++) {
            await workerFetch("runners", { method: "POST", body: JSON.stringify({ name: nu[i].name, url: nu[i].url }) });
          }
          toast("Added " + nu.length + " discovered runner" + (nu.length > 1 ? "s" : ""));
        }
        loadRunners();
      } catch (e) { toast("discover failed: " + e.message, true); }
      b.disabled = false; b.textContent = "🔎 Discover on network";
    };

    var deb;
    $("search").addEventListener("input", function () { clearTimeout(deb); deb = setTimeout(function () { state.page = 1; loadScenes(); }, 300); });
    $("sort").onchange = $("minres").onchange = function () { state.page = 1; loadScenes(); };
    var tagS = $("tagSearch");
    if (tagS) {
      // click/focus -> most-used popover; type -> live filter; Enter -> first match
      tagS.addEventListener("focus", function () { this.select(); tagMenuRender(""); });
      tagS.addEventListener("input", function () { tagMenuRender(this.value); });
      tagS.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { this.blur(); return; }
        if (e.key === "Enter") {
          e.preventDefault();
          var q = this.value.trim().toLowerCase();
          if (!q) { tagPick(null); return; }
          var first = (state.tags || []).filter(function (t) { return t.name.toLowerCase().indexOf(q) >= 0; })[0];
          if (first) tagPick(first);
        }
      });
      tagS.addEventListener("blur", function () {
        // let a menu mousedown-select resolve first, then close + restore the label
        setTimeout(function () {
          if ($("tagMenu")) $("tagMenu").hidden = true;
          var cur = (state.tags || []).filter(function (t) { return String(t.id) === String($("tagf").value); })[0];
          $("tagSearch").value = cur ? cur.name : "";
        }, 130);
      });
    }
    // engine choice persists across visits ("" was the old DeepMosaics default)
    try {
      var se = localStorage.getItem("dc_engine"), sq = localStorage.getItem("dc_ladaq");
      if (se && ENGINE_LABEL[se]) $("engine").value = se;
      if (sq) $("ladaq").value = sq;
      _pinRestore.engine = localStorage.getItem("dc_enginePin");   // applied once options exist
      _pinRestore.runner = localStorage.getItem("dc_runnerPin");
      if ($("rtxMode")) $("rtxMode").value = localStorage.getItem("dc_rtx_mode") || "off";
      if ($("rtxDenoise")) $("rtxDenoise").value = localStorage.getItem("dc_rtx_denoise") || "auto";
      if ($("rtxDeblur")) $("rtxDeblur").value = localStorage.getItem("dc_rtx_deblur") || "auto";
      if ($("previewToggle")) $("previewToggle").checked = localStorage.getItem("dc_preview") === "1";
    } catch (e) {}
    var syncEngine = function () {
      var e = $("engine").value;
      rebuildPins();                                              // refresh engine/runner options
      var jasna = $("enginePin") && $("enginePin").value === "jasna";
      $("ladaq").hidden = e === "upscale" || e === "transcode" || jasna;  // Lada-only detect model
      $("txq").hidden = e !== "transcode";
      updateRtx();
      refreshSelBtn();
      renderConn();
      updatePreview();
    };
    syncEngine();
    $("engine").onchange = function () {
      syncEngine();
      try { localStorage.setItem("dc_engine", $("engine").value); } catch (e) {}
    };
    $("enginePin").onchange = function () {
      syncEngine();       // refilter the runner pin, refresh ladaq visibility + preview
      try { localStorage.setItem("dc_enginePin", $("enginePin").value); } catch (e) {}
    };
    $("runnerPin").onchange = function () {
      updatePreview();
      try { localStorage.setItem("dc_runnerPin", $("runnerPin").value); } catch (e) {}
    };
    $("ladaq").onchange = function () {
      try { localStorage.setItem("dc_ladaq", $("ladaq").value); } catch (e) {}
    };
    if ($("rtxMode")) $("rtxMode").onchange = function () {
      updateRtx();       // toggle denoise/deblur visibility with the chosen mode
      try { localStorage.setItem("dc_rtx_mode", $("rtxMode").value); } catch (e) {}
    };
    if ($("rtxDenoise")) $("rtxDenoise").onchange = function () {
      try { localStorage.setItem("dc_rtx_denoise", $("rtxDenoise").value); } catch (e) {}
    };
    if ($("rtxDeblur")) $("rtxDeblur").onchange = function () {
      try { localStorage.setItem("dc_rtx_deblur", $("rtxDeblur").value); } catch (e) {}
    };
    if ($("previewToggle")) $("previewToggle").onchange = function () {
      try { localStorage.setItem("dc_preview", $("previewToggle").checked ? "1" : "0"); } catch (e) {}
    };
    $("prev").onclick = function () { if (state.page > 1) { state.page--; loadScenes(); } };
    $("next").onclick = function () { state.page++; loadScenes(); };
    $("decensorSel").onclick = decensorSelected;
    $("clearDone").onclick = function () {
      workerFetch("jobs").then(function (js) {
        js.forEach(function (j) { if (!RUNNING[j.state] && j.state !== "review_ready") state.dismissed.add(j.id); });
        pollJobs();
      }).catch(function () {});
    };
  }

  async function main() {
    bind();
    await loadToken();
    await updateConn();
    await loadTags();
    await loadScenes();
    pollJobs();
    pollGpu();
    state.pollTimer = setInterval(pollJobs, 1500);
    setInterval(updateConn, 15000);
    setInterval(pollGpu, 3000);
  }
  main();
})();
