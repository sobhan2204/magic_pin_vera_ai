"""Guard: real credentials must only ever live in .env (git-ignored), never in files that get committed."""
import re
import subprocess

from app.config import parse_dotenv
from conftest import ROOT

SECRET_PATTERNS = [r"gsk_[A-Za-z0-9]{20,}", r"csk-[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9]{30,}", r"gQAAAA[A-Za-z0-9=]{20,}",
                   r"[a-z0-9-]+\.upstash\.io"]


def test_env_example_has_no_secret_values():
    values = parse_dotenv((ROOT / ".env.example").read_text(encoding="utf-8-sig"))
    for k in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "GROQ_API_KEY", "ALT_API_KEY", "ALT_BASE_URL", "DEBUG_TOKEN",
              "CONTACT_EMAIL", "SUBMITTED_AT", "ALT_MODEL", "ALT_PROVIDER_NAME"):
        assert values.get(k, "") == "", f".env.example must leave {k} empty"


def test_env_file_is_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "!.env.example" in ignored


def test_no_secret_patterns_in_tracked_or_untracked_source_files():
    try:
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.splitlines()
    except Exception:
        files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and ".env" != p.name]
    hits = []
    for rel in files:
        if rel == ".env" or rel.startswith(("dataset/", "examples/", "magicpin-ai-challenge/")) or not rel.endswith(
                (".py", ".md", ".json", ".toml", ".txt", ".example", ".sh", ".ps1", ".yml", ".yaml")):
            continue
        text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for pat in SECRET_PATTERNS:
            for m in re.finditer(pat, text):
                if m.group(0) in ("x.upstash.io",) or rel == "tests/test_no_secrets.py":
                    continue
                hits.append((rel, m.group(0)[:12] + "..."))
    assert hits == [], hits
