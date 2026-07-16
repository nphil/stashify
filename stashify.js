// Stashify — Stash UI widget.
// Adds an on-scene-page panel: a button to decensor the current scene via the
// worker container, a live progress bar, then a review player with
// "Replace original" / "Discard". Self-contained (no csLib/PluginApi coupling)
// so it survives Stash version changes: it reads the scene id from the URL and
// renders a fixed panel rather than patching internal components.
(function () {
  "use strict";

  var PLUGIN_ID = "stashify";
  var POLL_MS = 1500;
  var cfgCache = null;
  var current = { sceneId: null, jobId: null, timer: null };

  var MIXED_MSG = "Mixed content: Stash is loaded over HTTPS but the Worker URL is HTTP. " +
    "Browsers block that. Open Stash over http:// on your LAN, or serve the worker over HTTPS.";

  function mixedContent(url) {
    return location.protocol === "https:" && /^http:\/\//i.test(url || "");
  }

  // ---- helpers ----------------------------------------------------------- //

  function sceneIdFromUrl() {
    var m = location.pathname.match(/\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  async function stashGQL(query, variables) {
    var r = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ query: query, variables: variables || {} }),
    });
    var j = await r.json();
    if (j.errors && j.errors.length) throw new Error(j.errors[0].message);
    return j.data;
  }

  async function loadConfig(force) {
    if (!force && cfgCache && cfgCache.url) return cfgCache;
    var data = await stashGQL("query { configuration { plugins } }");
    var p = ((data.configuration || {}).plugins || {})[PLUGIN_ID] || {};
    cfgCache = {
      url: (p.workerUrl || "").replace(/\/+$/, ""),
      token: p.workerToken || "",
    };
    return cfgCache;
  }

  // Low-level GET used by the connection test (returns status in the error so
  // we can tell a rejected token from an unreachable worker).
  async function rawWorker(cfg, path, token) {
    var headers = {};
    if (token) headers["X-Decensor-Token"] = token;
    var r = await fetch(cfg.url + path, { headers: headers });
    var text = await r.text();
    var b;
    try { b = text ? JSON.parse(text) : {}; }
    catch (e) {
      // A reverse proxy in front of the worker may return HTML/plain text for 5xx.
      if (!r.ok) throw new Error(String(r.status)); // keep status leading for the 401 check
      throw new Error("worker returned non-JSON (HTTP " + r.status + ")");
    }
    if (!r.ok) throw new Error(r.status + (b.error ? " " + b.error : ""));
    return b;
  }

  async function testConnection(out) {
    out.className = "decensor-test-result";
    out.textContent = "Testing…";
    var cfg;
    try {
      cfg = await loadConfig(true); // re-read settings in case they just changed
    } catch (e) {
      out.className = "decensor-test-result decensor-err";
      out.textContent = "Config error: " + (e.message || e);
      return;
    }
    if (!cfg.url) {
      out.className = "decensor-test-result decensor-err";
      out.textContent = "Set the Worker URL in the plugin settings first.";
      return;
    }
    if (mixedContent(cfg.url)) {
      out.className = "decensor-test-result decensor-err";
      out.textContent = "✗ " + MIXED_MSG;
      return;
    }
    // 1) reachability + CORS (health needs no token)
    var health;
    try {
      health = await rawWorker(cfg, "/api/health");
    } catch (e) {
      out.className = "decensor-test-result decensor-err";
      out.textContent = "✗ Can't reach worker at " + cfg.url + "\n" + (e.message || e) +
        "\nCheck the URL, that the container is running, and CORS/CSP.";
      return;
    }
    // 2) token check via an authed endpoint
    var tokenMsg;
    try {
      await rawWorker(cfg, "/api/jobs", cfg.token);
      tokenMsg = cfg.token ? "token OK" : "no token set";
    } catch (e) {
      if (/^401/.test(String(e.message))) {
        out.className = "decensor-test-result decensor-err";
        out.textContent = "✗ Reached the worker, but the token was rejected.\n" +
          "Match Worker Token to the container's WORKER_TOKEN.";
        return;
      }
      tokenMsg = "jobs check failed: " + (e.message || e);
    }
    out.className = "decensor-test-result decensor-ok";
    out.textContent = "✓ Connected — backend " + health.backend + ", GPU " + health.gpu +
      ", upscale " + (health.postUpscale ? "on" : "off") + " · " + tokenMsg;
  }

  async function workerFetch(path, opts) {
    var cfg = await loadConfig();
    if (!cfg.url) throw new Error("Set the Worker URL in Settings > Plugins > Stashify.");
    if (mixedContent(cfg.url)) throw new Error(MIXED_MSG);
    opts = opts || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (cfg.token) headers["X-Decensor-Token"] = cfg.token;
    var r = await fetch(cfg.url + path, Object.assign({}, opts, { headers: headers }));
    var text = await r.text();
    var body;
    try { body = text ? JSON.parse(text) : {}; }
    catch (e) { throw new Error("worker HTTP " + r.status + (r.ok ? " (non-JSON response)" : "")); }
    if (!r.ok) throw new Error(body.error || ("worker HTTP " + r.status));
    return body;
  }

  // ---- panel ------------------------------------------------------------- //

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function ensurePanel() {
    var panel = document.getElementById("decensor-panel");
    if (panel) return panel;
    panel = el("div", "decensor-panel");
    panel.id = "decensor-panel";
    panel.innerHTML =
      '<div class="decensor-head">' +
        '<span class="decensor-title">🩹 Stashify</span>' +
        '<button class="decensor-x" title="hide">×</button>' +
      "</div>" +
      '<div class="decensor-body"></div>';
    document.body.appendChild(panel);
    panel.querySelector(".decensor-x").onclick = function () {
      panel.classList.add("decensor-hidden");
    };
    return panel;
  }

  function body() {
    return ensurePanel().querySelector(".decensor-body");
  }

  function showIdle() {
    ensurePanel().classList.remove("decensor-hidden");
    var b = body();
    b.innerHTML = "";
    var btn = el("button", "decensor-btn decensor-primary", "Decensor this scene");
    btn.onclick = startJob;
    b.appendChild(btn);

    var test = el("button", "decensor-btn decensor-test", "Test connection");
    var result = el("div", "decensor-test-result");
    test.onclick = function () { testConnection(result); };
    b.appendChild(test);
    b.appendChild(result);

    b.appendChild(el("div", "decensor-note", "Runs DeepMosaics" +
      "&nbsp;→&nbsp;Real-ESRGAN on the worker, then lets you review before replacing."));
  }

  function showProgress(job) {
    var b = body();
    b.innerHTML = "";
    var pct = Math.round((job.progress || 0) * 100);
    b.appendChild(el("div", "decensor-msg", job.message || job.state));
    var bar = el("div", "decensor-bar");
    bar.appendChild(el("div", "decensor-fill")).style.width = pct + "%";
    b.appendChild(bar);
    b.appendChild(el("div", "decensor-note", pct + "%"));
  }

  function showReview(job) {
    var b = body();
    b.innerHTML = "";
    b.appendChild(el("div", "decensor-msg", "Preview ready — review it:"));
    if (job.review_scene_id) {
      var v = el("video", "decensor-video");
      v.controls = true;
      v.preload = "metadata";
      v.src = "/scene/" + job.review_scene_id + "/stream";
      b.appendChild(v);
    }
    var row = el("div", "decensor-row");
    var replace = el("button", "decensor-btn decensor-danger", "Replace original");
    var discard = el("button", "decensor-btn", "Discard");
    // Disable both immediately so a fast double-click can't fire two requests.
    replace.onclick = function () { replace.disabled = true; discard.disabled = true; action("replace"); };
    discard.onclick = function () { replace.disabled = true; discard.disabled = true; action("discard"); };
    row.appendChild(replace);
    row.appendChild(discard);
    b.appendChild(row);
    b.appendChild(el("div", "decensor-note",
      "Replace overwrites the original file in place (no backup); tags & history are kept."));
  }

  function showDone(msg, reload) {
    var b = body();
    b.innerHTML = "";
    b.appendChild(el("div", "decensor-msg decensor-ok", msg));
    if (reload) setTimeout(function () { location.reload(); }, 1200);
    else setTimeout(showIdle, 1400);
  }

  function showError(msg) {
    var b = body();
    b.innerHTML = "";
    b.appendChild(el("div", "decensor-msg decensor-err", msg));
    var retry = el("button", "decensor-btn", "Back");
    retry.onclick = showIdle;
    b.appendChild(retry);
  }

  // ---- job lifecycle ----------------------------------------------------- //

  function stopPolling() {
    if (current.timer) { clearInterval(current.timer); current.timer = null; }
  }

  function poll() {
    stopPolling();
    current.timer = setInterval(async function () {
      try {
        var job = await workerFetch("/api/jobs/" + current.jobId);
        if (job.state === "running" || job.state === "queued" ||
            job.state === "replacing" || job.state === "discarding") {
          showProgress(job);
        } else if (job.state === "review_ready") {
          stopPolling();
          showReview(job);
        } else if (job.state === "replaced") {
          stopPolling();
          showDone("Original replaced ✓", true);
        } else if (job.state === "discarded") {
          stopPolling();
          showDone("Preview discarded", false);
        } else if (job.state === "error") {
          stopPolling();
          showError(job.error || job.message || "Failed");
        }
      } catch (e) {
        stopPolling();
        showError(String(e.message || e));
      }
    }, POLL_MS);
  }

  async function startJob() {
    try {
      showProgress({ progress: 0, message: "Submitting…" });
      var job = await workerFetch("/api/decensor", {
        method: "POST",
        body: JSON.stringify({ scene_id: current.sceneId }),
      });
      current.jobId = job.id;
      poll();
    } catch (e) {
      showError(String(e.message || e));
    }
  }

  async function action(kind) {
    try {
      showProgress({ progress: 0, message: kind === "replace" ? "Replacing…" : "Discarding…" });
      await workerFetch("/api/jobs/" + current.jobId + "/" + kind, { method: "POST" });
      poll();
    } catch (e) {
      showError(String(e.message || e));
    }
  }

  // ---- route watching ---------------------------------------------------- //

  function tick() {
    var sceneId = sceneIdFromUrl();
    var panel = document.getElementById("decensor-panel");
    if (!sceneId) {
      if (panel) panel.remove();
      stopPolling();
      current = { sceneId: null, jobId: null, timer: null };
      return;
    }
    if (sceneId !== current.sceneId) {
      // navigated to a different scene: reset
      stopPolling();
      current = { sceneId: sceneId, jobId: null, timer: null };
      showIdle();
    }
  }

  setInterval(tick, 700);
  tick();
})();
