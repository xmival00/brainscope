"""Command line: `brainscope` starts the app; subcommands for scripting and tests."""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np


def main(argv=None):
    ap = argparse.ArgumentParser(prog="brainscope", description="CT-to-MRI coregistration and lead contact "
                                 "localization. Research use only.")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("app", help="start the app (default)")
    a.add_argument("--projects", help="folder that holds the projects (default ~/brainscope-projects)")
    a.add_argument("--browser", action="store_true", help="use the web browser instead of a native window")
    a.add_argument("--port", type=int, default=0)
    i = sub.add_parser("info", help="show a NIfTI's geometry")
    i.add_argument("file")
    d = sub.add_parser("demo", help="write a synthetic demo case (MRI, CT with leads, ground truth)")
    d.add_argument("out")
    c = sub.add_parser("dicom", help="convert a DICOM folder to NIfTI")
    c.add_argument("folder")
    c.add_argument("out")
    r = sub.add_parser("coreg", help="rigid CT-to-MRI coregistration (headers as they are)")
    r.add_argument("--ct", required=True)
    r.add_argument("--mri", required=True)
    r.add_argument("--species", default="human", choices=["human", "pig", "other"])
    r.add_argument("--out", required=True, help="text file for the 4x4 CT->MRI matrix (RAS mm)")
    t = sub.add_parser("detect", help="detect the contacts of one lead in a CT (world coordinates of the CT)")
    t.add_argument("--ct", required=True)
    t.add_argument("--tip", type=float, nargs=3, required=True)
    t.add_argument("--entry", type=float, nargs=3, required=True)
    t.add_argument("--preset", required=True)
    t.add_argument("--name", default="A")
    t.add_argument("--curved", action="store_true")
    b = sub.add_parser("debug-bundle", help="write a small zip (coarse scans, CT crops around leads, project state) "
                       "to share when something goes wrong")
    b.add_argument("project", help="project folder")
    sub.add_parser("selftest", help="quick check that the installation works")
    args = ap.parse_args(argv)

    if args.cmd in (None, "app"):
        from .app import run

        return run(getattr(args, "projects", None), getattr(args, "browser", False), getattr(args, "port", 0))
    if args.cmd == "info":
        from . import volume

        v = volume.load(args.file)
        print(json.dumps({"shape": v.data.shape, "spacing": v.spacing.round(4).tolist(),
                          "affine": v.affine.round(4).tolist(), "min": float(v.data.min()),
                          "max": float(v.data.max())}, indent=1))
    elif args.cmd == "demo":
        from .demo import make

        print(f"demo written to {make(args.out)}")
    elif args.cmd == "dicom":
        from .dicom import convert

        print(json.dumps(convert(args.folder, args.out), indent=1))
    elif args.cmd == "coreg":
        from . import register, volume

        ct, mri = volume.load(args.ct), volume.load(args.mri)
        m, metric = register.coregister(ct.data, ct.affine, mri.data, mri.affine, species=args.species)
        np.savetxt(args.out, m, fmt="%.8f", header=f"CT world -> MRI world, RAS+ mm; metric {metric:.5f}")
    elif args.cmd == "detect":
        from . import detect, presets, volume

        ct = volume.load(args.ct)
        lead = detect.detect(ct.data, ct.affine, args.tip, args.entry, presets.load_all()[args.preset],
                             lead=args.name, curved=args.curved)
        for c, xyz in zip(lead["contacts"], detect.positions(lead)):
            print(presets.label(lead["preset"], args.name, c["index"]), *np.round(xyz, 3), c["mode"],
                  c["confidence"], sep="\t")
    elif args.cmd == "debug-bundle":
        from .project import Project

        print(Project(args.project).debug_bundle())
    elif args.cmd == "selftest":
        return selftest()
    return 0


def selftest():
    from . import detect, dicom, phantom, presets

    p = presets.load_all()["dixi-microdeep-d08-05"]
    data, affine, truth, tip = phantom.lead_ct(p, size=40.0)
    lead = detect.detect(data, affine, tip, truth[-1] + 5 * (truth[-1] - truth[0]) / 14, p)
    err = float(np.abs(detect.positions(lead) - truth).max())
    dicom.binary()
    ok = err < 0.3
    print(f"brainscope selftest: {'OK' if ok else 'FAILED'} (max contact error {err:.3f} mm)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
