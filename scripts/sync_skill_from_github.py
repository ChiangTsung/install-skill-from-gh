#!/usr/bin/env python3
"""Inspect a GitHub-hosted skill and install it only if it passes a safety check."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


HIGH_RISK_PATTERNS = [
    (re.compile(r"\bcurl\b.+\|\s*(bash|sh)\b"), "shell pipeline downloads and executes remote code"),
    (re.compile(r"\bwget\b.+\|\s*(bash|sh)\b"), "shell pipeline downloads and executes remote code"),
    (re.compile(r"\brm\s+-rf\s+/"), "destructive root delete command"),
    (re.compile(r"\beval\s*\("), "dynamic eval usage"),
    (re.compile(r"\bexec\s*\("), "dynamic exec usage"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb pattern"),
]

MEDIUM_RISK_PATTERNS = [
    (re.compile(r"\bos\.system\s*\("), "shell execution via os.system"),
    (re.compile(r"\bsubprocess\.(run|Popen|call)\s*\("), "process execution via subprocess"),
    (re.compile(r"\bshutil\.rmtree\s*\("), "recursive directory removal"),
    (re.compile(r"\bchmod\s+777\b"), "overly permissive chmod"),
    (re.compile(r"\bsudo\b"), "privileged shell command"),
    (re.compile(r"\bssh\b"), "ssh command"),
    (re.compile(r"\bscp\b"), "scp command"),
]

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
}


@dataclass
class Finding:
    severity: str
    path: str
    reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub owner/repo")
    parser.add_argument("--url", help="GitHub repo or tree URL")
    parser.add_argument("--path", help="Path to the skill within the repo")
    parser.add_argument("--ref", default="main", help="Git ref to inspect/install")
    parser.add_argument("--dest", help="Destination skills directory")
    parser.add_argument("--name", help="Destination skill name override")
    parser.add_argument(
        "--method",
        choices=("auto", "download", "git"),
        default="auto",
        help="Install method to pass to the system installer",
    )
    parser.add_argument("--check-only", action="store_true", help="Only inspect the candidate")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()
    if not args.repo and not args.url:
        parser.error("one of --repo or --url is required")
    return args


def parse_github_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    if parsed.netloc not in {"github.com", "www.github.com"}:
        raise ValueError("URL must point to github.com")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("URL must include owner and repo")
    repo = f"{parts[0]}/{parts[1]}"
    ref = "main"
    skill_path = ""
    if len(parts) >= 5 and parts[2] == "tree":
        ref = parts[3]
        skill_path = "/".join(parts[4:])
    elif len(parts) > 2 and parts[2] not in {"tree", "blob"}:
        skill_path = "/".join(parts[2:])
    return repo, ref, skill_path


def resolve_source(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.url:
        repo, ref, url_path = parse_github_url(args.url)
        if args.repo and args.repo != repo:
            raise ValueError("--repo does not match --url")
        repo = args.repo or repo
        ref = args.ref if args.ref != "main" else ref
        path = args.path or url_path
        return repo, ref, path
    return args.repo, args.ref, args.path or ""


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def sparse_clone(repo: str, ref: str, rel_path: str) -> tuple[Path, Path]:
    tempdir = Path(tempfile.mkdtemp(prefix="skill-sync-"))
    repo_url = f"https://github.com/{repo}.git"
    clone = run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, str(tempdir)])
    if clone.returncode != 0:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise RuntimeError(clone.stderr.strip() or clone.stdout.strip() or "git clone failed")

    checkout = run(["git", "-C", str(tempdir), "checkout", ref])
    if checkout.returncode != 0:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise RuntimeError(checkout.stderr.strip() or checkout.stdout.strip() or f"git checkout {ref} failed")

    if rel_path:
        sparse = run(["git", "-C", str(tempdir), "sparse-checkout", "set", "--no-cone", rel_path])
        if sparse.returncode != 0:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise RuntimeError(sparse.stderr.strip() or sparse.stdout.strip() or "git sparse-checkout failed")

    target = tempdir / rel_path if rel_path else tempdir
    return tempdir, target


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    try:
        sample = path.read_bytes()[:1024]
    except OSError:
        return False
    return b"\x00" not in sample


def scan_text(path: Path, rel_path: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return findings
    for pattern, reason in HIGH_RISK_PATTERNS:
        if pattern.search(content):
            findings.append(Finding("high", rel_path, reason))
    for pattern, reason in MEDIUM_RISK_PATTERNS:
        if pattern.search(content):
            findings.append(Finding("medium", rel_path, reason))
    return findings


def scan_tree(target: Path, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(target.rglob("*")):
        rel_path = str(path.relative_to(repo_root))
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            findings.append(Finding("high", rel_path, "symlink present"))
            continue
        if not path.is_file():
            continue
        try:
            mode = path.stat().st_mode
        except OSError:
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append(Finding("medium", rel_path, "large file over 5 MB"))
        if stat.S_ISREG(mode) and mode & stat.S_IXUSR and "scripts" in path.parts:
            findings.append(Finding("medium", rel_path, "executable file inside scripts"))
        if is_text_file(path):
            findings.extend(scan_text(path, rel_path))
        elif "assets" not in path.parts:
            findings.append(Finding("medium", rel_path, "binary or opaque file outside assets"))
    return findings


def build_report(repo: str, ref: str, rel_path: str, target: Path, repo_root: Path) -> dict:
    skill_md = target / "SKILL.md"
    findings = scan_tree(target, repo_root) if target.exists() else []
    high_findings = [f for f in findings if f.severity == "high"]
    return {
        "repo": repo,
        "ref": ref,
        "path": rel_path or ".",
        "resolved_path": str(target),
        "is_skill": skill_md.exists(),
        "safe_to_install": skill_md.exists() and not high_findings,
        "findings": [asdict(f) for f in findings],
    }


def print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    print(f"repo: {report['repo']}")
    print(f"ref: {report['ref']}")
    print(f"path: {report['path']}")
    print(f"is_skill: {report['is_skill']}")
    print(f"safe_to_install: {report['safe_to_install']}")
    if report["findings"]:
        print("findings:")
        for finding in report["findings"]:
            print(f"  - [{finding['severity']}] {finding['path']}: {finding['reason']}")
    else:
        print("findings: none")


def installer_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / ".system" / "skill-installer" / "scripts" / "install-skill-from-github.py"


def install_skill(args: argparse.Namespace) -> None:
    script = installer_path()
    if not script.exists():
        raise RuntimeError(f"installer script not found: {script}")
    cmd = [sys.executable, str(script)]
    if args.url:
        cmd.extend(["--url", args.url])
    else:
        cmd.extend(["--repo", args.repo])
    if args.path:
        cmd.extend(["--path", args.path])
    if args.ref:
        cmd.extend(["--ref", args.ref])
    if args.dest:
        cmd.extend(["--dest", args.dest])
    if args.name:
        cmd.extend(["--name", args.name])
    if args.method:
        cmd.extend(["--method", args.method])
    proc = subprocess.run(cmd, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("installation failed")


def main() -> int:
    args = parse_args()
    try:
        repo, ref, rel_path = resolve_source(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo_root: Path | None = None
    try:
        repo_root, target = sparse_clone(repo, ref, rel_path)
        report = build_report(repo, ref, rel_path, target, repo_root)
        print_report(report, args.json)
        if args.check_only:
            return 0 if report["safe_to_install"] else 1
        if not report["safe_to_install"]:
            print("installation aborted because the candidate did not pass the safety check", file=sys.stderr)
            return 1
        install_skill(args)
        print("Installed successfully. Restart Codex to pick up the new skill.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if repo_root is not None:
            shutil.rmtree(repo_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
