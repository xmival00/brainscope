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

## Status
- Done: M0 (plan).
- Next: M1, engine core (see `docs/PLAN.md` section 8).
- Waiting on the user: open questions in `docs/PLAN.md` section 10.
