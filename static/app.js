"use strict";

const $ = (id) => document.getElementById(id);
let sources = new Map();
let project = { version: 2, crops: {}, channels: {} };
let channel = null;
let sourceId = null;
let playhead = 0;
let draft = null;
let trimDrag = null;
let previewEnd = null;
let previewClipId = null;
let playbackFrame = null;
let pendingSeek = null;
let revision = 0;
let savedRevision = 0;
let saveTimer = null;
let saveChain = Promise.resolve();
let dragId = null;
let sequenceDragged = false;
let scanErrors = [];
let pointerDrag = null;
let editingViewport = null;
const collapsedBranches = new Set();
const MIN_ZOOM = 0.25;
const MIN_CENTER = -2;
const MAX_CENTER = 3;
const MIN_CLIP_DURATION = 0.05;
const TIME_EPSILON = 1e-8;
const video = $("video");
const outputVideo = $("output-video");
const outputCanvas = $("output-canvas");
const outputContext = outputCanvas.getContext("2d");
const crtCanvas = $("crt-canvas");
const crtInputs = [...document.querySelectorAll("[data-crt]")];
const crtDefaults = {
  enabled: $("crt-enabled").defaultChecked,
  ...Object.fromEntries(crtInputs.map((input) => [input.dataset.crt, Number(input.defaultValue)])),
};
let crtRenderer = null;
let crtError = null;
const outputState = {
  signature: null, clips: [], index: -1, position: 0, playing: false, ready: false,
  loading: false, finished: false, error: null, controller: null, frame: null,
};
let panelShare = 0.5;
let dividerDrag = null;
let exportJob = null;
let exportStarting = false;

function showError(error) {
  $("error").textContent = error.message || String(error);
  $("error").hidden = false;
}

function clearError() {
  $("error").hidden = true;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function action(callback) {
  return async (...args) => {
    clearError();
    try { await callback(...args); } catch (error) { showError(error); }
  };
}

function formatTime(seconds) {
  const hundredths = Math.round(seconds * 100);
  const minutes = Math.floor(hundredths / 6000);
  return `${String(minutes).padStart(2, "0")}:${((hundredths % 6000) / 100).toFixed(2).padStart(5, "0")}`;
}

function sourceCrop(id = sourceId) {
  return project.crops[id] || { x: 0.5, y: 0.5, zoom: 1 };
}

function geometry(source, crop) {
  const unit = Math.floor(Math.min(source.width / 4, source.height / 3) / crop.zoom / 2) * 2;
  const width = unit * 4;
  const height = unit * 3;
  return {
    width, height,
    x: Math.floor((source.width * crop.x - width / 2) / 2 + 0.5) * 2,
    y: Math.floor((source.height * crop.y - height / 2) / 2 + 0.5) * 2,
  };
}

function changed() {
  revision++;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => save().catch(showError), 500);
  renderOutput();
  renderTimeline();
  renderWarnings();
}

function save() {
  clearTimeout(saveTimer);
  saveChain = saveChain.catch(() => {}).then(async () => {
    if (revision === savedRevision || trimDrag) return;
    const savingRevision = revision;
    const snapshot = JSON.stringify(project);
    await api("/api/project", { method: "PUT", body: snapshot });
    savedRevision = savingRevision;
  });
  return saveChain;
}

function makeButton(text, callback, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.className = className;
  button.addEventListener("click", action(callback));
  return button;
}

function setButtonIcon(button, name) {
  if (button.dataset.icon === name) return;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  button.replaceChildren(svg);
  button.dataset.icon = name;
}

function allChannels() {
  return [...new Set([...sources.values()].map((s) => s.channel).concat(Object.keys(project.channels)))].sort();
}

function missingSources() {
  return new Set([
    ...Object.keys(project.crops).filter((id) => !sources.has(id)),
    ...Object.values(project.channels).flat().filter((clip) => !sources.has(clip.source)).map((clip) => clip.source),
  ]);
}

function missingChannels() {
  const known = new Set([...sources.values()].map((source) => source.channel));
  return Object.keys(project.channels).filter((name) => !known.has(name));
}

function renderWarnings() {
  const missing = missingSources();
  const absentChannels = missingChannels();
  $("warnings").replaceChildren();
  const messages = [...scanErrors];
  if (missing.size) {
    messages.push(`Saved edits reference missing footage:\n${[...missing].join("\n")}\nRestore those files, or remove their edits to enable saving and export.`);
  }
  if (absentChannels.length) messages.push(`Channels with no readable footage: ${absentChannels.join(", ")}`);
  $("warnings").hidden = messages.length === 0;
  $("warnings").append(document.createTextNode(messages.join("\n")));
  if (missing.size || absentChannels.length) {
    $("warnings").append(makeButton("Remove edits for missing footage", () => {
      if (!confirm("Remove crops and clips for all missing sources? This cannot be undone.")) return;
      for (const id of missing) delete project.crops[id];
      for (const name of absentChannels) delete project.channels[name];
      for (const name of Object.keys(project.channels)) {
        project.channels[name] = project.channels[name].filter((clip) => !missing.has(clip.source));
        if (!project.channels[name].length && ![...sources.values()].some((s) => s.channel === name)) {
          delete project.channels[name];
        }
      }
      changed();
      renderLibrary();
    }));
  }
  updateExportButton();
}

function updateExportButton() {
  const active = exportStarting || (exportJob && !["complete", "failed"].includes(exportJob.status));
  $("export-all").disabled = Boolean(active) || missingSources().size > 0 || missingChannels().length > 0
    || !Object.values(project.channels).some((clips) => clips.length);
  $("export-all").textContent = active ? "Exporting..." : "Export all";
}

async function watchExport(id) {
  try {
    exportJob = await api(`/api/exports/${encodeURIComponent(id)}`);
    $("export-state").hidden = false;
    $("export-progress").value = exportJob.progress;
    $("export-message").textContent = exportJob.error || exportJob.message;
    updateExportButton();
    if (exportJob.status === "failed") {
      showError(new Error(exportJob.error || "Export failed."));
    } else if (exportJob.status === "complete") {
      const count = exportJob.outputs.length;
      $("export-message").textContent = `Exported ${count} channel${count === 1 ? "" : "s"}.`;
    } else {
      setTimeout(() => watchExport(id), 750);
    }
  } catch (error) {
    $("export-state").hidden = false;
    $("export-message").textContent = "Cannot check export progress. Refresh to reconnect.";
    showError(error);
  }
}

async function exportAll() {
  if (trimDrag) throw new Error("Finish trimming before exporting.");
  const snapshot = structuredClone(project);
  exportStarting = true;
  updateExportButton();
  $("export-state").hidden = false;
  $("export-progress").value = 0;
  $("export-message").textContent = "Preparing export...";
  try {
    await save();
    exportJob = await api("/api/exports/all", {
      method: "POST", body: JSON.stringify({ project: snapshot }),
    });
    watchExport(exportJob.id);
  } catch (error) {
    $("export-message").textContent = `Export failed: ${error.message}`;
    throw error;
  } finally {
    exportStarting = false;
    updateExportButton();
  }
}

function renderLibrary() {
  $("library").replaceChildren();
  if (!allChannels().length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Add videos under VideoContent\\Source\\Channel name\\, then reload the page.";
    $("library").append(hint);
  }
  const tree = document.createElement("ul");
  tree.className = "file-tree";
  $("library").append(tree);

  function branch(parent, name, key, className, onSelect) {
    const item = document.createElement("li");
    item.className = "tree-branch";
    const row = document.createElement("div");
    row.className = "tree-row";
    const children = document.createElement("ul");
    children.className = "tree-children";
    const toggleBranch = () => {
      if (collapsedBranches.has(key)) collapsedBranches.delete(key);
      else collapsedBranches.add(key);
      updateExpanded();
    };
    const toggle = makeButton("", toggleBranch, "tree-toggle");
    const label = makeButton(name, onSelect || toggleBranch, className);
    const updateExpanded = () => {
      const expanded = !collapsedBranches.has(key);
      children.hidden = !expanded;
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${name}`);
      if (!onSelect) label.setAttribute("aria-expanded", String(expanded));
    };
    updateExpanded();
    row.append(toggle, label);
    item.append(row, children);
    parent.append(item);
    return children;
  }

  function renderContents(node, parent, path) {
    const folders = [...node.folders].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }));
    for (const [name, folder] of folders) {
      const key = `${path}/${name}`;
      const children = branch(parent, name, key, "folder-button");
      renderContents(folder, children, key);
    }
    const files = [...node.files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
    for (const source of files) {
      const item = document.createElement("li");
      item.className = "tree-file";
      const button = makeButton(source.name, () => selectSource(source.id),
        `source-button${source.id === sourceId ? " active" : ""}`);
      button.title = source.id;
      if (source.id === sourceId) button.setAttribute("aria-current", "true");
      item.append(button);
      parent.append(item);
    }
  }

  for (const name of allChannels()) {
    const children = branch(tree, name, name, `channel-button${channel === name ? " active" : ""}`, () => {
      channel = name;
      const first = [...sources.values()].find((s) => s.channel === name);
      if (first) selectSource(first.id);
      else { clearEditor(); renderLibrary(); renderOutput(); }
    });
    const root = { folders: new Map(), files: [] };
    for (const source of sources.values()) {
      if (source.channel !== name) continue;
      let node = root;
      for (const folder of source.id.split("/").slice(1, -1)) {
        if (!node.folders.has(folder)) node.folders.set(folder, { folders: new Map(), files: [] });
        node = node.folders.get(folder);
      }
      node.files.push(source);
    }
    renderContents(root, children, name);
  }
}

function selectSource(id) {
  const source = sources.get(id);
  if (!source) throw new Error("This source is missing. Restore the file and reload the page.");
  const different = sourceId !== id;
  pointerDrag = null;
  sourceId = id;
  const parts = id.split("/");
  for (let depth = 1; depth < parts.length; depth++) {
    collapsedBranches.delete(parts.slice(0, depth).join("/"));
  }
  channel = source.channel;
  draft = trimDrag = previewEnd = previewClipId = null;
  $("empty-editor").hidden = true;
  $("editor-controls").hidden = false;
  $("seek").max = String(source.duration);
  if (different) {
    video.pause();
    playhead = 0;
    pendingSeek = 0;
    $("preview-error").hidden = true;
    video.src = `/api/media/${id.split("/").map(encodeURIComponent).join("/")}`;
    video.load();
  }
  renderLibrary();
  renderOutput();
  renderTimeline();
  layoutVideo();
}

function clearEditor() {
  video.pause();
  video.removeAttribute("src");
  video.load();
  sourceId = draft = trimDrag = previewEnd = previewClipId = pendingSeek = null;
  playhead = 0;
  pointerDrag = null;
  $("empty-editor").hidden = false;
  $("editor-controls").hidden = true;
  renderTimeline();
}

function layoutVideo() {
  const source = sources.get(sourceId);
  if (!source) return;
  const crop = sourceCrop();
  const rect = geometry(source, crop);
  const stage = $("stage");
  const frame = $("crop-frame");
  const minX = Math.min(0, rect.x), minY = Math.min(0, rect.y);
  const boundsWidth = Math.max(source.width, rect.x + rect.width) - minX;
  const boundsHeight = Math.max(source.height, rect.y + rect.height) - minY;
  const fittedScale = Math.min((stage.clientWidth - 32) / boundsWidth, (stage.clientHeight - 32) / boundsHeight);
  const fitted = {
    scale: fittedScale,
    left: (stage.clientWidth - boundsWidth * fittedScale) / 2 - minX * fittedScale,
    top: (stage.clientHeight - boundsHeight * fittedScale) / 2 - minY * fittedScale,
  };
  editingViewport = pointerDrag ? pointerDrag.viewport : fitted;
  const { scale, left, top } = editingViewport;
  Object.assign(video.style, {
    width: `${source.width * scale}px`, height: `${source.height * scale}px`,
    left: `${left}px`, top: `${top}px`,
  });
  Object.assign(frame.style, {
    width: `${rect.width * scale}px`, height: `${rect.height * scale}px`,
    left: `${left + rect.x * scale}px`,
    top: `${top + rect.y * scale}px`,
  });
}

function moveClip(id, target) {
  const clips = project.channels[channel];
  const index = clips.findIndex((c) => c.id === id);
  if (index < 0 || index === target) return;
  clips.splice(target, 0, clips.splice(index, 1)[0]);
  changed();
}

function updateOutputControls() {
  const unavailable = !outputState.clips.length || outputState.clips.some((clip) => !sources.has(clip.source));
  $("output-play").disabled = unavailable || Boolean(outputState.error);
  $("output-seek").disabled = unavailable || Boolean(outputState.error);
  updateOutputTimeline();
  $("output-play").textContent = outputState.playing ? "Pause" : "Play";
  $("output-play").setAttribute("aria-label", outputState.playing ? "Pause channel" : "Play channel");
  $("output-empty").hidden = outputState.ready;
  $("output-empty").textContent = outputState.error || (unavailable
    ? outputState.clips.length ? "Restore missing footage to preview this channel." : "Select clips to preview this channel."
    : "Loading preview...");
  [...$("output-sequence").children].forEach((tile, index) => {
    tile.classList.toggle("active", index === outputState.index);
    tile.setAttribute("aria-pressed", String(index === outputState.index));
  });
}

function outputDuration() {
  return outputState.clips.reduce((total, clip) => total + clip.end - clip.start, 0);
}

function updateOutputTimeline() {
  const duration = outputDuration();
  $("output-seek").max = String(duration || 1);
  $("output-seek").value = String(outputState.position);
  $("output-seek").setAttribute("aria-valuetext", `${formatTime(outputState.position)} of ${formatTime(duration)}`);
  $("output-time").value = `${formatTime(outputState.position)} / ${formatTime(duration)}`;
}

function syncOutputPosition() {
  const clip = outputState.clips[outputState.index];
  if (outputState.finished) {
    outputState.position = outputDuration();
  } else if (clip && outputState.ready) {
    outputState.position = outputState.clips.slice(0, outputState.index)
      .reduce((total, item) => total + item.end - item.start, 0)
      + clamp(outputVideo.currentTime - clip.start, 0, clip.end - clip.start);
  }
  updateOutputTimeline();
}

function pauseOutput() {
  syncOutputPosition();
  outputState.playing = false;
  outputVideo.pause();
  cancelAnimationFrame(outputState.frame);
  outputState.frame = null;
  updateOutputControls();
}

function failOutput(error) {
  if (error.name === "AbortError") return;
  pauseOutput();
  outputState.loading = outputState.ready = false;
  outputState.error = `Cannot preview this footage: ${error.message}`;
  updateOutputControls();
  showError(error);
}

function drawOutput() {
  outputContext.fillStyle = "#000";
  outputContext.fillRect(0, 0, outputCanvas.width, outputCanvas.height);
  const clip = outputState.clips[outputState.index];
  const source = clip && sources.get(clip.source);
  if (source && outputState.ready && outputVideo.readyState >= 2) {
    const rect = geometry(source, sourceCrop(clip.source));
    const scale = outputCanvas.width / rect.width;
    outputContext.drawImage(outputVideo, -rect.x * scale, -rect.y * scale,
      source.width * scale, source.height * scale);
  }
  drawCrt();
}

function renderCrtControls() {
  const settings = project.crt || crtDefaults;
  $("crt-enabled").checked = settings.enabled;
  for (const input of crtInputs) {
    const value = settings[input.dataset.crt];
    input.value = String(value);
    input.disabled = !settings.enabled || Boolean(crtError);
    $(`${input.id}-value`).value = `${Math.round(value * 100)}%`;
  }
  $("crt-error").hidden = !settings.enabled || !crtError;
  $("crt-error").textContent = crtError
    ? `CRT effect unavailable: ${crtError.message} Unfiltered preview is shown.` : "";
}

function drawCrt() {
  const settings = project.crt || crtDefaults;
  crtCanvas.hidden = !settings.enabled || Boolean(crtError);
  if (crtCanvas.hidden) return;
  try {
    crtRenderer ??= new CrtRenderer(crtCanvas, $("crt-shader").textContent);
    crtRenderer.draw(outputCanvas, settings);
  } catch (error) {
    crtError = error;
    crtCanvas.hidden = true;
    renderCrtControls();
  }
}

function resizeOutput() {
  const width = Math.max(8, Math.min(1600, Math.round(outputCanvas.clientWidth * window.devicePixelRatio / 4) * 4));
  if (outputCanvas.width !== width) {
    outputCanvas.width = width;
    outputCanvas.height = width * 3 / 4;
  }
  drawOutput();
}

function waitOutputEvent(name, signal, trigger = () => {}) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      outputVideo.removeEventListener(name, done);
      outputVideo.removeEventListener("error", failed);
      signal.removeEventListener("abort", aborted);
    };
    const done = () => { cleanup(); resolve(); };
    const failed = () => {
      cleanup();
      reject(new Error(outputVideo.error?.message || "The browser cannot decode this source."));
    };
    const aborted = () => { cleanup(); reject(new DOMException("Preview superseded", "AbortError")); };
    if (signal.aborted) { aborted(); return; }
    outputVideo.addEventListener(name, done, { once: true });
    outputVideo.addEventListener("error", failed, { once: true });
    signal.addEventListener("abort", aborted, { once: true });
    try { trigger(); } catch (error) { cleanup(); reject(error); }
  });
}

async function cueOutput(index, playing, offset = 0, finished = false) {
  outputState.controller?.abort();
  const controller = new AbortController();
  outputState.controller = controller;
  const { signal } = controller;
  cancelAnimationFrame(outputState.frame);
  outputVideo.pause();
  const clip = outputState.clips[index];
  if (!clip || !sources.has(clip.source)) throw new Error("This clip's source is missing.");
  const position = outputState.clips.slice(0, index)
    .reduce((total, item) => total + item.end - item.start, 0) + offset;
  Object.assign(outputState, { index, position, playing, ready: false, loading: true, finished, error: null });
  updateOutputControls();
  const time = finished ? clip.end - Math.min(1 / 30, (clip.end - clip.start) / 2) : clip.start + offset;
  outputVideo.dataset.clipId = clip.id;
  if (outputVideo.dataset.source !== clip.source || outputVideo.readyState < 1) {
    await waitOutputEvent("loadedmetadata", signal, () => {
      outputVideo.dataset.source = clip.source;
      outputVideo.src = `/api/media/${clip.source.split("/").map(encodeURIComponent).join("/")}`;
      outputVideo.load();
    });
  }
  if (signal.aborted) throw new DOMException("Preview superseded", "AbortError");
  if (outputVideo.seeking || Math.abs(outputVideo.currentTime - time) > TIME_EPSILON) {
    await waitOutputEvent("seeked", signal, () => { outputVideo.currentTime = time; });
  }
  if (outputVideo.readyState < 2) await waitOutputEvent("loadeddata", signal);
  if (signal.aborted) throw new DOMException("Preview superseded", "AbortError");
  outputState.ready = true;
  outputState.loading = false;
  drawOutput();
  updateOutputControls();
  if (outputState.playing) {
    video.pause();
    await outputVideo.play();
    if (!signal.aborted && outputState.playing) outputState.frame = requestAnimationFrame(outputTick);
  }
}

async function seekOutput(time) {
  const duration = outputDuration();
  if (!duration) throw new Error("Select clips to preview this channel.");
  let remaining = clamp(time, 0, duration);
  const finished = duration - remaining <= TIME_EPSILON;
  for (let index = 0; index < outputState.clips.length; index++) {
    const clip = outputState.clips[index];
    const length = clip.end - clip.start;
    if (remaining < length - TIME_EPSILON || index === outputState.clips.length - 1) {
      await cueOutput(index, outputState.playing && !finished, clamp(remaining, 0, length), finished);
      return;
    }
    remaining -= length;
  }
}

function advanceOutput() {
  if (!outputState.playing || !outputState.ready) return;
  outputVideo.pause();
  cancelAnimationFrame(outputState.frame);
  if (outputState.index + 1 < outputState.clips.length) {
    cueOutput(outputState.index + 1, true).catch(failOutput);
  } else {
    outputState.finished = true;
    pauseOutput();
  }
}

function outputTick() {
  if (!outputState.playing || !outputState.ready) return;
  syncOutputPosition();
  const clip = outputState.clips[outputState.index];
  if (outputVideo.currentTime >= clip.end || outputVideo.ended) {
    advanceOutput();
    return;
  }
  drawOutput();
  outputState.frame = requestAnimationFrame(outputTick);
}

async function toggleOutput() {
  if (outputState.playing) { pauseOutput(); return; }
  video.pause();
  if (outputState.finished || outputState.index < 0) {
    await cueOutput(0, true);
  } else if (outputState.loading) {
    outputState.playing = true;
    updateOutputControls();
  } else if (outputState.ready) {
    const controller = outputState.controller;
    outputState.playing = true;
    updateOutputControls();
    await outputVideo.play();
    if (controller === outputState.controller && outputState.playing) {
      outputState.frame = requestAnimationFrame(outputTick);
    }
  }
}

function renderOutput() {
  if (trimDrag) { pauseOutput(); return; }
  const clips = project.channels[channel] || [];
  const signature = JSON.stringify([channel, clips]);
  if (outputState.signature === signature) { drawOutput(); return; }
  pauseOutput();
  outputState.controller?.abort();
  Object.assign(outputState, {
    signature, clips: clips.map((clip) => ({ ...clip })), index: -1, position: 0,
    ready: false, loading: false, finished: false, error: null,
  });
  $("output-sequence").replaceChildren();
  clips.forEach((clip, index) => {
    const source = sources.get(clip.source);
    const tile = makeButton("", () => {
      if (!sequenceDragged) return cueOutput(index, true).catch(failOutput);
    }, "sequence-clip");
    tile.draggable = true;
    tile.dataset.clipId = clip.id;
    tile.setAttribute("aria-label", `Preview clip ${index + 1}: ${source?.name || "Missing source"}`);
    tile.title = `${source?.name || clip.source} - drag to reorder; Alt+Left/Right also moves clips`;
    const number = document.createElement("span");
    number.className = "sequence-number";
    number.textContent = String(index + 1);
    const label = document.createElement("span");
    label.textContent = source?.name || clip.source;
    tile.append(number, label);
    tile.addEventListener("dragstart", (event) => {
      dragId = clip.id;
      sequenceDragged = true;
      event.dataTransfer.setData("text/plain", clip.id);
      event.dataTransfer.effectAllowed = "move";
      tile.classList.add("dragging");
    });
    tile.addEventListener("dragover", (event) => { event.preventDefault(); tile.classList.add("drop-target"); });
    tile.addEventListener("dragleave", () => tile.classList.remove("drop-target"));
    tile.addEventListener("drop", (event) => {
      event.preventDefault();
      if (dragId) {
        const from = clips.findIndex((item) => item.id === dragId);
        const bounds = tile.getBoundingClientRect();
        let target = index + (event.clientX > bounds.left + bounds.width / 2 ? 1 : 0);
        if (from < target) target--;
        moveClip(dragId, target);
      }
      dragId = null;
      setTimeout(() => { sequenceDragged = false; }, 0);
    });
    tile.addEventListener("dragend", () => {
      dragId = null;
      tile.classList.remove("dragging");
      document.querySelectorAll(".sequence-clip").forEach((item) => item.classList.remove("drop-target"));
      setTimeout(() => { sequenceDragged = false; }, 0);
    });
    tile.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      moveClip(clip.id, clamp(index + (event.key === "ArrowRight" ? 1 : -1), 0, clips.length - 1));
      [...$("output-sequence").children].find((item) => item.dataset.clipId === clip.id).focus({ preventScroll: true });
    });
    $("output-sequence").append(tile);
  });
  drawOutput();
  updateOutputControls();
  if (clips.length && clips.every((clip) => sources.has(clip.source))) cueOutput(0, false).catch(failOutput);
}

function sourceClips() {
  return (project.channels[channel] || []).filter((clip) => clip.source === sourceId)
    .sort((a, b) => a.start - b.start);
}

function availableGap(time) {
  const source = sources.get(sourceId);
  if (!source) return null;
  let minimum = 0, maximum = source.duration;
  for (const clip of sourceClips()) {
    if (time > clip.start + TIME_EPSILON && time < clip.end - TIME_EPSILON) return null;
    if (clip.end <= time + TIME_EPSILON) minimum = Math.max(minimum, clip.end);
    if (clip.start >= time - TIME_EPSILON) maximum = Math.min(maximum, clip.start);
  }
  return maximum - minimum + TIME_EPSILON >= MIN_CLIP_DURATION ? { minimum, maximum } : null;
}

function seek(time) {
  const source = sources.get(sourceId);
  if (!source) return;
  playhead = clamp(time, draft ? draft.minimum : 0, draft ? draft.maximum : source.duration);
  if (video.readyState >= 1) video.currentTime = playhead;
  else pendingSeek = playhead;
  updateTimelineCursor();
}

async function previewClip(clip) {
  video.pause();
  selectSource(clip.source);
  seek(clip.start);
  previewEnd = clip.end;
  previewClipId = clip.id;
  await video.play();
}

function removeClip(id) {
  const clips = project.channels[channel] || [];
  const index = clips.findIndex((clip) => clip.id === id);
  if (index < 0) throw new Error("This clip no longer exists.");
  if (previewClipId === id) {
    video.pause();
    previewEnd = previewClipId = null;
  }
  clips.splice(index, 1);
  if (draft) {
    const gap = availableGap(draft.anchor);
    draft = gap ? { anchor: draft.anchor, ...gap } : null;
  }
  changed();
}

function updateTimelineCursor() {
  const source = sources.get(sourceId);
  const button = $("clip-action");
  if (!source) { button.disabled = true; return; }
  $("seek").value = String(playhead);
  $("seek").setAttribute("aria-valuetext", formatTime(playhead));
  $("time").textContent = `${formatTime(playhead)} / ${formatTime(source.duration)}`;
  button.style.left = `${7 + Math.max(0, $("seek").clientWidth - 14) * playhead / source.duration}px`;
  setButtonIcon(button, draft ? "check" : "plus");
  button.setAttribute("aria-label", draft ? "Complete clip" : "Start clip");
  const gap = availableGap(playhead);
  button.disabled = Boolean(trimDrag) || (draft
    ? Math.abs(playhead - draft.anchor) + TIME_EPSILON < MIN_CLIP_DURATION : !gap);
  button.title = draft ? "Complete clip here (Escape cancels)" : gap
    ? "Start a clip at this time" : "Seek to unused footage to start a clip";
  $("available-range").hidden = $("draft-range").hidden = $("draft-anchor").hidden = !draft;
  if (draft) {
    $("available-range").style.left = `${100 * draft.minimum / source.duration}%`;
    $("available-range").style.width = `${100 * (draft.maximum - draft.minimum) / source.duration}%`;
    $("draft-range").style.left = `${100 * Math.min(playhead, draft.anchor) / source.duration}%`;
    $("draft-range").style.width = `${100 * Math.abs(playhead - draft.anchor) / source.duration}%`;
    $("draft-anchor").style.left = `${100 * draft.anchor / source.duration}%`;
  }
}

function renderTimeline() {
  const track = $("clip-track");
  track.replaceChildren();
  if (!sources.has(sourceId)) { updateTimelineCursor(); return; }
  sourceClips().forEach((clip, index) => {
    const block = document.createElement("div");
    block.className = "clip-block";
    block.id = `timeline-${clip.id}`;
    block.dataset.clipId = clip.id;
    block.tabIndex = 0;
    block.setAttribute("role", "group");
    const controls = document.createElement("div");
    controls.className = "clip-block-controls";
    const play = makeButton("", () => previewClip(clip), "clip-play");
    play.setAttribute("aria-label", `Play clip ${index + 1}`);
    play.title = "Play clip";
    const remove = makeButton("", () => removeClip(clip.id), "clip-delete");
    setButtonIcon(remove, "trash");
    remove.setAttribute("aria-label", `Delete clip ${index + 1}`);
    remove.title = "Delete clip";
    controls.append(play, remove);
    block.append(controls);
    for (const edge of ["start", "end"]) {
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = `clip-trim ${edge}`;
      handle.dataset.edge = edge;
      handle.setAttribute("aria-label", `Trim clip ${index + 1} ${edge}`);
      handle.title = `Drag to trim ${edge}; arrow keys adjust by 0.1s`;
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        video.pause();
        draft = previewEnd = previewClipId = null;
        trimDrag = { clip, edge, originalStart: clip.start, originalEnd: clip.end,
          bounds: trimBounds(clip), track: track.getBoundingClientRect() };
        handle.focus({ preventScroll: true });
        handle.setPointerCapture(event.pointerId);
        updateTimelineCursor();
      });
      handle.addEventListener("pointermove", (event) => {
        if (!trimDrag || trimDrag.clip.id !== clip.id || trimDrag.edge !== edge) return;
        const time = (event.clientX - trimDrag.track.left) / trimDrag.track.width * sources.get(sourceId).duration;
        trimClip(clip, edge, time, trimDrag.bounds);
      });
      handle.addEventListener("pointerup", () => finishTrim(false));
      handle.addEventListener("pointercancel", () => finishTrim(true));
      handle.addEventListener("lostpointercapture", () => finishTrim(false));
      handle.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        video.pause();
        draft = previewEnd = previewClipId = null;
        const delta = (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 1 : 0.1);
        trimClip(clip, edge, clip[edge] + delta, trimBounds(clip));
        changed();
        document.getElementById(`timeline-${clip.id}`).querySelector(`[data-edge="${edge}"]`).focus({ preventScroll: true });
      });
      block.append(handle);
    }
    track.append(block);
  });
  layoutClipBlocks();
  updateTimelineCursor();
}

function layoutClipBlocks() {
  const source = sources.get(sourceId);
  if (!source) return;
  const width = $("clip-track").clientWidth;
  sourceClips().forEach((clip, index) => {
    const block = document.getElementById(`timeline-${clip.id}`);
    if (!block) return;
    const left = width * clip.start / source.duration;
    const blockWidth = width * (clip.end - clip.start) / source.duration;
    block.style.left = `${100 * clip.start / source.duration}%`;
    block.style.width = `${100 * (clip.end - clip.start) / source.duration}%`;
    block.setAttribute("aria-label", `Clip ${index + 1}`);
    block.classList.toggle("wide", blockWidth >= 70);
    block.classList.toggle("narrow", blockWidth < 16);
    block.querySelector(".clip-block-controls").style.left = `${clamp(left + (blockWidth - 56) / 2, 0, Math.max(0, width - 56)) - left}px`;
  });
}

function trimBounds(clip) {
  let minimum = 0, maximum = sources.get(sourceId).duration;
  for (const other of sourceClips()) {
    if (other.id === clip.id) continue;
    if (other.end <= clip.start + TIME_EPSILON) minimum = Math.max(minimum, other.end);
    if (other.start >= clip.end - TIME_EPSILON) maximum = Math.min(maximum, other.start);
  }
  return { minimum, maximum };
}

function trimClip(clip, edge, time, bounds) {
  const rounded = Math.round(time * 100) / 100;
  clip[edge] = edge === "start" ? clamp(rounded, bounds.minimum, clip.end - MIN_CLIP_DURATION)
    : clamp(rounded, clip.start + MIN_CLIP_DURATION, bounds.maximum);
  seek(clip[edge]);
  layoutClipBlocks();
  renderOutput();
}

function finishTrim(cancelled) {
  if (!trimDrag) return;
  const { clip, originalStart, originalEnd } = trimDrag;
  trimDrag = null;
  if (cancelled) {
    Object.assign(clip, { start: originalStart, end: originalEnd });
    renderOutput();
    renderTimeline();
    if (revision !== savedRevision) save().catch(showError);
  } else if (clip.start !== originalStart || clip.end !== originalEnd) changed();
  else {
    updateTimelineCursor();
    if (revision !== savedRevision) save().catch(showError);
  }
}

function markClip() {
  video.pause();
  previewEnd = previewClipId = null;
  if (!draft) {
    const gap = availableGap(playhead);
    if (!gap) throw new Error("Start a clip in unused footage.");
    draft = { anchor: playhead, ...gap };
    updateTimelineCursor();
    return;
  }
  const start = Math.min(draft.anchor, playhead), end = Math.max(draft.anchor, playhead);
  if (end - start + TIME_EPSILON < MIN_CLIP_DURATION) throw new Error("Clips must be at least 0.05 seconds long.");
  (project.channels[channel] ||= []).push({ id: crypto.randomUUID(), source: sourceId, start, end });
  draft = null;
  changed();
}

async function loadLibrary() {
  const data = await api("/api/library");
  sources = new Map(data.sources.map((s) => [s.id, s]));
  project = data.project;
  project.crt ??= { ...crtDefaults };
  renderCrtControls();
  scanErrors = data.errors;
  if (data.activeJob) exportJob = { id: data.activeJob, status: "running" };
  revision = savedRevision = 0;
  renderWarnings();
  const next = sources.has(sourceId) ? sourceId : sources.keys().next().value;
  if (next) selectSource(next);
  else {
    clearEditor();
    channel = allChannels()[0] || null;
    renderLibrary();
    renderOutput();
  }
  if (data.activeJob) watchExport(data.activeJob);
}

$("export-all").addEventListener("click", action(exportAll));
$("output-play").addEventListener("click", action(() => toggleOutput().catch(failOutput)));
$("output-seek").addEventListener("input", action(() => seekOutput(Number($("output-seek").value)).catch(failOutput)));
$("crt-enabled").addEventListener("change", () => {
  project.crt = { ...(project.crt || crtDefaults), enabled: $("crt-enabled").checked };
  renderCrtControls();
  changed();
});
for (const input of crtInputs) {
  input.addEventListener("input", () => {
    project.crt = { ...(project.crt || crtDefaults), [input.dataset.crt]: Number(input.value) };
    renderCrtControls();
    changed();
  });
}
crtCanvas.addEventListener("webglcontextlost", (event) => {
  event.preventDefault();
  crtError = new Error("The browser lost the CRT graphics context; waiting for recovery.");
  crtCanvas.hidden = true;
  renderCrtControls();
});
crtCanvas.addEventListener("webglcontextrestored", () => {
  crtRenderer = crtError = null;
  renderCrtControls();
  drawCrt();
});
outputVideo.addEventListener("ended", () => {
  if (outputVideo.ended) advanceOutput();
});
outputVideo.addEventListener("timeupdate", () => {
  if (outputState.ready && !outputState.loading) syncOutputPosition();
});
outputVideo.addEventListener("error", () => {
  if (!outputState.loading && outputState.index >= 0) {
    failOutput(new Error(outputVideo.error?.message || "The browser cannot decode this source."));
  }
});

function setPanelShare(share) {
  panelShare = clamp(share, 0.2, 0.8);
  $("workspace").style.gridTemplateColumns = `minmax(0, ${panelShare}fr) 10px minmax(0, ${1 - panelShare}fr)`;
  $("panel-divider").setAttribute("aria-valuenow", String(Math.round(panelShare * 100)));
}

$("panel-divider").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  const workspace = $("workspace");
  const style = getComputedStyle(workspace);
  dividerDrag = {
    x: event.clientX, share: panelShare,
    width: workspace.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - 10,
  };
  document.body.classList.add("resizing");
  $("panel-divider").setPointerCapture(event.pointerId);
});
$("panel-divider").addEventListener("pointermove", (event) => {
  if (dividerDrag) setPanelShare(dividerDrag.share + (event.clientX - dividerDrag.x) / dividerDrag.width);
});
for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
  $("panel-divider").addEventListener(name, () => {
    dividerDrag = null;
    document.body.classList.remove("resizing");
  });
}
$("panel-divider").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  setPanelShare(event.key === "Home" ? 0.2 : event.key === "End" ? 0.8
    : panelShare + (event.key === "ArrowRight" ? 0.05 : -0.05));
});

$("clip-action").addEventListener("click", action(markClip));
$("play").addEventListener("click", action(async () => {
  previewEnd = previewClipId = null;
  if (video.paused) await video.play(); else video.pause();
}));
$("seek").addEventListener("input", () => {
  video.pause();
  previewEnd = previewClipId = null;
  seek(Number($("seek").value));
});
video.addEventListener("loadedmetadata", () => {
  if (pendingSeek !== null) {
    const time = pendingSeek;
    pendingSeek = null;
    seek(time);
  }
  layoutVideo();
});
function syncPlayback() {
  if (pendingSeek !== null || !sources.has(sourceId)) return;
  if (previewEnd !== null && video.currentTime >= previewEnd) {
    const end = previewEnd;
    video.pause();
    previewEnd = previewClipId = null;
    seek(end);
    return;
  }
  if (draft) {
    const bounded = clamp(video.currentTime, draft.minimum, draft.maximum);
    if (video.currentTime < draft.minimum || video.currentTime >= draft.maximum) video.pause();
    if (Math.abs(bounded - video.currentTime) > TIME_EPSILON) {
      seek(bounded);
      return;
    }
  }
  playhead = video.currentTime;
  updateTimelineCursor();
}
video.addEventListener("timeupdate", syncPlayback);
function playbackTick() {
  syncPlayback();
  if (!video.paused) playbackFrame = requestAnimationFrame(playbackTick);
}
video.addEventListener("play", () => {
  pauseOutput();
  $("play").textContent = "Pause";
  cancelAnimationFrame(playbackFrame);
  playbackFrame = requestAnimationFrame(playbackTick);
});
video.addEventListener("pause", () => {
  $("play").textContent = "Play";
  cancelAnimationFrame(playbackFrame);
  playbackFrame = null;
});
video.addEventListener("error", () => { $("preview-error").hidden = false; });
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (trimDrag) finishTrim(true);
    draft = null;
    updateTimelineCursor();
  }
});

function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

function setCrop(crop) {
  project.crops[sourceId] = crop;
  layoutVideo();
  changed();
}

function resizedCrop(drag, unit) {
  const source = sources.get(sourceId);
  const baseUnit = Math.min(source.width / 4, source.height / 3);
  const maxZoom = Math.floor(Math.min(4, baseUnit / 2) * 100) / 100;
  const zoom = Math.round(clamp(baseUnit / Math.max(unit, 0.001), MIN_ZOOM, maxZoom) * 100) / 100;
  const { width, height } = geometry(source, { ...drag.crop, zoom });
  return {
    x: clamp((drag.anchorX + drag.signX * width / 2) / source.width, MIN_CENTER, MAX_CENTER),
    y: clamp((drag.anchorY + drag.signY * height / 2) / source.height, MIN_CENTER, MAX_CENTER),
    zoom,
  };
}

function cornerDrag(corner, rect, crop) {
  const signX = corner.includes("w") ? -1 : 1;
  const signY = corner.includes("n") ? -1 : 1;
  return { crop: { ...crop }, rect, signX, signY,
    anchorX: rect.x + (signX < 0 ? rect.width : 0),
    anchorY: rect.y + (signY < 0 ? rect.height : 0) };
}

$("crop-frame").addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !sourceId) return;
  event.preventDefault();
  const source = sources.get(sourceId);
  const crop = sourceCrop();
  const rect = geometry(source, crop);
  const corner = event.target.closest("[data-corner]")?.dataset.corner;
  pointerDrag = { x: event.clientX, y: event.clientY, crop: { ...crop },
    viewport: { ...editingViewport }, corner,
    ...(corner ? cornerDrag(corner, rect, crop) : {}) };
  $("crop-frame").setPointerCapture(event.pointerId);
});
$("crop-frame").addEventListener("pointermove", (event) => {
  if (!pointerDrag) return;
  const drag = pointerDrag;
  const dx = (event.clientX - drag.x) / drag.viewport.scale;
  const dy = (event.clientY - drag.y) / drag.viewport.scale;
  if (drag.corner) {
    const width = drag.rect.width + drag.signX * dx;
    const height = drag.rect.height + drag.signY * dy;
    setCrop(resizedCrop(drag, (width * 4 + height * 3) / 25));
  } else {
    const source = sources.get(sourceId);
    setCrop({
      x: clamp(drag.crop.x + dx / source.width, MIN_CENTER, MAX_CENTER),
      y: clamp(drag.crop.y + dy / source.height, MIN_CENTER, MAX_CENTER), zoom: drag.crop.zoom,
    });
  }
});
for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) {
  $("crop-frame").addEventListener(event, () => { pointerDrag = null; layoutVideo(); });
}
for (const handle of document.querySelectorAll(".crop-handle")) {
  handle.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const crop = sourceCrop();
    const rect = geometry(sources.get(sourceId), crop);
    const drag = cornerDrag(handle.dataset.corner, rect, crop);
    const direction = { ArrowLeft: -drag.signX, ArrowRight: drag.signX,
      ArrowUp: -drag.signY, ArrowDown: drag.signY }[event.key];
    setCrop(resizedCrop(drag, rect.width / 4 * (1 + direction * (event.shiftKey ? 0.1 : 0.02))));
  });
}
const resizeObserver = new ResizeObserver(() => {
  layoutVideo();
  layoutClipBlocks();
  updateTimelineCursor();
  resizeOutput();
});
resizeObserver.observe($("stage"));
resizeObserver.observe($("timeline"));
resizeObserver.observe($("output-stage"));
window.addEventListener("beforeunload", (event) => {
  if (revision !== savedRevision) { event.preventDefault(); event.returnValue = ""; }
});
loadLibrary().catch(showError);
