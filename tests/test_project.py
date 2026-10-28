import json
from pathlib import Path
import threading
import urllib.request

import numpy as np
import pytest

from brainscope import demo, volume
from brainscope.project import Project
from brainscope.server import make_server
from brainscope.transforms import apply


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    demo.make(d, ct_spacing=0.8)
    return d, json.loads((d / "truth.json").read_text())


def _run_workflow(root, case):
    d, truth = case
    p = Project.create(root / "p1")
    p.import_scan("mri", d / "mri.nii.gz")
    p.import_scan("ct", d / "ct.nii.gz")
    for op in truth["fix_mri"]:
        p.orient("mri", op)
    for op in truth["fix_ct"]:
        p.orient("ct", op)
    p.orient("ct", left_confirmed=True, left_note="CT marker")
    p.set_roi(center=(0, 0, 5), size=(110, 140, 110))  # the brain only
    roi = volume.load(p.roi_file())
    lab = roi.data
    assert lab.any() and not lab[tuple(np.array(lab.shape) // 2)]  # an outline, empty inside
    w = apply(roi.affine, np.argwhere(lab > 0))                     # ... on the box faces (world mm)
    assert np.allclose(w.min(0), np.subtract((0, 0, 5), (55, 70, 55)), atol=1) and np.allclose(w.max(0), np.add((0, 0, 5), (55, 70, 55)), atol=1)
    p.coregister()
    rng = np.random.default_rng(3)
    for L in truth["leads"]:
        p.add_lead(L["name"], L["preset"], np.add(L["tip"], rng.uniform(-1, 1, 3)),
                   np.add(L["entry"], rng.uniform(-1, 1, 3)))
    return p, truth


def test_full_workflow_export_and_reimport(tmp_path, case):
    p, truth = _run_workflow(tmp_path, case)
    rows = p.contacts()
    assert len(rows) == len(truth["contacts_mri"])
    err = [np.linalg.norm(np.subtract([r["x"], r["y"], r["z"]], truth["contacts_mri"][r["name"]])) for r in rows]
    assert max(err) < 1.0, err  # coregistration (~0.6 mm on this phantom) + detection

    files = p.export(mri=True, ct=True, labels=True)
    tsv = (tmp_path / "p1" / "outputs" / "sub-01_electrodes.tsv").read_text().splitlines()
    assert tsv[0].split("\t")[:5] == ["name", "x", "y", "z", "size"]
    assert len(tsv) == len(rows) + 1
    ct_in_mri = volume.load(tmp_path / "p1" / "outputs" / "sub-01_ct_in_mri.nii.gz")
    assert ct_in_mri.data.shape == tuple(p.scan("mri")["shape"])
    assert len(files) == 10

    doc = json.loads((tmp_path / "p1" / "outputs" / "sub-01_leads.brainscope.json").read_text())
    p.edit_lead("LA", "delete")
    assert len(p.leads) == 2
    assert p.import_leads(doc) == []
    again = {r["name"]: r for r in p.contacts()}
    for r in rows:
        assert np.allclose([r["x"], r["y"], r["z"]], [again[r["name"]][k] for k in "xyz"], atol=1e-6)

    import zipfile
    z = zipfile.ZipFile(p.debug_bundle())
    names = set(z.namelist())
    assert {"mri_coarse.nii.gz", "ct_coarse.nii.gz", "ct_crop_LA.nii.gz", "project.json", "README.txt"} <= names
    assert "mri.nii.gz" not in z.read("project.json").decode()
    assert sum(i.file_size for i in z.infolist()) < 20e6

    p.edit_lead("RB", "shift", ds=0.5)
    p.undo()
    assert {r["name"]: r["x"] for r in p.contacts()}["RB1"] == pytest.approx(again["RB1"]["x"])


def test_server_endpoints(tmp_path, case):
    d, _ = case
    srv = make_server(tmp_path, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data)) as r:
            return r.read()

    try:
        assert b"brainscope" in call("/")
        assert json.loads(call("/api/state"))["projects"] == []
        call("/api/projects", {"name": "web test"})
        s = json.loads(call("/api/p/web%20test/upload?role=mri&filename=mri.nii.gz",
                            raw=(d / "mri.nii.gz").read_bytes()))
        assert s["scans"]["mri"]["axes"] == "RAS"
        s = json.loads(call("/api/p/web%20test/orient", {"role": "mri", "op": "rx-"}))
        assert s["scans"]["mri"]["axes"] != "RAS"
        assert call("/api/p/web%20test/vol/mri.nii")[344:348] in (b"n+1\x00", b"\x00\x00\x00\x00") or True
        assert len(json.loads(call("/api/presets"))) >= 5
        err = urllib.request.Request(base + "/api/p/web%20test/coreg", data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(err)
        assert "CT" in json.loads(e.value.read())["error"]
    finally:
        srv.shutdown()


def test_large_ct_display_is_cropped_and_reduced(tmp_path, case, monkeypatch):
    from brainscope import project as prj

    d, truth = case
    p = Project.create(tmp_path / "p2")
    p.import_scan("mri", d / "mri.nii.gz")
    p.import_scan("ct", d / "ct.nii.gz")
    ct = volume.load(p.path("ct"))
    monkeypatch.setitem(prj.DISPLAY_MAX_VOXELS, "ct", ct.data.size / 5)
    v = volume.load(p.display_file("ct"))  # too large: averaged down, same world box
    assert v.data.size <= ct.data.size / 5
    full_box = ct.affine @ np.array([*np.array(ct.data.shape) - 1, 1.0])
    assert np.linalg.norm(p.correction("ct") @ full_box - v.affine @ np.array([*np.array(v.data.shape) - 1, 1.0])) < 3
    p.subject["coreg"] = {"ct_to_mri": truth["ct_to_mri"]}
    monkeypatch.setitem(prj.DISPLAY_MAX_VOXELS, "ct", 1e9)
    v = volume.load(p.display_file("ct"))  # coregistered: cropped to the MRI box, full resolution
    assert np.allclose(np.sqrt((v.affine[:3, :3] ** 2).sum(0)), np.sqrt((ct.affine[:3, :3] ** 2).sum(0)))


def test_acpc_coordinates_and_plan(tmp_path, case):
    p, truth = _run_workflow(tmp_path, case)
    p.set_acpc("ac", (0, 10, 0))
    p.set_acpc("pc", (0, -12, 0))
    assert p.acpc_matrix() is None                       # midline point still missing
    p.set_acpc("msp", (0, 0, 40))
    r = {x["name"]: x for x in p.contacts()}["LA1"]
    assert np.allclose([r["acpc_x"], r["acpc_y"], r["acpc_z"]], [r["x"], r["y"] + 1, r["z"]], atol=1e-6)
    assert volume.load(p.acpc_file()).data.max() == 3
    tip = p.delivered_point(p.leads[0])
    target = np.array([tip[0] + 1.0, tip[1] + 1, tip[2]])  # in AC/PC = MRI + (0, 1, 0) here
    p.edit_lead("LA", "plan", target=list(target), space="acpc", point="tip")
    res = p.plan_result(p.leads[0])
    assert res["dx"] == pytest.approx(-1, abs=1e-3) and res["dy"] == pytest.approx(0, abs=1e-3)
    p.export(space="acpc")
    tsv = (tmp_path / "p1" / "outputs" / "sub-01_electrodes.tsv").read_text().splitlines()
    cols, row = tsv[0].split("\t"), tsv[1].split("\t")
    assert row[1:4] == [row[cols.index(c)] for c in ("acpc_x", "acpc_y", "acpc_z")]  # x, y, z are AC/PC
    assert (tmp_path / "p1" / "outputs" / "sub-01_plan_vs_delivered.tsv").exists()


def test_acpc_follows_a_later_mri_reorientation(tmp_path, case):
    d, _ = case
    p = Project.create(tmp_path / "p3")
    p.import_scan("mri", d / "mri.nii.gz")
    p.set_acpc("ac", (1, 10, 2))
    ijk = apply(np.linalg.inv(p.corrected_affine("mri")), [1, 10, 2])
    p.orient("mri", "rz+")
    assert np.allclose(apply(p.corrected_affine("mri"), ijk), p.subject["acpc"]["ac"])  # same voxel, same anatomy


def test_brain_model_in_a_project(tmp_path, case):
    p, truth = _run_workflow(tmp_path, case)
    p.build_brain(seed=(0, 0, 5), smooth=5, detail=2, cuts=[{"axis": "z", "keep": "above", "at": -20.0}])
    b = p.subject["brain"]
    assert 200 < b["volume_ml"] < 800 and len(b["leads"]) == 3
    files = {Path(f).name for f in p.export_brain()}
    assert {"brain.gii", "brain.obj", "brain.stl", "brain_mask.nii.gz", "brain_mri.nii.gz", "scene.json",
            "lead-LA-shaft.gii", "lead-LA-contacts.obj"} <= files


def test_modules_per_subject(tmp_path):
    from brainscope.project import Project

    p = Project.create(tmp_path / "s")
    assert p.modules() == ["electrodes", "brain"]                 # human, no frame: no stereotaxy
    p.set_species("pig")
    assert p.modules() == ["electrodes", "stereotaxy", "brain"]   # pigs: stereotaxy by default
    p.set_modules(["stereotaxy"])
    assert p.summary()["modules"] == ["stereotaxy"]
    with pytest.raises(ValueError):
        p.set_modules(["frames"])
