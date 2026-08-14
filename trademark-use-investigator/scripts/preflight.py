#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from edge_profile import preferred_chromium_browser
from process_utils import run_bounded


SINGLEFILE_VERSION = "2.0.83"
PLAYWRIGHT_VERSION = "1.61.1"
MIN_NODE_MAJOR = 20
PINNED_PYTHON_DISTRIBUTIONS = {
    "PyMuPDF": "1.27.2.3",
    "Pillow": "11.3.0",
    "opencv-python-headless": "4.13.0.92",
    "openpyxl": "3.1.5",
    "et_xmlfile": "2.0.0",
}

ASSISTED_REQUIRED_FILES = (
    "SKILL.md",
    "references/free-assisted-browser.md",
    "scripts/preflight.py",
    "scripts/cherrystudio_orchestrator.py",
    "scripts/cherrystudio-report-guard.py",
    "scripts/partial_materials_contract.py",
    "scripts/runtime_policy.py",
    "scripts/runtime-policy.json",
    "scripts/audit_cherrystudio_run.py",
    "scripts/browser_state_guard.py",
    "scripts/edge_profile.py",
    "scripts/process_utils.py",
    "scripts/qcc_reference_guard.py",
    "scripts/run_lock.py",
    "scripts/init-run-from-json.py",
    "scripts/init-run.py",
    "scripts/build-query-plan.py",
    "scripts/build-manual-capture-queue.py",
    "scripts/build-sales-login-queue.py",
    "scripts/sales_platforms.py",
    "scripts/fetch-qcc-trademark-reference.py",
    "scripts/open-sales-login-profile.py",
    "scripts/capture-qcc-reference-from-cdp.mjs",
    "scripts/qcc-search-support.mjs",
    "scripts/run-visual-sales-after-login.py",
    "scripts/run-public-search-matrix.py",
    "scripts/discover-search-results.mjs",
    "scripts/sales-platform-assisted-discover.mjs",
    "scripts/browser-artifact-lock.mjs",
    "scripts/controlled-search-scheduler.mjs",
    "scripts/atomic-text-write.mjs",
    "scripts/sales-risk-cooldown.mjs",
    "scripts/sales-request-pacing.mjs",
    "scripts/sales-query-binding.mjs",
    "scripts/viewport-tile-capture.mjs",
    "scripts/stitch-viewport-tiles.py",
    "scripts/build-sales-platform-results.py",
    "scripts/validate-sales-pagination.py",
    "scripts/capture-sales-after-login.py",
    "scripts/capture-page-to-pdf.mjs",
    "scripts/archive-singlefile.py",
    "scripts/verify-offline-page.mjs",
    "scripts/trim-pdf-empty-tail.py",
    "scripts/raster-to-pdf.py",
    "scripts/retain-visual-mark-matches.py",
    "scripts/visual_match_utils.py",
    "scripts/build-related-results-pdf.py",
    "scripts/merge-evidence-pdf.py",
    "scripts/build-all-detected-html-pdf.py",
)

FULL_ONLY_REQUIRED_FILES = (
    "references/archive-and-binder-protocol.md",
    "references/cancellation-filing-pagination.md",
    "references/discovery-protocol.md",
    "references/evidence-record-schema.md",
    "references/execution-profiles.md",
    "references/manual-assisted-capture.md",
    "references/matching-rubric.md",
    "references/official-sales-api.md",
    "references/quick-mode.md",
    "references/vision-model-protocol.md",
    "scripts/url_utils.py",
    "scripts/provenance_utils.py",
    "scripts/discovery_limits.py",
    "scripts/discover-web.py",
    "scripts/quick-discover.py",
    "scripts/rank-frontier.py",
    "scripts/probe-target-page.py",
    "scripts/quick-probe.py",
    "scripts/capture-target-page.py",
    "scripts/quick-capture.py",
    "scripts/compare-screenshots.py",
    "scripts/build-review-packet.py",
    "scripts/import-discovery-results.py",
    "scripts/visual_consensus.py",
    "scripts/record-visual-review.py",
    "scripts/promote-candidate.py",
    "scripts/fs_safety.py",
    "scripts/validate-run.py",
    "scripts/finalize-run.py",
    "scripts/import-official-api-results.py",
    "scripts/prefilter-api-images.py",
    "scripts/capture-api-shortlist.py",
    "scripts/open-manual-extension-setup.py",
    "scripts/manual-capture-workstation.py",
    "scripts/instant_data_import.py",
    "scripts/validate-manual-capture.py",
    "scripts/build-manual-sales-pdf.py",
    "assets/manual-capture-extension/manifest.json",
    "assets/manual-capture-extension/service-worker.js",
    "assets/manual-capture-extension/popup.html",
    "assets/manual-capture-extension/popup.js",
    "assets/manual-capture-extension/icon.svg",
)


def required_files_for_profile(profile: str) -> tuple[str, ...]:
    if profile == "assisted":
        return ASSISTED_REQUIRED_FILES
    if profile == "full":
        return ASSISTED_REQUIRED_FILES + FULL_ONLY_REQUIRED_FILES
    raise ValueError(f"Unknown preflight profile: {profile}")


def python_module_available(name: str) -> tuple[bool, str]:
    """Actually import binary dependencies; ``find_spec`` misses broken DLLs."""
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, str(getattr(module, "__version__", None) or getattr(module, "__file__", "imported"))


def distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def node_module_available(node: str | None, skill_dir: Path) -> tuple[bool, str]:
    if not node:
        return False, "node executable not found"
    package = skill_dir / "node_modules" / "playwright-core" / "package.json"
    if not package.is_file():
        return False, f"local playwright-core {PLAYWRIGHT_VERSION} is not installed"
    try:
        installed = json.loads(package.read_text(encoding="utf-8")).get("version")
    except Exception as exc:
        return False, str(exc)
    if installed != PLAYWRIGHT_VERSION:
        return False, f"expected {PLAYWRIGHT_VERSION}, found {installed!r}"
    probe = (
        "const path=require('path');"
        "const resolved=require.resolve('playwright-core/package.json');"
        "const root=path.resolve(process.cwd(),'node_modules','playwright-core');"
        "const actual=path.dirname(path.resolve(resolved));"
        "if(actual.toLowerCase()!==root.toLowerCase())process.exit(3);"
        "console.log(resolved)"
    )
    result = run_bounded([node, "-e", probe], cwd=skill_dir, timeout=20)
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail or f"local playwright-core {PLAYWRIGHT_VERSION}"


def node_version(node: str | None) -> tuple[str | None, int | None]:
    if not node:
        return None, None
    result = run_bounded([node, "--version"], timeout=10)
    value = (result.stdout or result.stderr).strip()
    match = re.search(r"v?(\d+)", value)
    return value or None, int(match.group(1)) if match else None


def singlefile_available(node: str | None, skill_dir: Path) -> tuple[bool, str]:
    if not node:
        return False, "node executable not found"
    entry = skill_dir / "node_modules" / "single-file-cli" / "single-file-node.js"
    package = entry.parent / "package.json"
    if not entry.is_file() or not package.is_file():
        return False, f"local single-file-cli {SINGLEFILE_VERSION} is not installed"
    try:
        installed = json.loads(package.read_text(encoding="utf-8")).get("version")
    except Exception as exc:
        return False, str(exc)
    if installed != SINGLEFILE_VERSION:
        return False, f"expected {SINGLEFILE_VERSION}, found {installed!r}"
    try:
        result = run_bounded([node, str(entry), "--version"], timeout=90)
        return result.returncode == 0, (result.stdout or result.stderr).strip()
    except Exception as exc:
        return False, str(exc)


def writable(path: Path) -> tuple[bool, str | None]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="preflight-", dir=path, delete=True):
            pass
        return True, None
    except Exception as exc:
        return False, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the URL-discovery and offline-web-evidence runtime")
    parser.add_argument("--skill-dir", help="Skill directory; defaults to the parent of this script")
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--profile", choices=("full", "assisted"), default="full")
    parser.add_argument("--output")
    args = parser.parse_args()

    skill_dir = Path(args.skill_dir).resolve() if args.skill_dir else Path(__file__).resolve().parents[1]
    run_root = Path(args.run_root).resolve()
    node = shutil.which("node")
    node_version_text, node_major = node_version(node)
    playwright_ok, playwright_detail = node_module_available(node, skill_dir)
    if args.profile == "full":
        singlefile_ok, singlefile_detail = singlefile_available(node, skill_dir)
    else:
        singlefile_ok, singlefile_detail = False, "not checked: optional in attached assisted-browser mode"
    try:
        selected_browser, browser_executable = preferred_chromium_browser()
    except FileNotFoundError:
        selected_browser, browser_executable = None, None
    write_ok, write_error = writable(run_root)
    fitz_ok, fitz_detail = python_module_available("fitz")
    pillow_ok, pillow_detail = python_module_available("PIL")
    opencv_ok, opencv_detail = python_module_available("cv2")
    numpy_ok, numpy_detail = python_module_available("numpy")
    expected_numpy = "2.4.4" if sys.version_info >= (3, 14) else "2.2.6"
    pinned_python = {
        **PINNED_PYTHON_DISTRIBUTIONS,
        "numpy": expected_numpy,
    }
    installed_python = {name: distribution_version(name) for name in pinned_python}
    if args.profile == "full":
        openpyxl_ok, openpyxl_detail = python_module_available("openpyxl")
        firecrawl_cli = shutil.which("firecrawl")
        firecrawl_configured = bool(os.environ.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_URL"))
        crawl4ai_available, crawl4ai_detail = python_module_available("crawl4ai")
    else:
        openpyxl_ok, openpyxl_detail = False, "not checked: full-profile manual export dependency"
        firecrawl_cli, firecrawl_configured = None, False
        crawl4ai_available, crawl4ai_detail = False, "not checked: full-profile optional discovery dependency"

    required_files = required_files_for_profile(args.profile)
    missing_files = [name for name in required_files if not (skill_dir / name).is_file()]
    blocking = []
    warnings = []
    if not node:
        blocking.append("Node.js is not available")
    elif node_major is None or node_major < MIN_NODE_MAJOR:
        blocking.append(f"Node.js {MIN_NODE_MAJOR} or newer is required; found {node_version_text or 'unknown'}")
    if sys.version_info < (3, 10):
        blocking.append(f"Python 3.10 or newer is required; found {sys.version.split()[0]}")
    elif sys.version_info >= (3, 15):
        blocking.append(f"This locked Windows release supports Python 3.10 through 3.14; found {sys.version.split()[0]}")
    if not playwright_ok:
        blocking.append(f"Pinned local playwright-core {PLAYWRIGHT_VERSION} is unavailable: {playwright_detail}")
    if args.profile == "full" and not singlefile_ok:
        blocking.append(f"Pinned SingleFile CLI {SINGLEFILE_VERSION} is unavailable: {singlefile_detail}")
    if not browser_executable:
        blocking.append("Microsoft Edge or Google Chrome executable was not found")
    if not fitz_ok:
        blocking.append("PyMuPDF is not installed")
    if not pillow_ok:
        blocking.append("Pillow is not installed")
    if not opencv_ok:
        blocking.append("OpenCV (cv2) is not installed")
    if not numpy_ok:
        blocking.append("NumPy is not installed")
    for name in ("PyMuPDF", "Pillow", "opencv-python-headless", "numpy"):
        installed = installed_python.get(name)
        expected = pinned_python[name]
        if installed is not None and installed != expected:
            blocking.append(f"Pinned Python dependency {name}=={expected} is required; found {installed}")
    if not write_ok:
        blocking.append(f"Run root is not writable: {write_error}")
    if missing_files:
        blocking.append("Missing skill files: " + ", ".join(missing_files))
    if args.profile == "full" and not firecrawl_configured:
        warnings.append("Firecrawl is optional and not configured; it affects only ordinary public-web discovery, not the official sales-API branch")
    if args.profile == "full" and not openpyxl_ok:
        warnings.append("openpyxl is optional; XLSX structured-export import is unavailable, but CSV import remains supported")
    if args.profile == "full" and sys.version_info >= (3, 14) and crawl4ai_available:
        warnings.append("Crawl4AI is optional and should run in a separate supported Python 3.13 environment, not this Python 3.14 runtime")
    if distribution_version("opencv-python") is not None:
        warnings.append("Both opencv-python and opencv-python-headless are installed; keep only the locked headless package in the production runtime")

    report = {
        "schema_version": "2.0", "ok": not blocking, "preflight_profile": args.profile,
        "skill_dir": str(skill_dir), "run_root": str(run_root), "python": sys.executable,
        "node": node, "node_version": node_version_text, "npx": shutil.which("npx"),
        "playwright_available": playwright_ok,
        "playwright_version": PLAYWRIGHT_VERSION,
        "playwright_detail": playwright_detail,
        "browser_selection_policy": "edge_then_chrome",
        "selected_browser": selected_browser,
        "browser_fallback_used": selected_browser == "chrome",
        "browser_executable": str(browser_executable) if browser_executable else None,
        "pymupdf_available": fitz_ok, "pillow_available": pillow_ok,
        "opencv_available": opencv_ok, "numpy_available": numpy_ok,
        "openpyxl_available": openpyxl_ok,
        "python_dependency_details": {
            "fitz": fitz_detail, "PIL": pillow_detail, "cv2": opencv_detail,
            "numpy": numpy_detail, "openpyxl": openpyxl_detail,
        },
        "pinned_python_distributions": pinned_python,
        "installed_python_distributions": installed_python,
        "run_root_writable": write_ok,
        "required_skill_files": list(required_files),
        "missing_skill_files": missing_files,
        "singlefile": {
            "available": singlefile_ok, "version": SINGLEFILE_VERSION, "detail": singlefile_detail,
            "mode": "external_cli", "required": args.profile == "full",
        },
        "firecrawl": {"cli": firecrawl_cli, "configured": firecrawl_configured, "required": False},
        "crawl4ai": {"available": crawl4ai_available, "detail": crawl4ai_detail, "required": False},
        "blocking": blocking, "warnings": warnings,
        "install_command": None if playwright_ok and (singlefile_ok or args.profile == "assisted") else f'npm install --omit=dev --ignore-scripts --prefix "{skill_dir}"',
        "python_install_command": (
            None if fitz_ok and pillow_ok and opencv_ok and numpy_ok
            else f'"{sys.executable}" -m pip install --only-binary=:all: -r "<DELIVERY_PACKAGE>\\requirements-lock.txt"'
        ),
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).resolve().write_text(payload, encoding="utf-8")
    print(payload, end="")
    raise SystemExit(0 if report["ok"] else 2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
