"""read-only commit comparison for personal darktable installations"""

import argparse
import json
import re
import subprocess
from pathlib import Path


def command(args):
    options = {}
    if hasattr(subprocess, "STARTUPINFO"):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30, **options)


def git(repo, *args):
    return command(["git", "-C", str(repo), *args])


def resolve(repo, ref):
    if not ref or ref.startswith("-"):
        return None
    result = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    return result.stdout.strip() if result.returncode == 0 else None


def ancestor(repo, older, newer):
    result = git(repo, "merge-base", "--is-ancestor", older, newer)
    if result.returncode not in (0, 1):
        raise ValueError("cannot compare commit ancestry; check fetched history")
    return result.returncode == 0


def version_identity(output):
    match = re.search(r"^darktable\s+(\S+)", output, re.MULTILINE)
    if not match:
        return None, None
    version = match.group(1)
    if "dirty" in version:
        return version, None
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", version):
        return version, version.lower()
    described = re.search(r"(?:~|-)g([0-9a-fA-F]{7,40})(?:$|[~-])", version)
    return version, described.group(1).lower() if described else None


def software_changed(repo, installed, target, paths):
    for path in paths:
        if path.startswith(".agents/") or path.endswith(".md"):
            continue
        if path == ".github/workflows/personal-packages.yml":
            before = git(repo, "show", installed + ":" + path)
            after = git(repo, "show", target + ":" + path)
            trigger_filter = "    paths-ignore:\n      - '.agents/**'\n      - '**/*.md'\n"
            if (before.returncode == 0 and after.returncode == 0
                    and before.stdout.replace(trigger_filter, "") == after.stdout.replace(trigger_filter, "")):
                continue
        return True
    return False


def inspect(repo, target_ref, output, remote_tip=None, installed_commit=None):
    target = resolve(repo, target_ref)
    if target is None:
        raise ValueError("target commit is unavailable; fetch the selected branch first")
    if remote_tip is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", remote_tip):
        raise ValueError("remote tip must be a verified full Git SHA")
    version, commit_hint = version_identity(output or "")
    if installed_commit:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", installed_commit):
            raise ValueError("installed commit must be a verified full Git SHA")
        if version and ("dirty" in version or (commit_hint and not installed_commit.lower().startswith(commit_hint))):
            raise ValueError("installed commit conflicts with the executable version")
        commit_hint = installed_commit.lower()
    installed = resolve(repo, commit_hint) if commit_hint else None
    result = {
        "installed_version": version,
        "installed_commit": installed,
        "target_commit": target,
        "remote_verified": remote_tip is not None and target == remote_tip.lower(),
        "relation": "unknown" if output or installed_commit else "missing",
        "changed_paths": [],
        "software_changed": None,
        "needs_install_or_update": None,
        "build_allowed_by_version": False,
        "ai_compiled": None,
    }
    ai = re.search(r"^\s*AI\s*->\s*(ENABLED|DISABLED)\s*$", output or "", re.MULTILINE)
    if ai:
        result["ai_compiled"] = ai.group(1) == "ENABLED"
    if remote_tip is not None and not result["remote_verified"]:
        result["relation"] = "stale_target"
        return result
    if installed is None:
        if result["relation"] == "missing":
            result["needs_install_or_update"] = True
        return result
    if installed == target:
        result.update(relation="current", software_changed=False, needs_install_or_update=False)
    elif ancestor(repo, installed, target):
        diff = git(repo, "diff", "--name-only", "--no-renames", installed, target, "--")
        if diff.returncode:
            raise ValueError("cannot read changed paths")
        paths = diff.stdout.splitlines()
        changed = software_changed(repo, installed, target, paths)
        result.update(relation="behind", changed_paths=paths, software_changed=changed,
                      needs_install_or_update=changed,
                      build_allowed_by_version=changed and result["remote_verified"])
    elif ancestor(repo, target, installed):
        result.update(relation="ahead", needs_install_or_update=False)
    else:
        result["relation"] = "diverged"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--target-ref", default="origin/codex/dev")
    parser.add_argument("--remote-tip", help="full SHA just read from the selected remote branch")
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--installed-executable", type=Path)
    baseline.add_argument("--version-file", type=Path, help="saved version output for offline checks")
    parser.add_argument("--installed-commit", help="verified full commit from installation/package provenance")
    args = parser.parse_args()
    try:
        output = None
        if args.version_file:
            output = args.version_file.read_text(encoding="utf-8-sig")
        elif args.installed_executable:
            if not args.installed_executable.is_file():
                raise ValueError("configured executable does not exist; discover the actual installation")
            check = command([str(args.installed_executable.resolve()), "--version"])
            output = check.stdout
            # the GUI version path can return 1 even when it printed a valid version
            if check.returncode not in (0, 1) or not version_identity(output)[0]:
                raise ValueError("executable did not return recognizable darktable version output")
        result = inspect(args.repo, args.target_ref, output, args.remote_tip, args.installed_commit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error), "build_allowed_by_version": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
