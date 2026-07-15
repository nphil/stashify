// Decensor dashboard — a standalone WebUI for the decensor worker.
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
      TOKEN = (((d.configuration || {}).plugins || {}).decensor || {}).workerToken || "";
    } catch (e) { /* worker may accept unauthenticated */ }
  }
  async function updateConn() {
    try {
      var h = await workerFetch("health");
      conn.className = "conn ok";
      conn.textContent = "● " + h.backend + " · GPU " + h.gpu + (h.postUpscale ? " · upscale on" : "");
    } catch (e) {
      conn.className = "conn err"; conn.textContent = "worker unreachable";
    }
  }

  // ---- scene browser ----------------------------------------------------- //
  var SCENE_Q = "query($filter: FindFilterType) {" +
    " findScenes(filter: $filter) { count scenes {" +
    " id title date files { path width height duration size } studio { name } tags { name } } } }";

  function isDone(scene) {
    return (scene.tags || []).some(function (t) { return /^Decensored/i.test(t.name); });
  }

  async function loadScenes() {
    grid.innerHTML = "<div class='empty'>Loading…</div>";
    var sort = $("sort").value;
    var qs = "scenes?q=" + encodeURIComponent($("search").value.trim()) +
      "&page=" + state.page + "&per_page=" + PER + "&sort=" + encodeURIComponent(sort);
    var res;
    try { res = await workerFetch(qs); }
    catch (e) {
      grid.innerHTML = "<div class='empty'>Couldn't load scenes: " + esc(e.message) +
        "<br>Check the worker can reach Stash (STASH_URL / API key).</div>";
      return;
    }
    state.count = res.count;
    var minres = parseInt($("minres").value, 10) || 0;
    var hideDone = $("hideDone").checked;
    grid.innerHTML = "";
    var shown = 0;
    res.scenes.forEach(function (s) {
      var f = (s.files || [])[0] || {};
      if (minres && (f.height || 0) < minres) return;
      if (hideDone && isDone(s)) return;
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
    var b = $("decensorSel"); b.textContent = "Decensor selected (" + state.sel.size + ")"; b.disabled = state.sel.size === 0;
  }

  async function decensorSelected() {
    var ids = Array.from(state.sel);
    if (!ids.length) return;
    var ok = 0;
    for (var i = 0; i < ids.length; i++) {
      try { await workerFetch("decensor", { method: "POST", body: JSON.stringify({ scene_id: ids[i] }) }); ok++; }
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

  async function pollJobs() {
    var jobs;
    try { jobs = await workerFetch("jobs"); }
    catch (e) {
      if (/Not logged|401/.test(e.message)) { /* token issue */ }
      return;
    }
    jobs = jobs.filter(function (j) { return !state.dismissed.has(j.id); });
    jobs.reverse();
    joblist.innerHTML = "";
    jobsEmpty.style.display = jobs.length ? "none" : "block";
    jobs.forEach(function (j) { joblist.appendChild(jobCard(j)); });
  }

  function statChip(g, label, val) {
    if (val == null || val === "") return;
    var s = el("div", "stat");
    s.appendChild(el("span", "sl", label));
    s.appendChild(el("span", "sv", val));
    g.appendChild(s);
  }
  function statsGrid(j, pct) {
    var g = el("div", "stats");
    statChip(g, "progress", pct + "%");
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
    return g;
  }

  function jobCard(j) {
    var name = (state.scenes[j.scene_id] || {}).title || ("Scene " + j.scene_id);
    var c = el("div", "job");
    c.appendChild(el("div", "jt", esc(name)));

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
    } else if (j.state === "replaced" || j.state === "discarded" || j.state === "cancelled") {
      var done = j.state === "replaced" ? "Original replaced ✓"
        : (j.state === "cancelled" ? "Cancelled" : "Preview discarded");
      c.appendChild(el("div", "jmsg" + (j.state === "cancelled" ? "" : " ok"), done));
      var d = el("div", "row"); var b = el("button", "btn", "Dismiss");
      b.onclick = function () { state.dismissed.add(j.id); pollJobs(); }; d.appendChild(b); c.appendChild(d);
    } else if (j.state === "error") {
      c.appendChild(el("div", "jmsg err", j.error || j.message || "Failed"));
      var d2 = el("div", "row"); var b2 = el("button", "btn", "Dismiss");
      b2.onclick = function () { state.dismissed.add(j.id); pollJobs(); }; d2.appendChild(b2); c.appendChild(d2);
    } else {
      // queued / running / paused / replacing / discarding
      var pct = Math.round((j.progress || 0) * 100);
      c.appendChild(el("div", "jmsg" + (j.paused ? " paused" : ""), esc(j.paused ? "Paused" : (j.message || j.state))));
      var bar = el("div", "bar" + (j.paused ? " is-paused" : ""));
      bar.appendChild(el("div", "fill")).style.width = pct + "%";
      c.appendChild(bar);
      if (j.state === "running") c.appendChild(statsGrid(j, pct));
      var ctl = el("div", "row");
      if (j.state === "running") {
        var pr = el("button", "btn", j.paused ? "Resume" : "Pause");
        pr.onclick = function () { pr.disabled = true; jobAction(j.id, j.paused ? "resume" : "pause"); };
        ctl.appendChild(pr);
      }
      if (j.state === "running" || j.state === "queued") {
        var cx = el("button", "btn btn-danger", "Cancel");
        cx.onclick = function () { cx.disabled = true; jobAction(j.id, "cancel"); };
        ctl.appendChild(cx);
      }
      if (ctl.children.length) c.appendChild(ctl);
    }
    return c;
  }

  async function jobAction(id, kind) {
    try { await workerFetch("jobs/" + id + "/" + kind, { method: "POST" }); }
    catch (e) { toast(kind + " failed: " + e.message, true); }
    pollJobs();
  }

  // ---- init -------------------------------------------------------------- //
  function bind() {
    var deb;
    $("search").addEventListener("input", function () { clearTimeout(deb); deb = setTimeout(function () { state.page = 1; loadScenes(); }, 300); });
    $("sort").onchange = $("minres").onchange = $("hideDone").onchange = function () { state.page = 1; loadScenes(); };
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
    await loadScenes();
    pollJobs();
    state.pollTimer = setInterval(pollJobs, 1500);
    setInterval(updateConn, 15000);
  }
  main();
})();
