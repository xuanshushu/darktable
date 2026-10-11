"""exercise update decisions against a disposable real Git history"""

import subprocess
import tempfile
from pathlib import Path

from check_version import inspect


def run(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


def commit(repo, path, content):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    run(repo, "add", "--", path)
    run(repo, "commit", "-m", "fixture change")
    return run(repo, "rev-parse", "HEAD")


def main():
    with tempfile.TemporaryDirectory(prefix="darktable-version-test-") as directory:
        repo = Path(directory)
        run(repo, "init")
        run(repo, "config", "user.email", "fixture@example.invalid")
        run(repo, "config", "user.name", "fixture")
        base = commit(repo, "src/app.c", "int value = 1;\n")
        documents = commit(repo, ".agents/skills/demo/SKILL.md", "instructions\n")
        code = commit(repo, "src/app.c", "int value = 2;\n")
        current = inspect(repo, code, "darktable " + code[:10], code)
        assert current["relation"] == "current" and not current["build_allowed_by_version"]
        docs = inspect(repo, documents, "darktable " + base[:10], documents)
        assert docs["relation"] == "behind" and not docs["needs_install_or_update"]
        assert not docs["build_allowed_by_version"]
        changed = inspect(repo, code, "darktable 5.3.0+42~g" + base[:10], code)
        assert changed["needs_install_or_update"] and changed["build_allowed_by_version"]
        assert "src/app.c" in changed["changed_paths"]
        ahead = inspect(repo, base, "darktable " + code[:10], base)
        assert ahead["relation"] == "ahead" and not ahead["build_allowed_by_version"]
        stale = inspect(repo, base, "darktable " + base[:10], code)
        assert stale["relation"] == "stale_target" and not stale["build_allowed_by_version"]
        offline = inspect(repo, code, "darktable " + base[:10])
        assert offline["relation"] == "behind" and not offline["build_allowed_by_version"]
        for version in ("darktable 5.2.1", "darktable " + base[:10] + "-dirty", "darktable abcdef0123"):
            unknown = inspect(repo, code, version, code)
            assert unknown["relation"] == "unknown" and not unknown["build_allowed_by_version"]
        missing = inspect(repo, code, None, code)
        assert missing["relation"] == "missing" and not missing["build_allowed_by_version"]
        provenance = inspect(repo, code, None, code, base)
        assert provenance["relation"] == "behind" and provenance["build_allowed_by_version"]
        for version in ("darktable " + code[:10], "darktable " + base[:10] + "-dirty"):
            try:
                inspect(repo, code, version, code, base)
            except ValueError:
                pass
            else:
                raise AssertionError("contradictory provenance accepted")
        ai = inspect(repo, code, "darktable " + code[:10] + "\n  AI -> DISABLED\n", code)
        assert ai["ai_compiled"] is False and not ai["build_allowed_by_version"]
        run(repo, "checkout", "--detach", base)
        alternate = commit(repo, "src/app.c", "int value = 3;\n")
        diverged = inspect(repo, code, "darktable " + alternate[:10], code)
        assert diverged["relation"] == "diverged" and not diverged["build_allowed_by_version"]
        run(repo, "checkout", "--detach", documents)
        renamed = repo / "src/renamed.c"
        renamed.write_text((repo / "src/app.c").read_text(encoding="utf-8"), encoding="utf-8")
        run(repo, "rm", "src/app.c")
        run(repo, "add", "src/renamed.c")
        run(repo, "commit", "-m", "fixture rename")
        rename_tip = run(repo, "rev-parse", "HEAD")
        rename = inspect(repo, rename_tip, "darktable " + base[:10], rename_tip)
        assert rename["build_allowed_by_version"] and "src/app.c" in rename["changed_paths"]
        workflow_path = ".github/workflows/personal-packages.yml"
        workflow = "on:\n  push:\n    branches:\n      - codex/dev\njobs:\n  build:\n    env:\n      ECO: -DUSE_AI=ON\n"
        packaged = commit(repo, workflow_path, workflow)
        filtered = workflow.replace("jobs:\n", "    paths-ignore:\n      - '.agents/**'\n      - '**/*.md'\njobs:\n")
        trigger_tip = commit(repo, workflow_path, filtered)
        trigger = inspect(repo, trigger_tip, "darktable " + packaged[:10], trigger_tip)
        assert not trigger["software_changed"] and not trigger["build_allowed_by_version"]
        flags_tip = commit(repo, workflow_path, filtered.replace("USE_AI=ON", "USE_AI=OFF"))
        flags = inspect(repo, flags_tip, "darktable " + packaged[:10], flags_tip)
        assert flags["software_changed"] and flags["build_allowed_by_version"]
    print("passed: same version, docs-only changes, upgrade, newer version, stale/unknown/dirty version, missing installation, verified package provenance, divergent history, renamed source, trigger-only changes, build flag changes")


if __name__ == "__main__":
    main()
