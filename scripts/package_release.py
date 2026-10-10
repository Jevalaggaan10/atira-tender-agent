#!/usr/bin/env python3
"""Build an allowlisted release: the runtime, exactly, and nothing else.

A published package must be code-only: the root runtime modules, atira_agent/*.py, agent.json, pyproject.toml and
uv.lock. Never docs, tests, .git, Entire checkpoints, .agents/.opencode, logs, credentials, scenario inputs or run
evidence — and never a symlink or a path that climbs out of the tree. `plan` lists what would go in; `stage` copies it
into a staging directory (what `atira test-run <staging>` should point at); `check` audits an existing folder or
tarball member by member — not just shape: the exact runtime set, so an empty or merely tidy folder is refused.

`stage` guards its target before it touches anything (only a staging folder it recognises as its own is ever
replaced) and parses the staged .py files as AST afterwards: `compileall` would leave a __pycache__ in the stage.

    uv run python scripts/package_release.py plan
    uv run python scripts/package_release.py stage --out ../release-stage
    uv run python scripts/package_release.py check --target ../release-stage   # or a .tar.gz
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# top-level runtime files the sandbox needs, by name (a new root module is deliberately NOT picked up by itself)
ROOT_FILES = ("agent.py", "run.py", "browsers.py", "models.py", "agent.json", "pyproject.toml", "uv.lock")
PACKAGE_DIR = "atira_agent"
# the package modules the runtime imports at startup: a release missing one would crash the run on import, so
# `plan` refuses an incomplete tree and `check` refuses a folder or tarball that does not carry the full set.
REQUIRED_PACKAGE_MODULES = ("__init__.py", "contracts.py", "documents.py", "events.py", "evidence.py",
                             "pricing.py", "runtime.py", "runtime_browser.py", "runtime_models.py",
                             "runtime_submission.py")
ALLOWED_SUFFIXES = (".py", ".json", ".toml", ".lock")
# a file whose path contains any of these is refused even if the allowlist would take it
FORBIDDEN_WORDS = (".git", ".entire", ".agents", ".opencode", ".dev-input", ".dev-runs", "credential", "secret",
                   "token", "cookie", "fixture", "scenario", "evidence-log", ".log", ".jsonl", "node_modules",
                   ".venv", "__pycache__")
MAX_FILE_BYTES = 4_000_000
# `atira test-run` packs EVERY file it finds in the staging folder, so the upload must hold exactly the runtime: no
# marker, no .gitignore, nothing extra. Our ownership record for a stage therefore lives OUTSIDE it, next to it,
# keyed by the stage's resolved path.
STAGE_META_SUFFIX = ".atira-release-staging.json"


class PackageError(RuntimeError):
    pass


def _safe_relpath(rel: str) -> bool:
    path = Path(rel)
    return (not path.is_absolute() and ".." not in path.parts and not rel.startswith("/")
            and not path.parts[0].startswith(".") and path.suffix in ALLOWED_SUFFIXES)


def _release_paths() -> frozenset:
    return frozenset(ROOT_FILES) | {f"{PACKAGE_DIR}/{name}" for name in REQUIRED_PACKAGE_MODULES}


def _reject(name: str, reason: str) -> None:
    raise PackageError(f"refusing {name}: {reason}")


def _check_file(name: str, source: Path) -> None:
    if any(word in name.lower() for word in FORBIDDEN_WORDS):
        _reject(name, "the path names something that must stay out of a release")
    if not _safe_relpath(name):
        _reject(name, "not a safe, allowlisted relative path")
    if source.is_symlink():
        _reject(name, "symlinks are never packaged")
    if not source.is_file():
        _reject(name, "not a regular file")
    size = source.stat().st_size
    if size == 0 or size > MAX_FILE_BYTES:
        _reject(name, f"size {size} outside 1..{MAX_FILE_BYTES}")


def _audit_paths(relpaths) -> None:
    """The shape every release (planned, staged or archived) must have: exactly the runtime, once, no extras."""
    relpaths = list(relpaths)
    seen = set()
    dupes = sorted({r for r in relpaths if r in seen or seen.add(r)})
    if dupes:
        raise PackageError(f"a release must not carry the same path twice: {dupes}")
    unexpected = sorted(set(relpaths) - _release_paths())
    if unexpected:
        _reject("release", f"it carries files that are not the runtime: {unexpected}")
    missing = sorted(_release_paths() - set(relpaths))
    if missing:
        _reject("release", f"the runtime imports these and the release is missing them: {missing}")


def plan(root: Path = ROOT) -> dict:
    """Every file that would go into the release, with its size and sha256, or a PackageError."""
    root = Path(root)
    files: list[dict] = []
    for name in ROOT_FILES:
        source = root / name
        if not source.exists():
            _reject(name, "listed as runtime but missing from the tree")
        _check_file(name, source)
        files.append({"path": name, "size": source.stat().st_size,
                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    package = root / PACKAGE_DIR
    if package.is_symlink():
        _reject(PACKAGE_DIR, "is a symlink; the package is walked for real files only")
    if not package.is_dir():
        _reject(PACKAGE_DIR, "the agent package is missing")
    for source in sorted(package.rglob("*")):
        rel = source.relative_to(root).as_posix()
        if source.name.endswith((".pyc", ".pyo")) or "__pycache__" in source.parts:
            continue  # our own interpreter's cache from earlier test runs: never packaged, and never the plan's fault
        if source.is_dir() and not source.is_symlink():  # a symlinked dir would smuggle a whole tree in
            continue
        if source.suffix not in ALLOWED_SUFFIXES:
            _reject(rel, "only .py/.json/.toml/.lock files are allowed in the package")
        if source.suffix == ".py" and any(word in rel.lower() for word in ("fixture", "sample_data", "scenario")):
            _reject(rel, "a data or fixture module never belongs in the runtime")
        _check_file(rel, source)
        files.append({"path": rel, "size": source.stat().st_size,
                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    _audit_paths(f["path"] for f in files)
    return {"root": str(root), "files": files, "count": len(files),
            "bytes": sum(f["size"] for f in files)}


def _stage_meta(out: Path) -> Path:
    """Our ownership record for a stage sits BESIDE it, keyed by its resolved path — never inside the staging
    folder, because `atira test-run` uploads every file it finds there and the upload must be the runtime only."""
    return Path(f"{out.parent / out.name}{STAGE_META_SUFFIX}")


def _glob_escape(name: str) -> str:
    return name.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def _stage_is_ours(out: Path) -> bool:
    """True when the ownership record beside the stage says we created it."""
    # glob, not a literal path: a stage named with [ ] or * is still ours; the key match below confirms it
    for meta in out.parent.glob(_glob_escape(out.name) + STAGE_META_SUFFIX):
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("staged_by") == "scripts/package_release.py" and record.get("stage") == str(out.resolve()):
            return True
    return False


def _stage_mark(out: Path) -> None:
    # drop any stale records for this name (a renamed stage must not leave orphans), then write ours
    for meta in out.parent.glob(_glob_escape(out.name) + STAGE_META_SUFFIX):
        meta.unlink(missing_ok=True)
    _stage_meta(out).write_text(json.dumps(
        {"staged_by": "scripts/package_release.py", "stage": str(Path(out).resolve())}) + "\n", encoding="utf-8")


def _parseable(path: Path) -> None:
    """Syntax-check a .py file by AST parse — compileall would litter the stage with __pycache__."""
    try:
        ast.parse(path.read_bytes(), filename=str(path))
    except SyntaxError as exc:
        _reject(path.name, f"it does not parse: {exc}")


def stage(out: Path, root: Path = ROOT) -> dict:
    """Copy the plan into a clean staging directory: guard the target first, build elsewhere, swap last.

    Nothing is deleted before it is proven replaceable, so a bad --out (the source root, an ancestor, a symlink,
    or a folder with someone else's files in it) cannot destroy anything. Even our own marked stage is only
    replaced while it still holds EXACTLY the runtime: a file anyone added since the marker was written is
    foreign content and the stage is refused whole, never rmtree'd. The ownership record lives beside the stage,
    never inside it (`atira test-run` uploads every file in the folder).
    """
    import os as _os
    import shutil
    import tempfile

    out = Path(out)
    if not out.is_absolute():
        out = Path(_os.getcwd()) / out  # resolve relative --out against the CWD now, not against a temp dir later
    listing = plan(root)  # nothing is touched until the tree to copy has been audited

    # guard before ANY mutation: out must be absent, an empty dir, or a folder this script staged before
    resolved_root = Path(root).resolve()
    if out.is_symlink():
        _reject(str(out), "is a symlink; a staging folder is never created through one")
    if out.exists() and not out.is_dir():
        _reject(str(out), "exists and is not a directory")
    ours = False
    if out.exists():
        resolved = out.resolve()
        if resolved == resolved_root or resolved_root.is_relative_to(resolved):
            _reject(str(out), "is the source tree or an ancestor of it; refusing to delete anything")
        if any(out.iterdir()):
            if not _stage_is_ours(out):
                _reject(str(out), "holds files this script did not stage; refusing to delete it — remove it "
                                  "yourself or point --out at a fresh folder")
            ours = True
    # build in a sibling temp folder (same filesystem: the swap is a rename), then move it into place
    build_parent = Path(tempfile.mkdtemp(prefix=f".{out.name}.build.", dir=str(out.parent if out.parent.is_dir()
                                   else Path(resolved_root))))
    build = build_parent / out.name
    try:
        build.mkdir()
        for item in listing["files"]:
            source = Path(root) / item["path"]
            target = build / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        expected = {item["path"] for item in listing["files"]}
        staged = _audit_tree(build)
        _audit_paths(staged)
        if extra := (staged - expected):
            _reject(str(out), f"staged tree holds something unplanned: {sorted(extra)}")
        for path in sorted(build.rglob("*.py")):
            _parseable(path)
        if ours:
            # The marker proves who CREATED the folder, not what it holds NOW. Anyone could have added content
            # since; only a stage that still holds EXACTLY the runtime (plus its planned dirs, no symlinks) may
            # be replaced — foreign additions are refused whole and kept alive, never rmtree'd.
            keepable = {f["path"] for f in listing["files"]}
            if any(p.is_symlink() for p in out.rglob("*")):
                _reject(str(out), "is our own stage but now contains a symlink; refusing to follow or delete it")
            expected_dirs = {p.as_posix() for path in keepable for p in Path(path).parents[:-1]}
            present = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() or p.is_dir()}
            foreign = sorted(present - keepable - expected_dirs)
            if foreign:
                _reject(str(out), "is our own stage but now also holds files we did not stage "
                                  f"({foreign}); refusing to delete them — remove them yourself or point "
                                  "--out at a fresh folder")
            # proven replaceable: swap (nothing foreign can ride along; extras cannot survive the rename)
            shutil.rmtree(out)
        os.replace(build, out)
        _stage_mark(out)
    finally:
        shutil.rmtree(build_parent, ignore_errors=True)
    return listing


def _audit_tree(folder: Path) -> set[str]:
    """Every file under a folder: a symlink anywhere (to a file or a directory) is refused on sight."""
    found = []
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink():  # before the is_dir test: a symlinked dir must not be skipped either
            _reject(rel, "a staged symlink")
        if path.is_dir():
            continue
        _check_file(rel, path)
        found.append(rel)
    return set(found)


def check(target: Path) -> dict:
    """Audit a staging folder or a .tar.gz: the exact runtime set, every member allowlisted, regular, in the cap."""
    import tarfile

    target = Path(target)
    if target.is_dir():
        found = sorted(_audit_tree(target))
        if not found:
            _reject(str(target), "holds nothing at all: an empty folder is not a release")
        _audit_paths(found)
        return {"target": str(target), "files": found, "count": len(found)}
    if tarfile.is_tarfile(target):
        found = []
        with tarfile.open(target) as archive:
            for member in archive.getmembers():
                rel = Path(member.name).as_posix()
                if not _safe_relpath(rel):
                    _reject(rel, "unsafe or disallowed path in the archive")
                if any(word in rel.lower() for word in FORBIDDEN_WORDS):
                    _reject(rel, "the archive names something that must stay out of a release")
                if member.issym() or member.islnk() or not member.isfile():
                    _reject(rel, "only regular files, no links or directories-as-entries")
                if member.size < 1 or member.size > MAX_FILE_BYTES:
                    _reject(rel, f"size {member.size} outside the cap")
                found.append(rel)
        if not found:
            _reject(str(target), "is an empty archive: not a release")
        _audit_paths(found)
        return {"target": str(target), "files": sorted(found), "count": len(found)}
    _reject(str(target), "neither a folder nor a tarball")
    raise AssertionError  # pragma: no cover


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "stage", "check"))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--out", default=str(ROOT.parent / "release-stage"))
    parser.add_argument("--target", help="folder or .tar.gz to audit (check); defaults to --out")
    parser.add_argument("--json", action="store_true", help="print the machine-readable result")
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            result = plan(Path(args.root))
        elif args.action == "stage":
            result = stage(Path(args.out), Path(args.root))
        else:
            result = check(Path(args.target or args.out))
    except PackageError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{args.action}: {result.get('count')} files, {result.get('bytes', 'n/a')} bytes")
        for item in result.get("files", []):
            print("  " + (item["path"] if isinstance(item, dict) else item))
    return 0


if __name__ == "__main__":
    sys.exit(main())
