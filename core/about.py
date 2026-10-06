"""The About page's inventory of open-source components, built from what is actually installed.

Attribution lists written by hand drift the first time Dependabot bumps a pin. Reading the
installed distributions instead means the page always names the exact versions running and
carries the license and NOTICE texts those packages ship, which is what their licenses require.
"""
import re
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from django.conf import settings
from packaging.requirements import Requirement

NOTICE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
# Files Django ships for optional GIS bindings this application never loads.
SKIP_PATHS = ("django/contrib/gis/",)

# Separate programs the reference compose stack runs beside the application. They talk to it over
# the network and are not linked into it, so their licenses do not extend to this software; they are
# listed so an operator knows what they are running and where its source is.
SERVICES = [
    {"name": "Debian (python:3.14-slim base image)", "version": "", "license": "Various (per package)",
     "role": "Operating system packages in the application image", "url": "https://sources.debian.org/"},
    {"name": "Caddy", "version": "2", "license": "Apache-2.0", "role": "HTTPS reverse proxy",
     "url": "https://github.com/caddyserver/caddy"},
    {"name": "MySQL Community Server", "version": "8.4", "license": "GPL-2.0 with Universal FOSS Exception",
     "role": "Database", "url": "https://github.com/mysql/mysql-server"},
    {"name": "Redis", "version": "7.4", "license": "RSALv2 / SSPLv1 (source-available)",
     "role": "Cache and task queue", "url": "https://github.com/redis/redis"},
    {"name": "ClamAV", "version": "1.4", "license": "GPL-2.0", "role": "Upload malware scanning",
     "url": "https://github.com/Cisco-Talos/clamav"},
    {"name": "DocuSeal", "version": "3.3.0", "license": "AGPL-3.0", "role": "Electronic signature service",
     "url": "https://github.com/docusealco/docuseal"},
]


def _key(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements_file():
    path = Path(settings.BASE_DIR) / "requirements.txt"
    if not path.exists():
        return []
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("-")]


def _license_name(meta):
    expression = meta.get("License-Expression")
    if expression:
        return expression
    declared = (meta.get("License") or "").strip()
    # Some packages paste the whole license into this field; a classifier names it better.
    if declared and "\n" not in declared and len(declared) <= 60:
        return declared
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return " / ".join(c for c in classifiers if c != "OSI Approved") or "See license text"


def _homepage(meta):
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in {"homepage", "home", "source", "source code", "repository", "code"}:
            return url.strip()
    return meta.get("Home-page") or ""


def _texts(dist):
    texts = []
    for file in dist.files or []:
        path = str(file).replace("\\", "/")
        if path.startswith(SKIP_PATHS) or not NOTICE_FILE.match(path.rsplit("/", 1)[-1]):
            continue
        try:
            body = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if body and body.strip():
            texts.append({"name": path, "text": body.strip()})
    return texts


@lru_cache(maxsize=1)
def python_components():
    """Every distribution the runtime requirements pull in, with its shipped license texts."""
    found = {}

    def walk(spec, extras=()):
        requirement = Requirement(spec)
        key = _key(requirement.name)
        try:
            dist = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            return
        wanted = set(requirement.extras) | set(extras)
        if key in found and wanted <= found[key]["extras"]:
            return
        entry = found.setdefault(key, {"dist": dist, "extras": set()})
        entry["extras"] |= wanted
        environments = [{"extra": extra} for extra in (entry["extras"] or {""})]
        for dependency in dist.requires or []:
            child = Requirement(dependency)
            if child.marker is None or any(child.marker.evaluate(env) for env in environments):
                walk(dependency)

    for spec in _requirements_file():
        walk(spec)
    rows = []
    for entry in found.values():
        dist = entry["dist"]
        meta = dist.metadata
        rows.append({"name": meta["Name"], "version": dist.version, "license": _license_name(meta),
                     "summary": meta.get("Summary") or "", "url": _homepage(meta), "texts": _texts(dist)})
    return sorted(rows, key=lambda row: row["name"].lower())


@lru_cache(maxsize=1)
def bundled_components():
    """Browser code and fonts shipped in ``static/vendor``, each with the license files beside it."""
    root = Path(settings.BASE_DIR) / "static" / "vendor" / "pdfjs"
    if not root.exists():
        return []
    version_file = root / "VERSION"
    summary = version_file.read_text(encoding="utf-8").splitlines()[0] if version_file.exists() else "pdf.js"
    match = re.search(r"(\d+\.\d+\.\d+)", summary)
    texts = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and NOTICE_FILE.match(path.name):
            texts.append({"name": path.relative_to(root).as_posix(), "text": path.read_text(encoding="utf-8", errors="replace").strip()})
    return [{"name": "PDF.js (pdfjs-dist)", "version": match.group(1) if match else "", "license": "Apache-2.0",
             "summary": "In-browser document viewer, with the Foxit and Liberation standard fonts and the "
                        "JBIG2, OpenJPEG and QCMS image decoders it bundles.",
             "url": "https://github.com/mozilla/pdf.js", "texts": texts}]


@lru_cache(maxsize=1)
def runtime_license():
    """The CPython license text, which the interpreter installs alongside its standard library."""
    for candidate in (Path(sys.base_prefix) / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt",
                      Path(sys.base_prefix) / "LICENSE.txt"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    return ""
