#!/usr/bin/env python3
"""Render local .dev-input/ worker-loop config from tracked templates.

Templates carry NO machine paths and NO scenario facts; absolute paths and the
current base sha are computed here at run time and written only into the
ignored .dev-input/ directory. Session ids stay empty until a wave is
bootstrapped — the controller also adopts by worktree cwd, so empty sids work.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TPL = REPO / ".dev-input-templates"
ROLES = ("runtime", "core", "integration")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None,
                    help="base commit (default: current HEAD)")
    ap.add_argument("--work-root", default=None,
                    help="worktrees root (default: <repo parent>/work/qwen-worktrees)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    base = a.base or subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    work = Path(a.work_root) if a.work_root else REPO.parent / "work" / "qwen-worktrees"
    out = REPO / ".dev-input"
    out.mkdir(exist_ok=True)

    plan = json.loads((TPL / "worktree-plan.template.json").read_text())
    plan["base"] = base
    for role in plan["worktrees"]:
        plan["worktrees"][role] = str(work / role)
    boot = json.loads((TPL / "bootstrap-workers.template.json").read_text())
    for role, w in boot["workers"].items():
        w["directory"] = str(work / role)
        w["log"] = str(work.parent / f"qwen-{role}.jsonl")

    for name, data in (("worktree-plan.json", plan),
                       ("bootstrap-workers.json", boot)):
        dest = out / name
        if dest.exists() and not a.force:
            print(f"kept existing {dest} (--force to overwrite)")
            continue
        dest.write_text(json.dumps(data, indent=2) + "\n")
        print(f"wrote {dest}")

    prompts = out / "worker-prompts"
    prompts.mkdir(exist_ok=True)
    for role in ROLES:
        dest = prompts / f"{role}.txt"
        src = TPL / "worker-prompts" / f"{role}.txt"
        if dest.exists() and not a.force:
            print(f"kept existing {dest}")
            continue
        shutil.copyfile(src, dest)
        print(f"wrote {dest}")
    print("base:", base, "\nwork root:", work)


if __name__ == "__main__":
    main()
