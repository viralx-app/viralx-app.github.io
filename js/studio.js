// VIRALX AI Studio — generate videos (ComfyUI main / Seedance / Wan),
// talking avatars, watch jobs finish, post results to the feed.
(function () {
  var sb = window.VX && window.VX.sb;
  var cfg = window.VX_CONFIG || {};
  var me = null;
  var pollers = {};

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  async function fnCall(name, body) {
    var sess = (await sb.auth.getSession()).data.session;
    var res = await fetch(cfg.SUPABASE_URL + "/functions/v1/" + name, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (sess ? sess.access_token : ""),
        "apikey": cfg.SUPABASE_ANON_KEY
      },
      body: JSON.stringify(body)
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data;
  }

  async function uploadFile(file, kind) {
    var ext = (file.name ? file.name.split(".").pop() : "") || (kind === "audio" ? "webm" : "jpg");
    ext = String(ext).toLowerCase().replace(/[^a-z0-9]/g, "") || "bin";
    var path = me.id + "/ai/" + Date.now() + "-" + kind + "." + ext;
    var up = await sb.storage.from("videos").upload(path, file, { contentType: file.type || "application/octet-stream" });
    if (up.error) throw up.error;
    return sb.storage.from("videos").getPublicUrl(path).data.publicUrl;
  }

  // ---------- tabs ----------
  $("tab-video").addEventListener("click", function () {
    $("pane-video").classList.remove("hidden"); $("pane-avatar").classList.add("hidden");
    $("tab-video").classList.add("btn-green"); $("tab-avatar").classList.remove("btn-green");
  });
  $("tab-avatar").addEventListener("click", function () {
    $("pane-avatar").classList.remove("hidden"); $("pane-video").classList.add("hidden");
    $("tab-avatar").classList.add("btn-green"); $("tab-video").classList.remove("btn-green");
  });
  $("engine").addEventListener("change", function () {
    $("res-wrap").style.display = this.value === "seedance" ? "" : "none";
  });
  $("res-wrap").style.display = "none";
  // Arriving from the Create menu's Avatar card opens the Avatar pane.
  if (location.hash === "#avatar") $("tab-avatar").click();

  // ---------- generations list ----------
  function genCard(g) {
    var d = document.createElement("div");
    d.className = "card";
    d.id = "gen-" + g.id;
    renderGen(d, g);
    return d;
  }
  function renderGen(d, g) {
    var chip = g.status === "done" ? "✅ Done" : g.status === "error" ? "⚠️ Failed" : "⏳ " + (g.status === "running" ? "Rendering…" : "Queued…");
    var engineName = { comfy: "ComfyUI", "comfy-ltx": "LTX-2.5", wan: "Wan", seedance: "Seedance", avatar: "Avatar" }[g.model] || g.model;
    var html = '<p style="margin:0 0 6px;"><b>' + esc(engineName) + "</b> · " + chip + "</p>";
    if (g.prompt) html += '<p class="muted" style="margin:0 0 8px;">' + esc(g.prompt) + "</p>";
    if (g.status === "done" && g.video_url) {
      html += '<video src="' + esc(g.video_url) + '" controls playsinline loop style="width:100%;border-radius:10px;"></video>' +
        '<div style="display:flex;gap:8px;margin-top:8px;">' +
        '<button class="btn btn-green" data-post="' + g.id + '" type="button">Post to feed</button>' +
        '<a class="btn" href="' + esc(g.video_url) + '" download target="_blank" rel="noopener">Download</a></div>' +
        '<p class="muted" data-posted style="margin:6px 0 0;"></p>';
    }
    if (g.status === "error") html += '<p class="form-error" style="margin:0;">' + esc(g.error || "Something went wrong.") + "</p>";
    d.innerHTML = html;
    var pb = d.querySelector("[data-post]");
    if (pb) pb.addEventListener("click", function () { postToFeed(g, d); });
  }
  async function postToFeed(g, d) {
    var note = d.querySelector("[data-posted]");
    try {
      var ins = await sb.from("videos").insert({
        user_id: me.id,
        caption: (g.prompt || "Made with VIRALX AI Studio").slice(0, 300),
        hashtags: ["#viralx", "#ai"],
        category: "entertainment",
        video_url: g.video_url,
        storage_path: null
      });
      if (ins.error) throw ins.error;
      note.textContent = "Posted to the feed ✅";
    } catch (ex) { note.textContent = ex.message || String(ex); }
  }
  function watchJob(g) {
    if (pollers[g.id]) return;
    pollers[g.id] = setInterval(async function () {
      try {
        var out = await fnCall("vx-status", { generation_id: g.id });
        var fresh = out.generation;
        var d = $("gen-" + g.id);
        if (d) renderGen(d, fresh);
        if (fresh.status === "done" || fresh.status === "error") {
          clearInterval(pollers[g.id]); delete pollers[g.id];
        }
      } catch (e) { /* keep polling through blips */ }
    }, 6000);
  }
  async function loadGens() {
    var q = await sb.from("vx_generations").select("*").order("created_at", { ascending: false }).limit(20);
    if (q.error || !q.data || !q.data.length) return;
    var wrap = $("gens"); wrap.innerHTML = "";
    q.data.forEach(function (g) {
      wrap.appendChild(genCard(g));
      if (g.status === "queued" || g.status === "running") watchJob(g);
    });
  }
  function prependGen(g) {
    var wrap = $("gens");
    if (wrap.querySelector("p.muted")) wrap.innerHTML = "";
    wrap.insertBefore(genCard(g), wrap.firstChild);
    watchJob(g);
  }

  // ---------- video generate ----------
  $("generate").addEventListener("click", async function () {
    var err = $("gen-error"); err.textContent = "";
    var btn = this; btn.disabled = true; btn.textContent = "Sending to the studio…";
    try {
      var imageUrl = null;
      var f = $("photo").files[0];
      if (f) imageUrl = await uploadFile(f, "image");
      var out = await fnCall("vx-generate", {
        model: $("engine").value,
        prompt: $("prompt").value.trim(),
        image_url: imageUrl,
        duration: $("duration").value,
        resolution: $("resolution").value,
        aspect_ratio: $("aspect").value
      });
      $("quota").textContent = "AI generations today: " + out.used_today + " of " + out.daily_cap + ".";
      prependGen(out.generation);
    } catch (ex) { err.textContent = ex.message || String(ex); }
    finally { btn.disabled = false; btn.textContent = "Generate video"; }
  });

  // ---------- avatar: voice recording ----------
  var recBlob = null, recorder = null, recStream = null;
  $("rec-btn").addEventListener("click", async function () {
    if (recorder && recorder.state === "recording") {
      recorder.stop();
      return;
    }
    try {
      recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) { $("rec-status").textContent = "Mic blocked — allow microphone or upload audio instead."; return; }
    var chunks = [];
    recorder = new MediaRecorder(recStream);
    recorder.ondataavailable = function (ev) { chunks.push(ev.data); };
    recorder.onstop = function () {
      recBlob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      recBlob.name = "voice.webm";
      var pv = $("rec-preview");
      pv.src = URL.createObjectURL(recBlob);
      pv.classList.remove("hidden");
      $("rec-status").textContent = "Voice recorded ✅";
      $("rec-btn").textContent = "🎙️ Re-record voice";
      recStream.getTracks().forEach(function (t) { t.stop(); });
    };
    recorder.start();
    $("rec-btn").textContent = "⏹ Stop recording";
    $("rec-status").textContent = "Recording… speak your line.";
  });

  $("av-generate").addEventListener("click", async function () {
    var err = $("av-error"); err.textContent = "";
    var btn = this; btn.disabled = true; btn.textContent = "Building your avatar…";
    try {
      var photo = $("av-photo").files[0];
      if (!photo) throw new Error("Pick a clear face photo first.");
      var imageUrl = await uploadFile(photo, "image");
      var audioFile = $("av-audio").files[0] || recBlob;
      if (!audioFile) throw new Error("Record your voice or upload an audio clip.");
      var audioUrl = await uploadFile(audioFile, "audio");
      var out = await fnCall("vx-generate", {
        model: "avatar",
        prompt: $("av-prompt").value.trim(),
        image_url: imageUrl,
        audio_url: audioUrl
      });
      $("quota").textContent = "AI generations today: " + out.used_today + " of " + out.daily_cap + ".";
      prependGen(out.generation);
      document.querySelector("#gens").scrollIntoView({ behavior: "smooth" });
    } catch (ex) { err.textContent = ex.message || String(ex); }
    finally { btn.disabled = false; btn.textContent = "Create talking avatar"; }
  });

  // ---------- boot ----------
  (async function () {
    if (!sb) { $("quota").textContent = "The studio backend is still warming up."; return; }
    var u = (await sb.auth.getUser()).data.user;
    if (!u) return; // need-login card stays visible
    me = u;
    $("need-login").classList.add("hidden");
    $("studio-ui").classList.remove("hidden");
    var dayStart = new Date(); dayStart.setHours(0, 0, 0, 0);
    var c = await sb.from("vx_generations").select("id", { count: "exact", head: true }).gte("created_at", dayStart.toISOString());
    if (!c.error) $("quota").textContent = "AI generations today: " + (c.count || 0) + " (free: 10/day · PRO: 100/day).";
    loadGens();
  })();
})();
