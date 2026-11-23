/* Photothèque — interface (Bootstrap 5 + JS sans framework) */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clone = (o) => JSON.parse(JSON.stringify(o));
const PAGE = 300;
const EFFECTS = ["bw", "sepia"];

const state = {
  lang: "fr", dict: {}, view: { type: "all" }, items: [], total: 0, loading: false,
  selection: new Set(), excluded: new Set(), selectAll: false, lastClicked: null,
  folders: [], dates: [], tags: [], albums: [], clusters: [], expanded: new Set(), roots: [],
  semantic: false, settings: {}, facesAll: false,
};

/* ------------------------------------------------------------------ utilitaires */
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* ignore */ }
    toast(msg);
    throw new Error(msg);
  }
  const type = r.headers.get("content-type") || "";
  return type.includes("json") ? r.json() : r.blob();
}

function t(key, vars = {}) {
  let s = state.dict[key] ?? key;
  for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

/* Petite fenêtre de confirmation ou de saisie (remplace confirm() et prompt()).
   Renvoie true / la valeur saisie, ou null si annulée. */
function dialog({ title, body = "", input = false, value = "", ok = t("common.ok"), danger = false }) {
  const el = $("#dialog"), modal = bootstrap.Modal.getOrCreateInstance(el);
  $("#dlgTitle").textContent = title;
  $("#dlgBody").innerHTML = body;
  const field = $("#dlgInput");
  field.classList.toggle("d-none", !input);
  field.value = value;
  const okBtn = $("#dlgOk");
  okBtn.textContent = ok;
  okBtn.className = `btn btn-sm ${danger ? "btn-danger" : "btn-primary"}`;
  return new Promise((resolve) => {
    let result = null;
    const submit = () => {
      if (input && !field.value.trim()) { field.focus(); return; }
      result = input ? field.value.trim() : true;
      modal.hide();
    };
    okBtn.onclick = submit;
    field.onkeydown = (e) => { if (e.key === "Enter") submit(); };
    el.addEventListener("shown.bs.modal", () => (input ? field : okBtn).focus(), { once: true });
    el.addEventListener("hidden.bs.modal", () => resolve(result), { once: true });
    modal.show();
  });
}
const askText = (title, value = "") => dialog({ title, input: true, value });

function toast(msg) {
  $("#toast .toast-body").textContent = msg;
  bootstrap.Toast.getOrCreateInstance($("#toast"), { delay: 3000 }).show();
}

const monthName = (m) => new Intl.DateTimeFormat(state.lang, { month: "long" }).format(new Date(2000, m - 1, 1));
const fmtDate = (iso) => iso ? new Intl.DateTimeFormat(state.lang, { dateStyle: "long", timeStyle: "short" }).format(new Date(iso)) : "";
const fmtDay = (y, m, d) => new Intl.DateTimeFormat(state.lang, { dateStyle: "full" }).format(new Date(y, m - 1, d));
const weekday = (y, m, d) => new Intl.DateTimeFormat(state.lang, { weekday: "long" }).format(new Date(y, m - 1, d));
function fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = String(sec % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

const fmtSize = (bytes) => {
  if (!bytes) return "?";
  const fr = state.lang === "fr", nf = (v, d) => new Intl.NumberFormat(state.lang, { maximumFractionDigits: d }).format(v);
  if (bytes < 1048576) return `${nf(bytes / 1024, 0)} ${fr ? "Ko" : "KB"}`;
  return `${nf(bytes / 1048576, bytes < 10485760 ? 1 : 0)} ${fr ? "Mo" : "MB"}`;
};
const baseName = (p) => p.split(/[\\/]/).filter(Boolean).pop() || p;

/* ------------------------------------------------------------------ langue */
async function setLang(lang) {
  state.lang = lang;
  state.dict = await (await fetch(`/static/i18n/${lang}.json`)).json();
  document.documentElement.lang = lang;
  try { localStorage.setItem("lang", lang); } catch (_) { /* stockage indisponible */ }
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  $$("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  $$(".lang-btn").forEach((b) => b.classList.toggle("active", b.dataset.lang === lang));
  renderSidebar();
  updateTitle();
  if (viewer.photo) viewer.renderPanel();
}

/* ------------------------------------------------------------------ barre latérale */
// Chaque panneau s'affiche dès que ses données arrivent : un panneau lent ne bloque pas les autres.
async function loadSidebar() {
  const panel = (url, apply) => api(url).then((data) => { apply(data); markActive(); }).catch(() => {});
  await Promise.all([
    panel("/api/folders", (d) => { state.folders = d; renderFolders(); }),
    panel("/api/dates", (d) => { state.dates = d; renderDates(); }),
    panel("/api/tags", (d) => {
      state.tags = d;
      renderTags();
      $("#tagList").innerHTML = d.filter((x) => x.kind === "keyword").map((x) => `<option value="${esc(x.name)}">`).join("");
    }),
    panel("/api/albums", (d) => { state.albums = d; renderAlbums(); updateTitle(); }),
    panel(`/api/faces/clusters?min_faces=${state.facesAll ? 1 : 2}`, (d) => { state.clusters = d; renderFaces(); }),
  ]);
}

function renderSidebar() {
  renderFolders(); renderDates(); renderTags(); renderAlbums(); renderFaces(); markActive();
}

function sideItem(label, view, { icon = "", count = null, depth = 0, toggle = null, extra = "" } = {}) {
  const pad = 0.9 + depth * 0.9;
  const tog = toggle === null ? `<span class="toggle"></span>`
    : `<span class="toggle" data-toggle="${esc(toggle.key)}"><i class="bi bi-chevron-${toggle.open ? "down" : "right"}"></i></span>`;
  return `<a href="#" class="side-item" style="padding-left:${pad}rem" data-view='${esc(JSON.stringify(view))}'>
    ${tog}${icon ? `<i class="bi ${icon}"></i>` : ""}<span class="text-truncate">${esc(label)}</span>
    ${extra}${count !== null ? `<span class="count">${count}</span>` : ""}</a>`;
}

function renderFolders() {
  const html = [];
  for (const root of [...new Set(state.folders.map((f) => f.root))]) {
    const nodes = new Map();
    const get = (path) => {
      if (!nodes.has(path)) nodes.set(path, { path, own: 0, total: 0, children: new Set() });
      return nodes.get(path);
    };
    get("");
    for (const f of state.folders.filter((x) => x.root === root)) {
      const parts = f.folder ? f.folder.split("/") : [];
      get(f.folder).own += f.n;
      for (let i = 0; i <= parts.length; i++) {
        const p = parts.slice(0, i).join("/");
        get(p).total += f.n;
        if (i < parts.length) get(p).children.add(parts.slice(0, i + 1).join("/"));
      }
    }
    const walk = (path, depth) => {
      const n = nodes.get(path);
      const key = `${root}|${path}`;
      const open = state.expanded.has(key) || depth === 0;
      const kids = [...n.children].sort((a, b) => a.localeCompare(b, state.lang, { numeric: true }));
      const label = path ? baseName(path) : baseName(root);
      html.push(sideItem(label, { type: "folder", root, folder: path },
        { icon: depth === 0 ? "bi-hdd" : "bi-folder", count: n.total, depth,
          toggle: kids.length ? { key, open } : null,
          extra: `<span class="folder-menu ms-1" title="${esc(t("folder.exclude"))}" data-exclude-root="${esc(root)}"
                   data-exclude-folder="${esc(path)}" data-n="${n.total}" data-name="${esc(label)}"><i class="bi bi-eye-slash"></i></span>` }));
      if (open) kids.forEach((k) => walk(k, depth + 1));
    };
    walk("", 0);
  }
  $("#panel-folders").innerHTML = html.join("");
}

function renderDates() {
  const years = new Map();
  for (const d of state.dates) {
    if (!years.has(d.year)) years.set(d.year, new Map());
    const months = years.get(d.year);
    if (!months.has(d.month)) months.set(d.month, []);
    months.get(d.month).push(d);
  }
  const html = [];
  for (const [y, months] of years) {
    const yKey = `y${y}`, yOpen = state.expanded.has(yKey);
    const yCount = [...months.values()].flat().reduce((s, d) => s + d.n, 0);
    html.push(sideItem(String(y), { type: "date", year: y }, { icon: "bi-calendar3", count: yCount, toggle: { key: yKey, open: yOpen } }));
    if (!yOpen) continue;
    for (const [m, days] of months) {
      const mKey = `m${y}-${m}`, mOpen = state.expanded.has(mKey);
      html.push(sideItem(monthName(m), { type: "date", year: y, month: m },
        { count: days.reduce((s, d) => s + d.n, 0), depth: 1, toggle: { key: mKey, open: mOpen } }));
      if (mOpen) days.forEach((d) => html.push(sideItem(`${d.day} ${weekday(y, m, d.day)}`,
        { type: "date", year: y, month: m, day: d.day }, { count: d.n, depth: 2 })));
    }
  }
  $("#panel-dates").innerHTML = html.join("");
}

function renderTags() {
  const groups = [["keyword", "nav.keywords", "bi-tag"], ["person", "nav.people", "bi-person"]];
  let html = "";
  for (const [kind, label, icon] of groups) {
    const list = state.tags.filter((x) => x.kind === kind);
    if (!list.length) continue;
    html += `<div class="side-heading">${esc(t(label))}</div>`;
    html += list.map((x) => sideItem(x.name, { type: "tag", tag: x.name, kind }, { icon, count: x.n })).join("");
  }
  $("#panel-tags").innerHTML = html || `<div class="side-heading">${esc(t("nav.noTags"))}</div>`;
}

function renderAlbums() {
  let html = `<div class="side-actions"><button class="btn btn-sm btn-outline-primary w-100" id="newAlbum"><i class="bi bi-plus"></i> ${esc(t("nav.newAlbum"))}</button></div>`;
  if (!state.albums.length) html += `<div class="side-heading">${esc(t("nav.noAlbums"))}</div>`;
  html += state.albums.map((a) => sideItem(a.name, { type: "album", album: a.id }, {
    icon: a.source === "picasa" ? "bi-collection-play" : "bi-collection", count: a.n,
    extra: `<span class="album-menu ms-1"><i class="bi bi-pencil" data-rename="${esc(a.id)}" title="${esc(t("common.rename"))}"></i>
            <i class="bi bi-trash ms-1" data-delete="${esc(a.id)}" title="${esc(t("common.delete"))}"></i></span>`,
  })).join("");
  $("#panel-albums").innerHTML = html;
  $("#newAlbum").onclick = async () => {
    const name = await askText(t("prompt.album"));
    if (!name) return;
    await api("/api/albums", { json: { name } });
    await loadSidebar();
  };
}

const clusterLabel = (c) => c.name || t("faces.unnamed", { id: c.id });

function renderFaces() {
  const toggle = `<div class="form-check form-switch small px-3 py-2 ms-2"><input class="form-check-input" type="checkbox" id="facesAll"
    ${state.facesAll ? "checked" : ""}><label class="form-check-label" for="facesAll">${esc(t("faces.showSingles"))}</label></div>`;
  if (!state.clusters.length) {
    $("#panel-faces").innerHTML = toggle + `<div class="side-heading text-wrap" style="text-transform:none">${esc(t("faces.none"))}</div>`;
    $("#facesAll").onchange = (e) => { state.facesAll = e.target.checked; loadSidebar(); };
    return;
  }
  $("#panel-faces").innerHTML = toggle + state.clusters.map((c) =>
    `<a href="#" class="side-item face" data-view='${esc(JSON.stringify({ type: "cluster", id: c.id }))}'>
      <img class="face-thumb" loading="lazy" src="/api/faces/${c.cover}/thumb" alt="">
      <span class="text-truncate ${c.name ? "" : "fst-italic text-body-secondary"}">${esc(clusterLabel(c))}</span>
      <span class="count">${c.photos}</span></a>`).join("");
  $("#facesAll").onchange = (e) => { state.facesAll = e.target.checked; loadSidebar(); };
}

function markActive() {
  const cur = JSON.stringify(state.view);
  $$(".side-item").forEach((el) => el.classList.toggle("active", el.dataset.view === cur));
}

document.addEventListener("click", async (e) => {
  const tog = e.target.closest("[data-toggle]");
  if (tog) {
    e.preventDefault(); e.stopPropagation();
    const k = tog.dataset.toggle;
    state.expanded.has(k) ? state.expanded.delete(k) : state.expanded.add(k);
    renderSidebar();
    return;
  }
  const exc = e.target.closest("[data-exclude-root]");
  if (exc) {
    e.preventDefault(); e.stopPropagation();
    const { excludeRoot: root, excludeFolder: folder, n, name } = exc.dataset;
    const body = `<p class="mb-2">${esc(t("folder.excludeConfirm", { name, n }))}</p>`
      + (folder ? "" : `<p class="mb-0 text-body-secondary"><i class="bi bi-info-circle"></i> ${esc(t("folder.excludeRoot"))}</p>`);
    if (!await dialog({ title: t("folder.excludeTitle"), body, ok: t("folder.exclude"), danger: true })) return;
    await api("/api/folders/exclude", { json: { root, folder } });
    if (state.view.type === "folder" && state.view.root === root
        && (!folder || state.view.folder === folder || state.view.folder.startsWith(`${folder}/`))) state.view = { type: "all" };
    setTimeout(pollScan, 300);
    return;
  }
  const ren = e.target.closest("[data-rename]");
  if (ren) {
    e.preventDefault(); e.stopPropagation();
    const a = state.albums.find((x) => x.id === ren.dataset.rename);
    const name = await askText(t("prompt.renameAlbum"), a.name);
    if (name) { await api(`/api/albums/${a.id}`, { method: "PUT", json: { name } }); await loadSidebar(); updateTitle(); }
    return;
  }
  const del = e.target.closest("[data-delete]");
  if (del) {
    e.preventDefault(); e.stopPropagation();
    const a = state.albums.find((x) => x.id === del.dataset.delete);
    const body = `<p class="mb-2">${esc(t("prompt.deleteAlbum", { name: a.name, n: a.n }))}</p>`
      + (a.source === "picasa" ? `<p class="mb-0 text-body-secondary"><i class="bi bi-info-circle"></i> ${esc(t("prompt.deleteAlbumPicasa"))}</p>` : "");
    if (await dialog({ title: t("dialog.deleteAlbumTitle"), body, ok: t("common.delete"), danger: true })) {
      await api(`/api/albums/${a.id}`, { method: "DELETE" });
      await loadSidebar();
      if (state.view.album === a.id) setView({ type: "all" });
    }
    return;
  }
  const item = e.target.closest(".side-item");
  if (item) {
    e.preventDefault();
    setView(JSON.parse(item.dataset.view));
  }
});

$$("#sideTabs .nav-link").forEach((b) => b.addEventListener("click", () => {
  $$("#sideTabs .nav-link").forEach((x) => x.classList.toggle("active", x === b));
  $$(".side-panel").forEach((p) => p.classList.toggle("d-none", p.id !== `panel-${b.dataset.tab}`));
}));

/* ------------------------------------------------------------------ vue principale */
function viewParams() {
  const v = state.view, p = new URLSearchParams();
  if (v.type === "folder") { p.set("root", v.root); p.set("folder", v.folder); if ($("#subfolders").checked) p.set("recursive", "true"); }
  if (v.type === "date") { p.set("year", v.year); if (v.month) p.set("month", v.month); if (v.day) p.set("day", v.day); }
  if (v.type === "tag") { p.set("tag", v.tag); p.set("kind", v.kind); }
  if (v.type === "album") p.set("album", v.album);
  if (v.type === "starred") p.set("starred", "true");
  if (v.type === "search") p.set("q", v.q);
  if (v.type === "semantic") p.set("semantic", v.q);
  if (v.type === "similar") p.set("similar", v.id);
  if (v.type === "cluster") p.set("cluster", v.id);
  return p;
}

function updateTitle() {
  const v = state.view;
  let title = t("nav.all");
  if (v.type === "folder") title = v.folder ? v.folder : baseName(v.root);
  if (v.type === "date") title = v.day ? fmtDay(v.year, v.month, v.day) : v.month ? `${monthName(v.month)} ${v.year}` : String(v.year);
  if (v.type === "tag") title = v.tag;
  if (v.type === "album") title = state.albums.find((a) => a.id === v.album)?.name || "";
  if (v.type === "starred") title = t("nav.starred");
  if (v.type === "search") title = t("view.search", { q: v.q });
  if (v.type === "semantic") title = t("view.semantic", { q: v.q });
  if (v.type === "similar") title = t("view.similar", { name: v.name });
  if (v.type === "cluster") {
    const c = state.clusters.find((x) => x.id === v.id);
    title = c ? clusterLabel(c) : "";
    if ($("#clusterForm").dataset.for !== String(v.id)) {
      $("#clusterName").value = c?.name || "";
      $("#clusterForm").dataset.for = String(v.id);
    }
  }
  $("#clusterForm").classList.toggle("d-none", v.type !== "cluster");
  $("#viewTitle").textContent = title;
  $("#viewCount").textContent = t("view.count", { n: state.total });
  $("#subfoldersWrap").classList.toggle("d-none", v.type !== "folder");
  $("#selFromAlbum").classList.toggle("d-none", v.type !== "album");
  updateSelectionBar();
}

/* ------------------------------------------------------------------ grille virtualisée
   Seules les vignettes visibles (et quelques rangées autour) existent dans la page. Les données
   sont chargées par pages de PAGE photos à la demande : 200 000 photos se parcourent comme 200. */
const grid = { cols: 1, size: 170, gap: 6, pad: 10, tiles: new Map(), pages: new Set(), loading: new Set(), gen: 0 };

function setView(view, { keepScroll = false } = {}) {
  state.view = view;
  state.items = [];
  state.total = 0;
  clearSelection(false);
  grid.gen += 1;
  grid.pages.clear();
  grid.loading.clear();
  for (const el of grid.tiles.values()) el.remove();
  grid.tiles.clear();
  if (!keepScroll) $(".content").scrollTop = 0;
  markActive(); updateTitle();
  loadPage(0);
}

async function loadPage(page) {
  if (grid.pages.has(page) || grid.loading.has(page)) return;
  const gen = grid.gen;
  grid.loading.add(page);
  const p = viewParams();
  p.set("offset", page * PAGE); p.set("limit", PAGE);
  try {
    const res = await api(`/api/photos?${p}`);
    if (gen !== grid.gen) return;           // la vue a changé entre-temps
    res.items.forEach((it, i) => { state.items[page * PAGE + i] = it; });
    grid.pages.add(page);
    if (state.total !== res.total) { state.total = res.total; layoutGrid(); }
    $("#empty").classList.toggle("d-none", state.total > 0);
    updateTitle();
    renderGrid(true);
  } finally {
    grid.loading.delete(page);
  }
}

// S'assure que la photo n° idx est chargée (navigation dans la visionneuse, sélection par plage).
async function ensureIndex(idx) {
  const page = Math.floor(idx / PAGE);
  if (state.items[idx] || idx >= state.total) return state.items[idx];
  if (grid.loading.has(page)) {
    while (grid.loading.has(page)) await new Promise((r) => setTimeout(r, 30));
  } else {
    await loadPage(page);
  }
  return state.items[idx];
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#vGoFolder") || !viewer.photo) return;
  e.preventDefault();
  goToFolder(viewer.photo);
});

// Ouvre le dossier d'une photo (depuis les propriétés) et l'y montre en surbrillance.
async function goToFolder(p) {
  const parts = p.folder ? p.folder.split("/") : [];
  for (let i = 0; i < parts.length; i++) state.expanded.add(`${p.root}|${parts.slice(0, i).join("/")}`);
  $('#sideTabs .nav-link[data-tab="folders"]').click();
  viewer.modal.hide();
  setView({ type: "folder", root: p.root, folder: p.folder || "" });
  renderSidebar();
  const active = $("#panel-folders .side-item.active");
  if (active) active.scrollIntoView({ block: "center" });
  const q = viewParams();
  q.set("pid", p.id);
  let index = null;
  try { ({ index } = await api(`/api/photos/index?${q}`)); } catch (_) { return; }
  if (index == null) return;
  await ensureIndex(index);
  layoutGrid();
  const row = Math.floor(index / grid.cols);
  const content = $(".content");
  content.scrollTop = Math.max(0, $("#grid").offsetTop + grid.pad + row * (grid.size + grid.gap)
                                   - (content.clientHeight - grid.size) / 2);
  renderGrid();
  requestAnimationFrame(() => {
    const tile = grid.tiles.get(index);
    if (tile) { tile.classList.add("located"); setTimeout(() => tile.classList.remove("located"), 2500); }
  });
}

function layoutGrid() {
  const el = $("#grid");
  const width = el.clientWidth || $(".content").clientWidth;
  grid.cols = Math.max(1, Math.floor((width - 2 * grid.pad + grid.gap) / (170 + grid.gap)));
  grid.size = (width - 2 * grid.pad - grid.gap * (grid.cols - 1)) / grid.cols;
  const rows = Math.ceil(state.total / grid.cols);
  el.style.height = `${rows ? rows * (grid.size + grid.gap) - grid.gap + 2 * grid.pad : 0}px`;
  for (const [idx, tile] of grid.tiles) placeTile(tile, idx);
}

function placeTile(tile, idx) {
  const r = Math.floor(idx / grid.cols), c = idx % grid.cols;
  Object.assign(tile.style, { width: `${grid.size}px`, height: `${grid.size}px`,
    transform: `translate(${grid.pad + c * (grid.size + grid.gap)}px, ${grid.pad + r * (grid.size + grid.gap)}px)` });
}

let renderQueued = false;
function renderGrid(now = false) {
  if (!now) {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; renderGrid(true); });
    return;
  }
  const content = $(".content"), el = $("#grid");
  const top = el.getBoundingClientRect().top - content.getBoundingClientRect().top + content.scrollTop;
  const rowH = grid.size + grid.gap;
  const first = Math.max(0, Math.floor((content.scrollTop - top - grid.pad) / rowH) - 3);
  const last = Math.ceil((content.scrollTop - top + content.clientHeight) / rowH) + 3;
  const start = first * grid.cols, end = Math.min(state.total, (last + 1) * grid.cols);
  // Pages nécessaires
  for (let pg = Math.floor(start / PAGE); pg <= Math.floor(Math.max(start, end - 1) / PAGE); pg++) loadPage(pg);
  // Retirer les vignettes sorties de la fenêtre
  for (const [idx, tile] of grid.tiles) {
    if (idx < start || idx >= end) { tile.remove(); grid.tiles.delete(idx); }
  }
  // Ajouter / compléter celles qui manquent
  for (let idx = start; idx < end; idx++) {
    const it = state.items[idx];
    let tile = grid.tiles.get(idx);
    if (tile && (tile.dataset.id || !it)) continue;
    const fresh = makeTile(it, idx);
    if (tile) tile.replaceWith(fresh); else el.appendChild(fresh);
    grid.tiles.set(idx, fresh);
  }
}

function makeTile(it, idx) {
  const div = document.createElement("div");
  div.className = "tile";
  div.dataset.idx = idx;
  if (it) {
    div.dataset.id = it.id;
    if (isSelected(it.id)) div.classList.add("selected");
    const badges = [it.star ? `<i class="bi bi-star-fill star"></i>` : "",
      it.ops_source === "user" ? `<i class="bi bi-magic" title="${esc(t("viewer.source.user"))}"></i>` : "",
      it.ops_source === "picasa" ? `<i class="bi bi-p-square" title="${esc(t("viewer.source.picasa"))}"></i>` : ""].join("");
    const vbadge = it.media === "video" ? `<span class="vbadge" title="${esc(t("view.video"))}"><i class="bi bi-film"></i>${fmtDuration(it.duration)}</span>` : "";
    div.innerHTML = `<img decoding="async" src="/api/photos/${it.id}/thumb?v=${it.ops_hash}" alt="">
      ${vbadge}<div class="badges">${badges}</div>
      <div class="name"><div class="fname">${esc(it.filename)}</div>${it.caption ? `<div class="caption">${esc(it.caption)}</div>` : ""}</div>`;
  } else {
    div.classList.add("placeholder");
  }
  placeTile(div, idx);
  return div;
}

function refreshTile(item) {
  for (const [idx, tile] of grid.tiles) {
    if (Number(tile.dataset.id) === item.id) {
      const fresh = makeTile(item, idx);
      tile.replaceWith(fresh);
      grid.tiles.set(idx, fresh);
    }
  }
}

$(".content").addEventListener("scroll", () => renderGrid(), { passive: true });
new ResizeObserver(() => { layoutGrid(); renderGrid(); }).observe($("#grid"));
window.addEventListener("resize", () => { if (viewer.isVideo) viewer.fitVideo(); });

/* ------------------------------------------------------------------ sélection
   « Tout sélectionner » ne charge rien : la sélection est décrite par les filtres de la vue
   (moins d'éventuelles exclusions) et c'est le serveur qui la résout au moment d'agir. */
function isSelected(id) {
  return state.selectAll ? !state.excluded.has(id) : state.selection.has(id);
}
function selectionCount() {
  return state.selectAll ? state.total - state.excluded.size : state.selection.size;
}
function selectionPayload() {
  if (state.selectAll) {
    return { selection: { query: Object.fromEntries(viewParams()), exclude: [...state.excluded] } };
  }
  return { photo_ids: [...state.selection] };
}
function paintSelection() {
  for (const tile of grid.tiles.values()) {
    if (tile.dataset.id) tile.classList.toggle("selected", isSelected(Number(tile.dataset.id)));
  }
  updateSelectionBar();
}
function clearSelection(paint = true) {
  state.selection = new Set();
  state.excluded = new Set();
  state.selectAll = false;
  state.lastClicked = null;
  if (paint) paintSelection();
}

$("#grid").addEventListener("click", async (e) => {
  const tile = e.target.closest(".tile");
  if (!tile || !tile.dataset.id) return;
  const idx = Number(tile.dataset.idx), id = Number(tile.dataset.id);
  if (e.shiftKey && state.lastClicked !== null) {
    const [a, b] = [Math.min(state.lastClicked, idx), Math.max(state.lastClicked, idx)];
    for (let i = a; i <= b; i++) {
      const it = await ensureIndex(i);
      if (!it) continue;
      if (state.selectAll) state.excluded.delete(it.id); else state.selection.add(it.id);
    }
  } else if (e.ctrlKey || e.metaKey) {
    const set = state.selectAll ? state.excluded : state.selection;
    set.has(id) ? set.delete(id) : set.add(id);
  } else {
    clearSelection(false);
    state.selection.add(id);
  }
  state.lastClicked = idx;
  paintSelection();
});
$("#grid").addEventListener("dblclick", (e) => {
  const tile = e.target.closest(".tile");
  if (tile && tile.dataset.id) viewer.open(Number(tile.dataset.idx));
});

function updateSelectionBar() {
  const n = selectionCount();
  $("#selectionBar").classList.toggle("d-none", n === 0);
  $("#selAll").classList.toggle("d-none", n > 0);
  $("#selCount").textContent = t("view.selected", { n: n.toLocaleString(state.lang) });
}

$("#selAll").onclick = () => {
  clearSelection(false);
  state.selectAll = true;
  paintSelection();
};
$("#selNone").onclick = () => clearSelection();

async function bulkAction(fn) {
  const n = selectionCount();
  if (n > 500) toast(t("sel.working", { n: n.toLocaleString(state.lang) }));
  await fn(selectionPayload());
}
$("#selTag").onclick = async () => {
  const name = await askText(t("prompt.tag"));
  if (!name) return;
  await bulkAction((sel) => api("/api/tags/assign", { json: { ...sel, name } }));
  await loadSidebar();
};
$("#selAlbum").onclick = () => pickAlbum(async (albumId) => {
  await bulkAction((sel) => api(`/api/albums/${albumId}/add`, { json: sel }));
  await loadSidebar();
});
$("#selFromAlbum").onclick = async () => {
  await bulkAction((sel) => api(`/api/albums/${state.view.album}/remove`, { json: sel }));
  await loadSidebar();
  setView(state.view);
};
// Les photos des sous-dossiers sont incluses par défaut ; le choix est mémorisé.
try { $("#subfolders").checked = localStorage.getItem("subfolders") !== "0"; } catch (_) { $("#subfolders").checked = true; }
$("#subfolders").onchange = () => {
  try { localStorage.setItem("subfolders", $("#subfolders").checked ? "1" : "0"); } catch (_) { /* stockage indisponible */ }
  setView(state.view);
};

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  searchTimer = setTimeout(() => runSearch(q), 350);
});
function runSearch(q) {
  setView(q ? { type: state.semantic ? "semantic" : "search", q } : { type: "all" });
}
$("#btnSemantic").onclick = () => {
  state.semantic = !state.semantic;
  $("#btnSemantic").classList.toggle("active", state.semantic);
  const q = $("#search").value.trim();
  if (q) runSearch(q);
};
$("#clusterDelete").onclick = async () => {
  const c = state.clusters.find((x) => x.id === state.view.id);
  if (!c || !await dialog({ title: t("faces.deleteTitle"), body: esc(t("faces.deleteConfirm", { name: clusterLabel(c) })),
    ok: t("common.delete"), danger: true })) return;
  await api(`/api/faces/clusters/${c.id}`, { method: "DELETE" });
  toast(t("faces.deleted"));
  await loadSidebar();
  setView({ type: "all" });
};
$("#clusterForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#clusterName").value.trim();
  if (!name) return;
  const { id } = await api(`/api/faces/clusters/${state.view.id}/name`, { json: { name } });
  toast(t("faces.named", { name }));
  await loadSidebar();
  $("#clusterForm").dataset.for = "";
  setView({ type: "cluster", id });
});

function pickAlbum(onPick) {
  const modal = bootstrap.Modal.getOrCreateInstance($("#albumPicker"));
  $("#albumPickerList").innerHTML = state.albums.map((a) =>
    `<button class="list-group-item list-group-item-action" data-id="${esc(a.id)}">${esc(a.name)} <span class="text-body-secondary small">(${a.n})</span></button>`).join("");
  $$("#albumPickerList button").forEach((b) => b.onclick = async () => { modal.hide(); await onPick(b.dataset.id); });
  $("#albumPickerNew").onclick = async () => {
    modal.hide();
    await new Promise((r) => $("#albumPicker").addEventListener("hidden.bs.modal", r, { once: true }));
    const name = await askText(t("prompt.album"));
    if (!name) return;
    const { id } = await api("/api/albums", { json: { name } });
    modal.hide();
    await onPick(id);
  };
  modal.show();
}

/* ------------------------------------------------------------------ visionneuse et retouches */
const viewer = {
  idx: 0, photo: null, draft: [], renderedStraighten: 0, objectUrl: null, cropping: false, cropRect: null,
  modal: null,

  async open(idx) {
    await this.ensureOpen();
    await this.load(idx);
  },

  // Ouvre la fenêtre si besoin, y compris pendant l'animation de fermeture précédente.
  async ensureOpen() {
    const el = $("#viewer");
    this.modal = this.modal || new bootstrap.Modal(el);
    if (el.classList.contains("show") && !this.closing) return;
    if (this.closing) await new Promise((r) => el.addEventListener("hidden.bs.modal", r, { once: true }));
    slideshow.initInterval();
    await new Promise((r) => { el.addEventListener("shown.bs.modal", r, { once: true }); this.modal.show(); });
  },

  async load(idx) {
    if (idx < 0 || idx >= state.total) return;
    zoom.exit(false);
    const item = await ensureIndex(idx);
    if (!item) return;
    if (!this.photo || this.photo.id !== item.id) { this.blank(); this.stopVideo(); }
    this.idx = idx;
    this.photo = await api(`/api/photos/${item.id}`);
    this.isVideo = this.photo.media === "video";
    $(".viewer").classList.toggle("is-video", this.isVideo);
    this.draft = clone(this.photo.ops);
    this.perspEstimate = null;
    this.beforeShown = false;
    this.stopCrop();
    this.renderPanel();
    this.renderImage();
  },

  // Nouvelle photo : l'ancienne disparaît tout de suite (image et nom), la nouvelle s'affiche dès
  // qu'elle est prête. Pour les retouches d'une même photo, l'image reste en place (pas de flick).
  blank() {
    $("#vImg").classList.add("switching");
    $("#vName").textContent = "";
    $("#vMeta").textContent = "";
  },

  dirty() { return JSON.stringify(this.draft) !== JSON.stringify(this.photo.ops); },

  // Calcule l'aperçu des retouches puis l'affiche d'un seul coup : tant que la nouvelle image
  // n'est pas prête, l'image actuelle (et son éventuelle rotation CSS de prévisualisation) reste
  // à l'écran. Plus d'aller-retour visible au relâchement du curseur de redressement.
  async renderImage() {
    if (this.isVideo) return this.renderVideo();
    const img = $("#vImg");
    const token = (this.renderToken = (this.renderToken || 0) + 1);
    zoom.exit(false);
    const st = this.draft.find((o) => o.op === "straighten");
    const targetStraighten = st ? st.deg : 0;
    this.renderEditState();
    // L'indicateur de chargement n'apparaît que si le calcul dure.
    const spinner = setTimeout(() => { if (token === this.renderToken) $("#vLoading").classList.remove("d-none"); }, 350);
    let src, blobUrl = null;
    try {
      if (!this.dirty()) {
        src = `/api/photos/${this.photo.id}/preview?v=${this.photo.ops_hash}`;
      } else {
        const blob = await api(`/api/photos/${this.photo.id}/render`, { json: { ops: this.draft, size: 1600 } });
        src = blobUrl = URL.createObjectURL(blob);
      }
      const pre = new Image();
      pre.src = src;
      try { await pre.decode(); } catch (_) { await new Promise((r) => { pre.onload = pre.onerror = r; }); }
    } finally {
      clearTimeout(spinner);
    }
    if (token !== this.renderToken) {   // une retouche plus récente a déjà été demandée
      if (blobUrl) URL.revokeObjectURL(blobUrl);
      return;
    }
    // Échange instantané : nouvelle image et suppression de la rotation CSS dans la même image affichée
    img.style.transition = "none";
    img.src = src;
    img.classList.remove("switching");
    img.style.transform = "";
    this.renderedStraighten = targetStraighten;
    void img.offsetWidth;
    img.style.transition = "";
    if (this.objectUrl && this.objectUrl !== blobUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = blobUrl;
    $("#vLoading").classList.add("d-none");
    this.prefetch();
  },

  /* ---- vidéos : lecture directe (MP4) ou copie convertie par le serveur (vieux AVI...) ---- */
  renderVideo() {
    const v = $("#vVideo"), p = this.photo;
    this.renderEditState();
    const src = `/api/photos/${p.id}/video`;
    if (v.dataset.pid !== String(p.id)) {
      v.dataset.pid = p.id;
      const spinner = setTimeout(() => { if (v.dataset.pid === String(p.id)) $("#vLoading").classList.remove("d-none"); }, 350);
      const ready = () => { clearTimeout(spinner); $("#vLoading").classList.add("d-none"); this.fitVideo(); };
      v.onloadedmetadata = ready;
      v.onerror = () => { ready(); toast(t("viewer.videoError")); };
      v.onended = () => slideshow.videoEnded();
      v.src = src;
      v.classList.remove("d-none");
      if (slideshow.active && slideshow.playing) { slideshow.holdForVideo(); v.play().catch(() => {}); }
    }
    this.fitVideo();
    this.prefetch();
  },

  // Rotation (seule retouche des vidéos) : en CSS, réduite pour tenir dans le cadre si quart de tour.
  fitVideo() {
    const v = $("#vVideo");
    const rot = this.draft.find((o) => o.op === "rotate90");
    const n = rot ? rot.n : 0;
    let scale = 1;
    if (n % 2 && v.clientWidth && v.clientHeight) {
      const stage = $(".viewer-stage").getBoundingClientRect();
      const maxW = stage.width - 120, maxH = stage.height - 40;
      // une fois tournée, la largeur affichée devient la hauteur : on réduit pour tenir dans le cadre
      scale = Math.min(1, maxW / v.clientHeight, maxH / v.clientWidth);
    }
    v.style.transform = n ? `rotate(${n * 90}deg) scale(${scale})` : "";
  },

  stopVideo() {
    const v = $("#vVideo");
    if (!v.dataset.pid) return;
    v.pause();
    v.removeAttribute("src");
    v.load();
    delete v.dataset.pid;
    v.style.transform = "";
    v.classList.add("d-none");
    $("#vLoading").classList.add("d-none");
  },

  // Précharge les aperçus voisins : le serveur les calcule pendant qu'on regarde la photo.
  prefetch() {
    for (const d of [1, -1, 2]) {
      const it = state.items[this.idx + d];
      if (it && it.media !== "video") new Image().src = `/api/photos/${it.id}/preview?v=${it.ops_hash}`;
    }
    if (this.idx + 3 < state.total && !state.items[this.idx + 3]) ensureIndex(this.idx + 3);
  },

  renderPanel() {
    const p = this.photo;
    if (!p) return;
    $("#vName").textContent = p.filename;
    $("#vStar").innerHTML = `<i class="bi ${p.star ? "bi-star-fill text-warning" : "bi-star"}"></i>`;
    $("#vMeta").innerHTML = `${esc(t("viewer.date"))} : ${esc(fmtDate(p.taken_at))}<br>
      ${esc(t("viewer.folder"))} : <a href="#" id="vGoFolder" title="${esc(t("viewer.goFolder"))}">${esc(p.folder || baseName(p.root))} <i class="bi bi-box-arrow-up-right small"></i></a><br>
      ${esc(t("viewer.size"))} : ${p.width ?? "?"} × ${p.height ?? "?"}<br>
      ${esc(t("viewer.weight"))} : ${esc(fmtSize(p.size))}`
      + (p.camera ? `<br>${esc(t("viewer.camera"))} : ${esc(p.camera)}` : "")
      + (p.plant && p.plant.species ? `<br><i class="bi bi-flower1"></i> <i>${esc(p.plant.species)}</i>`
          + (p.plant.common ? ` (${esc(p.plant.common)})` : "")
          + ` · ${Math.round(p.plant.score * 100)} %`
          + (p.plant.family ? ` · ${esc(p.plant.family)}` : "")
          + ` <span class="text-body-secondary">— Pl@ntNet</span>` : "")
      + (p.video ? `<br>${esc(t("viewer.duration"))} : ${fmtDuration(p.duration)} · ${esc(p.video.codec || "?")}`
          + (p.video.audio ? ` / ${esc(p.video.audio)}` : "")
          + (p.video.playable ? "" : ` <span class="badge text-bg-secondary">${esc(t("viewer.videoConverted"))}</span>`) : "")
      + (p.exif_orientation && p.exif_orientation !== 1 ? `<br>${esc(t("viewer.orientation"))} : ${p.exif_orientation}` : "");
    // Pictogramme discret à droite du nom : le .picasa.ini du dossier n'a pas d'entrée pour ce fichier
    const dirs = (p.picasa && p.picasa.directives) || [];
    const mark = $("#vPicasaMark");
    mark.classList.toggle("d-none", !dirs.length);
    mark.title = dirs.length ? `${t("viewer.picasaDirectives")} : ` + dirs.map((k) => {
      const [kind, n] = k.split(":");
      return t(`viewer.picasaKind.${kind}`, { n });
    }).join(", ") : "";
    $("#vCaption").value = p.caption || "";
    $("#vTags").innerHTML = p.tags.map((x) => {
      const auto = x.source === "auto" || x.source === "face";
      return `<span class="tag-chip ${x.kind}${auto ? " auto" : ""}">${x.kind === "person" ? '<i class="bi bi-person"></i>' : ""}${esc(x.name)}
       ${auto ? `<span class="src">${esc(t("tag.auto"))}</span><button data-accept="${esc(x.name)}" data-kind="${x.kind}" title="${esc(t("tag.accept"))}"><i class="bi bi-check-lg"></i></button>`
              : x.source !== "user" ? `<span class="src">${esc(x.source)}</span>` : ""}
       <button data-untag="${esc(x.name)}"><i class="bi bi-x"></i></button></span>`;
    }).join("");
    $("#vSimilar").classList.toggle("d-none", !p.has_embedding);
    $("#vAlbums").innerHTML = p.albums.length ? p.albums.map((a) => `<i class="bi bi-collection"></i> ${esc(a.name)}`).join("<br>") : "—";
    const unsup = $("#vUnsupported");
    unsup.classList.toggle("d-none", !p.picasa_unsupported.length);
    unsup.textContent = t("viewer.unsupported", { list: p.picasa_unsupported.join(", ") });
    $("#eExport").href = `/api/photos/${p.id}/export`;
    $("#eOriginal").href = `/api/photos/${p.id}/original`;
    $("#ePicasa").disabled = !p.picasa_ops.length;
    $("#eEffects").innerHTML = EFFECTS.map((fx) =>
      `<button class="btn btn-sm btn-outline-secondary fx-btn" data-fx="${fx}">${esc(t(`viewer.fx.${fx}`))}</button>`).join("");
    this.renderEditState();
  },

  renderEditState() {
    const p = this.photo;
    if (!p) return;
    const src = this.dirty() ? "user" : (p.ops_source || "none");
    $("#vSource").textContent = t(`viewer.source.${src}`);
    $("#vDirty").classList.toggle("d-none", !this.dirty());
    const st = this.draft.find((o) => o.op === "straighten");
    $("#eStraighten").value = st ? st.deg : 0;
    $("#eStraightenVal").textContent = `${(st ? st.deg : 0).toFixed(1)}°`;
    $$("#eEffects .fx-btn").forEach((b) => b.classList.toggle("active", this.draft.some((o) => o.op === b.dataset.fx)));
    $("#eCropRemove").disabled = !this.draft.some((o) => o.op === "crop");
    const enh = this.draft.find((o) => o.op === "autoenhance");
    $("#eEnhanceCtl").classList.toggle("d-none", !enh);
    // Contour bleu au repos, bleu plein + coche quand l'amélioration est appliquée
    const eb = $("#eEnhance");
    eb.classList.toggle("btn-primary", !!enh);
    eb.classList.toggle("btn-outline-primary", !enh);
    if (!eb.disabled) eb.querySelector("i").className = `bi ${enh ? "bi-check2-circle" : "bi-magic"}`;
    $("#eEnhance").title = enh ? t("viewer.autoenhanceToggle") : t("viewer.autoenhanceHelp");
    if (enh) {
      $("#eEnhanceStrength").value = Math.round(enh.strength * 100);
      $("#eEnhanceVal").textContent = `${Math.round(enh.strength * 100)} %`;
    }
    const persp = this.draft.find((o) => o.op === "perspective");
    $("#ePerspCtl").classList.toggle("d-none", !persp);
    if (persp) {
      $("#eStrength").value = Math.round(persp.strength * 100);
      $("#eStrengthVal").textContent = `${Math.round(persp.strength * 100)} %`;
      if (!this.perspEstimate) $("#ePerspInfo").textContent = t("viewer.perspectiveSaved",
        { pitch: (+persp.pitch).toFixed(1), roll: (+persp.roll).toFixed(1) });
    } else if (!this.perspEstimate) {
      $("#ePerspInfo").textContent = "";
    }
  },

  setOp(kind, value, position = "end") {
    this.draft = this.draft.filter((o) => o.op !== kind);
    if (value) {
      if (position === "start") this.draft.unshift(value);
      else this.draft.push(value);
    }
  },

  rotate(delta) {
    const cur = this.draft.find((o) => o.op === "rotate90");
    const n = (((cur ? cur.n : 0) + delta) % 4 + 4) % 4;
    // Le recadrage est relatif à l'image tournée : on le retire pour éviter un cadrage incohérent.
    this.draft = this.draft.filter((o) => o.op !== "crop");
    this.setOp("rotate90", n ? { op: "rotate90", n } : null, "start");
    this.renderImage();
  },

  previewStraighten(deg) {
    zoom.exit(false);
    const img = $("#vImg");
    const delta = deg - this.renderedStraighten;
    const rad = Math.abs(delta) * Math.PI / 180;
    const w = img.clientWidth, h = img.clientHeight;
    const s = Math.cos(rad) + Math.sin(rad) * Math.max(w, h) / Math.min(w, h);
    img.style.transform = `rotate(${delta}deg) scale(${s})`;
    $("#eStraightenVal").textContent = `${(+deg).toFixed(1)}°`;
  },

  commitStraighten(deg) {
    deg = +deg;
    const idx = this.draft.findIndex((o) => o.op === "straighten");
    if (Math.abs(deg) < 0.05) {
      if (idx >= 0) this.draft.splice(idx, 1);
    } else if (idx >= 0) {
      this.draft[idx] = { op: "straighten", deg };
    } else {
      // Avant le recadrage, après les quarts de tour et la perspective
      const at = this.draft.findIndex((o) => !["rotate90", "perspective"].includes(o.op));
      this.draft.splice(at < 0 ? this.draft.length : at, 0, { op: "straighten", deg });
    }
    this.renderImage();
  },

  toggleEffect(fx) {
    if (this.draft.some((o) => o.op === fx)) this.draft = this.draft.filter((o) => o.op !== fx);
    else this.draft.push({ op: fx });
    this.renderImage();
  },

  async detectPerspective() {
    const info = $("#ePerspInfo");
    info.textContent = t("viewer.detecting");
    const res = await api(`/api/photos/${this.photo.id}/perspective`, { method: "POST" });
    if (!res.found) { info.textContent = t("viewer.perspectiveNone"); this.perspEstimate = null; return; }
    this.perspEstimate = res.estimate;
    info.textContent = t("viewer.perspectiveFound", { pitch: res.estimate.pitch.toFixed(1),
      roll: res.estimate.roll.toFixed(1), lines: res.estimate.lines })
      + (res.f35_from_exif ? "" : ` ${t("viewer.perspectiveNoExif")}`);
    this.draft = res.ops;
    this.renderImage();
  },

  async autoEnhance() {
    const btn = $("#eEnhance"), label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm"></span> ${esc(t("viewer.analyzing"))}`;
    try {
      // L'analyse porte sur la photo avec ses autres retouches (non enregistrées comprises).
      const res = await api(`/api/photos/${this.photo.id}/enhance`, { method: "POST" });
      const op = res.params;
      const keep = this.draft.filter((o) => o.op !== "autoenhance");
      this.draft = [...keep, op];
      this.renderImage();
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  },

  setEnhanceStrength(v) {
    const op = this.draft.find((o) => o.op === "autoenhance");
    if (!op) return;
    op.strength = v / 100;
    this.renderImage();
  },

  // Avant / après : affiche la photo sans aucune retouche tant que le bouton (ou O) est maintenu.
  async showOriginal(on) {
    if (this.isVideo) return;
    const img = $("#vImg");
    if (!on) {
      if (this.beforeShown) { img.src = this.afterSrc; this.beforeShown = false; }
      return;
    }
    if (this.beforeShown || !this.photo) return;
    if (this.originalFor !== this.photo.id) {
      const blob = await api(`/api/photos/${this.photo.id}/render`, { json: { ops: [], size: 1600 } });
      if (this.originalUrl) URL.revokeObjectURL(this.originalUrl);
      this.originalUrl = URL.createObjectURL(blob);
      this.originalFor = this.photo.id;
    }
    this.afterSrc = img.src;
    this.beforeShown = true;
    img.src = this.originalUrl;
  },

  setStrength(v) {
    const op = this.draft.find((o) => o.op === "perspective");
    if (!op) return;
    op.strength = v / 100;
    this.renderImage();
  },

  startCrop() {
    this.cropping = true;
    this.cropRect = null;
    $("#vWrap").classList.add("cropping");
    $("#vImg").style.transform = "";
  },

  stopCrop() {
    this.cropping = false;
    this.cropRect = null;
    $("#vWrap").classList.remove("cropping");
    $("#vCrop").classList.add("d-none");
    $("#eCropApply").classList.add("d-none");
  },

  applyCrop() {
    if (!this.cropRect) return;
    const c = this.cropRect, r4 = (v) => Math.round(v * 10000) / 10000;
    this.draft.push({ op: "crop", l: r4(c.l), t: r4(c.t), r: r4(c.r), b: r4(c.b) });
    this.stopCrop();
    this.renderImage();
  },

  async save(ops) {
    this.photo = await api(`/api/photos/${this.photo.id}/ops`, { method: "PUT", json: { ops } });
    this.draft = clone(this.photo.ops);
    const item = state.items[this.idx];
    Object.assign(item, { ops_hash: this.photo.ops_hash, ops_source: this.photo.ops_source });
    refreshTile(item);
    toast(t("viewer.saved"));
    this.renderPanel();
    this.renderImage();
  },
};

$("#vPrev").onclick = () => viewer.load(viewer.idx - 1);
$("#vNext").onclick = () => viewer.load(viewer.idx + 1);
$("#eRotL").onclick = () => viewer.rotate(-1);
$("#eRotR").onclick = () => viewer.rotate(1);
$("#eStraighten").addEventListener("input", (e) => { $("#vGrid").classList.remove("d-none"); viewer.previewStraighten(e.target.value); });
$("#eStraighten").addEventListener("change", (e) => { $("#vGrid").classList.add("d-none"); viewer.commitStraighten(e.target.value); });
$("#eEffects").addEventListener("click", (e) => { const b = e.target.closest("[data-fx]"); if (b) viewer.toggleEffect(b.dataset.fx); });
$("#eDetect").onclick = () => viewer.detectPerspective();
$("#eEnhance").onclick = (e) => {
  e.currentTarget.blur();  // sans quoi le focus garde le bouton en surbrillance
  if (viewer.draft.some((o) => o.op === "autoenhance")) { viewer.setOp("autoenhance", null); viewer.renderImage(); }
  else viewer.autoEnhance();
};
$("#eEnhanceStrength").addEventListener("input", (e) => { $("#eEnhanceVal").textContent = `${e.target.value} %`; });
$("#eEnhanceStrength").addEventListener("change", (e) => viewer.setEnhanceStrength(+e.target.value));
for (const ev of ["pointerdown", "pointerup", "pointerleave", "pointercancel"]) {
  $("#eCompare").addEventListener(ev, (e) => viewer.showOriginal(e.type === "pointerdown"));
}
document.addEventListener("keyup", (e) => { if (e.key.toLowerCase() === "o") viewer.showOriginal(false); });
$("#eStrength").addEventListener("input", (e) => { $("#eStrengthVal").textContent = `${e.target.value} %`; });
$("#eStrength").addEventListener("change", (e) => viewer.setStrength(+e.target.value));
$("#ePerspRemove").onclick = () => { viewer.setOp("perspective", null); viewer.perspEstimate = null; viewer.renderImage(); };
$("#eCrop").onclick = () => viewer.startCrop();
$("#eCropApply").onclick = () => viewer.applyCrop();
$("#eCropRemove").onclick = () => { viewer.draft = viewer.draft.filter((o) => o.op !== "crop"); viewer.renderImage(); };
$("#eSave").onclick = () => viewer.save(viewer.draft);
$("#eDiscard").onclick = () => { viewer.draft = clone(viewer.photo.ops); viewer.stopCrop(); viewer.renderImage(); };
$("#ePicasa").onclick = () => viewer.save(null);
$("#eReset").onclick = () => viewer.save([]);
$("#vStar").onclick = async () => {
  const star = !viewer.photo.star;
  await api(`/api/photos/${viewer.photo.id}/star`, { method: "PUT", json: { star } });
  viewer.photo.star = star ? 1 : 0;
  const item = state.items[viewer.idx];
  item.star = viewer.photo.star;
  refreshTile(item);
  viewer.renderPanel();
};
$("#vCaption").addEventListener("change", async (e) => {
  const caption = e.target.value.trim();
  await api(`/api/photos/${viewer.photo.id}/caption`, { method: "PUT", json: { caption } });
  viewer.photo.caption = caption;
  const item = state.items[viewer.idx];
  if (item) { item.caption = caption || null; refreshTile(item); }
});
$("#vTagInput").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter" || !e.target.value.trim()) return;
  await api("/api/tags/assign", { json: { photo_ids: [viewer.photo.id], name: e.target.value.trim() } });
  e.target.value = "";
  viewer.photo = await api(`/api/photos/${viewer.photo.id}`);
  viewer.renderPanel();
  loadSidebar();
});
$("#vSimilar").onclick = () => {
  const p = viewer.photo;
  viewer.modal.hide();
  setView({ type: "similar", id: p.id, name: p.filename });
};
$("#vTags").addEventListener("click", async (e) => {
  const acc = e.target.closest("[data-accept]");
  if (acc) {
    await api("/api/tags/assign", { json: { photo_ids: [viewer.photo.id], name: acc.dataset.accept, kind: acc.dataset.kind } });
    viewer.photo = await api(`/api/photos/${viewer.photo.id}`);
    viewer.renderPanel();
    return;
  }
  const b = e.target.closest("[data-untag]");
  if (!b) return;
  await api("/api/tags/unassign", { json: { photo_ids: [viewer.photo.id], name: b.dataset.untag } });
  viewer.photo = await api(`/api/photos/${viewer.photo.id}`);
  viewer.renderPanel();
  loadSidebar();
});

// Outil de recadrage : tracer un rectangle sur l'aperçu
(() => {
  const wrap = $("#vWrap"), box = $("#vCrop");
  let start = null;
  const rel = (e) => {
    const r = $("#vImg").getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)), r };
  };
  wrap.addEventListener("pointerdown", (e) => {
    if (!viewer.cropping || e.target.closest("#ssBar, #vFull")) return;
    start = rel(e);
    wrap.setPointerCapture(e.pointerId);
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!viewer.cropping || !start) return;
    const cur = rel(e), r = cur.r, wr = wrap.getBoundingClientRect();
    const l = Math.min(start.x, cur.x), tt = Math.min(start.y, cur.y);
    const rr = Math.max(start.x, cur.x), b = Math.max(start.y, cur.y);
    Object.assign(box.style, { left: `${r.left - wr.left + l * r.width}px`, top: `${r.top - wr.top + tt * r.height}px`,
      width: `${(rr - l) * r.width}px`, height: `${(b - tt) * r.height}px` });
    box.classList.remove("d-none");
    viewer.cropRect = { l, t: tt, r: rr, b };
  });
  wrap.addEventListener("pointerup", () => {
    if (!viewer.cropping || !start) return;
    start = null;
    const c = viewer.cropRect;
    $("#eCropApply").classList.toggle("d-none", !(c && c.r - c.l > 0.02 && c.b - c.t > 0.02));
  });
})();

document.addEventListener("keydown", (e) => {
  if (!$("#viewer").classList.contains("show") || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (e.key === "ArrowLeft") { slideshow.restart(); viewer.load(viewer.idx - 1); }
  if (e.key === "ArrowRight") { slideshow.restart(); viewer.load(viewer.idx + 1); }
  if (e.key === " " && slideshow.active) { e.preventDefault(); slideshow.toggle(); }
  else if (e.key === " " && viewer.isVideo && document.activeElement !== $("#vVideo")) {
    e.preventDefault();
    const v = $("#vVideo");
    if (v.paused) v.play().catch(() => {}); else v.pause();
  }
  if (e.key.toLowerCase() === "o" && !e.repeat) viewer.showOriginal(true);
  if (e.key === "Escape" && !slideshow.active && !$("#viewer").contains(document.activeElement)) viewer.modal.hide();
});
$("#viewer").addEventListener("hide.bs.modal", () => { viewer.closing = true; });
$("#viewer").addEventListener("hidden.bs.modal", () => {
  viewer.closing = false; slideshow.stop(); setFullview(false); viewer.photo = null; viewer.stopCrop();
  viewer.stopVideo(); viewer.isVideo = false; $(".viewer").classList.remove("is-video");
  viewer.blank();   // à la prochaine ouverture, aucune trace de la photo précédente
});

// Molette : photo suivante / précédente dans la visionneuse
let wheelLock = 0;
$(".viewer-stage").addEventListener("wheel", (e) => {
  if (viewer.cropping) return;
  zoom.exit(false);
  e.preventDefault();
  const now = Date.now();
  if (now - wheelLock < 350 || Math.abs(e.deltaY) < 4) return;
  wheelLock = now;
  slideshow.restart();
  viewer.load(viewer.idx + (e.deltaY > 0 ? 1 : -1));
}, { passive: false });

/* ------------------------------------------------------------------ diaporama */
const slideshow = {
  active: false, playing: false, timer: null, hideCursor: null,
  interval() { return Number($("#ssInterval").value) * 1000; },
  initInterval() {
    const sel = $("#ssInterval");
    const wanted = String(Math.round(state.settings.slideshow_interval || 5));
    if (![...sel.options].some((o) => o.value === wanted)) sel.add(new Option(`${wanted} s`, wanted));
    sel.value = wanted;
  },
  // Depuis la grille : à partir de la photo sélectionnée (ou de la première)
  async start() {
    if (!state.total) return;
    let idx = 0;
    if (!state.selectAll && state.selection.size) {
      idx = Math.max(0, state.items.findIndex((i) => i && state.selection.has(i.id)));
    }
    await viewer.ensureOpen();
    await viewer.load(idx);
    this.enter();
  },
  // Depuis la visionneuse : à partir de la photo affichée
  enter() {
    this.active = true;
    $(".viewer").classList.add("slideshow");
    try { $("#viewer").requestFullscreen().catch(() => {}); } catch (_) { /* plein écran refusé */ }
    this.play();
  },
  play() {
    this.playing = true;
    this.icon("bi-pause-fill", "slideshow.pause");
    this.restart();
  },
  pause() {
    this.playing = false;
    clearInterval(this.timer);
    if (viewer.isVideo) $("#vVideo").pause();
    this.icon("bi-play-fill", this.active ? "slideshow.play" : "slideshow.start");
  },
  icon(cls, key) {
    $("#ssToggle").innerHTML = `<i class="bi ${cls}"></i>`;
    $("#ssToggle").title = t(key);
  },
  toggle() {
    if (!this.active) this.enter();
    else if (this.playing) this.pause();
    else this.play();
  },
  restart() {
    if (!this.active || !this.playing) return;
    clearInterval(this.timer);
    if (viewer.isVideo) {                 // une vidéo est lue en entier, puis on passe à la suite
      const v = $("#vVideo");
      if (v.paused && !v.ended) v.play().catch(() => {});
      return;
    }
    this.timer = setInterval(() => this.next(), this.interval());
  },
  next() { return viewer.load(viewer.idx + 1 < state.total ? viewer.idx + 1 : 0); },
  holdForVideo() { clearInterval(this.timer); },
  async videoEnded() {
    if (!this.active || !this.playing) return;
    await this.next();
    this.restart();
  },
  stop() {
    this.active = false;
    this.pause();
    $(".viewer").classList.remove("slideshow", "show-cursor");
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    // Le bouton de sortie disparaît : on rend le focus à la fenêtre pour que Échap la ferme.
    if ($("#viewer").classList.contains("show")) $("#viewer").focus();
  },
};
$("#btnSlideshow").onclick = () => slideshow.start();
$("#ssToggle").onclick = () => slideshow.toggle();
$("#ssPrev").onclick = () => { slideshow.restart(); viewer.load(viewer.idx - 1); };
$("#ssNext").onclick = () => { slideshow.restart(); viewer.load(viewer.idx + 1); };
$("#ssInterval").onchange = () => slideshow.restart();
$("#ssExit").onclick = () => slideshow.stop();
$(".viewer").addEventListener("mousemove", () => {
  if (!slideshow.active) return;
  $(".viewer").classList.add("show-cursor");
  clearTimeout(slideshow.hideCursor);
  slideshow.hideCursor = setTimeout(() => $(".viewer").classList.remove("show-cursor"), 1500);
});
// Sortie du plein écran (Échap) : on quitte le diaporama mais on reste dans la visionneuse.
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && slideshow.active) slideshow.stop();
});

/* ------------------------------------------------------------------ zoom (comme Picasa) */
// Clic gauche maintenu plus de 0,5 s : zoom à 100 % sous le curseur, curseur en main.
// Glisser en gardant le bouton enfoncé : déplacement. Clic simple : retour à l'affichage normal.
const zoom = {
  active: false, scale: 1, tx: 0, ty: 0, timer: null, press: null, exitedAt: 0, fullFor: null,
  HOLD_MS: 500, MOVE_TOLERANCE: 6,

  justExited() { return Date.now() - this.exitedAt < 400; },

  geometry() {
    const img = $("#vImg"), stage = $(".viewer-stage").getBoundingClientRect();
    const box = $("#vWrap").getBoundingClientRect();
    return { img, stage, box, w: img.clientWidth, h: img.clientHeight };
  },

  // Limite le déplacement pour que l'image recouvre la zone d'affichage (ou y reste centrée).
  clamp() {
    const { stage, box, w, h } = this.geometry();
    const zw = w * this.scale, zh = h * this.scale;
    const ox = box.left - stage.left, oy = box.top - stage.top;
    this.tx = zw >= stage.width ? Math.min(-ox, Math.max(stage.width - zw - ox, this.tx)) : (stage.width - zw) / 2 - ox;
    this.ty = zh >= stage.height ? Math.min(-oy, Math.max(stage.height - zh - oy, this.ty)) : (stage.height - zh) / 2 - oy;
  },

  apply(animate = false) {
    const img = $("#vImg"), wrap = $("#vWrap");
    wrap.classList.toggle("zoom-anim", animate);
    img.style.transformOrigin = "0 0";
    img.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
  },

  enter(clientX, clientY) {
    const { img, w } = this.geometry();
    if (!w) return;
    const r = img.getBoundingClientRect();
    const px = clientX - r.left, py = clientY - r.top;      // point visé, dans l'image affichée
    const fullW = (viewer.photo.width || img.naturalWidth);
    this.scale = Math.max(fullW / w, 2);                    // 100 % (au moins ×2)
    this.tx = px - px * this.scale;                          // le point visé reste sous le curseur
    this.ty = py - py * this.scale;
    this.active = true;
    $("#vWrap").classList.add("zoomed");
    this.clamp();
    this.apply(true);
    this.loadFull();
  },

  // Remplace l'aperçu par le rendu pleine résolution (même cadre, donc aucun saut visuel).
  async loadFull() {
    const p = viewer.photo, key = `${p.id}:${JSON.stringify(viewer.draft)}`;
    if (this.fullFor === key) return;
    let src;
    if (!viewer.dirty()) {
      src = `/api/photos/${p.id}/full?v=${p.ops_hash}`;
    } else {
      const blob = await api(`/api/photos/${p.id}/render`, { json: { ops: viewer.draft, size: 0 } });
      src = URL.createObjectURL(blob);
    }
    const pre = new Image();
    pre.src = src;
    try { await pre.decode(); } catch (_) { return; }
    if (!this.active || viewer.photo !== p) return;
    $("#vImg").src = src;
    this.fullFor = key;
  },

  exit(animate = true) {
    clearTimeout(this.timer);
    this.press = null;
    if (!this.active) return;
    this.active = false;
    this.exitedAt = Date.now();
    this.fullFor = null;
    const img = $("#vImg"), wrap = $("#vWrap");
    wrap.classList.remove("panning");
    if (animate) {
      wrap.classList.add("zoom-anim");
      img.style.transform = "translate(0px, 0px) scale(1)";
      setTimeout(() => { if (!this.active) { wrap.classList.remove("zoomed", "zoom-anim"); img.style.transform = ""; img.style.transformOrigin = ""; } }, 190);
    } else {
      wrap.classList.remove("zoomed", "zoom-anim");
      img.style.transform = "";
      img.style.transformOrigin = "";
    }
  },
};

(() => {
  const wrap = $("#vWrap");
  const ignore = (e) => e.button !== 0 || viewer.cropping || slideshow.active || viewer.isVideo
    || e.target.closest("#ssBar, #vFull");
  wrap.addEventListener("pointerdown", (e) => {
    if (ignore(e)) return;
    e.preventDefault();
    wrap.setPointerCapture(e.pointerId);
    zoom.press = { x: e.clientX, y: e.clientY, lastX: e.clientX, lastY: e.clientY, moved: false,
                   startedZoomed: zoom.active, enteredNow: false };
    if (zoom.active) {
      wrap.classList.add("panning");
    } else {
      zoom.timer = setTimeout(() => {
        if (!zoom.press || zoom.press.moved) return;
        zoom.press.enteredNow = true;
        zoom.enter(zoom.press.lastX, zoom.press.lastY);
        wrap.classList.add("panning");
      }, zoom.HOLD_MS);
    }
  });
  wrap.addEventListener("pointermove", (e) => {
    const p = zoom.press;
    if (!p) return;
    if (Math.hypot(e.clientX - p.x, e.clientY - p.y) > zoom.MOVE_TOLERANCE) p.moved = true;
    if (zoom.active) {
      zoom.tx += e.clientX - p.lastX;
      zoom.ty += e.clientY - p.lastY;
      zoom.clamp();
      zoom.apply();
    } else if (p.moved) {
      clearTimeout(zoom.timer);   // la souris a bougé avant 0,5 s : pas de zoom
    }
    p.lastX = e.clientX;
    p.lastY = e.clientY;
  });
  const release = () => {
    const p = zoom.press;
    clearTimeout(zoom.timer);
    zoom.press = null;
    wrap.classList.remove("panning");
    // Clic simple (sans déplacement) alors que le zoom était déjà actif : retour à la normale.
    if (p && zoom.active && p.startedZoomed && !p.moved) zoom.exit();
  };
  wrap.addEventListener("pointerup", release);
  wrap.addEventListener("pointercancel", release);
  window.addEventListener("resize", () => { if (zoom.active) { zoom.clamp(); zoom.apply(); } });
})();

/* ------------------------------------------------------------------ plein écran */
function setFullview(on) {
  $(".viewer").classList.toggle("fullview", on);
  $("#vFull").innerHTML = `<i class="bi ${on ? "bi-fullscreen-exit" : "bi-arrows-fullscreen"}"></i>`;
  $("#vFull").title = t(on ? "viewer.exitFullscreen" : "viewer.fullscreen");
  if (on) { try { $("#viewer").requestFullscreen().catch(() => {}); } catch (_) { /* refusé */ } }
  else if (document.fullscreenElement && !slideshow.active) document.exitFullscreen().catch(() => {});
}
$("#vFull").onclick = (e) => { e.stopPropagation(); setFullview(!$(".viewer").classList.contains("fullview")); };
// Échap quitte d'abord le zoom (écouteur en phase de capture, avant celui de Bootstrap qui fermerait la fenêtre)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && zoom.active) { e.preventDefault(); e.stopPropagation(); zoom.exit(); }
}, true);
// Double-clic sur la photo : plein écran (écouteur sur le cadre, qui capture le pointeur)
$("#vWrap").addEventListener("dblclick", (e) => {
  if (e.target.closest("#ssBar, #vFull")) return;
  if (viewer.cropping || zoom.active || zoom.justExited()) return;
  setFullview(!$(".viewer").classList.contains("fullview"));
});
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && $(".viewer").classList.contains("fullview")) setFullview(false);
});

/* ------------------------------------------------------------------ thème */
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
function applyTheme(theme) {
  state.settings.theme = theme;
  const dark = theme === "dark" || (theme === "auto" && darkQuery.matches);
  document.documentElement.dataset.bsTheme = dark ? "dark" : "light";
  $("#btnTheme").innerHTML = `<i class="bi ${{ auto: "bi-circle-half", light: "bi-sun", dark: "bi-moon-stars" }[theme]}"></i>`;
  try { localStorage.setItem("theme", theme); } catch (_) { /* stockage indisponible */ }
}
darkQuery.addEventListener("change", () => { if (state.settings.theme === "auto") applyTheme("auto"); });
$("#btnTheme").onclick = () => {
  const next = { auto: "light", light: "dark", dark: "auto" }[state.settings.theme || "auto"];
  applyTheme(next);
  api("/api/settings", { method: "PUT", json: { theme: next } });
};

/* ------------------------------------------------------------------ état de l'IA */
function modelState(m) {
  if (!m) return "";
  if (m.ready) return `<span class="text-success">${esc(t("ai.model.ready"))}</span>`;
  if (m.running) return esc(t("ai.model.downloading", { mb: Math.round(m.done_mb) }));
  if (m.error) return `<span class="text-danger">${esc(t("ai.model.error", { err: m.error }))}</span>`;
  return `<span class="text-body-secondary">${esc(t("ai.model.missing"))}</span>`;
}

async function pollAI() {
  await refreshAI();
  setTimeout(pollAI, $("#settings").classList.contains("show") ? 2000 : 6000);
}

async function refreshAI() {
  try {
    const st = await api("/api/ai/status");
    let pending = 0;
    const parts = [];
    for (const [type, c] of Object.entries(st.jobs)) {
      const p = (c.pending || 0) + (c.running || 0);
      pending += p;
      parts.push(`${esc(t(`ai.type.${type}`))} : ${esc(t("ai.pending", { n: p }))}, ${esc(t("ai.done", { n: c.done || 0 }))}`
        + (c.error ? `, <span class="text-danger">${esc(t("ai.errors", { n: c.error }))}</span>` : ""));
    }
    const badge = $("#aiBadge");
    badge.classList.toggle("d-none", pending === 0);
    badge.textContent = t("ai.badge", { n: pending });
    if ($("#settings").classList.contains("show")) {
      $("#sQueue").innerHTML = parts.join(" · ") || "—";
      $("#sClipState").innerHTML = modelState(st.models.clip);
      $("#sFacesState").innerHTML = modelState(st.models[$("#sFaceModel").value]);
    }
    if (pending && !pollAI.wasBusy) pollAI.wasBusy = true;
    if (!pending && pollAI.wasBusy) { pollAI.wasBusy = false; loadSidebar(); }
  } catch (_) { /* serveur indisponible */ }
}
$("#aiBadge").onclick = () => openSettings();

/* ------------------------------------------------------------------ indexation et réglages */
async function pollScan() {
  const s = await api("/api/status");
  const el = $("#scanStatus");
  if (s.scan.running) {
    el.classList.remove("d-none");
    $("span:last-child", el).textContent = t("scan.running", { n: s.scan.done });
    setTimeout(pollScan, 1000);
  } else {
    el.classList.add("d-none");
    await loadSidebar();
    setView(state.view, { keepScroll: true });
  }
}

async function startScan() {
  await api("/api/scan", { method: "POST" });
  setTimeout(pollScan, 300);
}
$("#btnScan").onclick = startScan;

async function openSettings() {
  const s = (await api("/api/status")).settings;
  state.roots = s.roots;
  $("#sRoots").innerHTML = s.roots.map((r) => `<li class="list-group-item d-flex justify-content-between align-items-center small">
      <span class="text-truncate"><i class="bi bi-hdd me-2"></i>${esc(r)}</span>
      <button class="btn btn-sm btn-outline-danger" data-remove-root="${esc(r)}">${esc(t("settings.remove"))}</button></li>`).join("");
  state.settings = s;
  $("#sTilt").value = s.picasa_tilt_deg_per_unit;
  $("#sTheme").value = s.theme;
  $("#sInterval").value = s.slideshow_interval;
  $("#sRotateCw").checked = s.picasa_rotate_clockwise;
  $("#sAutoTags").value = s.perspective_auto_tags.join(", ");
  $("#sWatch").checked = s.watch_folders;
  $("#sCacheMax").value = s.cache_max_gb;
  refreshCacheUsage();
  $("#sEnhanceStrength").value = Math.round(s.enhance_strength * 100);
  $("#sExcluded").innerHTML = s.excluded_folders.length ? s.excluded_folders.map((f) =>
    `<li class="list-group-item d-flex justify-content-between align-items-center small">
      <span class="text-truncate"><i class="bi bi-eye-slash me-2"></i>${esc(f)}</span>
      <button class="btn btn-sm btn-outline-primary" data-include="${esc(f)}">${esc(t("settings.include"))}</button></li>`).join("")
    : `<li class="list-group-item small text-body-secondary">${esc(t("settings.noExcluded"))}</li>`;
  $("#sAiClip").checked = s.ai_clip;
  $("#sAiFaces").checked = s.ai_faces;
  $("#sFaceModel").value = s.face_model;
  $("#sTagLang").value = s.ai_tag_language;
  $("#sTagThreshold").value = s.ai_tag_threshold;
  $("#sStrength").value = s.perspective_strength;
  $("#sF35").value = s.default_f35;
  $("#sCalibResult").textContent = "";
  $("#sPnKey").value = s.plantnet_key || "";
  $("#sPnKey").type = "password";
  $("#sPnTags").value = (s.plantnet_tags || []).join(", ");
  $("#sPnMax").value = s.plantnet_daily_max;
  $("#sPnScore").value = Math.round((s.plantnet_min_score ?? 0.2) * 100);
  $("#sPnLang").value = s.plantnet_lang || "";
  refreshPlantNet();
  bootstrap.Modal.getOrCreateInstance($("#settings")).show();
  setTimeout(refreshAI, 200);
}
$("#btnSettings").onclick = openSettings;
$("#sRoots").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-remove-root]");
  if (!b) return;
  await api("/api/settings", { method: "PUT", json: { remove_root: b.dataset.removeRoot } });
  await openSettings();
  startScan();
});
const fmtBytes = (b) => {
  const nf = (v, d) => new Intl.NumberFormat(state.lang, { maximumFractionDigits: d }).format(v);
  const fr = state.lang === "fr";
  if (b >= 1024 ** 3) return `${nf(b / 1024 ** 3, 1)} ${fr ? "Go" : "GB"}`;
  if (b >= 1024 ** 2) return `${nf(b / 1024 ** 2, 0)} ${fr ? "Mo" : "MB"}`;
  return `${nf(b / 1024, 0)} ${fr ? "Ko" : "KB"}`;
};
async function refreshCacheUsage() {
  const u = await api("/api/cache");
  $("#sCacheUsage").textContent = t("settings.cacheUsage", { total: fmtBytes(u.total), max: fmtBytes(u.max),
    thumb: fmtBytes(u.thumb.bytes), preview: fmtBytes(u.preview.bytes), full: fmtBytes(u.full.bytes) });
}
$("#sCacheClear").onclick = async () => {
  const r = await api("/api/cache/clear", { json: { what: "previews" } });
  toast(t("settings.cacheCleared", { n: r.removed }));
  refreshCacheUsage();
};
$("#sExcluded").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-include]");
  if (!b) return;
  await api("/api/settings", { method: "PUT", json: { include_folder: b.dataset.include } });
  await openSettings();
  startScan();
});
$("#sRootAdd").onclick = async () => {
  const path = $("#sRootInput").value.trim();
  if (!path) return;
  await api("/api/settings", { method: "PUT", json: { add_root: path } });
  $("#sRootInput").value = "";
  await openSettings();
  startScan();
};
/* ---- Pl@ntNet ---- */
const plantnetSettings = () => ({
  plantnet_key: $("#sPnKey").value.trim(), plantnet_tags: $("#sPnTags").value,
  plantnet_daily_max: Math.max(0, Math.round(+$("#sPnMax").value || 0)),
  plantnet_min_score: Math.min(100, Math.max(0, +$("#sPnScore").value || 0)) / 100,
  plantnet_lang: $("#sPnLang").value,
});
let pnTimer = null;
async function refreshPlantNet() {
  clearTimeout(pnTimer);
  let st;
  try { st = await api("/api/plantnet"); } catch { return; }
  const q = st.quota;
  const parts = [t("plantnet.quota", { used: q.used, max: q.max, left: q.left, reset: q.reset }),
                 t("plantnet.pending", { n: st.pending })];
  if (st.running) parts.unshift(t("plantnet.running", { done: st.done, total: st.total }));
  if (st.running || st.message) parts.push(t("plantnet.results", { ok: st.identified, low: st.low, nf: st.notfound, err: st.errors }));
  if (!st.running && st.message && st.message !== "done") parts.push(`<span class="text-warning">${esc(t("plantnet.msg." + st.message))}</span>`);
  $("#sPnState").innerHTML = parts.map((x) => x.startsWith("<") ? x : esc(x)).join(" · ");
  $("#sPnStart").disabled = st.running || !st.key || !st.pending || !q.left;
  $("#sPnStop").classList.toggle("d-none", !st.running);
  if (st.running && $("#settings").classList.contains("show")) pnTimer = setTimeout(refreshPlantNet, 1500);
  if (!st.running && plantnetWasRunning) { plantnetWasRunning = false; setView(state.view, { keepScroll: true }); }
}
let plantnetWasRunning = false;
$("#sPnKeyShow").onclick = () => { const i = $("#sPnKey"); i.type = i.type === "password" ? "text" : "password"; };
["#sPnKey", "#sPnTags", "#sPnMax"].forEach((id) => $(id).addEventListener("change", async () => {
  await api("/api/settings", { method: "PUT", json: plantnetSettings() });
  refreshPlantNet();
}));
$("#sPnStart").onclick = async () => {
  await api("/api/settings", { method: "PUT", json: plantnetSettings() });
  try { await api("/api/plantnet/start", { method: "POST" }); } catch (e) { toast(t("plantnet.msg.key")); }
  plantnetWasRunning = true;
  refreshPlantNet();
};
$("#sPnStop").onclick = async () => { await api("/api/plantnet/stop", { method: "POST" }); refreshPlantNet(); };
$("#sPnRetry").onclick = async () => {
  const r = await api("/api/plantnet/reset", { json: { which: "failed" } });
  toast(t("plantnet.retried", { n: r.reset }));
  refreshPlantNet();
};

$("#sRetry").onclick = async () => { await api("/api/ai/retry", { method: "POST" }); refreshAI(); };
$("#sCalibrate").onclick = async () => {
  const out = $("#sCalibResult");
  out.textContent = "…";
  const r = await api("/api/calibrate", { method: "POST" });
  if (r.factor === null) { out.textContent = t("settings.calibrateNone"); return; }
  out.innerHTML = `${esc(t("settings.calibrateResult", { count: r.count, factor: r.factor }))}
    <button class="btn btn-sm btn-link p-0 ms-1" id="sApplyFactor">${esc(t("settings.applyFactor"))}</button>`;
  $("#sApplyFactor").onclick = async () => {
    $("#sTilt").value = r.factor;
    await api("/api/settings", { method: "PUT", json: { picasa_tilt_deg_per_unit: r.factor } });
    toast(t("settings.saved"));
    startScan();
  };
};
$("#sSave").onclick = async () => {
  state.settings = await api("/api/settings", { method: "PUT", json: {
    picasa_tilt_deg_per_unit: +$("#sTilt").value, perspective_strength: +$("#sStrength").value,
    default_f35: +$("#sF35").value, language: state.lang,
    theme: $("#sTheme").value, slideshow_interval: +$("#sInterval").value,
    picasa_rotate_clockwise: $("#sRotateCw").checked, perspective_auto_tags: $("#sAutoTags").value,
    ai_clip: $("#sAiClip").checked, ai_faces: $("#sAiFaces").checked, face_model: $("#sFaceModel").value,
    ai_tag_language: $("#sTagLang").value, ai_tag_threshold: +$("#sTagThreshold").value,
    watch_folders: $("#sWatch").checked, enhance_strength: +$("#sEnhanceStrength").value / 100,
    cache_max_gb: +$("#sCacheMax").value, ...plantnetSettings() } });
  applyTheme(state.settings.theme);
  toast(t("settings.saved"));
  bootstrap.Modal.getOrCreateInstance($("#settings")).hide();
  startScan();
};

$$(".lang-btn").forEach((b) => b.onclick = () => {
  setLang(b.dataset.lang);
  api("/api/settings", { method: "PUT", json: { language: b.dataset.lang } });
});

/* ------------------------------------------------------------------ démarrage */
(async () => {
  const status = await api("/api/status");
  state.settings = status.settings;
  let theme = status.settings.theme || "auto";
  try { theme = localStorage.getItem("theme") || theme; } catch (_) { /* stockage indisponible */ }
  applyTheme(theme);
  let lang = status.settings.language || "fr";
  try { lang = localStorage.getItem("lang") || lang; } catch (_) { /* stockage indisponible */ }
  await setLang(lang);
  await loadSidebar();
  setView({ type: "all" });
  if (status.scan.running) pollScan();
  pollAI();
  if (!status.settings.roots.length) openSettings();
})();
