import os

os.environ["VERA_NO_DOTENV"] = "1"          # tests must never pick up real keys from .env
os.environ["STORE_BACKEND"] = "memory"
os.environ["LLM_MODE"] = "mock"
os.environ.pop("DEBUG_TOKEN", None)

import importlib.util  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.config import reset_settings  # noqa: E402
from app.store import set_store  # noqa: E402
from app.store.memory_store import MemoryStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOW_ISO = "2026-04-26T10:35:00Z"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_dataset", ROOT / "dataset" / "generate_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def dataset():
    """Seed + expanded dataset exactly as magicpin's generator produces it."""
    g = _load_generator()
    rnd = random.Random(g.SEED)
    def _j(p):
        return json.loads(Path(p).read_text(encoding="utf-8"))
    d = ROOT / "dataset"
    cats = {c["slug"]: c for c in (_j(f) for f in (d / "categories").glob("*.json"))}
    m_seeds, c_seeds, t_seeds = (_j(d / "merchants_seed.json")["merchants"], _j(d / "customers_seed.json")["customers"],
                                 _j(d / "triggers_seed.json")["triggers"])
    merchants = g.expand_merchants(m_seeds, rnd)
    customers = g.expand_customers(c_seeds, merchants, rnd)
    triggers = g.expand_triggers(t_seeds, merchants, customers, rnd)
    return {"categories": cats, "merchants": merchants, "customers": customers, "triggers": triggers,
            "trigger_seeds": t_seeds}


@pytest.fixture(autouse=True)
def fresh_env():
    reset_settings()
    store = MemoryStore()
    set_store(store)
    yield store
    set_store(None)
    reset_settings()


@pytest_asyncio.fixture
async def client():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def push(client, scope, cid, payload, version=1):
    return await client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                                  "payload": payload, "delivered_at": NOW_ISO})


async def push_seed(client, dataset, trigger_id, extra_version=1):
    """Push category + merchant (+ customer) + trigger for one seed trigger."""
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == trigger_id)
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    await push(client, "category", m["category_slug"], dataset["categories"][m["category_slug"]])
    await push(client, "merchant", m["merchant_id"], m)
    if t.get("customer_id"):
        c = next(x for x in dataset["customers"] if x["customer_id"] == t["customer_id"])
        await push(client, "customer", c["customer_id"], c)
    await push(client, "trigger", t["id"], t, extra_version)
    return t, m
