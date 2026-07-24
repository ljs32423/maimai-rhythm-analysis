const state = {
  songs: [],
  activeSong: null,
  difficulty: null,
  jobId: null,
  jobTimer: null,
  jobStream: null,
  settings: null,
  visibleSongs: 120,
};

const $ = id => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? {"Content-Type": "application/json"} : {},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败：${response.status}`);
  return data;
}

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.add("hidden"), 3500);
}

function renderSongs() {
  const query = $("searchInput").value.trim().toLowerCase();
  const songs = state.songs.filter(song =>
    `${song.title || ""} ${song.artist || ""}`.toLowerCase().includes(query)
  );
  const visible = songs.slice(0, state.visibleSongs);
  $("songCount").textContent = visible.length < songs.length
    ? `已显示 ${visible.length} / ${songs.length} 首`
    : `${songs.length} 首`;
  $("loadMoreButton").classList.toggle("hidden", visible.length >= songs.length);
  $("songGrid").innerHTML = visible.map(song => `
    <article class="song-card panel" data-song="${encodeURIComponent(song.id)}">
      ${song.cover_url ? `<img class="song-cover" src="${song.cover_url}" alt="">` : ""}
      <div class="song-content">
        <h3 class="song-title">${escapeHtml(song.title)}</h3>
        <p class="song-artist">${escapeHtml(song.artist || "未知艺术家")}</p>
        <p class="song-meta">BPM ${escapeHtml(String(song.bpm || "—"))}</p>
        <div class="badges">
          ${(song.difficulties || []).map(diff =>
            `<span class="badge">${escapeHtml(diff.name)} · ${escapeHtml(String(diff.level))}</span>`
          ).join("")}
        </div>
      </div>
    </article>
  `).join("");
  document.querySelectorAll(".song-card").forEach(card => {
    card.addEventListener("click", () => openSong(decodeURIComponent(card.dataset.song)));
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[c]));
}

async function loadSongs() {
  const data = await api("/api/songs");
  state.songs = data.songs;
  renderSongs();
}

async function openSong(songId) {
  state.activeSong = await api(`/api/songs/${encodeURIComponent(songId)}`);
  $("workspaceTitle").textContent = `${state.activeSong.title} · ${state.activeSong.artist || ""}`;
  $("workspace").classList.remove("hidden");
  $("difficultyTabs").innerHTML = state.activeSong.difficulties.map(diff =>
    `<button data-difficulty="${diff.id}">${diff.name} · Lv.${escapeHtml(diff.level)}</button>`
  ).join("");
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.addEventListener("click", () => selectDifficulty(Number(button.dataset.difficulty)));
  });
  const preferred = state.activeSong.difficulties.find(d => d.id === 5) || state.activeSong.difficulties[0];
  if (preferred) await selectDifficulty(preferred.id);
  $("workspace").scrollIntoView({behavior: "smooth", block: "start"});
}

async function selectDifficulty(difficulty) {
  state.difficulty = difficulty;
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.difficulty) === difficulty);
  });
  const songId = encodeURIComponent(state.activeSong.id);
  const [meter, sweep] = await Promise.all([
    api(`/api/songs/${songId}/meter/${difficulty}`),
    api(`/api/songs/${songId}/sweep`),
  ]);
  $("meterEditor").value = JSON.stringify(meter.data, null, 2);
  $("sweepEditor").value = sweep.content;
  const diff = state.activeSong.difficulties.find(item => item.id === difficulty);
  const path = `/library/${songId}/outputs/${difficulty === 6 ? "ReMASTER" :
    diff.name}/html/analysis.html`;
  $("analysisLink").href = path;
  $("analysisLink").classList.toggle("disabled", !diff.outputs.analysis);
}

async function saveMeter() {
  let data;
  try { data = JSON.parse($("meterEditor").value); }
  catch (error) { toast(`JSON 格式错误：${error.message}`); return; }
  await api(`/api/songs/${encodeURIComponent(state.activeSong.id)}/meter/${state.difficulty}`, {
    method: "PUT", body: JSON.stringify({data}),
  });
  toast("拍号文件已保存，重新生成后生效");
}

async function saveSweep() {
  const content = $("sweepEditor").value;
  const result = await api(`/api/songs/${encodeURIComponent(state.activeSong.id)}/sweep`, {
    method: "PUT", body: JSON.stringify({content}),
  });
  toast(`扫键标记文件已保存，共 ${result.markers} 个 /S`);
}

async function startJob() {
  if (!state.activeSong || !state.difficulty) return;
  const job = await api("/api/jobs", {
    method: "POST",
    body: JSON.stringify({
      song_id: state.activeSong.id,
      difficulty: state.difficulty,
      force: $("forceInput").checked,
    }),
  });
  state.jobId = job.id;
  $("cancelButton").classList.remove("hidden");
  watchJob();
}

function renderJob(job) {
  $("progressBar").style.width = `${Math.round(job.progress * 100)}%`;
  $("jobStatus").textContent = `${job.status} · ${job.step || "等待"}`;
  $("jobLog").textContent = job.logs.join("\n");
  $("jobLog").scrollTop = $("jobLog").scrollHeight;
}

function watchJob() {
  if (!state.jobId) return;
  if (state.jobStream) state.jobStream.close();
  const source = new EventSource(`/api/jobs/${state.jobId}/events`);
  state.jobStream = source;
  source.onmessage = async event => {
    const job = JSON.parse(event.data);
    renderJob(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      source.close();
      state.jobStream = null;
      $("cancelButton").classList.add("hidden");
      if (job.status === "completed") {
        toast("处理完成");
        await openSong(state.activeSong.id);
      } else if (job.error) toast(job.error);
    }
  };
  source.onerror = async () => {
    source.close();
    state.jobStream = null;
    try {
      const job = await api(`/api/jobs/${state.jobId}`);
      renderJob(job);
      if (!["completed", "failed", "cancelled"].includes(job.status)) {
        state.jobTimer = setTimeout(watchJob, 1000);
      }
    } catch (error) {
      toast(error.message);
    }
  };
}

async function cancelJob() {
  if (!state.jobId) return;
  await api(`/api/jobs/${state.jobId}/cancel`, {method: "POST"});
  toast("已请求停止任务");
}

async function loadSettings() {
  state.settings = await api("/api/settings");
  $("encoderInput").value = state.settings.encoder;
  $("widthInput").value = state.settings.recording.width;
  $("heightInput").value = state.settings.recording.height;
  $("fpsInput").value = state.settings.recording.fps;
  $("qualityInput").value = state.settings.recording.quality;
}

async function saveSettings() {
  const data = structuredClone(state.settings);
  data.encoder = $("encoderInput").value;
  data.recording.width = Number($("widthInput").value);
  data.recording.height = Number($("heightInput").value);
  data.recording.fps = Number($("fpsInput").value);
  data.recording.quality = $("qualityInput").value;
  const result = await api("/api/settings", {
    method: "PUT", body: JSON.stringify({data}),
  });
  state.settings = result.data;
  toast("设置已保存；录制参数将在重启后生效");
}

async function loadSystem(refresh = false) {
  const data = await api(`/api/system${refresh ? "?refresh=true" : ""}`);
  const caps = data.capabilities;
  $("systemStatus").textContent = caps
    ? `FFmpeg ${caps.version} · ${caps.selected_name} · MajdataView ${data.majdata_available ? "就绪" : "缺失"}`
    : `FFmpeg ${data.ffmpeg ? "检测失败" : "缺失"} · MajdataView ${data.majdata_available ? "就绪" : "缺失"}`;
  $("encoderDetails").innerHTML = caps ? caps.encoders.map(item =>
    `<div>${item.available ? "●" : "○"} ${escapeHtml(item.name)}：${item.available ? "可用" : escapeHtml(item.reason || "不可用")}</div>`
  ).join("") : escapeHtml(data.capability_error || "未找到 FFmpeg");
}

function bind() {
  $("searchInput").addEventListener("input", () => {
    state.visibleSongs = 120;
    renderSongs();
  });
  $("loadMoreButton").addEventListener("click", () => {
    state.visibleSongs += 120;
    renderSongs();
  });
  $("refreshButton").addEventListener("click", loadSongs);
  $("settingsButton").addEventListener("click", () => $("settingsPanel").classList.toggle("hidden"));
  $("probeButton").addEventListener("click", () => loadSystem(true));
  $("saveSettingsButton").addEventListener("click", saveSettings);
  $("closeWorkspace").addEventListener("click", () => $("workspace").classList.add("hidden"));
  $("saveMeterButton").addEventListener("click", saveMeter);
  $("saveSweepButton").addEventListener("click", saveSweep);
  $("runButton").addEventListener("click", startJob);
  $("cancelButton").addEventListener("click", cancelJob);
}

async function boot() {
  bind();
  try {
    await Promise.all([loadSongs(), loadSettings(), loadSystem()]);
  } catch (error) {
    toast(error.message);
  }
}
boot();
