'use strict';
/* brainscope UI: plain JavaScript + Niivue. Talks to the local engine through /api. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '–' : Number(v).toFixed(d).replace(/^-(0\.?0*)$/, '$1'));
const hkey = (o) => { const s = JSON.stringify(o); let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); };
const xf = (a, m) => [0, 1, 2].map((i) => a[i][0] * m[0] + a[i][1] * m[1] + a[i][2] * m[2] + a[i][3]);  // 4x4 · point
const LEAD_COLORS = ['#ff5d5d', '#3fa7ff', '#3ecf8e', '#f0b429', '#c77dff', '#ff8fab', '#5ce1e6', '#ffd166', '#a3e635', '#fb923c'];

const S = {
  state: null, p: null, step: 'import', presets: [], orientRole: 'mri', layout: 'multi',
  ctWindow: 'bone', ctOpacity: 0.5, ctVisible: true, base: 'mri', win: { mri: null, ct: null }, winRole: 'ct', align: { ct: null, mri: null }, lead: null, contact: null, view: null,
  draft: { name: '', preset: '', hemisphere: 'n/a', curved: false, tip: null, entry: null },
  loaded: { meshes: null }, nudgeMm: 1, nudgeDeg: 1,
  exportOpts: { tsv: true, leads_file: true, transforms: true, mri: false, ct: false, labels: false, space: 'mri' },
  lastExport: null, replaceOnImport: false, showLeads: true, brainParams: null, brainExport: null,
};

// ---------- engine calls ----------
async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const P = (rest = '') => `/api/p/${encodeURIComponent(S.p.name)}${rest}`;
async function postRaw(path, blob) {
  const r = await fetch(path, { method: 'POST', body: blob });
  const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg, kind = 'info', ms = 4500) {
  const d = document.createElement('div');
  d.className = kind; d.textContent = msg; $('#toast').append(d);
  setTimeout(() => d.remove(), kind === 'error' ? 9000 : ms);
}
async function busy(text, fn) {
  $('#busyText').textContent = text; $('#busy').hidden = false;
  try { return await fn(); } catch (e) { toast(e.message, 'error'); return null; } finally { $('#busy').hidden = true; }
}
async function act(path, body, text) {
  const run = async () => { const p = await api(P(path), body); await setProject(p); return p; };
  return text ? busy(text, run) : run().catch((e) => { toast(e.message, 'error'); return null; });
}
const desktopApi = () => (S.state?.desktop && window.pywebview?.api ? window.pywebview.api : null);

// ---------- viewer ----------
const nv = new niivue.Niivue({
  backColor: [0, 0, 0, 1], crosshairColor: [0.25, 0.85, 1, 0.75], isRadiologicalConvention: false,
  show3Dcrosshair: true, isOrientationTextVisible: true, showAllOrientationMarkers: true, isOrientCube: true,
  dragAndDropEnabled: false, loadingText: '',
  meshThicknessOn2D: 0.5,  // 2D views show where a surface crosses the slice, not the whole surface over the scan
});
nv.setMouseEventConfig({ leftButton: { primary: 8, withShift: 3 }, centerButton: 3 });  // click: crosshair; Shift+drag, middle drag: pan (8, 3 = Niivue's crosshair, pan)
let nvReady = nv.attachToCanvas($('#gl'));
nv.onLocationChange = () => { updateCoords(); syncSliders(); };

function crosshairMM() {
  if (!nv.volumes.length) return null;
  const m = nv.frac2mm(nv.scene.crosshairPos, 0, true);
  return [m[0], m[1], m[2]];
}
function updateCoords() {
  const m = crosshairMM();
  const space = S.step === 'orient' ? `${S.orientRole.toUpperCase()} (corrected)` : 'MRI';
  let t = m ? `${space}  x ${fmt(m[0], 1)}  y ${fmt(m[1], 1)}  z ${fmt(m[2], 1)} mm` : '';
  const a = S.p?.acpc_matrix;
  if (m && a && S.step !== 'orient') {
    const q = [0, 1, 2].map((i) => a[i][0] * m[0] + a[i][1] * m[1] + a[i][2] * m[2] + a[i][3]);
    t += `   AC/PC  x ${fmt(q[0], 1)}  y ${fmt(q[1], 1)}  z ${fmt(q[2], 1)}`;
  }
  const fd = !modOn('stereotaxy') ? null : S.step === 'frame' && S.p ? S.p.frame?.scans?.[frameScan()]?.display_to_frame : S.p?.frame_from_display;
  if (m && fd && S.step !== 'orient') { const q = xf(fd, m); t += `   frame  X ${fmt(q[0], 1)}  Y ${fmt(q[1], 1)}  Z ${fmt(q[2], 1)}`; }
  $('#coords').textContent = t;
}
function goTo(mm) {
  if (!nv.volumes.length) return;
  nv.scene.crosshairPos = nv.mm2frac(mm, 0, true);  // world mm, as crosshairMM() reads them (differs for oblique scans)
  nv.drawScene(); nv.createOnLocationChange?.(); updateCoords();
}
function setLayout(l) {
  S.layout = l;
  $$('#layout button').forEach((b) => b.classList.toggle('on', b.dataset.layout === l));
  nv.setSliceType({ multi: nv.sliceTypeMultiplanar, axial: nv.sliceTypeAxial, coronal: nv.sliceTypeCoronal,
    sagittal: nv.sliceTypeSagittal, render: nv.sliceTypeRender }[l]);
  if (S.step === 'brain') refreshViewer();  // 3D: surfaces only; 2D: the MRI
}
// CT windows come from the engine (Hounsfield units, HU with an offset, or percentiles of an unknown scale)
const ctLevels = () => S.p?.scans.ct?.levels || { orient: [-200, 1500], base_bone: [-200, 2000], base_metal: [1000, 4000],
  overlay_bone: [300, 2000], overlay_metal: [1500, 4000] };

function scanKey(role) {
  const s = S.p.scans[role];
  return s && hkey([role, s.sha256, s.orient, role === 'ct' ? S.p.coreg?.ct_to_mri : null]);
}
function wantedVolumes() {
  const p = S.p, out = [];
  if (!p) return out;
  if (S.step === 'orient') {
    const r = S.orientRole;
    if (p.scans[r]) out.push({ role: r, colormap: 'gray', ...(r === 'ct' ? { cal_min: ctLevels().orient[0], cal_max: ctLevels().orient[1] } : {}) });
    return out;
  }
  if (S.step === 'frame') {  // the scan the localizer is found in, alone
    const r = frameScan();
    if (!r) return [];
    const w = ctLevels().base_bone;
    return [r === 'ct' ? { role: 'ct', colormap: 'gray', cal_min: w[0], cal_max: w[1] } : { role: 'mri', colormap: 'gray' }];
  }
  if (S.step === 'brain' && S.layout === 'render' && p.brain) return [];  // 3D: brain and lead surfaces only
  if (S.step === 'acpc' || S.step === 'brain') return p.scans.mri ? [{ role: 'mri', colormap: 'gray' }] : [];  // MRI alone, full resolution
  const fused = p.scans.mri && p.scans.ct;  // before coregistration the CT is placed by its header
  const overlay = S.ctVisible ? S.ctOpacity : 0;
  if (fused && S.base === 'ct') {  // CT at its own resolution shows thin leads; MRI blended on top
    const [lo, hi] = ctLevels()[`base_${S.ctWindow}`];
    return [{ role: 'ct', colormap: 'gray', cal_min: lo, cal_max: hi }, { role: 'mri', colormap: 'gray', opacity: overlay }];
  }
  if (p.scans.mri) out.push({ role: 'mri', colormap: 'gray' });
  if (fused) {
    const [lo, hi] = ctLevels()[`overlay_${S.ctWindow}`];
    out.push({ role: 'ct', colormap: 'warm', cal_min: lo, cal_max: hi, opacity: overlay });
  } else if (p.scans.ct && !p.scans.mri) out.push({ role: 'ct', colormap: 'gray', cal_min: ctLevels().orient[0], cal_max: ctLevels().orient[1] });
  return out;
}
function volSpec(v) {
  const full = v.role === 'ct' && S.step === 'frame';  // the frame step needs the uncropped CT
  const k = S.p.display?.[v.role]?.[full ? 'full' : 'view'] || scanKey(v.role);  // = the file's content: unchanged files are reused
  return { url: `${P(`/vol/${v.role}.nii`)}?v=${k}${full ? '&full=1' : ''}`, name: `${v.role}-${full ? 'full-' : ''}${k}.nii`, colormap: v.colormap,
    opacity: v.opacity ?? 1, ...(v.cal_min !== undefined ? { cal_min: v.cal_min, cal_max: v.cal_max } : {}),
    xform: v.role === 'ct' && S.p.coreg ? S.p.coreg.ct_to_mri : null };  // CT -> MRI, applied in the viewer
}
const mat4mul = (a, b) => a.map((r) => [0, 1, 2, 3].map((j) => r.reduce((t, x, k) => t + x * b[k][j], 0)));
function contactsKey() {
  const rows = S.p.leads.flatMap((l) => l.contacts.map((c) => [c.x, c.y, c.z]));
  return rows.length && S.p.scans.mri && S.p.coreg && S.step !== 'orient' && S.step !== 'import' ? hkey([rows, scanKey('mri')]) : null;
}
function extraLayers() {  // label overlays on the MRI grid: contacts (leads, export) and the coregistration box
  const out = [];
  if (!S.p) return out;
  const ck = contactsKey();
  if (ck) {
    const cm = { R: [0], G: [0], B: [0], A: [0], I: [0], labels: [''] };
    S.p.leads.forEach((l, i) => {
      const h = leadColor(i);
      cm.R.push(parseInt(h.slice(1, 3), 16)); cm.G.push(parseInt(h.slice(3, 5), 16)); cm.B.push(parseInt(h.slice(5, 7), 16));
      cm.A.push(255); cm.I.push(i + 1); cm.labels.push(l.name);
    });
    out.push({ key: `c${ck}`, url: `${P('/vol/contacts.nii.gz')}?v=${ck}`, name: `contacts-${ck}.nii.gz`, cm });
  }
  const ap = S.p.acpc || {};
  if ((S.step === 'acpc' || S.step === 'leads') && S.p.scans.mri && (ap.ac || ap.pc || ap.msp)) {
    const ak = hkey([ap, scanKey('mri')]);
    out.push({ key: `a${ak}`, url: `${P('/vol/acpc.nii.gz')}?v=${ak}`, name: `acpc-${ak}.nii.gz`,
      cm: { R: [0, 255, 63, 62], G: [0, 93, 167, 207], B: [0, 93, 255, 142], A: [0, 255, 255, 255], I: [0, 1, 2, 3], labels: ['', 'AC', 'PC', 'midline'] } });
  }
  if (S.step === 'brain' && S.p.brain) {
    const bk = hkey([S.p.brain.when]);
    out.push({ key: `b${bk}`, url: `${P('/brain/brain_mask.nii.gz')}?v=${bk}`, name: `brainmask-${bk}.nii.gz`,
      opacity: 0.3, cm: { R: [0, 255], G: [0, 120], B: [0, 160], A: [0, 255], I: [0, 1], labels: ['', 'brain'] } });  // the MRI stays readable
  }
  const fs = S.step === 'frame' && frameScan() ? S.p.frame?.scans?.[frameScan()] : null;
  if (fs && (fs.display_to_frame || fs.auto?.n_lines)) {  // auto-found rods, the model where it is now, rod centres
    const fk = hkey([frameScan(), fs.display_to_frame, fs.when, fs.auto?.n_lines, fs.manual, fs.excluded]);
    out.push({ key: `f${fk}`, url: `${P('/vol/frame.nii.gz')}?scan=${frameScan()}&v=${fk}`, name: `frame-${fk}.nii.gz`,
      cm: { R: [0, 255, 60, 70, 255], G: [0, 210, 220, 230, 80], B: [0, 40, 255, 110, 220], A: [0, 255, 255, 255, 255], I: [0, 1, 2, 3, 4],
        labels: ['', 'auto-found rods', 'localizer model', 'rod centres', 'manual centres'] } });
  }
  if (S.step === 'coreg' && S.p.roi && S.p.scans.mri) {
    const rk = hkey([S.p.roi, scanKey('mri')]);
    out.push({ key: `r${rk}`, url: `${P('/vol/roi.nii.gz')}?v=${rk}`, name: `roi-${rk}.nii.gz`,
      cm: { R: [0, 80], G: [0, 230], B: [0, 120], A: [0, 255], I: [0, 1], labels: ['', 'region of interest'] } });
  }
  return out;
}
const VOLS = new Map();  // url -> loaded image (scans and overlays stay in memory: switching steps does not reload them)
const VOLS_KEEP = 10;
async function loadImage(s) {  // from the memory cache, or fetched once (several at a time)
  let img = VOLS.get(s.url);
  if (img) { VOLS.delete(s.url); VOLS.set(s.url, img); return img; }  // most recently used last
  img = await niivue.NVImage.loadFromUrl({ url: s.url, name: s.name, opacity: s.opacity ?? 1, ...(s.cm ? {} : { colormap: s.colormap ?? 'gray' }) });
  img._url = s.url; img._fresh = true;
  if (s.cm) img.setColormapLabel(s.cm);
  VOLS.set(s.url, img);
  return img;
}
async function syncLayers(specs) {  // show exactly `specs` (in order) plus the band preview; Niivue redraws once
  const imgs = await Promise.all(specs.map(loadImage));
  const band = bandLayer(imgs[0]);
  const list = band ? [...imgs, band] : imgs;
  if (list.length !== nv.volumes.length || list.some((v, i) => v !== nv.volumes[i])) {
    const keep = nv.volumes.length && imgs.length && nv.volumes[0] !== imgs[0] ? crosshairMM() : null;  // a new base: stay put
    nv.volumes = list; nv.back = list[0] || null; nv.overlays = list.slice(1);
    if (keep) nv.scene.crosshairPos = nv.mm2frac(keep, 0, true);
    for (const u of [...VOLS.keys()]) if (VOLS.size > VOLS_KEEP && !specs.some((s) => s.url === u)) VOLS.delete(u);
  }
  specs.forEach((s, i) => {  // display settings change without reloading anything
    const v = imgs[i];
    v.opacity = s.opacity ?? 1;
    if (s.cm) return;
    if ('xform' in s) {  // the CT file stays in its own world; the coregistration moves it here (no reload on a nudge)
      const xk = JSON.stringify(s.xform);
      if (v._xk !== xk) { v._fileAffine ??= v.getAffine(); v.setAffine(s.xform ? mat4mul(s.xform, v._fileAffine) : v._fileAffine); v._xk = xk; }
    }
    if (s.colormap && v.colormap !== s.colormap) v.colormap = s.colormap;
    if (s.cal_min !== undefined) { v.cal_min = s.cal_min; v.cal_max = s.cal_max; }
    else if (!v._fresh && v.robust_min !== undefined) { v.cal_min = v.robust_min; v.cal_max = v.robust_max; }
    v._fresh = false;
  });
  if (band) setBandRange(band, imgs[0]);
  if (nv.volumes.length) nv.updateGLVolume(); else nv.drawScene();
}
let refreshing = null, refreshAgain = false;
async function refreshViewer() {  // one refresh at a time; calls during a refresh are folded into one more
  if (refreshing) { refreshAgain = true; return refreshing; }
  refreshing = (async () => { do { refreshAgain = false; await refreshViewerOnce(); } while (refreshAgain); })();
  try { await refreshing; } finally { refreshing = null; }
}
async function refreshViewerOnce() {
  await nvReady;
  $('#empty').hidden = !!(S.p && (S.p.scans.mri || S.p.scans.ct));
  $('#empty').textContent = S.p ? 'Import an MRI and a CT to start.' : 'Create or open a project to start.';
  const vols = wantedVolumes();
  vols.forEach((v) => { if (S.win[v.role]) [v.cal_min, v.cal_max] = S.win[v.role]; });  // the user's own window
  const extras = vols.length ? extraLayers() : [];  // no overlays without a scan (3D surfaces)
  try {
    await syncLayers([...vols.map(volSpec), ...extras.map((x) => ({ url: x.url, name: x.name, opacity: x.opacity ?? 1, cm: x.cm }))]);
  } catch (e) { toast(`Viewer: ${e.message}`, 'error'); }
  await syncMeshes();
  $('#ctCtl').style.visibility = vols.length > 1 ? 'visible' : 'hidden';
  syncWindow(vols);
  syncSliders();
  updateCoords();
}
async function syncMeshes() {  // brain surface and lead meshes, shown in the brain step (3D view)
  const b = S.p?.brain, want = S.step === 'brain' && b ? hkey([b.when, S.showLeads]) : null;
  if (want === S.loaded.meshes) return;
  S.loaded.meshes = want;
  const list = [];
  if (want) {
    list.push({ url: `${P('/brain/brain.gii')}?v=${want}`, name: `brain-${want}.gii`, rgba255: [235, 200, 205, 255] });
    if (S.showLeads) b.leads.forEach((l) => {
      const c = [1, 3, 5].map((i) => parseInt(l.color.slice(i, i + 2), 16));
      list.push({ url: `${P(`/brain/lead-${l.file}-shaft.gii`)}?v=${want}`, name: `shaft-${l.file}-${want}.gii`, rgba255: [150, 160, 175, 255] });
      list.push({ url: `${P(`/brain/lead-${l.file}-contacts.gii`)}?v=${want}`, name: `contacts-${l.file}-${want}.gii`, rgba255: [...c, 255] });
    });
  }
  if (!list.length) { while (nv.meshes.length) nv.removeMesh(nv.meshes[0]); nv.drawScene(); return; }  // loadMeshes([]) keeps the old ones
  try { await nv.loadMeshes(list); } catch (e) { toast(`Viewer: ${e.message}`, 'error'); }
}
const BAND_CM = { R: [255, 255, 255, 255, 255, 255], G: [50, 50, 50, 50, 50, 50], B: [50, 50, 50, 50, 50, 50],
  A: [0, 0, 255, 255, 0, 0], I: [0, 84, 85, 170, 171, 255] };  // only the middle third is drawn
function bandLayer(base) {  // frame step: copy of the scan drawn only inside the intensity band (red), or null
  const s = S.step === 'frame' && S.fmShowBand !== false && base ? S.p?.frame?.scans?.[frameScan()] : null;
  if (!s || !(S.fmBand || s.band)) return null;
  if (!S.bandCm) { nv.addColormap('brainscope-band', BAND_CM); S.bandCm = true; }
  if (S.bandImg?._src !== base) { const v = base.clone(); v._band = true; v._src = base; v.colormap = 'brainscope-band'; S.bandImg = v; }
  return S.bandImg;
}
function setBandRange(v, base) {
  const s = S.p.frame.scans[frameScan()], b = S.fmBand || s.band;
  const lo = b[0], hi = b[1] ?? base.global_max, w = Math.max(hi - lo, 1e-3);
  v.cal_min = lo - w; v.cal_max = hi + w;  // the band is the middle third of the colour map
  v.opacity = 0.75;
  [['#fmLo', lo], ['#fmHi', hi]].forEach(([id, x]) => {  // slider range = the scan's range (known once it is loaded)
    const el = $(id);
    if (el && document.activeElement !== el) { el.min = Math.floor(base.global_min); el.max = Math.ceil(base.global_max); el.value = x; }
  });
}
function syncBand() {  // while dragging the band sliders: only the range changes
  const v = nv.volumes.find((x) => x._band);
  if (!v || !(S.step === 'frame' && S.fmShowBand !== false)) { refreshViewer(); return; }
  setBandRange(v, nv.volumes[0]);
  nv.updateGLVolume();
}
function syncSliders() {  // slice position sliders follow the crosshair
  const c = nv.volumes.length ? nv.scene.crosshairPos : null;
  [['#slX', 0], ['#slY', 1], ['#slZ', 2]].forEach(([id, i]) => { $(id).disabled = !c; if (c) $(id).value = c[i]; });
}
function syncWindow(vols) {
  const roles = vols.map((v) => v.role);
  $('#winCtl').style.visibility = roles.length ? 'visible' : 'hidden';
  if (!roles.length) return;
  if (!roles.includes(S.winRole)) S.winRole = roles[roles.length - 1];
  $('#winRole').innerHTML = roles.map((r) => `<option value="${r}" ${r === S.winRole ? 'selected' : ''}>${r.toUpperCase()}</option>`).join('');
  const vol = nv.volumes[roles.indexOf(S.winRole)];
  if (vol) { $('#winLo').value = +Number(vol.cal_min).toPrecision(4); $('#winHi').value = +Number(vol.cal_max).toPrecision(4); }
}
function setWindow() {
  const lo = Number($('#winLo').value), hi = Number($('#winHi').value);
  if (!(hi > lo)) { toast('The upper value must be larger than the lower one.', 'error'); return; }
  S.win[S.winRole] = [lo, hi]; refreshViewer();
}

// ---------- projects ----------
async function loadState() {
  S.state = await api('/api/state');
  const sel = $('#projectSelect');
  sel.innerHTML = '<option value="">— select a subject —</option>' + S.state.projects.map((n) => `<option>${esc(n)}</option>`).join('');
  if (S.p) sel.value = S.p.name;
}
async function openProject(name) {
  if (!name) { S.p = null; S.lead = null; render(); refreshViewer(); return; }
  await busy('Opening subject…', async () => { await setProject(await api(`/api/p/${encodeURIComponent(name)}`)); });
}
async function setProject(p) {
  const changed = S.p?.name !== p.name;
  S.p = p;
  if (!changed && !visibleSteps().some(([k]) => k === S.step)) S.step = 'coreg';  // its module was just hidden
  if (changed) { S.lead = null; S.contact = null; S.view = null; S.loaded = { meshes: null }; S.win = { mri: null, ct: null }; S.align = { ct: null, mri: null }; S.brainParams = null; S.brainExport = null; S.fmScan = null; S.fmBand = null; S.step = firstOpenStep(); }
  if (S.lead && !p.leads.some((l) => l.name === S.lead)) { S.lead = null; S.view = null; }
  $('#projectSelect').value = p.name;
  $('#projectFolder').textContent = p.folder; $('#projectFolder').title = p.folder;
  $('#undo').disabled = !p.can_undo; $('#redo').disabled = !p.can_redo;
  if (p.warnings?.length) p.warnings.forEach((w) => toast(w, 'info', 9000));
  render();
  await refreshViewer();
  if (S.step === 'leads' && S.lead) await loadLeadView();
}
function firstOpenStep() {
  const p = S.p;
  if (!p.scans.mri || !p.scans.ct) return 'import';
  if (!p.coreg) return 'orient';
  return p.leads.length && modOn('electrodes') ? 'leads' : 'coreg';
}
async function newProject() {
  const name = prompt('Name of the new subject (letters, digits, space, . _ -):');
  if (!name) return;
  await busy('Creating subject…', async () => { const p = await api('/api/projects', { name }); await loadState(); await setProject(p); });
}
async function demoProject() {
  await busy('Building the synthetic demo case (about 10 s)…', async () => {
    const p = await api('/api/demo', {}); await loadState(); await setProject(p);
    toast('Demo: fix the orientation of both scans first (Orient step). The CT marker is on the subject\'s left.', 'info', 12000);
  });
}

// ---------- rendering ----------
// The subject steps feed independent task modules, each shown or hidden per subject (PLAN D29, D40).
const SECTIONS = [
  { key: 'subject', name: 'Subject', steps: [['import', 'Import'], ['orient', 'Orient'], ['acpc', 'AC/PC'], ['coreg', 'Coregister']] },
  { key: 'electrodes', name: 'Electrodes', steps: [['leads', 'Leads'], ['export', 'Export']] },
  { key: 'stereotaxy', name: 'Stereotaxy', steps: [['frame', 'Frame'], ['targets', 'Plans']] },
  { key: 'brain', name: 'Brain model', steps: [['brain', 'Brain']] },
];
const modOn = (k) => k === 'subject' || !S.p || (S.p.modules || []).includes(k);
const visibleSteps = () => SECTIONS.filter((s) => modOn(s.key)).flatMap((s) => s.steps);
function nextStep() {
  const v = visibleSteps(), i = v.findIndex(([k]) => k === S.step);
  return i >= 0 && i + 1 < v.length ? v[i + 1] : null;
}
function nextHTML(disabled = false, primary = true) {
  const n = nextStep();
  return n ? `<button class="${primary ? 'primary' : ''}" id="next" ${disabled ? 'disabled' : ''}>Next: ${n[1]}</button>` : '';
}
function bindNext() { if ($('#next')) $('#next').onclick = () => goStep(nextStep()[0]); }
function renderNav() {
  let n = 0;
  $('#steps').innerHTML = SECTIONS.filter((s) => modOn(s.key)).map((s) => `<div class="navsec">${s.name}</div>${s.steps.map(([k, t]) =>
    `<button data-step="${k}" class="${k === S.step ? 'on' : ''}" ${S.p ? '' : 'disabled'}><b>${++n}</b><span>${t}</span><i class="${stepStatus(k)}"></i></button>`).join('')}`).join('')
    + (S.p ? `<div class="navsec">Modules</div><div class="navmods">${SECTIONS.slice(1).map((s) =>
      `<label class="row small"><input type="checkbox" data-mod="${s.key}" ${modOn(s.key) ? 'checked' : ''}> ${s.name}</label>`).join('')}</div>` : '');
  $$('#steps button').forEach((b) => { b.onclick = () => goStep(b.dataset.step); });
  $$('[data-mod]').forEach((el) => { el.onchange = () => act('/modules', { modules: $$('[data-mod]').filter((x) => x.checked).map((x) => x.dataset.mod) }); });
}
function render() {
  renderNav();
  const panel = $('#panel');
  if (!S.p) { panel.innerHTML = welcomeHTML(); bindWelcome(); $('#inspector').hidden = true; return; }
  panel.innerHTML = { import: importHTML, orient: orientHTML, coreg: coregHTML, acpc: acpcHTML, frame: frameHTML, leads: leadsHTML,
    targets: targetsHTML, export: exportHTML, brain: brainHTML }[S.step]();
  ({ import: bindImport, orient: bindOrient, coreg: bindCoreg, acpc: bindAcpc, frame: bindFrame, leads: bindLeads, targets: bindTargets,
    export: bindExport, brain: bindBrain })[S.step]();
  $('#inspector').hidden = !(S.step === 'leads' && S.lead);
}
function stepStatus(step) {
  const p = S.p;
  if (!p) return 'todo';
  const sc = p.scans;
  switch (step) {
    case 'import': return sc.mri && sc.ct ? 'done' : 'todo';
    case 'orient': return sc.mri?.left_confirmed && sc.ct?.left_confirmed ? 'done' : (sc.mri || sc.ct ? 'warn' : 'todo');
    case 'coreg': return p.coreg?.accepted ? 'done' : (p.coreg ? 'warn' : 'todo');
    case 'acpc': return p.acpc_matrix ? 'done' : (p.acpc ? 'warn' : 'todo');
    case 'frame': return p.frame?.use ? (p.frame.model.verified ? 'done' : 'warn') : 'todo';
    case 'targets': return p.leads.some((l) => l.plan) ? 'done' : 'todo';
    case 'brain': return p.brain ? 'done' : 'todo';
    case 'leads': return p.leads.length ? 'done' : 'todo';
    default: return 'todo';
  }
}
function welcomeHTML() {
  const list = S.state?.projects || [];
  return `<h2>Welcome</h2>
  <p>brainscope coregisters a CT to an MRI and finds the contacts of intracranial leads. Coordinates are reported in the
  orientation-corrected MRI space. <b>Research use only.</b></p>
  <p class="small">Each subject (patient or animal) has shared steps (import, orientation, AC/PC, coregistration) and independent
  modules built on them, each shown or hidden per subject: <b>Electrodes</b> (lead contacts, export), <b>Stereotaxy</b> (frame
  from the localizer, planned versus delivered) and <b>Brain model</b> (figures, 3D export).</p>
  <div class="row"><button class="primary" id="wNew">New subject</button><button id="wDemo">Open a demo case</button></div>
  <h3>Subjects</h3>${list.length ? list.map((n) => `<div class="leadItem" data-open="${esc(n)}">${esc(n)}</div>`).join('') : '<p class="muted">No subjects yet.</p>'}
  <p class="muted small">Subjects are stored in ${esc(S.state?.projects_root || '')}</p>`;
}
function bindWelcome() {
  $('#wNew').onclick = newProject; $('#wDemo').onclick = demoProject;
  $$('[data-open]').forEach((el) => { el.onclick = () => openProject(el.dataset.open); });
}

// ---------- step 1: import ----------
function scanCard(role) {
  const s = S.p.scans[role];
  const title = role === 'mri' ? 'MRI' : 'CT';
  const info = s ? `<div class="kv"><span>File</span><span>${esc(s.source_name)}</span><span>Size</span><span>${s.shape.join(' × ')}</span>
    <span>Voxel (mm)</span><span>${s.spacing.map((x) => fmt(x, 2)).join(' × ')}</span></div>` : '<p class="muted">Not imported yet.</p>';
  return `<div class="card"><div class="row"><b>${title}</b><span class="spacer"></span>${s ? '<span class="badge ok">imported</span>' : ''}</div>${info}
    <div class="row"><button data-nifti="${role}" title="NIfTI (.nii, .nii.gz), MGZ/MGH, NRRD, MHA/MHD">Image file…</button><button data-dicom="${role}">DICOM folder…</button></div>
    ${role === 'ct' && s?.levels && s.levels.kind !== 'hu' ? `<p class="muted small">CT values are not in Hounsfield units (${s.levels.kind === 'hu+offset' ? `offset ${fmt(s.levels.offset, 0)}` : 'unknown scale'}); display and registration windows were adapted.</p>` : ''}
    ${role === 'ct' && s && S.p.leads.length ? '<p class="muted small">Importing another CT removes the leads of this project.</p>' : ''}</div>`;
}
function importHTML() {
  return `<h2>Import scans</h2>
  <p>Import the MRI (the output space) and the post-implant CT, as NIfTI files or DICOM folders. Files are copied into the project; originals are never changed.</p>
  ${scanCard('mri')}${scanCard('ct')}
  <h3>Species</h3><div class="row"><select id="species">${['human', 'pig', 'other'].map((s) => `<option ${S.p.species === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
  <span class="muted small">Sets the registration smoothing.</span></div>
  <div class="row"><span class="spacer"></span>${nextHTML(!(S.p.scans.mri && S.p.scans.ct))}</div>
  <div id="seriesPick"></div>`;
}
function bindImport() {
  $$('[data-nifti]').forEach((b) => { b.onclick = () => importNifti(b.dataset.nifti); });
  $$('[data-dicom]').forEach((b) => { b.onclick = () => importDicom(b.dataset.dicom); });
  $('#species').onchange = (e) => act('/species', { species: e.target.value });
  bindNext();
}
async function importNifti(role) {
  const d = desktopApi();
  if (d) {
    const path = await d.pick_nifti();
    if (path) await act('/import', { role, path }, `Importing ${role.toUpperCase()}…`);
    return;
  }
  const inp = $('#niftiInput');
  inp.onchange = async () => {
    const f = inp.files[0]; inp.value = '';
    if (!f) return;
    await busy(`Uploading ${f.name}…`, async () => setProject(await postRaw(P(`/upload?role=${role}&filename=${encodeURIComponent(f.name)}`), f)));
  };
  inp.click();
}
async function importDicom(role) {
  const d = desktopApi();
  let res = null;
  if (d) {
    const path = await d.pick_folder();
    if (!path) return;
    res = await busy('Converting DICOM…', () => api(P('/dicom'), { path }));
  } else {
    const inp = $('#dicomInput');
    const files = await new Promise((ok) => { inp.onchange = () => ok([...inp.files]); inp.click(); });
    inp.value = '';
    if (!files.length) return;
    const run = Math.random().toString(36).slice(2, 12);
    res = await busy(`Uploading ${files.length} DICOM files…`, async () => {
      for (let i = 0; i < files.length; i++) {
        $('#busyText').textContent = `Uploading DICOM files… ${i + 1} / ${files.length}`;
        await postRaw(P(`/dicom-file?run=${run}&rel=${encodeURIComponent(files[i].webkitRelativePath || files[i].name)}`), files[i]);
      }
      $('#busyText').textContent = 'Converting DICOM…';
      return api(P('/dicom'), { run });
    });
  }
  if (!res) return;
  if (res.series.length === 1) { await useSeries(res.run, res.series[0].file, role); return; }
  $('#seriesPick').innerHTML = `<div class="card"><b>Choose the ${role.toUpperCase()} series</b>
    <table class="contacts"><tr><th>#</th><th>Description</th><th>Size</th><th>Voxel</th></tr>
    ${res.series.map((s) => `<tr data-file="${esc(s.file)}"><td>${esc(s.series_number ?? '')}</td><td>${esc(s.description)} <span class="muted">${esc(s.modality)}</span></td>
    <td>${s.shape.join('×')}</td><td>${s.spacing.map((x) => fmt(x, 2)).join('×')}</td></tr>`).join('')}</table></div>`;
  $$('#seriesPick tr[data-file]').forEach((tr) => { tr.onclick = () => useSeries(res.run, tr.dataset.file, role); });
}
const useSeries = (run, file, role) => act('/dicom-use', { run, file, role }, 'Importing series…');

// ---------- step 2: orient ----------
function orientHTML() {
  const r = S.orientRole, s = S.p.scans[r];
  const ops = [['rx', 'sagittal'], ['ry', 'coronal'], ['rz', 'axial']];
  return `<h2>Orientation</h2>
  <div class="seg row">${['mri', 'ct'].map((x) => `<button data-role="${x}" class="${x === r ? 'on' : ''}" ${S.p.scans[x] ? '' : 'disabled'}>${x.toUpperCase()}
    ${S.p.scans[x]?.left_confirmed ? '✓' : ''}</button>`).join('')}</div>
  ${s ? `<p>Make the ${r.toUpperCase()} anatomically upright: <b>top</b> of the head up in the coronal and sagittal views, <b>front</b> (nose) up in the axial view,
  and the subject's <b>left on the left</b> of the screen (neurological view). Only the file header changes; voxels are never resampled.</p>
  ${orientGuideHTML()}
  <h3>Rotate 90° in a view</h3>
  ${ops.map(([op, view]) => `<div class="row"><label>${view}</label><button data-op="${op}+">↺ rotate</button><button data-op="${op}-">↻ rotate</button></div>`).join('')}
  <h3>Mirror</h3>
  <div class="grid3"><button data-op="fx">left ↔ right</button><button data-op="fy">front ↔ back</button><button data-op="fz">up ↔ down</button></div>
  <div class="row"><button data-reset="1" class="ghost">Reset to file header</button><span class="spacer"></span><span class="muted small">axes: <b class="mono">${esc(s.axes)}</b></span></div>
  <h3>Which side is left</h3>
  <div class="card"><label class="row"><input type="checkbox" id="leftOk" ${s.left_confirmed ? 'checked' : ''}> The subject's left is on the screen left in this scan</label>
  <div class="row"><input type="text" id="leftNote" placeholder="how you know (e.g. CT marker)" value="${esc(s.left_note)}" style="flex:1"></div>
  <p class="muted small">A left–right mirror cannot be detected from the images. Rotating or mirroring clears this confirmation.</p></div>
  <div class="row"><span class="spacer"></span>${nextHTML()}</div>` : '<p class="muted">Import this scan first.</p>'}`;
}
function orientGuideHTML() {  // how each view should look once the scan is oriented (for pigs: up = dorsal, front = snout)
  const t = (x, y, s) => `<text x="${x}" y="${y}" font-size="9" fill="#8b93a1" text-anchor="middle">${s}</text>`;
  const fig = (title, body, labels) => `<figure style="margin:0;text-align:center"><svg width="86" height="78" viewBox="0 0 86 78">
    <g stroke="#d6dbe4" stroke-width="1.5" fill="#2b3240">${body}</g>${labels}</svg><figcaption class="muted small">${title}</figcaption></figure>`;
  const lab = (top, left, right, bottom) => t(43, 9, top) + t(6, 42, left) + t(80, 42, right) + t(43, 76, bottom);
  return `<div class="card" style="display:flex;justify-content:space-between;padding:6px 8px">
    ${fig('axial', '<ellipse cx="43" cy="42" rx="22" ry="26"/><path d="M37 17 L43 7 L49 17" fill="#d6dbe4"/>', lab('A', 'L', 'R', 'P'))}
    ${fig('coronal', '<ellipse cx="43" cy="40" rx="22" ry="27"/><circle cx="35" cy="38" r="3" fill="#d6dbe4"/><circle cx="51" cy="38" r="3" fill="#d6dbe4"/>', lab('S', 'L', 'R', 'I'))}
    ${fig('sagittal', '<ellipse cx="40" cy="40" rx="24" ry="27"/><path d="M63 36 L74 46 L62 50" fill="#d6dbe4"/>', lab('S', 'P', 'A', 'I'))}
  </div>`;
}
function bindOrient() {
  $$('[data-role]').forEach((b) => { b.onclick = () => { S.orientRole = b.dataset.role; render(); refreshViewer(); }; });
  $$('[data-op]').forEach((b) => { b.onclick = () => act('/orient', { role: S.orientRole, op: b.dataset.op }); });
  $$('[data-reset]').forEach((b) => { b.onclick = () => act('/orient', { role: S.orientRole, reset: true }); });
  const ok = $('#leftOk');
  if (ok) {
    ok.onchange = () => act('/orient', { role: S.orientRole, left_confirmed: ok.checked, left_note: $('#leftNote').value });
    $('#leftNote').onchange = () => act('/orient', { role: S.orientRole, left_note: $('#leftNote').value });
    $('#next').onclick = () => {
      const other = S.orientRole === 'mri' ? 'ct' : 'mri';
      if (!S.p.scans[S.orientRole].left_confirmed) toast(`Left side of the ${S.orientRole.toUpperCase()} is not confirmed yet.`);
      if (S.p.scans[other] && !S.p.scans[other].left_confirmed) { S.orientRole = other; render(); refreshViewer(); return; }
      goStep(nextStep()[0]);
    };
  }
}

// ---------- step 3: coregister ----------
function coregHTML() {
  const c = S.p.coreg, ready = S.p.scans.mri && S.p.scans.ct, al = S.align;
  const axes = [['x', 'left–right'], ['y', 'back–front'], ['z', 'down–up']];
  const pt = (v) => (v ? v.map((x) => fmt(x, 1)).join(', ') : '<span class="muted">not set</span>');
  return `<h2>Coregister CT to MRI</h2>
  ${ready ? '' : '<p class="muted">Import both scans first.</p>'}
  <div class="card"><b>1. Rough alignment</b> <span class="muted small">(needed when the scanners' coordinates differ or the CT covers more than the head)</span>
    <p class="small">Show the CT, put the crosshair on a landmark (e.g. the middle of the brain, or a marker) and press <i>CT point</i>.
    Show the MRI, put the crosshair on the same landmark and press <i>MRI point</i>. Then move the CT onto the MRI.</p>
    <div class="row"><button id="showCt">Show CT</button><button id="ctPoint">CT point</button><span class="mono small">${pt(al.ct)}</span></div>
    <div class="row"><button id="showMri">Show MRI</button><button id="mriPoint">MRI point</button><span class="mono small">${pt(al.mri)}</span></div>
    <div class="row"><button id="moveCt" ${al.ct && al.mri ? '' : 'disabled'}>Move CT onto MRI</button>
      <span class="muted small">Tip: <i>Center</i> (above the images) brings the crosshair back into the image.</span></div>
  </div>
  <div class="card"><b>2. Automatic</b> <span class="muted small">(rigid, mutual information; starts from the current alignment)</span>
    <p class="small"><b>Region of interest</b> (recommended): only the MRI inside the green box is compared, so the body, the
    neck and loose soft tissue do not pull the alignment. Put the crosshair in the middle of the brain (MRI shown) and press <i>Box here</i>.</p>
    <div class="row"><button id="roiHere">Box here</button>
      ${['x', 'y', 'z'].map((a, i) => `<input type="number" data-roi="${i}" value="${roiSize()[i]}" min="10" max="400" title="${['left–right', 'back–front', 'down–up'][i]} size (mm)">`).join(' × ')} mm
      <button id="roiClear" class="ghost" ${S.p.roi ? '' : 'disabled'}>Clear</button></div>
    <p class="muted small">${S.p.roi ? `Box at ${S.p.roi.center.map((x) => fmt(x, 0)).join(', ')} mm is used.` : 'No box: the whole MRI is used.'}</p>
    <div class="row"><button class="primary" id="runCoreg" ${ready ? '' : 'disabled'}>${c ? 'Refine from current alignment' : 'Run coregistration'}</button>
    <button id="resetCoreg" class="ghost" ${c ? '' : 'disabled'}>Start over</button></div>
  </div>
  ${c ? `<div class="card"><div class="kv"><span>Method</span><span>${esc(c.method)}</span><span>Metric</span><span>${c.metric ?? '–'}</span>
     <span>Status</span><span>${c.accepted ? '<span class="badge ok">accepted</span>' : '<span class="badge warn">not accepted</span>'}</span></div></div>` : ''}
  <h3>3. Check and adjust by hand</h3>
  <p class="small">Check with the CT overlay (opacity slider above, <kbd>O</kbd> toggles).</p>
  <div class="row"><label>step</label><select id="nMm">${[0.25, 0.5, 1, 2, 5].map((v) => `<option ${v === S.nudgeMm ? 'selected' : ''}>${v}</option>`).join('')}</select> mm
    <select id="nDeg">${[0.25, 0.5, 1, 2, 5].map((v) => `<option ${v === S.nudgeDeg ? 'selected' : ''}>${v}</option>`).join('')}</select> °</div>
  ${axes.map(([a, n], i) => `<div class="row"><label>${n}</label><button data-t="${i}" data-s="-1">−</button><button data-t="${i}" data-s="1">+</button>
    <span class="muted small" style="margin-left:6px">rotate</span><button data-r="${i}" data-s="-1">↺</button><button data-r="${i}" data-s="1">↻</button></div>`).join('')}
  <div class="row"><span class="spacer"></span>${c ? `<button id="accept" class="${c.accepted ? '' : 'primary'}">${c.accepted ? 'Accepted ✓' : 'Accept alignment'}</button>` : ''}
  ${nextHTML(!c, false)}</div>`;
}
function bindCoreg() {
  $('#showCt').onclick = () => setBase('ct');
  $('#showMri').onclick = () => setBase('mri');
  $('#ctPoint').onclick = () => { S.align.ct = crosshairMM(); render(); };
  $('#mriPoint').onclick = () => { S.align.mri = crosshairMM(); render(); };
  $('#moveCt').onclick = async () => {
    const t = S.align.mri.map((x, i) => x - S.align.ct[i]);
    S.align = { ct: null, mri: null };
    if (await act('/nudge', { trans: t }, 'Moving the CT…')) { setBase('mri'); goTo(S.p.scans.mri.center); }
  };
  const sizes = () => $$('[data-roi]').map((el) => Number(el.value));
  $('#roiHere').onclick = () => { const m = crosshairMM(); if (m) act('/roi', { center: m, size: sizes() }); };
  $$('[data-roi]').forEach((el) => { el.onchange = () => { if (S.p.roi) act('/roi', { center: S.p.roi.center, size: sizes() }); }; });
  $('#roiClear').onclick = () => act('/roi', { clear: true });
  $('#runCoreg').onclick = () => act('/coreg', { from_current: !!S.p.coreg }, 'Coregistering (usually under a minute)…');
  $('#resetCoreg').onclick = () => act('/coreg-reset', {});
  $('#nMm').onchange = (e) => { S.nudgeMm = Number(e.target.value); };
  $('#nDeg').onchange = (e) => { S.nudgeDeg = Number(e.target.value); };
  $$('[data-t]').forEach((b) => { b.onclick = () => { const t = [0, 0, 0]; t[b.dataset.t] = S.nudgeMm * b.dataset.s; act('/nudge', { trans: t }); }; });
  $$('[data-r]').forEach((b) => { b.onclick = () => { const r = [0, 0, 0]; r[b.dataset.r] = S.nudgeDeg * b.dataset.s; act('/nudge', { rot: r }); }; });
  if ($('#accept')) $('#accept').onclick = () => act('/coreg-accept', { accepted: !S.p.coreg.accepted });
  bindNext();
}
function roiSize() {  // edge lengths (mm) of the box: the saved one, or a default for the species
  return S.p.roi?.size.map((x) => Math.round(x)) || ({ human: [150, 180, 140], pig: [80, 100, 70] }[S.p.species] || [110, 130, 100]);
}
function zoom(factor) {  // 2D zoom around the crosshair (Niivue's own formula); factor 0 = fit
  if (!nv.volumes.length) return;
  if (!factor) { nv.setPan2Dxyzmm([0, 0, 0, 1]); return; }
  const p = [...nv.scene.pan2Dxyzmm], c = nv.frac2mm(nv.scene.crosshairPos);  // Niivue's slice space, not crosshairMM(): they differ for oblique scans
  const z = Math.min(20, Math.max(0.5, Math.round(p[3] * factor * 10) / 10)), e = p[3] - z;
  nv.setPan2Dxyzmm([p[0] + e * c[0], p[1] + e * c[1], p[2] + e * c[2], z]);
}
function setBase(b) {
  S.base = b;
  $$('#base button').forEach((x) => x.classList.toggle('on', x.dataset.base === b));
  refreshViewer();
}
function centerView() {
  if (!nv.volumes.length) return;
  nv.scene.crosshairPos = [0.5, 0.5, 0.5];
  nv.scene.pan2Dxyzmm = [0, 0, 0, nv.scene.pan2Dxyzmm[3]];  // also bring a panned view back (keeps the zoom)
  nv.drawScene(); nv.createOnLocationChange?.(); updateCoords(); syncSliders();
}

// ---------- step 4: leads ----------
function nextLeadName() {
  const used = new Set(S.p.leads.map((l) => l.name));
  for (const a of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') if (!used.has(a)) return a;
  return `L${S.p.leads.length + 1}`;
}
const leadColor = (i) => LEAD_COLORS[i % LEAD_COLORS.length];
function leadsHTML() {
  if (!S.p.coreg) return '<h2>Leads</h2><p class="muted">Coregister the CT to the MRI first.</p>';
  const d = S.draft;
  if (!d.name) d.name = nextLeadName();
  if (!d.preset && S.presets.length) d.preset = S.presets[0].id;
  const pt = (v) => (v ? v.map((x) => fmt(x, 1)).join(', ') : '<span class="muted">not set</span>');
  const sel = S.p.leads.find((l) => l.name === S.lead);
  return `<h2>Leads</h2>
  <div class="card"><b>New lead</b>
    <div class="row"><label>Name</label><input type="text" id="dName" value="${esc(d.name)}" style="width:80px">
      <select id="dHemi">${['n/a', 'L', 'R'].map((h) => `<option ${d.hemisphere === h ? 'selected' : ''}>${h}</option>`).join('')}</select>
      <label class="muted small"><input type="checkbox" id="dCurved" ${d.curved ? 'checked' : ''}> curved</label></div>
    <div class="row"><label>Preset</label><select id="dPreset" style="flex:1">${S.presets.map((p) => `<option value="${esc(p.id)}" ${p.id === d.preset ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
      <button id="editPresets" title="Add or edit lead presets">Presets…</button></div>
    <div class="row"><label>Tip</label><span class="mono small" style="flex:1">${pt(d.tip)}</span><button id="setTip">Set at crosshair <kbd>T</kbd></button></div>
    <div class="row"><label>Entry</label><span class="mono small" style="flex:1">${pt(d.entry)}</span><button id="setEntry">Set at crosshair <kbd>E</kbd></button></div>
    <p class="muted small">Tip = deepest end of the lead. Entry = any point further along the lead (e.g. where it enters the brain).</p>
    <div class="row"><span class="spacer"></span><button class="primary" id="detect" ${d.tip && d.entry ? '' : 'disabled'}>Detect contacts</button></div>
  </div>
  <h3>Leads (${S.p.leads.length})</h3>
  ${S.p.leads.map((l, i) => `<div class="leadItem ${l.name === S.lead ? 'sel' : ''}" data-lead="${esc(l.name)}">
    <span class="sw" style="background:${leadColor(i)}"></span><b>${esc(l.name)}</b><span class="muted small" style="flex:1">${esc(l.preset.name)}</span>
    <span class="small">${l.contacts.length} · r ${fmt(l.fit?.r, 2)}</span></div>`).join('') || '<p class="muted small">None yet.</p>'}
  ${sel ? leadDetailHTML(sel) : ''}`;
}
function leadDetailHTML(l) {
  const c = S.contact;
  return `<h3>Lead ${esc(l.name)}</h3>
  <div class="row"><button id="lRedetect" title="Track the lead and fit the contacts again">Re-detect</button>
    <button id="lRefit" title="Fit the contacts again on the current axis">Re-fit</button>
    <button id="lShiftM">shift −0.25</button><button id="lShiftP">shift +0.25</button></div>
  <div class="row"><button id="lRename">Rename</button><select id="lHemi">${['n/a', 'L', 'R'].map((h) => `<option ${l.hemisphere === h ? 'selected' : ''}>${h}</option>`).join('')}</select>
    <button id="lPreset">Use selected preset</button><span class="spacer"></span><button id="lDelete" class="danger">Delete</button></div>
  ${l.fit ? `<p class="muted small">Fit: r ${fmt(l.fit.r, 3)}, blur σ ${fmt(l.fit.sigma_mm, 2)} mm, stretch ${fmt(l.fit.stretch, 2)},
    contacts ${l.fit.resolved ? 'separable on the CT' : 'not separable: placed from the preset geometry'}.</p>` : ''}
  <table class="contacts"><tr><th>Contact</th><th>s mm</th><th>mode</th><th>conf.</th><th>x</th><th>y</th><th>z</th></tr>
  ${l.contacts.map((r) => `<tr data-ci="${r.contact_index}" class="${c === r.contact_index ? 'sel' : ''}"><td>${esc(r.name)}</td><td>${fmt(r.s, 2)}</td>
    <td class="mode-${r.fit_mode}">${r.fit_mode}</td><td>${fmt(r.confidence, 2)}</td><td>${fmt(r.x, 1)}</td><td>${fmt(r.y, 1)}</td><td>${fmt(r.z, 1)}</td></tr>`).join('')}</table>
  ${c !== null && c !== undefined ? `<div class="row"><span class="muted small">Contact ${esc(l.contacts[c]?.name)}:</span>
    <button data-mv="-0.5">−0.5</button><button data-mv="-0.1">−0.1</button><button data-mv="0.1">+0.1</button><button data-mv="0.5">+0.5</button>
    <button id="anchor" title="Keep this contact here and fit the others to it">Fit others to it</button></div>` : ''}
`;
}
function planHTML(l) {  // planned target versus the delivered lead
  const pl = l.plan || { target: ['', '', ''], space: S.p.acpc_matrix ? 'acpc' : 'mri', point: 'tip', entry: null };
  const r = l.plan_result;
  const xyz = (id, v) => ['x', 'y', 'z'].map((a, i) => `<input type="number" step="any" id="${id}${i}" value="${v ? v[i] : ''}" placeholder="${a}" style="width:64px">`).join('');
  return `<h3>Planned target</h3><div class="card">
    <div class="row"><label>space</label><select id="plSpace"><option value="acpc" ${pl.space === 'acpc' ? 'selected' : ''}>AC/PC</option>
      <option value="frame" ${pl.space === 'frame' ? 'selected' : ''}>frame</option>
      <option value="mri" ${pl.space === 'mri' ? 'selected' : ''}>MRI</option></select>
      <label style="min-width:0">compare</label><select id="plPoint"><option value="tip">lead tip</option>
      ${l.contacts.map((c) => `<option value="c${c.contact_index}" ${pl.point === `c${c.contact_index}` ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></div>
    <div class="row"><label>target</label>${xyz('plT', l.plan?.target)}</div>
    <div class="row"><label>entry</label>${xyz('plE', l.plan?.entry)}<span class="muted small">optional</span></div>
    <div class="row"><button id="plSave">Save plan</button><button id="plClear" class="ghost" ${l.plan ? '' : 'disabled'}>Clear</button>
      <button id="plFromCross" class="ghost" title="Fill the target with the crosshair position">target = crosshair</button></div>
    ${r ? (r.error ? `<p class="small" style="color:var(--bad)">${esc(r.error)}</p>` : `<div class="kv">
      <span>delivered</span><span class="mono">${r.delivered.map((x) => fmt(x, 2)).join(', ')}</span>
      <span>error x, y, z</span><span class="mono">${fmt(r.dx, 2)}, ${fmt(r.dy, 2)}, ${fmt(r.dz, 2)} mm</span>
      <span>distance</span><span class="mono">${fmt(r.distance, 2)} mm</span>
      ${r.depth !== undefined ? `<span>depth</span><span class="mono">${fmt(r.depth, 2)} mm (+ = deeper)</span><span>radial</span><span class="mono">${fmt(r.radial, 2)} mm</span>` : ''}
    </div>`) : ''}</div>`;
}
function bindPlan(l, edit) {
  const read = (id) => [0, 1, 2].map((i) => $(`#${id}${i}`).value).map((v) => (v === '' ? null : Number(v)));
  $('#plSave').onclick = () => {
    const t = read('plT'), e = read('plE');
    if (t.some((v) => v === null)) { toast('Enter all three target coordinates.', 'error'); return; }
    edit({ action: 'plan', target: t, entry: e.some((v) => v === null) ? null : e, space: $('#plSpace').value, point: $('#plPoint').value });
  };
  $('#plClear').onclick = () => edit({ action: 'plan', clear: true });
  $('#plFromCross').onclick = () => {
    const m = crosshairMM(); if (!m) return;
    let q = m;
    const sp = $('#plSpace').value, a = sp === 'acpc' ? S.p.acpc_matrix : sp === 'frame' ? S.p.frame_from_display : null;
    if (sp !== 'mri') {
      if (!a) { toast(`Set up ${sp === 'acpc' ? 'AC/PC' : 'the frame'} first, or use MRI space.`, 'error'); return; }
      q = xf(a, m);
    }
    q.forEach((v, i) => { $(`#plT${i}`).value = v.toFixed(2); });
  };
}
function describeAxes(r) {  // rows of a frame rotation (frame axes in RAS) -> e.g. 'X→L, Y→A, Z→I'
  const n = [['R', 'L'], ['A', 'P'], ['S', 'I']];
  return ['X', 'Y', 'Z'].map((ax, i) => { const row = r[i].slice(0, 3), k = row.map(Math.abs).indexOf(Math.max(...row.map(Math.abs)));
    return `${ax}→${row[k] > 0 ? n[k][0] : n[k][1]}`; }).join(', ');
}
function frameScan() {  // the scan the localizer is being found in
  const sc = S.p?.scans || {};
  if (S.fmScan && sc[S.fmScan]) return S.fmScan;
  const u = S.p?.frame?.use;
  S.fmScan = u && sc[u] ? u : sc.ct ? 'ct' : sc.mri ? 'mri' : null;
  return S.fmScan;
}
const FRAME_LEGEND = [['#ff3232', 'intensity band'], ['#ffd228', 'auto-found rods'], ['#3cdcff', 'localizer model'],
  ['#46e66e', 'rod centres'], ['#ff50dc', 'manual centres']];  // = the colours of syncBand and the frame overlay
function nPlateSvg(p) {  // the plate's N as drawn in the model: rods vertical (frame Z down), diagonal as set
  const hs = [...p.rods, p.diagonal[0][0], p.diagonal[1][0]], h0 = Math.min(...hs), h1 = Math.max(...hs);
  const z0 = Math.min(p.z[0], p.diagonal[0][1], p.diagonal[1][1]), z1 = Math.max(p.z[1], p.diagonal[0][1], p.diagonal[1][1]);
  const X = (h) => 4 + 36 * (h - h0) / Math.max(h1 - h0, 1), Y = (z) => 3 + 22 * (z - z0) / Math.max(z1 - z0, 1);
  const ln = (a, b, c, d) => `<line x1="${X(a)}" y1="${Y(b)}" x2="${X(c)}" y2="${Y(d)}"/>`;
  return `<svg width="44" height="28" class="nsvg ${p.enabled ? '' : 'off'}">${p.rods.map((h) => ln(h, p.z[0], h, p.z[1])).join('')}
    ${ln(p.diagonal[0][0], p.diagonal[0][1], p.diagonal[1][0], p.diagonal[1][1])}</svg>`;
}
function frameHTML() {
  const f = S.p.frame || {}, m = f.model, r = frameScan();
  if (!r) return '<h2>Stereotactic frame</h2><p class="muted">Import the CT or the MRI with the localizer first.</p>';
  const s = f.scans?.[r] || {}, sum = s.summary, fits = s.auto?.fits || [], band = S.fmBand || s.band || [0, null];
  const v = nv.volumes.find((x) => !x._band && x.name?.startsWith(`${r}-`)), lo = v ? Math.floor(v.global_min) : -1024, hi = v ? Math.ceil(v.global_max) : 3071;
  const num = (i, k, x) => `<input type="number" step="any" data-pf="${i}.${k}" value="${x}" style="width:52px">`;
  const chip = (k) => { const t = f.scans?.[k]; return !S.p.scans[k] ? '' : t?.fitted ? ` <span class="small mono">${fmt(t.summary?.rms, 2)} mm</span>` : ''; };
  const fitted = ['ct', 'mri'].filter((k) => f.scans?.[k]?.fitted);
  const rods = sum?.rods ? Object.entries(sum.rods) : [];
  return `<h2>Stereotactic frame</h2>
  <p>Find the N-bar localizer in the CT or the MRI; contacts, AC/PC and plans then also get frame coordinates.</p>
  <div class="row"><label>scan</label><span class="seg" id="fmScan">${['ct', 'mri'].map((k) => `<button data-fs="${k}" class="${k === r ? 'on' : ''}" ${S.p.scans[k] ? '' : 'disabled'}>${k.toUpperCase()}${chip(k)}</button>`).join('')}</span></div>
  <div class="legend small">${FRAME_LEGEND.map(([c, t]) => `<span><i style="background:${c}"></i>${t}</span>`).join('')}</div>
  <div class="card"><b>Localizer plates</b>
    ${m.plates.map((p, i) => `<div class="row"><label class="row" style="min-width:96px"><input type="checkbox" data-puse="${i}" ${p.enabled ? 'checked' : ''}> ${esc(p.name)}</label>
      ${nPlateSvg(p)}<span class="spacer"></span>
      <button class="ghost" data-pflip="${i}" title="Mirror this plate's diagonal: the N turns into its mirror image">flip N</button>
      <button class="ghost" data-pgo="${esc(p.name)}" ${s.rods && p.enabled ? '' : 'disabled'} title="Crosshair to this plate">go</button></div>`).join('')}
    <p class="muted small">Switch off plates that are not in the scan or should not be used; flip an N whose diagonal runs the other way
    in your localizer. The placement stays; track again afterwards.</p></div>
  <div class="card"><b>1 · What counts as rod</b>
    <p class="muted small">Pick the intensity band so the rods light up red and the plates and bone do not. In the CT the rods can be only
    slightly brighter, or darker (fluid in plastic), than the plate around them.</p>
    <div class="row"><label>from</label><input type="range" id="fmLo" min="${lo}" max="${hi}" step="any" value="${band[0]}" style="flex:1">
      <input type="number" id="fmLoN" step="any" value="${fmt(band[0], 0)}" style="width:72px"></div>
    <div class="row"><label>to</label><input type="range" id="fmHi" min="${lo}" max="${hi}" step="any" value="${band[1] ?? hi}" style="flex:1" ${band[1] == null ? 'disabled' : ''}>
      <input type="number" id="fmHiN" step="any" value="${band[1] == null ? '' : fmt(band[1], 0)}" placeholder="no limit" style="width:72px"></div>
    <div class="row small"><label class="row"><input type="checkbox" id="fmOpen" ${band[1] == null ? 'checked' : ''}> no upper limit</label>
      <label class="row"><input type="checkbox" id="fmShow" ${S.fmShowBand !== false ? 'checked' : ''}> show band in red</label></div></div>
  <div class="card"><b>2 · Place the localizer</b>
    <p class="muted small">Cyan: where the localizer model is now. Find it automatically, or put it on the rods by hand (roughly, within a few mm).</p>
    <div class="row"><button id="fmAuto" class="primary">Auto-find</button><button id="fmPlace" title="Put the crosshair in the middle of the localizer (midway between the side plates), then press this">Centre at crosshair</button>
      <button id="fmPoseReset" class="ghost" title="Expected mounting, centred on the scan">Reset</button></div>
    <div class="row"><label>turn 90°</label>${[['x', 'L–R'], ['y', 'A–P'], ['z', 'S–I']].map(([k, n]) => `<button data-fturn="${k}" title="Turn the model 90° about the ${n} axis (Shift: the other way)">${n}</button>`).join('')}</div>
    <div class="row"><label>step</label><select id="fmMm">${[0.5, 1, 2, 5, 10].map((x) => `<option ${x === (S.fmMm ?? 2) ? 'selected' : ''}>${x}</option>`).join('')}</select> mm
      <select id="fmDeg">${[0.5, 1, 2, 5].map((x) => `<option ${x === (S.fmDeg ?? 1) ? 'selected' : ''}>${x}</option>`).join('')}</select> °</div>
    ${[['L–R', 0], ['A–P', 1], ['S–I', 2]].map(([n, i]) => `<div class="row"><label>${n}</label><button data-fmove="${i}" data-s="-1">−</button><button data-fmove="${i}" data-s="1">+</button>
      <span class="muted small" style="margin-left:6px">rotate</span><button data-frot="${i}" data-s="-1">↺</button><button data-frot="${i}" data-s="1">↻</button></div>`).join('')}
    ${fits.length > 1 ? `<p class="small">Auto-find candidates (best first):</p>${fits.map((x, i) => `<label class="row small"><input type="radio" name="fmFit" value="${i}" ${i === s.auto.chosen && s.method === 'auto' ? 'checked' : ''}>
      <span><b>${esc(x.axes)}</b> · ${fmt(x.rms, 2)} mm</span></label>`).join('')}` : ''}</div>
  <div class="card"><b>3 · Follow the rods and fit</b>
    <p class="muted small">Each rod is followed slice by slice near the model and the model is fitted to the rod centres (green).</p>
    <div class="row"><button class="primary" id="fmTrack" ${s.display_to_frame ? '' : 'disabled'}>Track rods and fit</button></div>
    ${sum ? `<div class="kv"><span>status</span><span>${s.fitted ? `<span class="badge ok">fitted (${esc(s.method)})</span>` : '<span class="badge warn">placed by hand, not fitted</span>'}</span>
      <span>residual</span><span>${fmt(sum.rms, 2)} mm</span><span>frame axes</span><span>${esc(sum.axes || '–')}</span></div>
      ${sum.rms > 1 ? '<p class="small" style="color:var(--bad)">Residual over 1 mm: check the band, the placement and the localizer model.</p>' : ''}
      ${rods.length ? `<table class="contacts"><tr><th>rod</th><th>found</th><th>residual</th><th></th></tr>${rods.map(([id, x]) =>
        `<tr><td>${esc(id)}</td><td class="${x.found < 0.6 * x.slices ? 'bad' : ''}">${x.found}/${x.slices}</td><td>${x.rms == null ? '–' : `${fmt(x.rms, 2)} mm`}</td>
        <td><button class="ghost" data-frod="${esc(id)}">go</button></td></tr>`).join('')}</table>` : ''}` : ''}</div>
  ${fitted.length ? `<div class="card"><b>Frame coordinates from</b>
    ${fitted.map((k) => `<label class="row small"><input type="radio" name="fmUse" value="${k}" ${f.use === k ? 'checked' : ''}> ${k.toUpperCase()} (${fmt(f.scans[k].summary?.rms, 2)} mm)</label>`).join('')}
    ${f.agreement != null ? `<p class="small">CT and MRI fits differ by <b>${fmt(f.agreement, 2)} mm</b> near the brain (includes the coregistration error).</p>` : ''}</div>` : ''}
  <details class="card"><summary><b>Localizer model</b> · ${esc(m.name)} <span class="badge ${m.verified ? 'ok' : 'warn'}">${m.verified ? 'geometry verified' : 'geometry not verified'}</span></summary>
    <p class="muted small">${esc(m.axes)}</p>
    ${m.verified ? '' : `<p class="small" style="color:var(--warn)">${esc(m.note || 'Check the rod geometry against your localizer before relying on frame coordinates.')}</p>`}
    <div style="overflow-x:auto"><table class="contacts"><tr><th>use</th><th>plate</th><th>axis</th><th>at</th><th>rod 1</th><th>rod 2</th><th>z from</th><th>z to</th><th>diagonal from (h, z)</th><th>to (h, z)</th></tr>
    ${m.plates.map((p, i) => `<tr><td><input type="checkbox" data-pf="${i}.enabled" ${p.enabled ? 'checked' : ''}></td><td>${esc(p.name)}</td>
      <td><select data-pf="${i}.axis"><option ${p.axis === 'x' ? 'selected' : ''}>x</option><option ${p.axis === 'y' ? 'selected' : ''}>y</option></select></td>
      <td>${num(i, 'position', p.position)}</td><td>${num(i, 'rods.0', p.rods[0])}</td><td>${num(i, 'rods.1', p.rods[1])}</td>
      <td>${num(i, 'z.0', p.z[0])}</td><td>${num(i, 'z.1', p.z[1])}</td>
      <td>${num(i, 'diagonal.0.0', p.diagonal[0][0])}${num(i, 'diagonal.0.1', p.diagonal[0][1])}</td>
      <td>${num(i, 'diagonal.1.0', p.diagonal[1][0])}${num(i, 'diagonal.1.1', p.diagonal[1][1])}</td></tr>`).join('')}</table></div>
    <p class="muted small">Plate "at" = its X (axis x, lateral plates) or Y (axis y, front/back plate); rods at that plate's horizontal
    position (Y or X); diagonal end points as (horizontal, Z). Expected mounting: <b>${describeAxes(m.mount)}</b> (frame axes in the subject).</p>
    <label class="row small"><input type="checkbox" id="fmVerified" ${m.verified ? 'checked' : ''}> This geometry was checked against our localizer</label>
    <div class="row"><button id="fmSave">Save model</button><button id="fmReset" class="ghost">Default</button>
      <button id="fmExport" class="ghost">Export JSON</button><button id="fmImport" class="ghost">Import JSON…</button>
      ${s.display_to_frame ? '<button id="fmMount" class="ghost" title="Prefer the current orientation when fits are equally good (e.g. animals)">Current orientation as expected mounting</button>' : ''}</div></details>
  <div class="row"><span class="spacer"></span>${nextHTML()}</div>`;
}
function readFrameModel() {
  const m = JSON.parse(JSON.stringify(S.p.frame.model));
  $$('[data-pf]').forEach((el) => {
    const [i, ...path] = el.dataset.pf.split('.');
    let o = m.plates[Number(i)];
    for (const k of path.slice(0, -1)) o = o[k];
    const last = path[path.length - 1];
    o[last] = el.type === 'checkbox' ? el.checked : el.tagName === 'SELECT' ? el.value : Number(el.value);
  });
  m.verified = $('#fmVerified').checked;
  return m;
}
function bindFrame() {
  const r = frameScan();
  if (!r) return;
  $$('[data-fs]').forEach((b) => { b.onclick = () => { S.fmScan = b.dataset.fs; S.fmBand = null; render(); refreshViewer(); }; });
  const pose = (body, text) => act('/frame-pose', { scan: r, ...body }, text);
  // band: live preview while dragging, saved (undoable) on release
  const band = () => [Number($('#fmLoN').value), $('#fmOpen').checked || $('#fmHiN').value === '' ? null : Number($('#fmHiN').value)];
  const live = (src) => {
    if (src === 'lo') $('#fmLoN').value = fmt(Number($('#fmLo').value), 0);
    if (src === 'hi') $('#fmHiN').value = fmt(Number($('#fmHi').value), 0);
    S.fmBand = band(); requestAnimationFrame(syncBand);
  };
  const save = () => { S.fmBand = band(); act('/frame-band', { scan: r, band: S.fmBand }).then(() => { S.fmBand = null; }); };
  $('#fmLo').oninput = () => live('lo'); $('#fmHi').oninput = () => live('hi');
  $('#fmLo').onchange = save; $('#fmHi').onchange = save; $('#fmLoN').onchange = save; $('#fmHiN').onchange = save;
  $('#fmOpen').onchange = (e) => {
    if (!e.target.checked && $('#fmHiN').value === '') $('#fmHiN').value = fmt(nv.volumes[0]?.global_max ?? 3071, 0);
    save();
  };
  $('#fmShow').onchange = (e) => { S.fmShowBand = e.target.checked; syncBand(); };
  // placing the model
  $('#fmAuto').onclick = async () => {
    if (!(await act('/frame-autofind', { scan: r }, `Searching the ${r.toUpperCase()} for the localizer rods…`)))
      await setProject(await api(P('')));  // keeps what was found, for the viewer
  };
  $('#fmPlace').onclick = () => { const c = crosshairMM(); if (c) pose({ op: 'place', point: c }); };
  $('#fmPoseReset').onclick = () => pose({ op: 'reset' });
  $$('[data-fturn]').forEach((b) => { b.onclick = (e) => pose({ op: 'turn', axis: b.dataset.fturn, deg: e.shiftKey ? -90 : 90 }); });
  $('#fmMm').onchange = (e) => { S.fmMm = Number(e.target.value); };
  $('#fmDeg').onchange = (e) => { S.fmDeg = Number(e.target.value); };
  $$('[data-fmove]').forEach((b) => { b.onclick = () => { const t = [0, 0, 0]; t[b.dataset.fmove] = (S.fmMm ?? 2) * b.dataset.s; pose({ op: 'nudge', move: t }); }; });
  $$('[data-frot]').forEach((b) => { b.onclick = () => { const t = [0, 0, 0]; t[b.dataset.frot] = (S.fmDeg ?? 1) * b.dataset.s; pose({ op: 'nudge', rot: t }); }; });
  $$('[name="fmFit"]').forEach((el) => { el.onchange = () => pose({ op: 'auto', index: Number(el.value) }); });
  $('#fmTrack').onclick = () => act('/frame-refine', { scan: r }, 'Following the rods and fitting the localizer…');
  const plate = (i, change) => { const m = JSON.parse(JSON.stringify(S.p.frame.model)); change(m.plates[i]); act('/frame-model', { model: m }); };
  $$('[data-puse]').forEach((el) => { el.onchange = () => plate(Number(el.dataset.puse), (p) => { p.enabled = el.checked; }); });
  $$('[data-pflip]').forEach((b) => { b.onclick = () => plate(Number(b.dataset.pflip), (p) => {
    p.diagonal = [[p.diagonal[0][0], p.diagonal[1][1]], [p.diagonal[1][0], p.diagonal[0][1]]];  // same ends horizontally, heights swapped
  }); });
  $$('[data-pgo]').forEach((b) => { b.onclick = () => {
    const segs = Object.entries(S.p.frame.scans[r].rods || {}).filter(([id]) => id.startsWith(`${b.dataset.pgo}-`)).flatMap(([, s]) => s);
    if (segs.length) goTo([0, 1, 2].map((k) => segs.reduce((t, q) => t + q[k], 0) / segs.length));
  }; });
  $$('[data-frod]').forEach((b) => { b.onclick = () => {
    const seg = S.p.frame.scans[r].rods?.[b.dataset.frod];
    if (seg) goTo(seg[0].map((x, i) => (x + seg[1][i]) / 2));
  }; });
  $$('[name="fmUse"]').forEach((el) => { el.onchange = () => act('/frame-use', { scan: el.value }); });
  // the localizer model
  $('#fmSave').onclick = () => act('/frame-model', { model: readFrameModel() });
  $('#fmReset').onclick = () => { if (confirm('Replace the model with the default geometry?')) act('/frame-model', { reset: true }); };
  $('#fmVerified').onchange = () => act('/frame-model', { model: readFrameModel() });
  $('#fmExport').onclick = () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(S.p.frame.model, null, 2)], { type: 'application/json' }));
    a.download = 'localizer.json'; a.click();
  };
  $('#fmImport').onclick = () => {
    const inp = $('#presetInput');
    inp.onchange = async () => { const f = inp.files[0]; inp.value = ''; if (f) act('/frame-model', { model: JSON.parse(await f.text()) }); };
    inp.click();
  };
  if ($('#fmMount')) $('#fmMount').onclick = () => {
    const m = readFrameModel(), x = S.p.frame.scans[r].display_to_frame;  // frame axes in the (RAS) display world
    m.mount = [0, 1, 2].map((i) => x[i].slice(0, 3));
    act('/frame-model', { model: m });
  };
  bindNext();
}
function targetsHTML() {
  const leads = S.p.leads;
  if (!S.p.coreg || !leads.length) return '<h2>Planned vs delivered</h2><p class="muted">Detect at least one lead first (Electrodes → Leads); the plans are compared with the detected leads.</p>';
  if (!leads.some((l) => l.name === S.target)) S.target = leads[0].name;
  const l = leads.find((x) => x.name === S.target);
  const rows = leads.filter((x) => x.plan);
  return `<h2>Planned vs delivered</h2>
  <p>Enter the planned target (and optionally the planned entry) of each lead in AC/PC, frame or MRI coordinates; the delivered lead
  tip or a chosen contact is compared with it.</p>
  <div class="row"><label>lead</label><select id="tgLead">${leads.map((x) => `<option ${x.name === S.target ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></div>
  ${planHTML(l)}
  ${rows.length ? `<h3>All plans</h3><table class="contacts"><tr><th>lead</th><th>space</th><th>point</th><th>distance</th><th>dx</th><th>dy</th><th>dz</th><th>depth</th><th>radial</th></tr>
    ${rows.map((x) => { const r = x.plan_result || {}; return `<tr><td>${esc(x.name)}</td><td>${x.plan.space}</td><td>${x.plan.point}</td>
      <td>${r.error ? esc(r.error) : fmt(r.distance, 2)}</td><td>${fmt(r.dx, 2)}</td><td>${fmt(r.dy, 2)}</td><td>${fmt(r.dz, 2)}</td><td>${fmt(r.depth, 2)}</td><td>${fmt(r.radial, 2)}</td></tr>`; }).join('')}</table>` : ''}
  <div class="row"><span class="spacer"></span>${nextHTML()}</div>`;
}
function bindTargets() {
  if (!$('#tgLead')) return;
  $('#tgLead').onchange = (e) => { S.target = e.target.value; render(); };
  bindPlan(S.p.leads.find((x) => x.name === S.target), (body) => act('/lead-edit', { name: S.target, ...body }));
  bindNext();
}
function brainHTML() {
  if (!S.p.coreg) return '<h2>Brain model</h2><p class="muted">Coregister the CT to the MRI first (the CT gives the skull).</p>';
  const b = S.p.brain, bp = S.brainParams || (S.brainParams = JSON.parse(JSON.stringify(b?.params || { use_mri: true, threshold: null, smooth: 20, detail: 1, cuts: [] })));
  const ax = { x: 'sagittal', y: 'coronal', z: 'axial' };
  const cutName = (c) => ({ 'z.above': 'remove below', 'z.below': 'remove above', 'y.above': 'remove behind', 'y.below': 'remove in front',
    'x.below': 'keep left', 'x.above': 'keep right' })[`${c.axis}.${c.keep}`];
  return `<h2>Brain model</h2>
  <p>A brain surface for figures: the space inside the skull from the CT, refined with the MRI so gyri and sulci show, cut as you need,
  smoothed, with the leads in 3D. Exported for Python/MATLAB.</p>
  <div class="card">
    <div class="row"><label>seed</label><span class="mono small" style="flex:1">${bp.seed ? bp.seed.map((x) => fmt(x, 1)).join(', ') : 'AC/PC midpoint, box or MRI centre'}</span>
      <button id="bSeed" title="A point inside the brain">Seed at crosshair</button></div>
    <label class="row small"><input type="checkbox" id="bMri" ${bp.use_mri ? 'checked' : ''}> Use the MRI to separate brain from CSF (shows gyri and sulci)</label>
    <div class="row"><label>MRI threshold</label><input type="number" id="bThr" step="any" value="${bp.threshold ?? ''}" placeholder="${b?.threshold_used != null ? fmt(b.threshold_used, 0) : 'auto'}" style="width:90px">
      <span class="muted small">brain brighter than this (auto: Otsu)</span></div>
    <h3>Cuts at the crosshair</h3>
    <div class="grid3"><button data-cut="z.above">remove below</button><button data-cut="z.below">remove above</button><button data-cut="y.above">remove behind</button>
      <button data-cut="y.below">remove in front</button><button data-cut="x.below">keep left</button><button data-cut="x.above">keep right</button></div>
    ${bp.cuts.map((c, i) => `<div class="row small"><span style="flex:1">${cutName(c)} · ${ax[c.axis]} at ${fmt(c.at, 1)} mm</span><button data-uncut="${i}" class="ghost">✕</button></div>`).join('')}
    <div class="row"><label>smoothing</label><input type="range" id="bSmooth" min="0" max="60" step="1" value="${bp.smooth}" style="flex:1"><span class="mono small">${bp.smooth}</span></div>
    <div class="row"><label>detail</label><select id="bDetail">${[[1, 'fine'], [2, 'medium'], [3, 'coarse']].map(([v, t]) => `<option value="${v}" ${bp.detail === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
      <span class="spacer"></span><button class="primary" id="bBuild">Build</button></div>
  </div>
  ${b ? `<div class="card"><div class="kv"><span>volume</span><span>${fmt(b.volume_ml, 1)} ml</span><span>surface</span><span>${b.vertices.toLocaleString()} vertices</span>
    <span>leads</span><span>${b.leads.length}</span></div>
    <label class="row small"><input type="checkbox" id="bLeads" ${S.showLeads ? 'checked' : ''}> show leads</label>
    <div class="row"><button id="b3d">3D view</button><button id="bExport">Export</button></div>
    ${S.brainExport ? `<p class="small">Written to <span class="mono">${esc(S.brainExport.folder)}</span>: ${S.brainExport.files.map(esc).join(', ')}</p>` : ''}</div>` : ''}`;
}
function bindBrain() {
  if (!S.p.coreg) return;
  const bp = S.brainParams;
  $('#bSeed').onclick = () => { bp.seed = crosshairMM(); render(); };
  $('#bMri').onchange = (e) => { bp.use_mri = e.target.checked; };
  $('#bThr').onchange = (e) => { bp.threshold = e.target.value === '' ? null : Number(e.target.value); };
  $$('[data-cut]').forEach((el) => { el.onclick = () => {
    const m = crosshairMM(); if (!m) return;
    const [axis, keep] = el.dataset.cut.split('.');
    bp.cuts = bp.cuts.filter((c) => c.axis !== axis || c.keep !== keep);  // the same cut again moves it
    bp.cuts.push({ axis, keep, at: m[{ x: 0, y: 1, z: 2 }[axis]] }); render();
  }; });
  $$('[data-uncut]').forEach((el) => { el.onclick = () => { bp.cuts.splice(Number(el.dataset.uncut), 1); render(); }; });
  $('#bSmooth').oninput = (e) => { bp.smooth = Number(e.target.value); e.target.nextElementSibling.textContent = bp.smooth; };
  $('#bDetail').onchange = (e) => { bp.detail = Number(e.target.value); };
  $('#bBuild').onclick = async () => {
    if (await act('/brain', bp, 'Building the brain model (mask, cuts, surface, leads)…')) { S.showLeads = S.showLeads ?? true; setLayout('render'); render(); refreshViewer(); }
  };
  if ($('#bLeads')) $('#bLeads').onchange = (e) => { S.showLeads = e.target.checked; refreshViewer(); };
  if ($('#b3d')) $('#b3d').onclick = () => setLayout('render');
  if ($('#bExport')) $('#bExport').onclick = async () => {
    const r = await busy('Exporting the brain model…', () => api(P('/brain-export'), {}));
    if (r) { S.brainExport = r; render(); }
  };
}
function acpcHTML() {
  const a = S.p.acpc || {}, ready = S.p.scans.mri;
  const row = (k, label, key) => `<div class="row"><label>${label}</label><span class="mono small" style="flex:1">${a[k] ? a[k].map((x) => fmt(x, 1)).join(', ') : '<span class="muted">not set</span>'}</span>
    <button data-setpt="${k}" ${ready ? '' : 'disabled'}>Set at crosshair <kbd>${key}</kbd></button><button data-gopt="${k}" class="ghost" ${a[k] ? '' : 'disabled'}>Go</button></div>`;
  return `<h2>AC/PC</h2>
  <p>Mark the anterior and posterior commissures and one point on the midline (e.g. high on the interhemispheric fissure) on the MRI.
  Done on the MRI alone, at its full resolution, before coregistration. Every contact then also gets AC/PC coordinates: <b>+y</b> from PC towards AC, <b>+z</b> towards the midline point (superior; dorsal in
  animals), <b>+x</b> to the subject's right.</p>
  <div class="card">${row('ac', 'AC', 'A')}${row('pc', 'PC', 'P')}${row('msp', 'midline', 'M')}
    <div class="row"><label>origin</label><select id="acOrigin">${[['mcp', 'mid-commissural point'], ['ac', 'AC'], ['pc', 'PC']].map(([v, t]) => `<option value="${v}" ${(a.origin || 'mcp') === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
    ${S.p.acpc_distance !== null ? `<p class="small">AC–PC distance: <b>${fmt(S.p.acpc_distance, 1)} mm</b></p>` : ''}
    <div class="row"><span class="badge ${S.p.acpc_matrix ? 'ok' : 'warn'}">${S.p.acpc_matrix ? 'AC/PC coordinates active' : 'incomplete'}</span><span class="spacer"></span>
      <button id="acClear" class="ghost" ${S.p.acpc ? '' : 'disabled'}>Clear</button></div></div>
  <p class="muted small">Markers: AC red, PC blue, midline green. Zoom in (+) for precise placement.</p>
  <div class="row"><span class="spacer"></span>${nextHTML()}</div>`;
}
function bindAcpc() {
  $$('[data-setpt]').forEach((b) => { b.onclick = () => setAcpcPoint(b.dataset.setpt); });
  $$('[data-gopt]').forEach((b) => { b.onclick = () => goTo(S.p.acpc[b.dataset.gopt]); });
  $('#acOrigin').onchange = (e) => act('/acpc', { origin: e.target.value });
  $('#acClear').onclick = () => act('/acpc', { clear: true });
  bindNext();
}
function setAcpcPoint(k) {
  const m = crosshairMM();
  if (m) act('/acpc', { point: k, xyz: m });
}
function bindLeads() {
  if (!S.p.coreg) return;
  const d = S.draft;
  $('#dName').oninput = (e) => { d.name = e.target.value; };
  $('#dHemi').onchange = (e) => { d.hemisphere = e.target.value; };
  $('#dCurved').onchange = (e) => { d.curved = e.target.checked; };
  $('#dPreset').onchange = (e) => { d.preset = e.target.value; };
  $('#editPresets').onclick = openPresets;
  $('#setTip').onclick = () => setPoint('tip');
  $('#setEntry').onclick = () => setPoint('entry');
  $('#detect').onclick = detectLead;
  $$('[data-lead]').forEach((el) => { el.onclick = () => selectLead(el.dataset.lead); });
  const l = S.p.leads.find((x) => x.name === S.lead);
  if (!l) return;
  const edit = (body, text) => act('/lead-edit', { name: l.name, ...body }, text);
  $('#lRedetect').onclick = () => edit({ action: 'redetect' }, 'Detecting…');
  $('#lRefit').onclick = () => edit({ action: 'refit' }, 'Fitting…');
  $('#lShiftM').onclick = () => edit({ action: 'shift', ds: -0.25 });
  $('#lShiftP').onclick = () => edit({ action: 'shift', ds: 0.25 });
  $('#lHemi').onchange = (e) => edit({ action: 'hemisphere', hemisphere: e.target.value });
  $('#lPreset').onclick = () => edit({ action: 'redetect', preset: d.preset }, 'Detecting with the selected preset…');
  $('#lRename').onclick = async () => {
    const n = prompt('New lead name:', l.name);
    if (n && n !== l.name) { const r = await edit({ action: 'rename', new_name: n }); if (r) { S.lead = n; render(); } }
  };
  $('#lDelete').onclick = async () => { if (confirm(`Delete lead ${l.name}?`)) { await edit({ action: 'delete' }); S.lead = null; S.contact = null; render(); refreshViewer(); } };
  $$('tr[data-ci]').forEach((tr) => { tr.onclick = () => selectContact(Number(tr.dataset.ci)); });
  $$('[data-mv]').forEach((b) => { b.onclick = () => moveContact(S.contact, l.contacts[S.contact].s + Number(b.dataset.mv)); });
  if ($('#anchor')) $('#anchor').onclick = () => edit({ action: 'refit', index: S.contact, s: l.contacts[S.contact].s }, 'Fitting…');
}
function setPoint(which) {
  const m = crosshairMM();
  if (!m) return;
  S.draft[which] = m;
  render();
}
async function detectLead() {
  const d = S.draft;
  const p = await act('/lead', { name: d.name, preset: d.preset, tip: d.tip, entry: d.entry, curved: d.curved, hemisphere: d.hemisphere }, 'Tracking the lead and fitting contacts…');
  if (!p) return;
  S.lead = d.name; S.contact = null;
  S.draft = { name: '', preset: d.preset, hemisphere: d.hemisphere, curved: false, tip: null, entry: null };
  render(); await loadLeadView();
}
async function selectLead(name) {
  S.lead = name; S.contact = null; render();
  const l = S.p.leads.find((x) => x.name === name);
  if (l?.contacts.length) goTo([l.contacts[0].x, l.contacts[0].y, l.contacts[0].z]);
  await loadLeadView();
}
function selectContact(i) {
  S.contact = i; render();
  const r = S.p.leads.find((x) => x.name === S.lead)?.contacts[i];
  if (r) { goTo([r.x, r.y, r.z]); drawInspector(); showXsec(r.s); }
}
async function moveContact(i, s) {
  await act('/lead-edit', { name: S.lead, action: 'move', index: i, s });
}
async function loadLeadView() {
  if (!S.lead) return;
  $('#insTitle').textContent = `Lead ${S.lead}`;
  try { S.view = await api(P(`/lead-view?lead=${encodeURIComponent(S.lead)}`)); } catch (e) { toast(e.message, 'error'); return; }
  drawInspector();
}

// ---------- inspector (straightened lead) ----------
const ins = { drag: null, geom: null, xsecTimer: null };
function windowOf(values) {
  const flat = values.flat().filter(Number.isFinite).sort((a, b) => a - b);
  return [flat[Math.floor(flat.length * 0.02)] ?? 0, flat[Math.floor(flat.length * 0.998)] ?? 1];
}
function grayImage(rows, lo, hi) { // rows: [s][lateral] -> image with s along x
  const w = rows.length, h = rows[0].length, img = new ImageData(w, h);
  for (let x = 0; x < w; x++) for (let y = 0; y < h; y++) {
    const v = Math.max(0, Math.min(255, ((rows[x][y] - lo) / (hi - lo)) * 255)), k = (y * w + x) * 4;
    img.data[k] = img.data[k + 1] = img.data[k + 2] = v; img.data[k + 3] = 255;
  }
  return img;
}
function drawInspector() {
  const cv = $('#strip'), v = S.view, l = S.p?.leads.find((x) => x.name === S.lead);
  if (!cv || !v || !l) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
  const n = v.profile.length, sOf = (x) => v.s0 + ((x - 8) / (W - 16)) * (n - 1) * v.ds, xOf = (s) => 8 + ((s - v.s0) / ((n - 1) * v.ds)) * (W - 16);
  ins.geom = { sOf, xOf };
  const [lo, hi] = windowOf([...v.strip1, ...v.strip2]);
  const stripH = Math.min(46, (H - 40) / 4), y1 = 16, y2 = y1 + stripH + 4, py = y2 + stripH + 10, ph = H - py - 6;
  const tmp = document.createElement('canvas');
  [[v.strip1, y1], [v.strip2, y2]].forEach(([rows, y]) => {
    const img = grayImage(rows, lo, hi); tmp.width = img.width; tmp.height = img.height;
    tmp.getContext('2d').putImageData(img, 0, 0);
    g.imageSmoothingEnabled = true; g.drawImage(tmp, 8, y, W - 16, stripH);
  });
  const pv = v.profile, mv = v.model, pmin = Math.min(...pv, ...mv), pmax = Math.max(...pv, ...mv);
  const yOf = (val) => py + ph - ((val - pmin) / (pmax - pmin || 1)) * ph;
  const line = (arr, color, wdt) => { g.beginPath(); arr.forEach((val, i) => { const x = xOf(v.s0 + i * v.ds); i ? g.lineTo(x, yOf(val)) : g.moveTo(x, yOf(val)); }); g.strokeStyle = color; g.lineWidth = wdt; g.stroke(); };
  line(pv, '#9aa3b2', 1.2); line(mv, '#f0b429', 1.4);
  const L = l.preset.contact_length_mm;
  l.contacts.forEach((c, i) => {
    const x0 = xOf(c.s - L / 2), x1 = xOf(c.s + L / 2), col = { fitted: '#3ecf8e', geometry: '#f0b429', manual: '#3fa7ff' }[c.fit_mode] || '#fff';
    g.fillStyle = col + (i === S.contact ? '55' : '22'); g.fillRect(x0, y1 - 2, x1 - x0, H - y1 - 2);
    g.strokeStyle = col; g.lineWidth = i === S.contact ? 2 : 1; g.beginPath(); g.moveTo(xOf(c.s), y1 - 2); g.lineTo(xOf(c.s), H - 4); g.stroke();
    g.fillStyle = col; g.font = '10px sans-serif'; g.textAlign = 'center'; g.fillText(c.name, xOf(c.s), 11);
  });
  g.fillStyle = '#8b93a1'; g.textAlign = 'left'; g.fillText('tip', xOf(0) + 2, H - 8);
  g.strokeStyle = '#8b93a1'; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(xOf(0), y1); g.lineTo(xOf(0), H - 4); g.stroke(); g.setLineDash([]);
}
function nearestContact(x) {
  const l = S.p.leads.find((y) => y.name === S.lead);
  let best = null;
  l.contacts.forEach((c, i) => { const d = Math.abs(ins.geom.xOf(c.s) - x); if (d < 10 && (!best || d < best.d)) best = { i, d }; });
  return best?.i ?? null;
}
async function showXsec(s) {
  if (!S.lead) return;
  try {
    const x = await api(P(`/xsec?lead=${encodeURIComponent(S.lead)}&s=${s}`));
    const cv = $('#xsec'), n = x.values.length, [lo, hi] = windowOf(x.values);
    const img = grayImage(x.values, lo, hi), tmp = document.createElement('canvas');
    tmp.width = n; tmp.height = n; tmp.getContext('2d').putImageData(img, 0, 0);
    const g = cv.getContext('2d'); g.imageSmoothingEnabled = true; g.drawImage(tmp, 0, 0, cv.width, cv.height);
    g.strokeStyle = 'rgba(240,180,41,.7)'; g.beginPath(); g.moveTo(cv.width / 2, 0); g.lineTo(cv.width / 2, cv.height);
    g.moveTo(0, cv.height / 2); g.lineTo(cv.width, cv.height / 2); g.stroke();
    g.fillStyle = '#e6e8ec'; g.font = '10px sans-serif'; g.fillText(`s ${fmt(s, 1)} mm · ${2 * x.radius} mm`, 4, 12);
  } catch (e) { /* hover errors are not worth a toast */ }
}
function bindInspector() {
  const cv = $('#strip');
  cv.onmousedown = (e) => {
    if (!ins.geom) return;
    const i = nearestContact(e.offsetX);
    if (i !== null) { ins.drag = { i, s: S.p.leads.find((l) => l.name === S.lead).contacts[i].s }; S.contact = i; drawInspector(); }
  };
  cv.onmousemove = (e) => {
    if (!ins.geom) return;
    const s = ins.geom.sOf(e.offsetX);
    if (ins.drag) {
      const l = S.p.leads.find((x) => x.name === S.lead);
      l.contacts[ins.drag.i].s = Math.round(s * 20) / 20; l.contacts[ins.drag.i].fit_mode = 'manual'; drawInspector();
    }
    clearTimeout(ins.xsecTimer); ins.xsecTimer = setTimeout(() => showXsec(s), 60);
  };
  window.addEventListener('mouseup', async () => {
    if (!ins.drag) return;
    const { i } = ins.drag; ins.drag = null;
    const l = S.p.leads.find((x) => x.name === S.lead);
    await moveContact(i, l.contacts[i].s);
    selectContact(i);
  });
  window.addEventListener('resize', drawInspector);
}

// ---------- step 5: export ----------
function exportHTML() {
  const o = S.exportOpts, ready = S.p.coreg && S.p.leads.length;
  const opt = (k, t) => `<label class="row"><input type="checkbox" data-opt="${k}" ${o[k] ? 'checked' : ''}> ${t}</label>`;
  const e = S.lastExport, dsk = desktopApi();
  return `<h2>Export</h2>
  ${ready ? '' : '<p class="muted">Detect at least one lead first.</p>'}
  <div class="card">
    ${opt('tsv', 'Contacts table (<span class="mono">electrodes.tsv</span>, BIDS columns) + coordinate system; with plans also <span class="mono">plan_vs_delivered.tsv</span>')}
    <div class="row" style="margin-left:22px"><label>x, y, z in</label><select id="expSpace"><option value="mri" ${o.space === 'mri' ? 'selected' : ''}>MRI space</option>
      <option value="acpc" ${o.space === 'acpc' ? 'selected' : ''} ${S.p.acpc_matrix ? '' : 'disabled'}>AC/PC coordinates</option>
      ${modOn('stereotaxy') ? `<option value="frame" ${o.space === 'frame' ? 'selected' : ''} ${S.p.frame_from_display ? '' : 'disabled'}>frame coordinates</option>` : ''}</select>
      <span class="muted small">(AC/PC columns are always added when defined)</span></div>
    ${opt('leads_file', 'Results file (<span class="mono">.brainscope.json</span>, can be loaded again)')}
    ${opt('transforms', 'Transforms (4×4 text, ITK .tfm)')}
    ${opt('mri', 'MRI with the corrected orientation (NIfTI)')}
    ${opt('ct', 'CT resampled onto the MRI grid (NIfTI)')}
    ${opt('labels', 'Contact label volume on the MRI grid (NIfTI)')}
    <div class="row"><span class="spacer"></span><button class="primary" id="doExport" ${ready ? '' : 'disabled'}>Export</button></div>
  </div>
  ${e ? `<div class="card"><b>Written to</b> <span class="mono small">${esc(e.folder)}</span><ul class="files">
    ${e.files.map((f) => `<li>${dsk ? esc(f) : `<a href="${P(`/output/${encodeURIComponent(f)}`)}" download>${esc(f)}</a>`}</li>`).join('')}</ul>
    ${dsk ? '<button id="openFolder">Open folder</button>' : ''}</div>` : ''}
  <h3>Debug bundle</h3>
  <div class="card"><p class="small">A small zip to share when something goes wrong: both scans at about 2 mm, the CT at full
    resolution around each lead, and the project state. File names are removed.</p>
    <div class="row"><button id="debugBundle">Make debug bundle</button></div>
    ${S.debug ? `<p class="small">${dsk ? esc(`${S.debug.folder}/${S.debug.file}`) : `<a href="${P(`/output/${encodeURIComponent(S.debug.file)}`)}" download>${esc(S.debug.file)}</a>`} (${S.debug.size_mb} MB)
    ${dsk ? '<button id="openDebug">Open folder</button>' : ''}</p>` : ''}</div>
  <h3>Load results</h3>
  <div class="card"><p class="small">Load a <span class="mono">.brainscope.json</span> results file into this project (the coregistration must exist).</p>
    <label class="row small"><input type="checkbox" id="replace" ${S.replaceOnImport ? 'checked' : ''}> Replace all current leads (otherwise leads with the same name are replaced)</label>
    <div class="row"><button id="loadResults" ${S.p.coreg ? '' : 'disabled'}>Load results file…</button></div></div>`;
}
function bindExport() {
  $$('[data-opt]').forEach((c) => { c.onchange = () => { S.exportOpts[c.dataset.opt] = c.checked; }; });
  $('#expSpace').onchange = (e) => { S.exportOpts.space = e.target.value; };
  $('#doExport').onclick = async () => {
    const r = await busy('Exporting…', () => api(P('/export'), S.exportOpts));
    if (r) { S.lastExport = r; toast(`${r.files.length} files written.`, 'ok'); render(); }
  };
  if ($('#openFolder')) $('#openFolder').onclick = () => desktopApi()?.open_folder(S.lastExport.folder);
  $('#debugBundle').onclick = async () => {
    const r = await busy('Making the debug bundle…', () => api(P('/debug-bundle'), {}));
    if (r) { S.debug = r; render(); }
  };
  if ($('#openDebug')) $('#openDebug').onclick = () => desktopApi()?.open_folder(S.debug.folder);
  $('#replace').onchange = (e) => { S.replaceOnImport = e.target.checked; };
  $('#loadResults').onclick = () => {
    const inp = $('#leadsInput');
    inp.onchange = async () => {
      const f = inp.files[0]; inp.value = '';
      if (!f) return;
      await busy('Loading results…', async () => {
        const p = await postRaw(P(`/leads-import?replace=${S.replaceOnImport ? 1 : 0}`), f);
        await setProject(p); toast(`Loaded ${p.leads.length} leads.`, 'ok');
      });
    };
    inp.click();
  };
}

// ---------- presets editor ----------
const PRESET_FIELDS = [['id', 'Id (letters, digits, . _ -)', 'text'], ['name', 'Name', 'text'], ['manufacturer', 'Manufacturer', 'text'],
  ['n_contacts', 'Number of contacts', 'number'], ['contact_length_mm', 'Contact length (mm)', 'number'],
  ['diameter_mm', 'Diameter (mm)', 'number'], ['spacing_mm', 'Center-to-center spacing (mm): one value, or one per gap', 'text'],
  ['tip_offset_mm', 'Tip to first contact edge (mm)', 'number'], ['label_pattern', 'Label pattern ({lead}, {n})', 'text'],
  ['first_index', 'Number of the deepest contact', 'number'], ['material', 'Material', 'text'], ['source', 'Source', 'text'], ['notes', 'Notes', 'text']];
let presetSel = null;
async function openPresets() {
  S.presets = await api('/api/presets');
  presetSel = S.presets.find((p) => p.id === S.draft.preset) || S.presets[0];
  renderPresets(); $('#presetDlg').showModal();
}
function renderPresets(edit) {
  const p = edit || presetSel, ro = p.builtin && !edit;
  $('#presetDlg').innerHTML = `<div class="dlg"><div class="list">${S.presets.map((x) => `<div data-pid="${esc(x.id)}" class="${x.id === presetSel?.id && !edit ? 'sel' : ''}">${esc(x.name)}
    ${x.builtin ? '<span class="muted">· built-in</span>' : ''}</div>`).join('')}</div>
    <div class="form">${ro ? '<p class="muted small">Built-in presets are read-only. Duplicate one to make your own version.</p>' : ''}
    ${PRESET_FIELDS.map(([k, t, type]) => `<div class="row"><label>${t}</label><input type="${type === 'number' ? 'number' : 'text'}" step="any" data-f="${k}"
      value="${esc(Array.isArray(p[k]) ? p[k].join(', ') : p[k])}" ${ro ? 'disabled' : ''}></div>`).join('')}
    <canvas id="presetPreview"></canvas></div></div>
    <div class="dlgFoot"><button id="pNew">New</button><button id="pDup">Duplicate</button><button id="pImport">Import JSON…</button>
    <button id="pExport">Export JSON</button><span class="spacer"></span>
    ${ro ? '' : '<button id="pDelete" class="danger">Delete</button><button id="pSave" class="primary">Save</button>'}<button id="pClose">Close</button></div>`;
  drawPreset(p);
  $$('[data-pid]').forEach((el) => { el.onclick = () => { presetSel = S.presets.find((x) => x.id === el.dataset.pid); renderPresets(); }; });
  $$('[data-f]').forEach((el) => { el.oninput = () => { try { drawPreset(readPresetForm()); } catch (e) { /* partial input */ } }; });
  $('#pClose').onclick = () => { $('#presetDlg').close(); render(); };
  $('#pNew').onclick = () => renderPresets({ id: 'my-lead', name: 'My lead', manufacturer: '', n_contacts: 8, contact_length_mm: 2, diameter_mm: 0.8,
    spacing_mm: 3.5, tip_offset_mm: 0, label_pattern: '{lead}{n}', first_index: 1, material: '', source: '', notes: '' });
  $('#pDup').onclick = () => renderPresets({ ...presetSel, id: `${presetSel.id}-copy`, name: `${presetSel.name} (copy)`, builtin: false });
  $('#pExport').onclick = () => {
    const p2 = { ...presetSel }; delete p2.builtin;
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(p2, null, 2)], { type: 'application/json' }));
    a.download = `${p2.id}.json`; a.click();
  };
  $('#pImport').onclick = () => {
    const inp = $('#presetInput');
    inp.onchange = async () => { const f = inp.files[0]; inp.value = ''; if (f) renderPresets({ ...JSON.parse(await f.text()), builtin: false }); };
    inp.click();
  };
  if ($('#pSave')) $('#pSave').onclick = async () => {
    try {
      const p2 = readPresetForm();
      S.presets = await api('/api/presets', p2); presetSel = S.presets.find((x) => x.id === p2.id); S.draft.preset = p2.id;
      toast('Preset saved.', 'ok'); renderPresets();
    } catch (e) { toast(e.message, 'error'); }
  };
  if ($('#pDelete')) $('#pDelete').onclick = async () => {
    if (!edit && confirm(`Delete preset ${presetSel.name}?`)) {
      try { S.presets = await api('/api/presets/delete', { id: presetSel.id }); presetSel = S.presets[0]; renderPresets(); } catch (e) { toast(e.message, 'error'); }
    } else if (edit) renderPresets();
  };
}
function readPresetForm() {
  const p = {};
  PRESET_FIELDS.forEach(([k, , type]) => { const v = $(`[data-f="${k}"]`).value.trim(); p[k] = type === 'number' ? Number(v) : v; });
  const sp = String(p.spacing_mm).split(/[,\s]+/).filter(Boolean).map(Number);
  p.spacing_mm = sp.length === 1 ? sp[0] : sp;
  return p;
}
function drawPreset(p) {
  const cv = $('#presetPreview'); if (!cv) return;
  const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const n = Number(p.n_contacts) || 1, sp = Array.isArray(p.spacing_mm) ? p.spacing_mm : Array(n - 1).fill(Number(p.spacing_mm));
  const L = Number(p.contact_length_mm), off = Number(p.tip_offset_mm) || 0, d = Number(p.diameter_mm);
  const centers = [off + L / 2]; sp.forEach((x) => centers.push(centers[centers.length - 1] + Number(x)));
  const total = centers[centers.length - 1] + L / 2 + 6, k = (W - 20) / total, cy = H / 2, r = Math.max(3, d * k / 2);
  g.fillStyle = '#3a4150'; g.fillRect(10, cy - r, total * k, 2 * r);
  g.fillStyle = '#d6dbe4'; centers.forEach((c, i) => {
    g.fillRect(10 + (c - L / 2) * k, cy - r - 1, L * k, 2 * r + 2);
    g.fillStyle = '#8b93a1'; g.font = '10px sans-serif'; g.textAlign = 'center';
    g.fillText(String((Number(p.first_index) || 0) + i), 10 + c * k, cy - r - 5); g.fillStyle = '#d6dbe4';
  });
  g.fillStyle = '#8b93a1'; g.textAlign = 'left'; g.fillText(`tip · total shown ${fmt(total, 1)} mm`, 10, H - 4);
}

// ---------- navigation and keys ----------
async function goStep(step) {
  S.step = step;
  if (step === 'leads') {  // base image: the finer scan (a coarse base would pixelate the other one)
    const vol = (r) => (S.p?.scans[r] ? S.p.scans[r].spacing.reduce((x, y) => x * y, 1) : Infinity);
    S.ctWindow = 'metal'; S.base = vol('mri') <= 2 * vol('ct') ? 'mri' : 'ct';
  }
  if (step === 'frame') { S.ctWindow = 'bone'; S.base = 'ct'; }
  if (step === 'brain') S.base = 'mri';
  if (step === 'acpc') S.base = 'mri';
  if (step === 'coreg') { S.ctWindow = 'bone'; S.base = 'mri'; }
  $('#ctWindow').value = S.ctWindow;
  $$('#base button').forEach((b) => b.classList.toggle('on', b.dataset.base === S.base));
  render(); await refreshViewer();
  if (step === 'leads' && S.lead) await loadLeadView();
}
document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, select, textarea') || $('#presetDlg').open) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 'z' && !e.shiftKey) { e.preventDefault(); $('#undo').click(); return; }
  if ((e.ctrlKey || e.metaKey) && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); $('#redo').click(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (S.step === 'leads' && k === 't') setPoint('tip');
  if (S.step === 'leads' && k === 'e') setPoint('entry');
  if (S.step === 'acpc' && ['a', 'p', 'm'].includes(k)) setAcpcPoint({ a: 'ac', p: 'pc', m: 'msp' }[k]);
  if (k === 'o') { S.ctVisible = !S.ctVisible; refreshViewer(); }
  if (k === '+' || k === '=') zoom(1.4);
  if (k === '-') zoom(1 / 1.4);
  if (k === '0') zoom(0);
});
$$('#layout button').forEach((b) => { b.onclick = () => setLayout(b.dataset.layout); });
$('#ctWindow').onchange = (e) => { S.ctWindow = e.target.value; S.win.ct = null; refreshViewer(); };
$('#winRole').onchange = (e) => { S.winRole = e.target.value; refreshViewer(); };
$('#winLo').onchange = setWindow;
$('#winHi').onchange = setWindow;
$('#winAuto').onclick = () => { S.win[S.winRole] = null; refreshViewer(); };
$$('#base button').forEach((b) => { b.onclick = () => setBase(b.dataset.base); });
$('#centerView').onclick = centerView;
[['#slX', 0], ['#slY', 1], ['#slZ', 2]].forEach(([id, i]) => {
  $(id).oninput = (e) => {
    if (!nv.volumes.length) return;
    const p = [...nv.scene.crosshairPos]; p[i] = Number(e.target.value);
    nv.scene.crosshairPos = p; nv.drawScene(); updateCoords();
  };
});
$('#zoomIn').onclick = () => zoom(1.4);
$('#zoomOut').onclick = () => zoom(1 / 1.4);
$('#zoomFit').onclick = () => zoom(0);
$('#ctOpacity').oninput = (e) => { S.ctOpacity = Number(e.target.value); S.ctVisible = true; refreshViewer(); };
$('#projectSelect').onchange = (e) => openProject(e.target.value);
$('#newProject').onclick = newProject;
$('#demoProject').onclick = demoProject;
$('#undo').onclick = () => act('/undo', {});
$('#redo').onclick = () => act('/redo', {});
bindInspector();

(async function start() {
  await nvReady;
  setLayout('multi');
  try {
    await loadState();
    S.presets = await api('/api/presets');
  } catch (e) { toast(e.message, 'error'); }
  render();
})();
