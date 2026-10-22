"""Check release candidates, not historical file contents; never print matches."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qsl, urlsplit


ALLOWED = re.compile(
    r"(?:pyproject\.toml|README(?:\.en)?\.md|LICENSE|SECURITY\.md|CONTRIBUTING\.md|"
    r"config\.example\.toml|\.gitignore|\.gitattributes|docs/[^/]+\.md|"
    r"src/cloudsub/[^/]+\.py|tests/test_[^/]+\.py|scripts/check_public\.py|"
    r"\.github/workflows/[^/]+\.ya?ml)"
)
EMAIL = re.compile(r"[\w.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PRIVATE_PATH = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]|/(?:Users|home)/[^/\s\"'<>]+")
CREDENTIAL_KEY = (
    r"(?<![\w])[\"']?(?:[A-Za-z_][A-Za-z0-9_]*_)?"
    r"(?:password|passwd|token|api[_-]?key|translation[_-]?key|secret|authorization|cookie|"
    r"access[_-]?token|refresh[_-]?token)[\"']?"
)
ASSIGNMENT = re.compile(r"(?i)" + CREDENTIAL_KEY + r"\s*[:=]\s*([\"'])([^\r\n]*?)\1")
YAML_SCALAR = re.compile(r"(?i)^\s*(?:-\s*)?" + CREDENTIAL_KEY + r"\s*:\s*([^\"'\s#][^\r\n#]*)")
TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,}|"
    r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
URL = re.compile(r"https?://[^\s\"'<>`]+")
SENSITIVE_QUERY = {
    "token", "access_token", "refresh_token", "password", "api_key",
    "signature", "sig", "x-amz-signature", "x-goog-signature",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def example_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in ("example.com", "example.org", "example.net", "invalid", "test"))


def placeholder(value: str) -> bool:
    if not value or (value.startswith("<") and value.endswith(">")):
        return True
    return bool(re.fullmatch(
        r"(?i)(?:(?:test|dummy|fake|example|your|placeholder|redacted)(?:[-_ ].*)?"
        r"|changeme|\$\{[^}]+\}|\$\{\{[^}]+\}\}|\$(?:env:)?[A-Za-z_][A-Za-z0-9_]*|__\w+__)", value
    ))


def line_rules(line: str, *, is_yaml: bool = False) -> set[str]:
    rules = set()
    if PRIVATE_PATH.search(line):
        rules.add("private-absolute-path")
    if any(not example_host(match[1]) for match in EMAIL.finditer(line)):
        rules.add("personal-email")
    if TOKEN.search(line):
        rules.add("credential-pattern")
    if any(not placeholder(match[2]) for match in ASSIGNMENT.finditer(line)):
        rules.add("credential-assignment")
    if is_yaml:
        match = YAML_SCALAR.search(line)
        if match:
            value = match[1].strip()
            if value not in {"null", "~", "true", "false", "|", ">", "|-", ">-"} and not placeholder(value):
                rules.add("credential-assignment")
    for match in URL.finditer(line):
        try:
            parsed = urlsplit(match[0])
            if parsed.password is not None and not example_host(parsed.hostname or ""):
                rules.add("url-credentials")
            if not example_host(parsed.hostname or "") and any(
                key.lower() in SENSITIVE_QUERY and not placeholder(value)
                for key, value in parse_qsl(parsed.query)
            ):
                rules.add("signed-url")
        except ValueError:
            rules.add("invalid-url")
    return rules


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def candidates(root: Path) -> list[str]:
    result = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if result.returncode:
        raise RuntimeError("Cannot enumerate Git release candidates")
    return sorted(set(result.stdout.decode("utf-8", errors="surrogateescape").strip("\0").split("\0")) - {""})


def scan(root: Path) -> list[Finding]:
    root = root.resolve()
    findings = []
    for name in candidates(root):
        if not ALLOWED.fullmatch(name):
            findings.append(Finding(name, 0, "path-not-allowed"))
            continue
        path = root / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
            findings.append(Finding(name, 0, "symlink-not-allowed"))
            continue
        try:
            if not path.resolve().is_relative_to(root):
                findings.append(Finding(name, 0, "path-outside-repository"))
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(Finding(name, 0, "unreadable-text"))
            continue
        for number, line in enumerate(content.splitlines(), 1):
            findings.extend(Finding(name, number, rule) for rule in sorted(
                line_rules(line, is_yaml=path.suffix.lower() in {".yml", ".yaml"})
            ))
    return findings


def history_counts(root: Path) -> tuple[int, int]:
    count = git(root, "rev-list", "--count", "HEAD")
    if count.returncode:
        return 0, 0
    authors = git(root, "log", "--format=%aN <%aE>")
    if authors.returncode:
        raise RuntimeError("Cannot count historical author identities")
    return int(count.stdout), len(set(authors.stdout.splitlines()))


def main(root: Path | None = None) -> int:
    root = root or Path(__file__).resolve().parents[1]
    try:
        findings = scan(root)
        commits, authors = history_counts(root)
    except (OSError, RuntimeError, ValueError):
        print("Release check failed: cannot inspect the repository.")
        return 1
    for finding in findings:
        print(f"{finding.path!r}:{finding.line}: {finding.rule}")
    print(f"History: {commits} commit(s), {authors} author identity/identities; historical contents not scanned.")
    if findings:
        print(f"Release check blocked: {len(findings)} finding(s). Matched contents are never printed.")
        return 1
    print("Release candidates passed. This check cannot guarantee that all private data is absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
