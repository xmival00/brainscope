'use strict';
/* brainscope UI: plain JavaScript + Niivue. Talks to the local engine through /api. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? '–' : Number(v).toFixed(d));
const hkey = (o) => { const s = JSON.stringify(o); let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); };
const LEAD_COLORS = ['#ff5d5d', '#3fa7ff', '#3ecf8e', '#f0b429', '#c77dff', '#ff8fab', '#5ce1e6', '#ffd166', '#a3e635', '#fb923c'];

const S = {
  state: null, p: null, step: 'import', presets: [], orientRole: 'mri', layout: 'multi',
  ctWindow: 'bone', ctOpacity: 0.5, ctVisible: true, base: 'mri', win: { mri: null, ct: null }, winRole: 'ct', lead: null, contact: null, view: null,
  draft: { name: '', preset: '', hemisphere: 'n/a', curved: false, tip: null, entry: null },
  loaded: { base: null, contacts: null }, cropRadius: 70, useCrop: false, nudgeMm: 1, nudgeDeg: 1,
  exportOpts: { tsv: true, leads_file: true, transforms: true, mri: false, ct: false, labels: false },
  lastExport: null, replaceOnImport: false,
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
});
let nvReady = nv.attachToCanvas($('#gl'));
nv.onLocationChange = () => updateCoords();

function crosshairMM() {
  if (!nv.volumes.length) return null;
  const m = nv.frac2mm(nv.scene.crosshairPos, 0, true);
  return [m[0], m[1], m[2]];
}
function updateCoords() {
  const m = crosshairMM();
  const space = S.step === 'orient' ? `${S.orientRole.toUpperCase()} (corrected)` : 'MRI';
  $('#coords').textContent = m ? `${space}  x ${fmt(m[0], 1)}  y ${fmt(m[1], 1)}  z ${fmt(m[2], 1)} mm` : '';
}
function goTo(mm) {
  if (!nv.volumes.length) return;
  nv.scene.crosshairPos = nv.mm2frac(mm);
  nv.drawScene(); nv.createOnLocationChange?.(); updateCoords();
}
function setLayout(l) {
  S.layout = l;
  $$('#layout button').forEach((b) => b.classList.toggle('on', b.dataset.layout === l));
  nv.setSliceType({ multi: nv.sliceTypeMultiplanar, axial: nv.sliceTypeAxial, coronal: nv.sliceTypeCoronal,
    sagittal: nv.sliceTypeSagittal, render: nv.sliceTypeRender }[l]);
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
  const k = scanKey(v.role);
  return { url: `${P(`/vol/${v.role}.nii`)}?v=${k}`, name: `${v.role}-${k}.nii`, colormap: v.colormap,
    opacity: v.opacity ?? 1, ...(v.cal_min !== undefined ? { cal_min: v.cal_min, cal_max: v.cal_max } : {}) };
}
function contactsKey() {
  const rows = S.p.leads.flatMap((l) => l.contacts.map((c) => [c.x, c.y, c.z]));
  return rows.length && S.p.scans.mri && S.p.coreg && S.step !== 'orient' && S.step !== 'import' ? hkey([rows, scanKey('mri')]) : null;
}
async function refreshViewer() {
  await nvReady;
  $('#empty').hidden = !!(S.p && (S.p.scans.mri || S.p.scans.ct));
  $('#empty').textContent = S.p ? 'Import an MRI and a CT to start.' : 'Create or open a project to start.';
  const vols = wantedVolumes();
  vols.forEach((v) => { if (S.win[v.role]) [v.cal_min, v.cal_max] = S.win[v.role]; });  // the user's own window
  const base = hkey([S.p?.name, vols.map((v) => [v.role, scanKey(v.role), v.colormap])]);
  const ck = S.p ? contactsKey() : null;
  try {
    if (base !== S.loaded.base) {
      S.loaded = { base, contacts: null };
      await nv.loadVolumes(vols.map(volSpec));
    } else {
      vols.forEach((v, i) => {
        const vol = nv.volumes[i];
        if (!vol) return;
        if (v.cal_min !== undefined) { vol.cal_min = v.cal_min; vol.cal_max = v.cal_max; }
        else if (vol.robust_min !== undefined) { vol.cal_min = vol.robust_min; vol.cal_max = vol.robust_max; }
        vol.opacity = v.opacity ?? 1;
      });
      if (nv.volumes.length) nv.updateGLVolume();
    }
    if (ck !== S.loaded.contacts) {
      while (nv.volumes.length > vols.length) nv.removeVolume(nv.volumes[nv.volumes.length - 1]);
      if (ck) {
        await nv.addVolumeFromUrl({ url: `${P('/vol/contacts.nii.gz')}?v=${ck}`, name: `contacts-${ck}.nii.gz`, opacity: 1 });
        const cm = { R: [0], G: [0], B: [0], A: [0], I: [0], labels: [''] };
        S.p.leads.forEach((l, i) => {
          const h = leadColor(i);
          cm.R.push(parseInt(h.slice(1, 3), 16)); cm.G.push(parseInt(h.slice(3, 5), 16)); cm.B.push(parseInt(h.slice(5, 7), 16));
          cm.A.push(255); cm.I.push(i + 1); cm.labels.push(l.name);
        });
        nv.volumes[nv.volumes.length - 1].setColormapLabel(cm);
        nv.updateGLVolume();
      }
      S.loaded.contacts = ck;
    }
  } catch (e) { toast(`Viewer: ${e.message}`, 'error'); }
  $('#ctCtl').style.visibility = vols.length > 1 ? 'visible' : 'hidden';
  syncWindow(vols);
  updateCoords();
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
  sel.innerHTML = '<option value="">— select a project —</option>' + S.state.projects.map((n) => `<option>${esc(n)}</option>`).join('');
  if (S.p) sel.value = S.p.name;
}
async function openProject(name) {
  if (!name) { S.p = null; S.lead = null; render(); refreshViewer(); return; }
  await busy('Opening project…', async () => { await setProject(await api(`/api/p/${encodeURIComponent(name)}`)); });
}
async function setProject(p) {
  const changed = S.p?.name !== p.name;
  S.p = p;
  if (changed) { S.lead = null; S.contact = null; S.view = null; S.loaded = { base: null, contacts: null }; S.win = { mri: null, ct: null }; S.step = firstOpenStep(); }
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
  return p.leads.length ? 'leads' : 'coreg';
}
async function newProject() {
  const name = prompt('Name of the new project (letters, digits, space, . _ -):');
  if (!name) return;
  await busy('Creating project…', async () => { const p = await api('/api/projects', { name }); await loadState(); await setProject(p); });
}
async function demoProject() {
  await busy('Building the synthetic demo case (about 10 s)…', async () => {
    const p = await api('/api/demo', {}); await loadState(); await setProject(p);
    toast('Demo: fix the orientation of both scans first (Orient step). The CT marker is on the subject\'s left.', 'info', 12000);
  });
}

// ---------- rendering ----------
function render() {
  $$('#steps button').forEach((b) => {
    b.classList.toggle('on', b.dataset.step === S.step);
    b.disabled = !S.p;
    b.querySelector('i').className = stepStatus(b.dataset.step);
  });
  const panel = $('#panel');
  if (!S.p) { panel.innerHTML = welcomeHTML(); bindWelcome(); $('#inspector').hidden = true; return; }
  panel.innerHTML = { import: importHTML, orient: orientHTML, coreg: coregHTML, leads: leadsHTML, export: exportHTML }[S.step]();
  ({ import: bindImport, orient: bindOrient, coreg: bindCoreg, leads: bindLeads, export: bindExport })[S.step]();
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
    case 'leads': return p.leads.length ? 'done' : 'todo';
    default: return 'todo';
  }
}
function welcomeHTML() {
  const list = S.state?.projects || [];
  return `<h2>Welcome</h2>
  <p>brainscope coregisters a CT to an MRI and finds the contacts of intracranial leads. Coordinates are reported in the
  orientation-corrected MRI space. <b>Research use only.</b></p>
  <div class="row"><button class="primary" id="wNew">New project</button><button id="wDemo">Open a demo case</button></div>
  <h3>Projects</h3>${list.length ? list.map((n) => `<div class="leadItem" data-open="${esc(n)}">${esc(n)}</div>`).join('') : '<p class="muted">No projects yet.</p>'}
  <p class="muted small">Projects are stored in ${esc(S.state?.projects_root || '')}</p>`;
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
  <div class="row"><span class="spacer"></span><button class="primary" id="next" ${S.p.scans.mri && S.p.scans.ct ? '' : 'disabled'}>Next: orient</button></div>
  <div id="seriesPick"></div>`;
}
function bindImport() {
  $$('[data-nifti]').forEach((b) => { b.onclick = () => importNifti(b.dataset.nifti); });
  $$('[data-dicom]').forEach((b) => { b.onclick = () => importDicom(b.dataset.dicom); });
  $('#species').onchange = (e) => act('/species', { species: e.target.value });
  $('#next').onclick = () => goStep('orient');
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
  <div class="row"><span class="spacer"></span><button class="primary" id="next">Next: coregister</button></div>` : '<p class="muted">Import this scan first.</p>'}`;
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
      goStep('coreg');
    };
  }
}

// ---------- step 3: coregister ----------
function coregHTML() {
  const c = S.p.coreg, ready = S.p.scans.mri && S.p.scans.ct;
  const axes = [['x', 'left–right'], ['y', 'back–front'], ['z', 'down–up']];
  return `<h2>Coregister CT to MRI</h2>
  ${ready ? '' : '<p class="muted">Import both scans first.</p>'}
  <p>Rigid alignment (mutual information). Check it with the CT overlay (opacity slider above, <kbd>O</kbd> toggles) before accepting.</p>
  <div class="card">
    <label class="row"><input type="checkbox" id="useCrop" ${S.useCrop ? 'checked' : ''}> Only use a box around the crosshair, half-size
      <input type="number" id="cropR" value="${S.cropRadius}" min="20" max="200"> mm</label>
    <p class="muted small">Useful for pigs: put the crosshair in the middle of the brain so muscles and sinuses do not dominate.</p>
    <div class="row"><button class="primary" id="runCoreg" ${ready ? '' : 'disabled'}>${c ? 'Run again' : 'Run coregistration'}</button>
    <button id="refine" ${c ? '' : 'disabled'}>Refine from current</button></div>
  </div>
  ${c ? `<div class="card"><div class="kv"><span>Method</span><span>${esc(c.method)}</span><span>Metric</span><span>${c.metric ?? '–'}</span>
     <span>Status</span><span>${c.accepted ? '<span class="badge ok">accepted</span>' : '<span class="badge warn">not accepted</span>'}</span></div></div>` : ''}
  <h3>Adjust by hand</h3>
  <div class="row"><label>step</label><select id="nMm">${[0.25, 0.5, 1, 2, 5].map((v) => `<option ${v === S.nudgeMm ? 'selected' : ''}>${v}</option>`).join('')}</select> mm
    <select id="nDeg">${[0.25, 0.5, 1, 2, 5].map((v) => `<option ${v === S.nudgeDeg ? 'selected' : ''}>${v}</option>`).join('')}</select> °</div>
  ${axes.map(([a, n], i) => `<div class="row"><label>${n}</label><button data-t="${i}" data-s="-1">−</button><button data-t="${i}" data-s="1">+</button>
    <span class="muted small" style="margin-left:6px">rotate</span><button data-r="${i}" data-s="-1">↺</button><button data-r="${i}" data-s="1">↻</button></div>`).join('')}
  <div class="row"><span class="spacer"></span>${c ? `<button id="accept" class="${c.accepted ? '' : 'primary'}">${c.accepted ? 'Accepted ✓' : 'Accept alignment'}</button>` : ''}
  <button id="next" ${c ? '' : 'disabled'}>Next: leads</button></div>`;
}
function bindCoreg() {
  $('#useCrop').onchange = (e) => { S.useCrop = e.target.checked; };
  $('#cropR').onchange = (e) => { S.cropRadius = Number(e.target.value); };
  const body = (from_current) => ({ from_current, ...(S.useCrop ? { crop_center: crosshairMM(), crop_radius: S.cropRadius } : {}) });
  $('#runCoreg').onclick = () => act('/coreg', body(false), 'Coregistering (usually under a minute)…');
  $('#refine').onclick = () => act('/coreg', body(true), 'Refining…');
  $('#nMm').onchange = (e) => { S.nudgeMm = Number(e.target.value); };
  $('#nDeg').onchange = (e) => { S.nudgeDeg = Number(e.target.value); };
  $$('[data-t]').forEach((b) => { b.onclick = () => { const t = [0, 0, 0]; t[b.dataset.t] = S.nudgeMm * b.dataset.s; act('/nudge', { trans: t }); }; });
  $$('[data-r]').forEach((b) => { b.onclick = () => { const r = [0, 0, 0]; r[b.dataset.r] = S.nudgeDeg * b.dataset.s; act('/nudge', { rot: r }); }; });
  if ($('#accept')) $('#accept').onclick = () => act('/coreg-accept', { accepted: !S.p.coreg.accepted });
  $('#next').onclick = () => goStep('leads');
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
    <button id="anchor" title="Keep this contact here and fit the others to it">Fit others to it</button></div>` : ''}`;
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
    ${opt('tsv', 'Contacts table (<span class="mono">electrodes.tsv</span>, BIDS columns, MRI space) + coordinate system')}
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
  if (step === 'leads') { S.ctWindow = 'metal'; S.base = 'ct'; }
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
  if (k === 'o') { S.ctVisible = !S.ctVisible; refreshViewer(); }
});
$$('#steps button').forEach((b) => { b.onclick = () => goStep(b.dataset.step); });
$$('#layout button').forEach((b) => { b.onclick = () => setLayout(b.dataset.layout); });
$('#ctWindow').onchange = (e) => { S.ctWindow = e.target.value; S.win.ct = null; refreshViewer(); };
$('#winRole').onchange = (e) => { S.winRole = e.target.value; refreshViewer(); };
$('#winLo').onchange = setWindow;
$('#winHi').onchange = setWindow;
$('#winAuto').onclick = () => { S.win[S.winRole] = null; refreshViewer(); };
$$('#base button').forEach((b) => { b.onclick = () => { S.base = b.dataset.base; $$('#base button').forEach((x) => x.classList.toggle('on', x === b)); refreshViewer(); }; });
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
