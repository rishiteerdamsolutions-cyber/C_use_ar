(function () {
  "use strict";

  var CFG = window.CUSEAR_CONFIG || {};
  var API_BASE = String(CFG.apiBase || "").replace(/\/$/, "");
  var SUPABASE_URL = String(CFG.supabaseUrl || "");
  var SUPABASE_ANON = String(CFG.supabaseAnonKey || "");
  var PRICING_URL = CFG.pricingUrl || "/pricing.html";

  var sb = null;
  var session = null;
  var me = null;
  var catalog = [];
  var activeView = "home";

  var $ = function (id) {
    return document.getElementById(id);
  };

  function toast(msg, isErr) {
    var el = $("toast");
    if (!el) return;
    el.textContent = msg || "";
    el.style.borderColor = isErr ? "var(--err)" : "var(--border)";
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () {
      el.classList.remove("show");
    }, 3200);
  }

  function apiHeaders() {
    var h = { "Content-Type": "application/json" };
    if (session && session.access_token) {
      h.Authorization = "Bearer " + session.access_token;
    } else if (me && me.agent_token) {
      h["X-Agent-Token"] = me.agent_token;
    }
    return h;
  }

  async function api(path, opts) {
    if (!API_BASE) throw new Error("API not configured");
    var r = await fetch(API_BASE + path, opts || {});
    var j = null;
    try {
      j = await r.json();
    } catch (e) {
      j = {};
    }
    if (!r.ok) {
      var detail = j && j.detail;
      throw new Error(typeof detail === "string" ? detail : r.statusText || "Request failed");
    }
    return j;
  }

  function showView(name) {
    activeView = name;
    document.querySelectorAll("[data-view]").forEach(function (el) {
      el.classList.toggle("hidden", el.getAttribute("data-view") !== name);
    });
    document.querySelectorAll(".nav-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-nav") === name);
    });
  }

  function detectOs() {
    var ua = navigator.userAgent || "";
    if (/Windows/i.test(ua)) return "windows";
    if (/Mac/i.test(ua)) return "mac";
    return "other";
  }

  function updateAgentUi(connected) {
    var dot = $("agentDot");
    var label = $("agentLabel");
    if (dot) dot.classList.toggle("on", !!connected);
    if (label) label.textContent = connected ? "Agent online" : "Agent offline";
  }

  async function refreshMe() {
    if (!session || !session.access_token) {
      me = null;
      return null;
    }
    try {
      me = await api("/api/cloud/v1/me", { headers: apiHeaders() });
      if (me && me.agent_token) {
        try {
          localStorage.setItem("cusear.agent_token.v1", me.agent_token);
        } catch (e) {}
      }
      updateAgentUi(!!(me && me.agent_connected));
      renderAccount();
      return me;
    } catch (e) {
      if (String(e.message || "").indexOf("401") >= 0 || String(e.message).toLowerCase().indexOf("session") >= 0) {
        me = null;
      }
      throw e;
    }
  }

  function renderAccount() {
    var text = "Not signed in";
    if (session && session.user) {
      var plan = (me && me.plan) || "—";
      var active = me && me.active !== false;
      text =
        (session.user.email || "User") +
        " · plan " +
        plan +
        (active ? "" : " (inactive)");
    }
    ["accountLine", "accountLine2"].forEach(function (id) {
      var el = $(id);
      if (el) el.textContent = text;
    });
  }

  async function refreshAgentStatus() {
    try {
      var j = await api("/api/cloud/v1/agent/status", { headers: apiHeaders() });
      updateAgentUi(!!j.connected);
      return j;
    } catch (e) {
      updateAgentUi(false);
      return null;
    }
  }

  async function loadCatalog() {
    try {
      var r = await fetch("/app/agents/catalog.json", { cache: "no-store" });
      var j = await r.json();
      catalog = j.agents || [];
    } catch (e) {
      catalog = [];
    }
    renderStore();
  }

  function planAllows(agent) {
    if (!me || !me.plan) return false;
    var p = String(me.plan).toLowerCase();
    return (agent.plans || []).indexOf(p) >= 0;
  }

  function renderStore() {
    var grid = $("storeGrid");
    if (!grid) return;
    grid.innerHTML = "";
    if (!catalog.length) {
      grid.innerHTML = "<p class='view-sub'>Store catalog unavailable.</p>";
      return;
    }
    catalog.forEach(function (agent) {
      var locked = !planAllows(agent);
      var card = document.createElement("article");
      card.className = "agent-card" + (locked ? " locked" : "");
      var plats = (agent.platforms || [])
        .map(function (p) {
          return "<span>" + p + "</span>";
        })
        .join("");
      card.innerHTML =
        "<span class='badge'>" +
        (agent.badge || "Agent") +
        "</span>" +
        "<h4>" +
        agent.name +
        "</h4>" +
        "<p>" +
        agent.description +
        "</p>" +
        "<div class='platforms'>" +
        plats +
        "</div>" +
        (locked
          ? "<button type='button' class='btn btn-secondary' data-upgrade>Upgrade plan</button>"
          : "<button type='button' class='btn btn-primary' data-agent='" +
            agent.id +
            "'>Add to account</button>");
      grid.appendChild(card);
    });
    grid.querySelectorAll("[data-upgrade]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        window.location.href = PRICING_URL;
      });
    });
    grid.querySelectorAll("[data-agent]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        toast("Agent provisioning from store ships with your plan workflow — contact support to attach " + btn.getAttribute("data-agent"));
      });
    });
  }

  async function refreshWorkflows() {
    var select = $("wfSelect");
    if (!select) return;
    var prev = select.value;
    select.innerHTML = '<option value="">Choose workflow…</option>';
    var j = await api("/api/cloud/v1/workflows", { headers: apiHeaders() });
    (j.workflows || []).forEach(function (wf) {
      var opt = document.createElement("option");
      opt.value = String(wf.id || "");
      var label = String(wf.name || wf.id || "workflow");
      if (wf.platform) label += " (" + wf.platform + ")";
      opt.textContent = label;
      select.appendChild(opt);
    });
    if (prev) select.value = prev;
  }

  async function refreshRuns() {
    var ul = $("runsList");
    if (!ul) return;
    ul.innerHTML = "";
    var j = await api("/api/cloud/v1/runs/recent?limit=20", { headers: apiHeaders() });
    (j.runs || []).forEach(function (row) {
      var li = document.createElement("li");
      var st = String(row.status || "?");
      li.innerHTML =
        "<strong>" +
        (row.workflow_name || "?") +
        "</strong> — <span class='" +
        (st === "completed" || st === "success" ? "status-ok" : st === "error" || st === "failed" ? "status-err" : "") +
        "'>" +
        st +
        "</span>" +
        (row.error ? "<br><small>" + row.error + "</small>" : "");
      ul.appendChild(li);
    });
    if (!ul.children.length) ul.innerHTML = "<li>No runs yet.</li>";
  }

  function buildContentMap() {
    var out = {};
    var map = [
      ["facebook", "runTextFacebook", "facebook_text"],
      ["instagram", "runTextInstagram", "instagram_text"],
      ["linkedin", "runTextLinkedin", "linkedin_text"],
      ["x", "runTextX", "x_text"],
      ["whatsapp", "runTextWhatsapp", "whatsapp_text"],
    ];
    map.forEach(function (row) {
      var el = $(row[1]);
      if (el && el.value.trim()) out[row[2]] = el.value.trim();
    });
    return out;
  }

  async function loginGoogle() {
    if (!sb) {
      toast("Auth not configured", true);
      return;
    }
    await sb.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: window.location.origin + "/app/" },
    });
  }

  async function logout() {
    if (sb) await sb.auth.signOut();
    session = null;
    me = null;
    showAuthGate(true);
    toast("Signed out");
  }

  function showAuthGate(show) {
    $("authGate").classList.toggle("hidden", !show);
    $("appMain").classList.toggle("hidden", show);
    $("bottomNav").classList.toggle("hidden", show);
  }

  function setupInstallUi() {
    var os = detectOs();
    var macBtn = $("btnMacInstall");
    var winBtn = $("btnWinInstall");
    var ios = $("installIosHint");
    if (macBtn) macBtn.classList.toggle("hidden", os !== "mac" && os !== "other");
    if (winBtn) winBtn.classList.toggle("hidden", os !== "windows" && os !== "other");
    if (ios) {
      var isIos = /iPhone|iPad|iPod/i.test(navigator.userAgent);
      ios.classList.toggle("hidden", !isIos);
      if (isIos) {
        ios.textContent =
          "On iPhone: tap Share → Add to Home Screen to install this app. Automation still requires a Mac with the agent running.";
      }
    }
  }

  async function copyAgentToken() {
    var t = (me && me.agent_token) || "";
    if (!t) {
      toast("Sign in and subscribe first", true);
      return;
    }
    try {
      await navigator.clipboard.writeText(t);
      toast("Agent token copied");
    } catch (e) {
      toast("Copy failed — select token manually", true);
    }
  }

  async function downloadTokenFile() {
    var t = (me && me.agent_token) || "";
    if (!t) {
      toast("No token yet", true);
      return;
    }
    var blob = new Blob([t], { type: "text/plain" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "agent-token.txt";
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Downloaded agent-token.txt — place next to launcher");
  }

  function registerPwa() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch(function () {});
  }

  function setupInstallPrompt() {
    var deferred;
    window.addEventListener("beforeinstallprompt", function (e) {
      e.preventDefault();
      deferred = e;
      $("btnInstallPwa").classList.remove("hidden");
    });
    $("btnInstallPwa").addEventListener("click", async function () {
      if (!deferred) {
        toast("Use browser menu → Install app");
        return;
      }
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
    });
  }

  function wireNav() {
    document.querySelectorAll(".nav-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showView(btn.getAttribute("data-nav") || "home");
      });
    });
  }

  function wireActions() {
    $("btnGoogleLogin").addEventListener("click", loginGoogle);
    $("btnLogout").addEventListener("click", logout);
    $("btnCopyToken").addEventListener("click", copyAgentToken);
    $("btnDownloadToken").addEventListener("click", downloadTokenFile);
    $("btnRefreshStatus").addEventListener("click", function () {
      refreshMe()
        .then(function () {
          return Promise.all([refreshAgentStatus(), refreshRuns(), refreshWorkflows()]);
        })
        .catch(function (e) {
          toast(String(e.message || e), true);
        });
    });
    $("btnRun").addEventListener("click", async function () {
      var wf = $("wfSelect").value.trim();
      if (!wf) {
        toast("Choose a workflow", true);
        return;
      }
      $("btnRun").disabled = true;
      try {
        var j = await api("/api/cloud/v1/runs/run-now", {
          method: "POST",
          headers: apiHeaders(),
          body: JSON.stringify({
            workflow_id: wf,
            content_map: buildContentMap(),
          }),
        });
        toast("Run " + (j.status || "queued"));
        await refreshRuns();
      } catch (e) {
        toast(String(e.message || e), true);
      } finally {
        $("btnRun").disabled = false;
      }
    });
    $("btnSaveSchedule").addEventListener("click", async function () {
      var wf = $("wfSelect").value.trim();
      if (!wf) {
        toast("Choose a workflow", true);
        return;
      }
      var days = Array.prototype.slice
        .call(document.querySelectorAll(".schDay:checked"))
        .map(function (el) {
          return el.value;
        });
      if (!days.length) {
        toast("Pick at least one day", true);
        return;
      }
      $("btnSaveSchedule").disabled = true;
      try {
        await api("/api/cloud/v1/schedules/create", {
          method: "POST",
          headers: apiHeaders(),
          body: JSON.stringify({
            workflow_id: wf,
            run_time: $("schTime").value,
            days: days,
            content_map: buildContentMap(),
            active: true,
          }),
        });
        toast("Schedule saved");
      } catch (e) {
        toast(String(e.message || e), true);
      } finally {
        $("btnSaveSchedule").disabled = false;
      }
    });
    $("btnGoPricing").addEventListener("click", function () {
      window.location.href = PRICING_URL;
    });
  }

  async function boot() {
    registerPwa();
    setupInstallUi();
    setupInstallPrompt();
    wireNav();
    wireActions();
    await loadCatalog();

    if (!SUPABASE_URL || !SUPABASE_ANON || !API_BASE) {
      showAuthGate(true);
      $("authMsg").textContent =
        "Set CUSEAR_SUPABASE_URL, CUSEAR_SUPABASE_ANON_KEY, and CUSEAR_API_BASE on the site before deploy.";
      return;
    }

    if (!window.supabase || !window.supabase.createClient) {
      showAuthGate(true);
      $("authMsg").textContent = "Supabase SDK failed to load.";
      return;
    }

    sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
    var out = await sb.auth.getSession();
    session = out && out.data ? out.data.session : null;

    sb.auth.onAuthStateChange(function (_ev, s) {
      session = s;
      if (session) {
        showAuthGate(false);
        refreshMe()
          .then(function () {
            return Promise.all([refreshAgentStatus(), refreshRuns(), refreshWorkflows()]);
          })
          .catch(function (e) {
            if (String(e.message || "").indexOf("inactive") >= 0 || String(e.message).indexOf("401") >= 0) {
              toast("Subscribe to activate your account", true);
            } else {
              toast(String(e.message || e), true);
            }
          });
      } else {
        showAuthGate(true);
      }
    });

    if (session) {
      showAuthGate(false);
      try {
        await refreshMe();
        await Promise.all([refreshAgentStatus(), refreshRuns(), refreshWorkflows()]);
      } catch (e) {
        toast(String(e.message || e), true);
      }
    } else {
      showAuthGate(true);
    }

    showView("home");
    setInterval(refreshAgentStatus, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
