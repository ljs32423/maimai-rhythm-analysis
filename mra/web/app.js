const state = {
  songs: [],
  activeSong: null,
  difficulty: null,
  jobId: null,
  jobTimer: null,
  jobStream: null,
  jobRunning: false,
  settings: null,
  visibleSongs: 120,
  // 编辑器未保存修改跟踪：saved 记录上次加载/保存的内容
  saved: {meter: "", sweep: ""},
  dirty: {meter: false, sweep: false},
};

const $ = id => document.getElementById(id);

// 统一错误兜底：任何异步操作失败都给出 toast，而不是静默的 unhandled rejection
function guard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      toast(error.message || String(error), "error");
    }
  };
}

// 操作期间禁用按钮，防止重复点击/重复提交
async function withBusy(button, fn) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    await fn();
  } finally {
    button.disabled = false;
  }
}

function updateDirty(key) {
  const editor = key === "meter" ? $("meterEditor") : $("sweepEditor");
  state.dirty[key] = editor.value !== state.saved[key];
}

function markSaved(key, content) {
  state.saved[key] = content;
  state.dirty[key] = false;
}

function confirmDiscardChanges() {
  const names = [
    state.dirty.meter && "拍号文件",
    state.dirty.sweep && "扫键标记",
  ].filter(Boolean);
  if (!names.length) return true;
  return window.confirm(`${names.join("、")}有未保存的修改，确定要放弃吗？`);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? {"Content-Type": "application/json"} : {},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败：${response.status}`);
  return data;
}

function toast(message, type = "info") {
  const el = $("toast");
  el.textContent = message;
  el.dataset.type = type;
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), type === "error" ? 6000 : 3500);
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
    card.addEventListener("click", () => {
      const id = decodeURIComponent(card.dataset.song);
      // 再次点击当前打开的歌曲时保持已选难度
      const keep = state.activeSong && state.activeSong.id === id ? state.difficulty : null;
      guard(openSong)(id, keep);
    });
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

function highlightTab(difficulty) {
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.difficulty) === difficulty);
  });
}

async function openSong(songId, preferredDifficulty = null) {
  if (state.activeSong && state.activeSong.id !== songId) {
    if (!confirmDiscardChanges()) return;
    state.dirty.meter = state.dirty.sweep = false;
  }
  state.activeSong = await api(`/api/songs/${encodeURIComponent(songId)}`);
  $("workspaceTitle").textContent = `${state.activeSong.title} · ${state.activeSong.artist || ""}`;
  $("workspace").classList.remove("hidden");
  $("difficultyTabs").innerHTML = state.activeSong.difficulties.map(diff =>
    `<button data-difficulty="${diff.id}">${diff.name} · Lv.${escapeHtml(diff.level)}</button>`
  ).join("");
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.addEventListener("click", () => selectDifficulty(Number(button.dataset.difficulty)));
  });
  // 优先保持用户当前选中的难度，其次 MASTER，最后第一个可用难度
  const preferred =
    state.activeSong.difficulties.find(d => d.id === preferredDifficulty) ||
    state.activeSong.difficulties.find(d => d.id === 5) ||
    state.activeSong.difficulties[0];
  if (preferred) await selectDifficulty(preferred.id);
  $("workspace").scrollIntoView({behavior: "smooth", block: "start"});
}

async function selectDifficulty(difficulty) {
  if (!state.activeSong) return;
  if (state.difficulty === difficulty) {
    // openSong 重建 Tab 后会走到这里，只需恢复高亮
    highlightTab(difficulty);
    return;
  }
  if (!confirmDiscardChanges()) return;
  const songId = encodeURIComponent(state.activeSong.id);
  try {
    const [meter, sweep] = await Promise.all([
      api(`/api/songs/${songId}/meter/${difficulty}`),
      api(`/api/songs/${songId}/sweep`),
    ]);
    state.difficulty = difficulty;
    const meterText = JSON.stringify(meter.data, null, 2);
    $("meterEditor").value = meterText;
    $("sweepEditor").value = sweep.content;
    markSaved("meter", meterText);
    markSaved("sweep", sweep.content);
    highlightTab(difficulty);
    const diff = state.activeSong.difficulties.find(item => item.id === difficulty);
    if (diff.outputs.analysis) {
      $("analysisLink").href = `/library/${songId}/outputs/${difficulty === 6 ? "ReMASTER" :
        diff.name}/html/analysis.html`;
    } else {
      $("analysisLink").removeAttribute("href");
    }
    $("analysisLink").classList.toggle("disabled", !diff.outputs.analysis);
  } catch (error) {
    // 失败时回退 Tab 高亮，保持界面与编辑器内容一致
    highlightTab(state.difficulty);
    toast(error.message, "error");
  }
}

async function saveMeter() {
  if (!state.activeSong || !state.difficulty) return;
  let data;
  try { data = JSON.parse($("meterEditor").value); }
  catch (error) { toast(`JSON 格式错误：${error.message}`, "error"); return; }
  await withBusy($("saveMeterButton"), async () => {
    await api(`/api/songs/${encodeURIComponent(state.activeSong.id)}/meter/${state.difficulty}`, {
      method: "PUT", body: JSON.stringify({data}),
    });
    markSaved("meter", $("meterEditor").value);
    toast("拍号文件已保存，重新生成后生效", "success");
  });
}

async function saveSweep() {
  if (!state.activeSong) return;
  const content = $("sweepEditor").value;
  await withBusy($("saveSweepButton"), async () => {
    const result = await api(`/api/songs/${encodeURIComponent(state.activeSong.id)}/sweep`, {
      method: "PUT", body: JSON.stringify({content}),
    });
    markSaved("sweep", content);
    toast(`扫键标记文件已保存，共 ${result.markers} 个 /S`, "success");
  });
}

async function startJob() {
  if (!state.activeSong || !state.difficulty) return;
  if (state.jobRunning) {
    toast("已有任务在运行，请先等待完成或停止当前任务", "error");
    return;
  }
  await withBusy($("runButton"), async () => {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        song_id: state.activeSong.id,
        difficulty: state.difficulty,
        force: $("forceInput").checked,
      }),
    });
    state.jobId = job.id;
    state.jobRunning = true;
    $("cancelButton").classList.remove("hidden");
    watchJob();
  });
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
      state.jobRunning = false;
      $("cancelButton").classList.add("hidden");
      if (job.status === "completed") {
        toast("处理完成", "success");
        // 重新加载歌曲信息（刷新产物状态），保持当前难度不变
        await guard(openSong)(state.activeSong.id, state.difficulty);
      } else if (job.error) toast(job.error, "error");
    }
  };
  source.onerror = async () => {
    source.close();
    state.jobStream = null;
    try {
      const job = await api(`/api/jobs/${state.jobId}`);
      renderJob(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        state.jobRunning = false;
        $("cancelButton").classList.add("hidden");
      } else {
        state.jobTimer = setTimeout(watchJob, 1000);
      }
    } catch (error) {
      state.jobRunning = false;
      toast(error.message, "error");
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
  if (!state.settings) return;
  const data = structuredClone(state.settings);
  data.encoder = $("encoderInput").value;
  data.recording.width = Number($("widthInput").value);
  data.recording.height = Number($("heightInput").value);
  data.recording.fps = Number($("fpsInput").value);
  data.recording.quality = $("qualityInput").value;
  await withBusy($("saveSettingsButton"), async () => {
    const result = await api("/api/settings", {
      method: "PUT", body: JSON.stringify({data}),
    });
    state.settings = result.data;
    toast("设置已保存；录制参数将在重启后生效", "success");
  });
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
  $("refreshButton").addEventListener("click", guard(loadSongs));
  $("settingsButton").addEventListener("click", () => $("settingsPanel").classList.toggle("hidden"));
  $("probeButton").addEventListener("click", () => guard(loadSystem)(true));
  $("saveSettingsButton").addEventListener("click", guard(saveSettings));
  $("closeWorkspace").addEventListener("click", () => {
    if (confirmDiscardChanges()) $("workspace").classList.add("hidden");
  });
  $("saveMeterButton").addEventListener("click", guard(saveMeter));
  $("saveSweepButton").addEventListener("click", guard(saveSweep));
  $("runButton").addEventListener("click", guard(startJob));
  $("cancelButton").addEventListener("click", guard(cancelJob));
  $("meterEditor").addEventListener("input", () => updateDirty("meter"));
  $("sweepEditor").addEventListener("input", () => updateDirty("sweep"));
  window.addEventListener("beforeunload", event => {
    if (state.dirty.meter || state.dirty.sweep) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
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
