from app.config import load_dotenv, parse_dotenv

SAMPLE = '''
# comment
STORE_BACKEND=redis                  # redis | memory
UPSTASH_REDIS_REST_URL="https://x.upstash.io"
GROQ_API_KEY='gsk_abc#def'
export LLM_MODE=live                 # live | mock
DEBUG_TOKEN= "tok 1"
EMPTY=
LLM_BATCH_SIZE=1                     # 1 = one call
BAD LINE
'''


def test_parse_dotenv_handles_quotes_comments_and_export():
    d = parse_dotenv(SAMPLE)
    assert d["STORE_BACKEND"] == "redis" and d["LLM_MODE"] == "live" and d["LLM_BATCH_SIZE"] == "1"
    assert d["UPSTASH_REDIS_REST_URL"] == "https://x.upstash.io"
    assert d["GROQ_API_KEY"] == "gsk_abc#def"            # '#' inside quotes is kept
    assert d["DEBUG_TOKEN"] == "tok 1" and d["EMPTY"] == ""
    assert "BAD LINE" not in d


def test_load_dotenv_never_overrides_real_env_and_is_disabled_under_tests(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("FOO_A=1\nFOO_B=2\n", encoding="utf-8")
    assert load_dotenv(f) == []                          # VERA_NO_DOTENV is set by conftest
    monkeypatch.delenv("VERA_NO_DOTENV")
    monkeypatch.setenv("FOO_B", "real")
    monkeypatch.delenv("FOO_A", raising=False)
    assert load_dotenv(f) == ["FOO_A"]
    import os
    assert os.environ["FOO_A"] == "1" and os.environ["FOO_B"] == "real"
    monkeypatch.delenv("FOO_A")
    monkeypatch.setenv("VERCEL", "1")
    assert load_dotenv(f) == []                          # never on Vercel
