# brainscope: assistant context

Desktop app (web UI in its own window) for CT-to-MRI coregistration and semi-automatic localization of lead
contacts, humans and pigs. Research use only. The plan, decisions and milestones are in `docs/PLAN.md`: read it
first, and record new decisions there (section 2).

## Hard rules
- World coordinates are RAS+ mm. Outputs are in the orientation-corrected MRI space. RAS/LPS conversion lives in
  one module only.
- Orientation fixes change the header affine only, never the voxels. Volumes are resampled once, only on export.
- Left/right is always the user's explicit decision per scan; never inferred automatically.
- Input files are never modified.
- No patient or animal data in the repo, tests, logs or screenshots. Data goes in `data/` (git-ignored) and is
  fetched by script.

## Working in small, resumable steps (limited credits)
- Each step ends with passing tests, a commit and a push. Nothing important stays only on one machine.
- Update the Status section below in the same commit, so the next session (anyone, anywhere) can continue.
- Keep steps small enough to finish in one session.

## Code map
- Engine (`src/brainscope/`): `volume` (NIfTI), `orientation` (header-only fixes), `transforms` (RAS/LPS, the only
  place they meet), `register` (rigid MI), `detect` (lead tracking + contact fit), `presets`, `dicom`, `project`
  (project folder, undo, leads in CT-raw world, exports, results file), `acpc`, `localizer` (N-bar frame),
  `brainmodel` (brain mask, cuts, surfaces, lead meshes, GIfTI/OBJ/STL), `server` (HTTP API + static UI), `app`
  (pywebview window, browser fallback), `cli`, `demo`/`phantom` (synthetic data with ground truth).
- UI: `src/brainscope/web/` (plain JS + vendored Niivue, no build step).
- Packaging: `packaging/` + `.github/workflows/build.yml` (manual or tag `v*`); tests: `.github/workflows/ci.yml`.

## Dev
`python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,desktop]" && python -m pytest`;
`brainscope app --browser` runs the UI in a browser. UI end-to-end check: headless Chromium (Playwright) on the
demo case (`brainscope demo DIR`), see git history for the script pattern.

## Status
- Done: plan; engine; DICOM; project + undo; results file import/export; TSV export; UI; debug bundle;
  installers workflow (draft pre-release on tag, no artifacts); tests (synthetic ground truth, DICOM round trip,
  HTTP API). Pre-releases: v0.1.0; v0.1.1 (DICOM fallback, more formats, orientation aids);
  v0.1.2 (lenient DICOM sidecars, CT intensity levels, manual window, CT overlay before coregistration);
  v0.1.3 (display copies of large scans, landmark rough alignment, Center button, logo, Apple silicon only);
  v0.1.4 (zoom, coregistration region-of-interest box);
  v0.1.5 (AC/PC landmarks and coordinates, planned vs delivered);
  v0.1.6 (stereotactic frame from the N-bar localizer, Targets step, AC/PC on the MRI first);
  v0.1.7 (subject steps + Leads/Brain model modules, Brain model (`brainmodel`), plane sliders, pan; PLAN D29-D30, D32).
- In progress: 0.1.8 = frame rework (PLAN D33-D36, steps a-d in PLAN section 8). Done: (a) engine
  (`localizer.track/fit_tracked/refine`, intensity band) + per-scan frame storage in `project` (`frame_state`,
  `frame_pose`, `frame_autofind`, `frame_refine`, `frame_use`, `frame_agreement`; 0.1.6 projects converted);
  the 0.1.7 Frame UI still runs through `frameCompat` in app.js. Next: (b) new Frame step UI (scan choice, band
  preview, expected rods + rough alignment, track/fit, agreement, choice; D39 Brainlab-like interaction), then
  (c) rod inspector, (d) debug bundle crops, docs, release.
- After that: Brainlab arc/collar plans (D37, convention to confirm: PLAN section 10.4); drawing tools + guided
  Brain model (D38); Structures (D31); strips (1xN, preset type "strip"); ask whether 2D grids are needed. Also: verify the localizer geometry on a real CT (PLAN section 10); the user tests on real human and pig data and sends debug bundles; tune detection on them (DBS leads,
  contacts mostly not separable); answer `docs/PLAN.md` section 10.
