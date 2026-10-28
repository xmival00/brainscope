# brainscope: plan and requirements

Status: version 0.1.0 implemented (see `README.md`); this file keeps the plan and the decisions. Research use only,
never for clinical decisions.

## 1. Goal

A desktop app (browser technology in its own window, Windows and macOS installers) that:

- **A.** coregisters a CT to an MRI (rigid), for humans and pigs, and
- **B.** localizes the contacts of intracranial leads: a person defines each lead, the app finds its
  contacts, the person corrects them.

The output is the contact coordinates in the MRI space, plus the coregistered volumes if the user asks for them.
It is built so the same UI and engine can later be embedded in the lab dashboard.

## 2. Decisions

From the user (2026-10-28):

| # | Decision |
|---|---|
| D1 | Research use only. No clinical decisions. |
| D2 | v1 = CT-to-MRI coregistration + lead/contact localization, for humans and pigs. No dog-specific work: dogs are covered by the generic animal support. |
| D3 | Multi-subject projects, annotation of brain structures and building new (pig) atlases are **later**. v1 must not block them (section 9), but does not build them. |
| D4 | Coordinates are given in the MRI space. For animals that is the **orientation-corrected** MRI space. |
| D5 | The user sets the orientation of **each scan** separately (CT and MRI): which way is up, which side is left, which way is front. |
| D6 | The user decides which side is truly left (a marker is in the CT). The app only has to make this easy and visible. Display: the subject's left is on the left of the screen. **(Confirm: this means neurological display, the opposite of the radiological convention.)** |
| D7 | Leads are defined by a person (how many, where each starts and ends). The automated part finds the contacts and places them; the person can fix them. Detection works in planes perpendicular to the lead, in several stages. |
| D8 | Write our own detection. Use existing tools only where they enable this approach (libraries are fine; whole external pipelines are not the plan). |
| D9 | Output: a contact table as a **TSV** compatible with BIDS `electrodes.tsv` (export only). Full BIDS datasets are not needed now. Results are also saved and loaded in our own format (`.brainscope.json`). |
| D10 | Input: NIfTI and DICOM. DICOM is converted inside the app; the user never has to deal with it. |
| D11 | No code signing for now. The OS warning on first start is acceptable. |
| D12 | Repository: `xmival00/brainscope` for now, may move to `bnelair` later. |
| D13 | Credits are limited: work in small steps that are each committed and pushed, so work can resume anywhere (see `CLAUDE.md`). |
| D14 | Lead presets are made by the user in the app. Leads in use are standard DBS leads (e.g. Cadence Neuroscience, Medtronic): contacts are often not separable on CT, so contacts are placed from the preset geometry; contact length and spacing must be exact. Directional (segmented) leads: level positions only, no segment orientation. |
| D15 | The dashboard will embed the UI and engine as they are (plain JS + local HTTP API). |
| D16 | Full scans cannot be shared with the assistant: the app makes a small debug bundle (scans at ~2 mm, full-resolution CT crops around each lead, project state without file names) to share instead. |
| D17 | Releases: only the installers (and SHA256SUMS) are stored, as release assets. A tag `v*` makes a draft pre-release that is published only when every platform built and passed its self-test; no workflow artifacts. |
| D18 | Import also MGZ/MGH, NRRD, MHA/MHD (converted to NIfTI once, on import). DICOM: dcm2niix first, SimpleITK's reader if dcm2niix cannot run (seen on a user machine); the self-test of each installer runs both. |
| D19 | Orientation aids: labels on all four sides of each view, Niivue's orientation cube, and a small picture of how each view should look. |
| D20 | CT intensity: at import the CT is classified as Hounsfield units, HU with an offset (e.g. 0-4095) or unknown scale; display and registration windows follow it. The user can always set the display window (min/max per volume) by hand. The CT is overlaid from the Import step on, placed by its header until coregistered. dcm2niix sidecars are read leniently (a Siemens CT had invalid JSON). |
| D21 | macOS: Apple silicon only; no Intel Mac build. |
| D22 | Large scans: the viewer gets a display copy (CT cropped to the MRI's field of view once coregistered, then averaged down to at most 40 M voxels; MRI up to 100 M); a 512x512x1137 CT failed to load otherwise. Engine always uses the full scan. |
| D23 | Coregistration: a rough alignment by one landmark clicked in each scan (CT and MRI scanner coordinates can be unrelated, and the CT can cover the whole body), then the automatic step starts from the current alignment; "Start over" resets it. A "Center" button brings the crosshair back into the image. |
| D24 | Viewer zoom around the crosshair (buttons, keys + − 0); the mouse wheel keeps scrolling slices. Coregistration region of interest: a box (edge lengths per axis, default by species) saved in the project and shown as a green outline; only the MRI inside it is compared. |

Decided during implementation (the user delegated these, 2026-10-28):

| # | Proposal | Why |
|---|---|---|
| P1 | Engine in Python: `nibabel` (NIfTI files and headers), `SimpleITK` (registration, resampling), `numpy`/`scipy`, `dcm2niix` (DICOM). | Mature, wheels for Windows/macOS/Linux, small enough for an installer. Nonlinear tools (ANTsPy) are added only when atlas work starts. |
| P2 | UI in plain JavaScript (no framework, no build step) with [Niivue](https://github.com/niivue/niivue) (WebGL2 NIfTI viewer), vendored. | Built for NIfTI, fast, has drawing tools for the later annotation work. No build toolchain to maintain or pay for in CI. |
| P3 | Desktop shell: pywebview (WebView2 on Windows, WKWebView on macOS) showing the web UI, engine in the same process, packaged with PyInstaller; falls back to the default browser. | Far smaller than Qt WebEngine or Electron. Installer pipeline follows [mef3io-studio](https://github.com/bnelair/mef3io-studio). |
| P4 | License Apache-2.0. | Matches mef3io-studio. Do not copy code from GPL tools (e.g. Lead-DBS); reimplement from papers. |

## 3. Architecture

```
UI (TypeScript + Niivue)
  Import | Orient | Coregister | Leads | Export
        |  local HTTP API (the same API later serves the dashboard)
Engine (Python)
  files and headers | orientation | transforms | registration
  lead detection | presets | export | DICOM conversion
  also usable as a CLI and a Python library (batch, tests)
```

- Desktop: one window, engine runs locally, no network, nothing leaves the computer.
- Dashboard (later): the same UI bundle embedded; the engine runs as a service. Keep API calls stateless (all
  state in the project file) so this needs no redesign.
- Volumes go to the viewer as NIfTI files served by the engine; the viewer never parses DICOM.

## 4. Coordinate conventions (the part that must never be wrong)

- World coordinates are **RAS+ in mm** (the NIfTI convention) everywhere in files and outputs.
  SimpleITK works in LPS internally: convert in **one** module only, covered by round-trip tests.
- NIfTI header: read the sform if its code is > 0, else the qform. Write both, identical.
- Spaces:
  - `CT-raw`, `MRI-raw`: as stored in the input files.
  - `CT-corrected`, `MRI-corrected`: after the user's orientation fix. **`MRI-corrected` is the output space.**
- Orientation fix = a 4x4 signed permutation (90-degree rotations and flips) applied to the header affine, about
  the volume center. **Voxels are never resampled for this.** One matrix is stored per scan.
- Coregistration result = one rigid 4x4 `CT-corrected -> MRI-corrected`.
- Contacts are found in CT voxels (no interpolation blur), then mapped:
  CT voxel -> `CT-corrected` world -> (rigid) -> `MRI-corrected` world -> MRI voxel (i, j, k).
- Everything is kept as matrices and composed. Images are resampled once, only when the user exports them.

## 5. Workflow (v1, one subject at a time)

### 5.1 Import
- NIfTI files, or a DICOM folder: `dcm2niix` lists the series (description, modality, size, voxel size), the user
  picks one, it is converted into the project folder (with its JSON sidecar). Inputs are never modified.
- dcm2niix also handles CT gantry tilt.

### 5.2 Orient (each scan separately, CT and MRI)
- Three views with live direction labels on the edges.
- The user sets: which way is **up** (dorsal/superior), which way is **front** (rostral/anterior), which side is
  **left**. Buttons: rotate 90 degrees, flip left/right, up/down, front/back.
- Up + front fix the rotation; left fixes the handedness (mirror). Left is always the user's explicit decision,
  recorded per scan (with an optional note, e.g. "CT marker").
- Saved as the per-scan matrix of section 4; the corrected NIfTI can be exported.

### 5.3 Coregister CT to MRI
- Rigid, 6 degrees of freedom, Mattes mutual information, coarse-to-fine.
- Start: align the head centers. Species preset (human, pig) sets smoothing and step sizes in mm.
- CT intensities clipped to a soft-tissue/bone window; metal masked out so artifacts do not pull the fit.
- Optional crop box drawn by the user around the skull/brain: important for pigs, where muscle and sinuses
  dominate the head.
- Quality check: blend slider, checkerboard, CT bone/metal outline on the MRI. Manual nudge (move/rotate),
  then re-run from there. User accepts.
- Export (optional): the transform (4x4 text and ITK `.tfm`), the CT resampled into the MRI grid, the
  orientation-corrected MRI.

### 5.4 Leads (multi-stage, in planes perpendicular to the lead)
1. **Define (user):** click the lead's tip and its entry point (any view), choose a preset, name it. The user
   decides how many leads there are.
2. **Refine the axis:** step along the tip-entry line (every 0.25-0.5 mm); in each perpendicular plane find the
   center of the metal within a small radius around the expected position (tracking, so crossing leads are not
   picked up). Fit a straight line; a smooth curve if the lead bends (option). Refine the tip where the metal
   signal ends.
3. **Reslice:** build a "straightened lead" volume: a stack of perpendicular planes along the fitted axis, lead
   along the z axis. Shown to the user as a cross-section (scroll along the lead) and two lengthwise views.
4. **Fit contacts:** take the intensity profile along the axis. Model it from the preset (contact length,
   spacing, offset from the tip) as contact boxes blurred by the CT resolution. Fit the shift along the axis (and
   a small stretch, about +/-3%). Where contacts are clearly separate on the CT, refine each center locally;
   where they are not (contacts too close for the CT resolution), place them from the geometry and mark them
   `geometry`. Each contact gets a confidence value.
5. **Correct (user):** drag one contact, shift all, change the preset, lock contacts and re-fit the rest. Views:
   cross-section, lengthwise views, profile plot with the fitted model.
6. **Map** the contact centers to `MRI-corrected` (section 4).

The straightened volume is computed by the engine and shown in the viewer as an ordinary volume in "lead space";
clicks are mapped back through its known matrix. This does not depend on the viewer supporting oblique slices.

### 5.5 Lead presets
- JSON files. Fields: `id`, `name`, `manufacturer`, `type` (depth for now), `n_contacts`, `contact_length_mm`,
  `diameter_mm`, `spacing` (one pitch in mm, or a list of contact-center distances from the tip),
  `tip_to_first_contact_mm`, `label_pattern` (e.g. `{lead}{index}`), `source` (datasheet and revision), `notes`.
- Built-in presets are read-only and cite their datasheet; the user copies one to edit it, or makes a new one.
  User presets live in the per-user app-data folder; import and export as JSON.
- Editor: a form with a live drawing of the lead to scale.
- A project stores a copy of every preset it used, so editing a preset later never changes old results.

### 5.6 Export
- Contact table, TSV, columns in this order:
  - BIDS `electrodes.tsv` columns: `name`, `x`, `y`, `z`, `size`, `group` (lead name), `type`, `material`,
    `manufacturer`, `hemisphere`;
  - extra columns: `lead`, `contact_index`, `mri_i`, `mri_j`, `mri_k`, `ct_x`, `ct_y`, `ct_z`, `fit_mode`
    (`fitted`/`geometry`/`manual`), `confidence`.
  - `x, y, z` = `MRI-corrected` world, RAS, mm.
- A JSON sidecar describing the space: MRI file name and SHA-256, the corrected affine, RAS, mm, app version.
- Optional: a label volume of the leads/contacts in the MRI grid; the coregistered volumes (5.3).

### 5.7 Project
- One folder: `project.json` (subjects, scans, per-scan orientation matrix and laterality note, transforms,
  leads, preset copies, provenance), `scans/` (converted NIfTI), `outputs/`.
- The schema already has a list of subjects, even if the v1 UI works on one at a time.
- Autosave; undo/redo; inputs are never modified.

## 6. Requirements

- Platforms: Windows 10/11 x64, macOS 14+ Apple silicon (Intel if the pipeline gives it for free), Linux optional.
- Offline. No paths, names or IDs in logs.
- Unsigned installers; README explains the first-start warning (as mef3io-studio does).
- Targets (to be calibrated on real data; current values are guesses):
  - rigid coregistration under 60 s on a laptop CPU;
  - a 10-lead case from import to export in under 10 minutes;
  - median contact error under 1 mm against reference positions (thin-slice CT).
- Input expectations: CT slices of 1 mm or thinner to separate contacts; otherwise contacts are placed from the
  preset geometry and marked so.

## 7. Development data

| Use | Source | Notes |
|---|---|---|
| Human SEEG: coregistration and detection with reference contacts | Brainstorm SEEG tutorial data (pre-implant T1 + post-implant CT, NIfTI, contact positions), e.g. Zenodo record 14807262 | check license before using in tests; download by script, never commit |
| Human SEEG, extra | Brainstorm contact-localization tutorial (`preMRI.nii`, `postCT.nii`) | |
| Pig MRI anatomy | Illinois piglet atlas v2.1 (4- and 12-week, NIfTI, registration required) | piglets, not adults; no CT |
| Pig CT + MRI with leads | none public found | user provides private cases, kept outside the repo (`data/` is git-ignored) |
| Ground truth for detection | synthetic phantoms: a CT-like volume with a lead of known geometry along a random oblique axis, blurred, with noise and metal blooming | built by the test suite |
| Ground truth for orientation | real volumes with random 90-degree rotations/flips written into the header | built by the test suite |

## 8. Milestones

Each milestone is cut into steps that end with passing tests, a commit and a push (see `CLAUDE.md`). Engine
first: it is testable without a UI, cheap to develop, and does not depend on the shell decision.

| | Scope | Done when |
|---|---|---|
| M0 | Repository, this plan, `CLAUDE.md` | pushed |
| M1 | Engine core: NIfTI read/write, orientation matrices, transform composition, RAS/LPS layer; CLI `info`, `orient` | round-trip and random-orientation tests pass |
| M2 | Lead detection core on phantoms: preset schema, axis refinement, straightened reslice, profile fit; CLI `detect` | phantom contacts found within 0.25 mm; geometry-only mode works |
| M3 | Coregistration core: rigid MI, metal mask, crop box, species presets; CLI `coreg`, volume export | Brainstorm data aligned; checked against its reference |
| M4 | DICOM import via dcm2niix; data fetch script | a DICOM series converts and opens with the right geometry |
| M5 | Shell spike: Qt WebEngine window + Niivue + local API, installers for Windows and macOS in GitHub Actions | installers open a NIfTI on both systems (else switch to Electron) |
| M6 | UI workflow: Import, Orient, Coregister (quality check, nudge), Leads (perpendicular views, correction), Export | a full human case done in the app |
| M7 | Preset editor, project save/reopen, autosave, undo | |
| M8 | Validation on human public data and the user's pig cases; user docs; v0.1 release | targets of section 6 met or re-set with the user |

Status (2026-10-28): M0-M7 done. M8: validated on synthetic data (see `README.md`); validation on real human and
pig scans is open. Installers are built by `.github/workflows/build.yml` (manual run or tag `v*`).

## 9. Later (context only, not v1)

Multi-subject projects; annotating brain structures as 3D label volumes; building new pig atlases (population
template, labels propagated and fused across subjects, probability maps, versioned atlas package); labeling every
contact with an atlas structure; human MNI normalization and FreeSurfer/FastSurfer/SynthSeg import; dashboard
service mode; ML assistance trained on the collected corrections.

What v1 already does so these plug in:
- the viewer and project already handle a label volume with a label table (the lead mask);
- spaces and transforms are explicit and composable, so an atlas space is one more transform (nonlinear warps
  added later with ANTsPy);
- `project.json` already has a list of subjects;
- the engine API is stateless, so it can run as a service.

## 10. Open questions

1. Display: confirm "left is on left" = neurological display (D6). Implemented that way.
