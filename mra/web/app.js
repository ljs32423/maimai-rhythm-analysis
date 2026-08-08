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
  songRequest: 0,
  editorEpoch: 0,
  editorRevision: 0,
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

function ownsEditor(songId, difficulty, epoch) {
  return (
    state.editorEpoch === epoch
    && state.activeSong?.id === songId
    && state.difficulty === difficulty
  );
}

function scanSweepLine(line) {
  let hasPlayable = false;
  let hasTerminalE = false;
  let hasCommaBeforeTerminal = false;
  const closing = {"(": ")", "{": "}", "[": "]"};
  for (let index = 0; index < line.length;) {
    const token = line[index];
    if (token === ",") {
      hasCommaBeforeTerminal = true;
      index += 1;
      continue;
    }
    if (/\s/.test(token) || token === "/" || token === "`") {
      index += 1;
      continue;
    }
    if (closing[token]) {
      const end = line.indexOf(closing[token], index + 1);
      index = end < 0 ? line.length : end + 1;
      continue;
    }
    if (/[0-9]/.test(token)) {
      let end = index + 1;
      while (/[0-9]/.test(line[end] || "")) end += 1;
      const button = Number(line.slice(index, end));
      if (button >= 1 && button <= 8) hasPlayable = true;
      index = end;
      continue;
    }
    if (/[ABCD]/.test(token)) {
      hasPlayable = true;
      index += 1;
      continue;
    }
    if (token === "E") {
      if (/[0-9hf]/.test(line[index + 1] || "")) {
        hasPlayable = true;
        index += 1;
        continue;
      }
      hasTerminalE = true;
      break;
    }
    index += 1;
  }
  return {hasPlayable, hasTerminalE, hasCommaBeforeTerminal};
}

function sweepMeasureNumbers(content) {
  let inote = false;
  let numberingStarted = false;
  let measure = 0;
  return content.split("\n").map(line => {
    const field = line.match(/^\s*&inote_[1-7]=(.*)$/);
    const body = field ? field[1] : line;
    const scanned = scanSweepLine(body);
    if (field) {
      inote = true;
      numberingStarted = false;
      measure = 0;
      if (!body.trim()) return "";
    } else if (/^\s*&/.test(line)) {
      inote = false;
      return "";
    }

    if (!inote || !line.trim()) return "";
    const pureEnd = (
      scanned.hasTerminalE
      && !scanned.hasPlayable
      && !scanned.hasCommaBeforeTerminal
    );
    if (pureEnd) {
      inote = false;
      return "";
    }
    if (!numberingStarted) {
      if (!scanned.hasPlayable) {
        if (scanned.hasTerminalE) inote = false;
        return "";
      }
      numberingStarted = true;
    }
    measure += 1;
    if (scanned.hasTerminalE) inote = false;
    return String(measure);
  }).join("\n");
}

function syncSweepMeasureGutter() {
  const editor = $("sweepEditor");
  $("sweepMeasureNumbers").style.transform = `translateY(${-editor.scrollTop}px)`;
}

function updateSweepMeasureGutter() {
  $("sweepMeasureNumbers").textContent = sweepMeasureNumbers($("sweepEditor").value);
  syncSweepMeasureGutter();
}

function setSweepEditorContent(content) {
  $("sweepEditor").value = content;
  updateSweepMeasureGutter();
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
  if (!songs.length) {
    $("songGrid").innerHTML = `<div class="empty-state panel">${
      query ? `没有匹配「${escapeHtml($("searchInput").value.trim())}」的歌曲` : "歌曲库为空"
    }</div>`;
    return;
  }
  $("songGrid").innerHTML = visible.map(song => `
    <article class="song-card panel" data-song="${encodeURIComponent(song.id)}" tabindex="0" role="button">
      ${song.cover_url ? `<img class="song-cover" src="${song.cover_url}" loading="lazy" decoding="async" alt="">` : ""}
      <div class="song-content">
        <h3 class="song-title">${escapeHtml(song.title)}</h3>
        <p class="song-artist">${escapeHtml(song.artist || "未知艺术家")}</p>
        <p class="song-meta">BPM ${escapeHtml(String(song.bpm || "—"))}</p>
        <div class="badges">
          ${(song.difficulties || []).map(diff =>
            `<span class="badge badge-d${diff.id}">${escapeHtml(diff.name)} · ${escapeHtml(String(diff.level))}</span>`
          ).join("")}
        </div>
      </div>
    </article>
  `).join("");
}

function renderSongSkeletons() {
  $("songCount").textContent = "正在加载歌曲…";
  $("loadMoreButton").classList.add("hidden");
  $("songGrid").innerHTML = Array.from({length: 12}, () =>
    `<article class="song-card panel skeleton" aria-hidden="true"></article>`
  ).join("");
}

function renderSongError(message) {
  $("songCount").textContent = "加载失败";
  $("songGrid").innerHTML = `<div class="empty-state panel">
    <p>歌曲库加载失败：${escapeHtml(message)}</p>
    <button id="retryLoadButton" class="secondary">重试</button>
  </div>`;
  $("retryLoadButton").addEventListener("click", guard(loadSongs));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  }[c]));
}

async function loadSongs() {
  renderSongSkeletons();
  try {
    const data = await api("/api/songs");
    state.songs = data.songs;
    renderSongs();
  } catch (error) {
    renderSongError(error.message);
    throw error;
  }
}

function highlightTab(difficulty) {
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.difficulty) === difficulty);
  });
}

// 根据当前歌曲与难度刷新“打开分析页面”链接；任务完成重新加载后走早退分支时也必须调用
function updateAnalysisLink() {
  if (!state.activeSong || !state.difficulty) return;
  const diff = state.activeSong.difficulties.find(item => item.id === state.difficulty);
  const songId = encodeURIComponent(state.activeSong.id);
  if (diff && diff.outputs.analysis) {
    $("analysisLink").href = `/library/${songId}/outputs/${state.difficulty === 6 ? "ReMASTER" :
      diff.name}/html/analysis.html`;
    $("analysisLink").classList.remove("disabled");
  } else {
    $("analysisLink").removeAttribute("href");
    $("analysisLink").classList.add("disabled");
  }
}

async function openSong(songId, preferredDifficulty = null, forceReload = false) {
  const previousSongId = state.activeSong?.id || null;
  const songChanged = previousSongId !== songId;
  const reloadEditors = songChanged || forceReload;
  if (state.activeSong && reloadEditors) {
    if (!confirmDiscardChanges()) return;
  }
  const request = ++state.songRequest;
  const editorEpoch = state.editorEpoch;
  const editorRevision = state.editorRevision;
  let song;
  try {
    song = await api(`/api/songs/${encodeURIComponent(songId)}`);
  } catch (error) {
    if (request === state.songRequest) throw error;
    return;
  }
  if (request !== state.songRequest) return;
  // 任何会重载编辑器的迟到响应，都不能盖掉随后发起的难度切换。
  if (reloadEditors && state.editorEpoch !== editorEpoch) return;
  if (reloadEditors && state.editorRevision !== editorRevision
      && !confirmDiscardChanges()) return;
  if (reloadEditors) {
    // 新歌曲或任务完成后，即使难度编号相同也必须重新加载两个编辑器。
    state.editorEpoch += 1;
    state.difficulty = null;
    $("meterEditor").value = "";
    setSweepEditorContent("");
    markSaved("meter", "");
    markSaved("sweep", "");
  }
  state.activeSong = song;
  $("workspaceTitle").textContent = `${state.activeSong.title} · ${state.activeSong.artist || ""}`;
  $("workspace").classList.remove("hidden");
  $("difficultyTabs").innerHTML = state.activeSong.difficulties.map(diff => {
    const status = diff.outputs.analysis ? "ready" : (diff.outputs.directory ? "partial" : "none");
    const tip = diff.outputs.analysis ? "已有分析页面"
      : diff.outputs.directory ? "已有部分产物，未生成分析页面"
      : "尚未生成产物";
    return `<button data-difficulty="${diff.id}" title="${tip}">` +
      `<span class="dot dot-${status}" aria-hidden="true"></span>${diff.name} · Lv.${escapeHtml(diff.level)}</button>`;
  }).join("");
  document.querySelectorAll("#difficultyTabs button").forEach(button => {
    button.addEventListener("click", () => selectDifficulty(Number(button.dataset.difficulty)));
  });
  // 优先保持用户当前选中的难度，其次 MASTER，最后第一个可用难度
  const preferred =
    state.activeSong.difficulties.find(d => d.id === preferredDifficulty) ||
    state.activeSong.difficulties.find(d => d.id === 5) ||
    state.activeSong.difficulties[0];
  if (preferred) await selectDifficulty(preferred.id);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("workspace").scrollIntoView({behavior: reduceMotion ? "auto" : "smooth", block: "start"});
}

async function selectDifficulty(difficulty) {
  if (!state.activeSong) return;
  if (state.difficulty === difficulty) {
    // openSong 重建 Tab 后会走到这里：恢复高亮，同时刷新分析链接状态
    highlightTab(difficulty);
    updateAnalysisLink();
    return;
  }
  if (!confirmDiscardChanges()) return;
  const activeSongId = state.activeSong.id;
  const songId = encodeURIComponent(activeSongId);
  const request = ++state.editorEpoch;
  const editorRevision = state.editorRevision;
  try {
    const [meter, sweep] = await Promise.all([
      api(`/api/songs/${songId}/meter/${difficulty}`),
      api(`/api/songs/${songId}/sweep/${difficulty}`),
    ]);
    if (request !== state.editorEpoch || state.activeSong?.id !== activeSongId) return;
    if (state.editorRevision !== editorRevision && !confirmDiscardChanges()) {
      highlightTab(state.difficulty);
      return;
    }
    state.difficulty = difficulty;
    const meterText = JSON.stringify(meter.data, null, 2);
    $("meterEditor").value = meterText;
    setSweepEditorContent(sweep.content);
    markSaved("meter", meterText);
    markSaved("sweep", sweep.content);
    highlightTab(difficulty);
    updateAnalysisLink();
  } catch (error) {
    if (request !== state.editorEpoch || state.activeSong?.id !== activeSongId) return;
    // 失败时回退 Tab 高亮，保持界面与编辑器内容一致
    highlightTab(state.difficulty);
    toast(error.message, "error");
  }
}

async function saveMeter() {
  if (!state.activeSong || !state.difficulty) return;
  const activeSongId = state.activeSong.id;
  const difficulty = state.difficulty;
  const epoch = state.editorEpoch;
  const meterContent = $("meterEditor").value;
  let data;
  try { data = JSON.parse(meterContent); }
  catch (error) { toast(`JSON 格式错误：${error.message}`, "error"); return; }
  await withBusy($("saveMeterButton"), async () => {
    const songId = encodeURIComponent(activeSongId);
    await api(`/api/songs/${songId}/meter/${difficulty}`, {
      method: "PUT", body: JSON.stringify({data}),
    });
    if (!ownsEditor(activeSongId, difficulty, epoch)) return;
    markSaved("meter", meterContent);
    updateDirty("meter");
    if (state.dirty.sweep) {
      toast("拍号文件已保存；扫键谱面有未保存修改，将在保存时按新拍号分行", "success");
      return;
    }
    const sweep = await api(`/api/songs/${songId}/sweep/${difficulty}`);
    if (!ownsEditor(activeSongId, difficulty, epoch)) return;
    if (state.dirty.sweep) {
      toast("拍号文件已保存；扫键谱面有未保存修改，将在保存时按新拍号分行", "success");
      return;
    }
    setSweepEditorContent(sweep.content);
    markSaved("sweep", sweep.content);
    toast("拍号文件已保存，扫键谱面已按新拍号重新分行", "success");
  });
}

async function saveSweep() {
  if (!state.activeSong || !state.difficulty) return;
  const activeSongId = state.activeSong.id;
  const difficulty = state.difficulty;
  const epoch = state.editorEpoch;
  const content = $("sweepEditor").value;
  await withBusy($("saveSweepButton"), async () => {
    const result = await api(
      `/api/songs/${encodeURIComponent(activeSongId)}/sweep/${difficulty}`,
      {
        method: "PUT", body: JSON.stringify({content}),
      },
    );
    if (!ownsEditor(activeSongId, difficulty, epoch)) return;
    if ($("sweepEditor").value !== content) {
      state.saved.sweep = result.content;
      updateDirty("sweep");
      toast(`扫键标记文件已保存，共 ${result.markers} 个 /S；编辑器中还有新的未保存修改`, "success");
      return;
    }
    setSweepEditorContent(result.content);
    markSaved("sweep", result.content);
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

async function handleTerminalJob(job) {
  if (job.id !== state.jobId || !state.jobRunning) return;
  state.jobRunning = false;
  $("cancelButton").classList.add("hidden");
  if (state.jobTimer) {
    clearTimeout(state.jobTimer);
    state.jobTimer = null;
  }
  if (job.status === "completed") {
    toast("处理完成", "success");
    await loadSongs();
    // 用户已切到别的歌曲时，只刷新歌曲列表，不触碰当前编辑器。
    if (state.activeSong?.id === job.song) {
      await openSong(job.song, state.difficulty, true);
    }
  } else if (job.error) {
    toast(job.error, "error");
  }
}

function watchJob() {
  if (!state.jobId) return;
  if (state.jobStream) state.jobStream.close();
  const watchedJobId = state.jobId;
  state.jobTimer = null;
  const source = new EventSource(`/api/jobs/${watchedJobId}/events`);
  state.jobStream = source;
  source.onmessage = async event => {
    if (state.jobId !== watchedJobId) return;
    const job = JSON.parse(event.data);
    renderJob(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      source.close();
      if (state.jobStream === source) state.jobStream = null;
      await guard(handleTerminalJob)(job);
    }
  };
  source.onerror = async () => {
    source.close();
    if (state.jobStream === source) state.jobStream = null;
    if (state.jobId !== watchedJobId || !state.jobRunning) return;
    try {
      const job = await api(`/api/jobs/${watchedJobId}`);
      if (state.jobId !== watchedJobId) return;
      renderJob(job);
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        await handleTerminalJob(job);
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
  let searchTimer = null;
  $("searchInput").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.visibleSongs = 120;
      renderSongs();
    }, 150);
  });
  $("loadMoreButton").addEventListener("click", () => {
    state.visibleSongs += 120;
    renderSongs();
  });
  // 事件委托：一次绑定，重渲染后无需重复挂监听
  $("songGrid").addEventListener("click", event => {
    const card = event.target.closest(".song-card");
    if (!card || card.classList.contains("skeleton")) return;
    const id = decodeURIComponent(card.dataset.song);
    // 再次点击当前打开的歌曲时保持已选难度
    const keep = state.activeSong && state.activeSong.id === id ? state.difficulty : null;
    guard(openSong)(id, keep);
  });
  // 键盘可达：Enter/空格 激活聚焦的卡片
  $("songGrid").addEventListener("keydown", event => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const card = event.target.closest(".song-card");
    if (!card || card.classList.contains("skeleton")) return;
    event.preventDefault();
    card.click();
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
  $("meterEditor").addEventListener("input", () => {
    state.editorRevision += 1;
    updateDirty("meter");
  });
  $("sweepEditor").addEventListener("input", () => {
    state.editorRevision += 1;
    updateDirty("sweep");
    updateSweepMeasureGutter();
  });
  $("sweepEditor").addEventListener("scroll", syncSweepMeasureGutter);
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    // 优先关闭设置面板，其次关闭工作区（有未保存修改时先确认）
    if (!$("settingsPanel").classList.contains("hidden")) {
      $("settingsPanel").classList.add("hidden");
      return;
    }
    if ($("workspace").classList.contains("hidden")) return;
    if (confirmDiscardChanges()) $("workspace").classList.add("hidden");
  });
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
