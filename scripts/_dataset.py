"""Load the seed + expanded dataset (same expansion as dataset/generate_dataset.py) with explicit UTF-8."""
from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(dataset_dir: Path | None = None) -> dict:
    d = Path(dataset_dir or ROOT / "dataset")
    expanded = d.parent / "expanded"
    j = lambda p: json.loads(Path(p).read_text(encoding="utf-8"))
    if (expanded / "merchants").exists() and (expanded / "triggers").exists():          # already generated
        return {
            "categories": {c["slug"]: c for c in (j(f) for f in sorted((expanded / "categories").glob("*.json")))},
            "merchants": [j(f) for f in sorted((expanded / "merchants").glob("*.json"))],
            "customers": [j(f) for f in sorted((expanded / "customers").glob("*.json"))],
            "triggers": [j(f) for f in sorted((expanded / "triggers").glob("*.json"))],
        }
    spec = importlib.util.spec_from_file_location("generate_dataset", d / "generate_dataset.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)                                                           # type: ignore[union-attr]
    rnd = random.Random(g.SEED)
    cats = {c["slug"]: c for c in (j(f) for f in sorted((d / "categories").glob("*.json")))}
    m_seeds, c_seeds, t_seeds = (j(d / "merchants_seed.json")["merchants"], j(d / "customers_seed.json")["customers"],
                                 j(d / "triggers_seed.json")["triggers"])
    merchants = g.expand_merchants(m_seeds, rnd)
    customers = g.expand_customers(c_seeds, merchants, rnd)
    triggers = g.expand_triggers(t_seeds, merchants, customers, rnd)
    return {"categories": cats, "merchants": merchants, "customers": customers, "triggers": triggers}
