import pytest

from app.resolver import build_factsheet
from app.normalize import normalize_trigger
from app.humanize import parse_dt
from app.verifier import extract_numbers, verify

NOW = parse_dt("2026-04-26T10:35:00Z")


@pytest.fixture
def meera_fs(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_001_research_digest_dentists")
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    return build_factsheet(normalize_trigger(t["id"], 1, t), m, dataset["categories"]["dentists"], None, NOW)


GOOD = ("Dr. Meera, JIDA Oct 2026, p.14 carries a new study: 3-month fluoride varnish recall outperforms 6-month "
        "for high-risk adult caries. Kya aap chahenge ki main abstract nikaal ke ek patient-ed WhatsApp draft kar doon?")


def out(body, cta="open_ended", **kw):
    return {"body": body, "cta": cta, **kw}


def test_good_message_passes(meera_fs):
    assert verify(out(GOOD), meera_fs) == []


def test_number_extraction_forms():
    assert extract_numbers("₹1,499 and 2,100 and 38% and 3.0% and 6pm and Wed 5 Nov and 22 days") == \
        {"1499", "2100", "38", "3", "6", "5", "22"}
    assert extract_numbers("Reply 1 for Wed, 2 for Thu. 2-min abstract", strip_allowed=True) == set()


def test_hallucinated_number_rejected(meera_fs):
    v = verify(out(GOOD.replace("3-month", "3-month (n=4,500)")), meera_fs)
    assert any("4500" in x for x in v)
    v = verify(out(GOOD.replace("Kya aap", "It cuts caries 57% more. Kya aap")), meera_fs)
    assert any("'57'" in x for x in v)


def test_unknown_entity_rejected(meera_fs):
    v = verify(out(GOOD.replace("JIDA Oct", "Lancet Oct")), meera_fs)
    assert any("Lancet" in x for x in v)


def test_taboo_rejected(meera_fs):
    v = verify(out(GOOD.replace("Kya aap", "This is guaranteed to help. Kya aap")), meera_fs)
    assert any("guaranteed" in x for x in v)


def test_jargon_and_snake_case_rejected(meera_fs):
    assert any("raw field name" in x for x in verify(out(GOOD.replace("study", "ctr_below_peer_median")), meera_fs))
    assert any("jargon" in x for x in verify(out(GOOD.replace("study", "trigger")), meera_fs))


def test_two_questions_and_buried_ask_rejected(meera_fs):
    v = verify(out(GOOD.replace("recall", "recall? Really", 1)), meera_fs)
    assert any("question mark" in x or "final sentence" in x for x in v)
    assert any("final sentence" in x for x in verify(out("Dr. Meera, want the abstract? JIDA Oct 2026, p.14 has a new study on 3-month recall for you."), meera_fs))


def test_wrong_salutation_rejected(meera_fs):
    v = verify(out(GOOD.replace("Dr. Meera", "Meera")), meera_fs)
    assert any("salutation" in x for x in v)


def test_url_rejected(meera_fs):
    v = verify(out(GOOD + " https://magicpin.com/blog"), meera_fs)
    assert any("URL" in x for x in v)


def test_shape_checks(meera_fs):
    assert verify("nope", meera_fs)
    assert verify(out(""), meera_fs) == ["body is empty"]
    assert any("cta" in x for x in verify(out(GOOD, cta="shout"), meera_fs))
    assert any("unknown fact ids" in x for x in verify(out(GOOD, facts_used=["F99"]), meera_fs))
    assert any("too short" in x for x in verify(out("Dr. Meera, hi?"), meera_fs))


def test_duplicate_detected(meera_fs):
    assert any("duplicate" in x for x in verify(out(GOOD), meera_fs, recent_bodies=[GOOD]))
    assert any("duplicate" in x for x in verify(out(GOOD), meera_fs, recent_bodies=[GOOD.replace("Kya aap chahenge", "Kya aap chahte hain")]))


def test_hi_en_needs_code_mix(dataset, meera_fs):
    assert meera_fs.language == "hi-en"                  # Meera speaks en + hi
    plain = ("Dr. Meera, JIDA Oct 2026, p.14 carries a new study: 3-month fluoride varnish recall outperforms 6-month "
             "for high-risk adult caries. Would it help to have the abstract pulled and a patient WhatsApp drafted?")
    assert any("Hindi-English" in x for x in verify(out(plain), meera_fs))

