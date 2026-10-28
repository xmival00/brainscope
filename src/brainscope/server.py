"""Local HTTP API and static UI. Binds to 127.0.0.1 only. The same API can later serve the dashboard."""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, dicom
from . import presets as pr
from .project import Project, check_name, jsonable, list_projects

ROUTES = []
CHUNK = 1 << 20


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


class App:
    def __init__(self, projects_root, desktop=False):
        self.root = Path(projects_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.desktop = desktop
        self.projects = {}
        self.lock = threading.Lock()

    def project(self, name):
        name = check_name(unquote(name), "project name")
        with self.lock:
            if name not in self.projects:
                if not (self.root / name / "project.json").exists():
                    raise ValueError(f"no project named {name!r}")
                self.projects[name] = Project(self.root / name)
            return self.projects[name]


class Handler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # no paths or names in logs
        pass

    def _dispatch(self, method):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self._left = int(self.headers.get("Content-Length", 0) or 0)
        try:
            for m, rx, fn in ROUTES:
                hit = rx.match(url.path)
                if m == method and hit:
                    try:
                        return fn(self, *hit.groups(), q=q)
                    except (ValueError, KeyError, RuntimeError, FileNotFoundError) as e:
                        return self.json({"error": str(e).strip("'\"")}, 400)
                    except Exception as e:  # noqa: BLE001
                        traceback.print_exc(file=sys.stderr)
                        return self.json({"error": f"internal error: {type(e).__name__}: {e}"}, 500)
            self.json({"error": "not found"}, 404)
        finally:  # an unread body would be parsed as the next request on this kept-alive connection
            if self._left > CHUNK:
                self.close_connection = True
            elif self._left > 0:
                self.rfile.read(self._left)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---- helpers ----
    def body_bytes_to(self, f):
        while self._left > 0:
            chunk = self.rfile.read(min(CHUNK, self._left))
            if not chunk:
                raise ValueError("upload interrupted")
            f.write(chunk)
            self._left -= len(chunk)

    def body_json(self):
        raw, self._left = self.rfile.read(self._left), 0
        return json.loads(raw or b"{}")

    def json(self, obj, code=200):
        raw = json.dumps(obj, default=jsonable).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def file(self, path, ctype="application/octet-stream", download_name=None):
        path = Path(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)


def _summary(h, p):
    return h.json(p.summary())


# ---- static UI ----
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


@route("GET", r"/(?:index\.html)?")
def index(h, q):
    return static(h, "index.html", q=q)


@route("GET", r"/web/([A-Za-z0-9_./-]+)")
def static(h, rel, q):
    if ".." in rel:
        raise ValueError("bad path")
    f = resources.files("brainscope").joinpath("web", *rel.split("/"))
    if not f.is_file():
        return h.json({"error": "not found"}, 404)
    with resources.as_file(f) as path:
        return h.file(path, STATIC_TYPES.get(Path(rel).suffix, "application/octet-stream"))


# ---- app and projects ----
@route("GET", r"/api/state")
def state(h, q):
    return h.json({"version": __version__, "projects": list_projects(h.app.root), "projects_root": str(h.app.root),
                   "desktop": h.app.desktop})


@route("POST", r"/api/projects")
def new_project(h, q):
    name = check_name(h.body_json().get("name", ""), "project name")
    Project.create(h.app.root / name)
    return _summary(h, h.app.project(name))


@route("POST", r"/api/demo")
def demo_project(h, q):
    from . import demo

    data = h.app.root / ".demo-data"
    if not (data / "truth.json").exists():
        demo.make(data)
    names = set(list_projects(h.app.root))
    name = next(n for n in ["Demo"] + [f"Demo {i}" for i in range(2, 1000)] if n not in names)
    p = Project.create(h.app.root / name)
    p.set_species("pig")
    p.import_scan("mri", data / "mri.nii.gz")
    p.import_scan("ct", data / "ct.nii.gz")
    p.undo_stack.clear()
    return _summary(h, h.app.project(name))


@route("GET", r"/api/p/([^/]+)")
def get_project(h, name, q):
    return _summary(h, h.app.project(name))


@route("POST", r"/api/p/([^/]+)/(undo|redo|species|orient|coreg|nudge|coreg-accept|lead|lead-edit)")
def mutate(h, name, action, q):
    p = h.app.project(name)
    b = h.body_json()
    with p.lock:
        if action == "undo":
            p.undo()
        elif action == "redo":
            p.redo()
        elif action == "species":
            p.set_species(b["species"])
        elif action == "orient":
            p.orient(b["role"], op=b.get("op"), reset=bool(b.get("reset")), left_confirmed=b.get("left_confirmed"),
                     left_note=b.get("left_note"))
        elif action == "coreg":
            p.coregister(crop_center=b.get("crop_center"), crop_radius=b.get("crop_radius"),
                         from_current=bool(b.get("from_current")))
        elif action == "nudge":
            p.nudge(b.get("rot", (0, 0, 0)), b.get("trans", (0, 0, 0)))
        elif action == "coreg-accept":
            p.accept_coreg(b.get("accepted", True))
        elif action == "lead":
            p.add_lead(b["name"], b["preset"], b["tip"], b["entry"], curved=bool(b.get("curved")),
                       hemisphere=b.get("hemisphere", "n/a"))
        elif action == "lead-edit":
            b = dict(b)
            p.edit_lead(b.pop("name"), b.pop("action"), **b)
    return _summary(h, p)


# ---- import ----
@route("POST", r"/api/p/([^/]+)/import")
def import_path(h, name, q):
    if not h.app.desktop:
        raise ValueError("importing by path is only available in the desktop app")
    b = h.body_json()
    p = h.app.project(name)
    with p.lock:
        p.import_scan(b["role"], b["path"])
    return _summary(h, p)


@route("POST", r"/api/p/([^/]+)/upload")
def upload(h, name, q):
    p = h.app.project(name)
    fname = Path(q.get("filename", "upload.nii.gz")).name
    with tempfile.TemporaryDirectory(dir=p.root / "cache") as d:
        tmp = Path(d) / fname
        with open(tmp, "wb") as f:
            h.body_bytes_to(f)
        with p.lock:
            p.import_scan(q["role"], tmp, source_name=fname)
    return _summary(h, p)


@route("POST", r"/api/p/([^/]+)/dicom-file")
def dicom_file(h, name, q):
    p = h.app.project(name)
    run = check_name(q["run"], "run")
    rel = Path(*[x for x in Path(q.get("rel", "file")).parts if x not in ("..", "/", "\\")][-4:])
    dst = p.root / "cache" / "dicom-in" / run / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        h.body_bytes_to(f)
    return h.json({"ok": True})


@route("POST", r"/api/p/([^/]+)/dicom")
def dicom_convert(h, name, q):
    p = h.app.project(name)
    b = h.body_json()
    if b.get("path"):
        if not h.app.desktop:
            raise ValueError("importing by path is only available in the desktop app")
        src = Path(b["path"])
    else:
        src = p.root / "cache" / "dicom-in" / check_name(b["run"], "run")
    run = uuid.uuid4().hex[:12]
    out = p.root / "cache" / "dicom-out" / run
    try:
        series = dicom.convert(src, out)
    finally:
        if not b.get("path"):
            shutil.rmtree(src, ignore_errors=True)
    return h.json({"run": run, "series": series})


@route("POST", r"/api/p/([^/]+)/dicom-use")
def dicom_use(h, name, q):
    p = h.app.project(name)
    b = h.body_json()
    f = p.root / "cache" / "dicom-out" / check_name(b["run"], "run") / Path(b["file"]).name
    with p.lock:
        p.import_scan(b["role"], f, source_name=f.name)
    return _summary(h, p)


# ---- viewer data ----
@route("GET", r"/api/p/([^/]+)/vol/(mri|ct)\.nii")
def vol(h, name, role, q):
    p = h.app.project(name)
    with p.lock:
        f = p.display_file(role)
    return h.file(f)


@route("GET", r"/api/p/([^/]+)/vol/contacts\.nii\.gz")
def vol_contacts(h, name, q):
    p = h.app.project(name)
    with p.lock:
        f = p.contacts_file()
    return h.file(f)


@route("GET", r"/api/p/([^/]+)/lead-view")
def lead_view(h, name, q):
    return h.json(h.app.project(name).lead_view(q["lead"]))


@route("GET", r"/api/p/([^/]+)/xsec")
def xsec(h, name, q):
    return h.json(h.app.project(name).cross_section(q["lead"], float(q["s"])))


# ---- results and export ----
@route("GET", r"/api/p/([^/]+)/leads-file")
def leads_file(h, name, q):
    p = h.app.project(name)
    raw = json.dumps(p.leads_document(), indent=1, default=jsonable).encode()
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    h.send_header("Content-Disposition", f'attachment; filename="{p.root.name}_leads.brainscope.json"')
    h.end_headers()
    h.wfile.write(raw)


@route("POST", r"/api/p/([^/]+)/leads-import")
def leads_import(h, name, q):
    p = h.app.project(name)
    doc = h.body_json()
    with p.lock:
        warnings = p.import_leads(doc, replace=q.get("replace") == "1")
    return h.json(dict(p.summary(), warnings=warnings))


@route("POST", r"/api/p/([^/]+)/export")
def export(h, name, q):
    p = h.app.project(name)
    b = h.body_json()
    opts = {k: bool(b[k]) for k in ("tsv", "leads_file", "transforms", "mri", "ct", "labels") if k in b}
    with p.lock:
        files = p.export(**opts)
    return h.json({"folder": str(p.root / "outputs"), "files": [Path(f).name for f in files]})


@route("POST", r"/api/p/([^/]+)/debug-bundle")
def debug_bundle(h, name, q):
    p = h.app.project(name)
    with p.lock:
        z = Path(p.debug_bundle())
    return h.json({"folder": str(z.parent), "file": z.name, "size_mb": round(z.stat().st_size / 1e6, 1)})


@route("GET", r"/api/p/([^/]+)/output/([A-Za-z0-9_.-]+)")
def output_file(h, name, fname, q):
    p = h.app.project(name)
    f = p.root / "outputs" / fname
    if not f.is_file():
        raise FileNotFoundError("no such output file")
    return h.file(f, download_name=fname)


# ---- presets ----
@route("GET", r"/api/presets")
def presets_list(h, q):
    return h.json(sorted(pr.load_all().values(), key=lambda p: (not p["builtin"], p["name"].lower())))


@route("POST", r"/api/presets")
def presets_save(h, q):
    pr.save_user(h.body_json())
    return presets_list(h, q=q)


@route("POST", r"/api/presets/delete")
def presets_delete(h, q):
    pr.delete_user(h.body_json()["id"])
    return presets_list(h, q=q)


def make_server(projects_root, port=0, desktop=False):
    handler = type("BoundHandler", (Handler,), {"app": App(projects_root, desktop)})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
