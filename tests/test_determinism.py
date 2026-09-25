import httpx

from app.store import set_store
from app.store.memory_store import MemoryStore
from conftest import NOW_ISO, push_seed

IDS = ["trg_001_research_digest_dentists", "trg_004_perf_dip_bharat", "trg_006_festival_diwali",
       "trg_010_ipl_match_delhi", "trg_018_supply_atorvastatin_recall", "trg_014_seasonal_acquisition_dip_powerhouse"]


async def _run(dataset):
    from app.main import app
    set_store(MemoryStore())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for tid in IDS:
            await push_seed(c, dataset, tid)
        r = await c.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": IDS})
        return r.json()["actions"]


async def test_same_inputs_give_identical_outputs(dataset):
    a = await _run(dataset)
    b = await _run(dataset)
    assert len(a) == len(IDS) and a == b


async def test_input_order_does_not_change_outputs(dataset):
    a = await _run(dataset)
    global IDS
    original = IDS
    IDS = list(reversed(original))
    try:
        b = await _run(dataset)
    finally:
        IDS = original
    assert sorted(a, key=lambda x: x["trigger_id"]) == sorted(b, key=lambda x: x["trigger_id"])
