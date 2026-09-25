import pytest

from app.replies.classifier import classify, wait_seconds

CASES = [
    # --- exact strings from judge_simulator.py
    ("Thank you for contacting us! Our team will respond shortly.", "auto_reply"),
    ("Ok lets do it. Whats next?", "commitment"),
    ("Stop messaging me. This is useless spam.", "opt_out"),
    # --- opt-out (beats everything)
    ("Not interested. Stop messaging me.", "opt_out"),
    ("please unsubscribe me", "opt_out"),
    ("band karo ye messages", "opt_out"),
    ("mat bhejo mujhe kuch", "opt_out"),
    ("remove me from this list", "opt_out"),
    ("yes stop", "opt_out"),                                   # ambiguous: safest reading is opt-out
    ("Don't message me again", "opt_out"),
    ("leave me alone", "opt_out"),
    # --- hostile without an explicit stop
    ("Why are you bothering me. This is useless.", "hostile"),
    ("this is a scam", "hostile"),
    ("what a waste of my time", "hostile"),
    ("bakwas hai ye sab", "hostile"),
    ("you people are idiots", "hostile"),
    # --- auto replies (checked before commitment: canned text can contain 'yes'/'received')
    ("Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.", "auto_reply"),
    ("ok thanks we will get back to you", "auto_reply"),
    ("This is an automated assistant. Yes we received your message.", "auto_reply"),
    ("Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi baatein hamari team tak pahuncha deti hoon.", "auto_reply"),
    ("We are currently unavailable. Business hours are 9 to 6.", "auto_reply"),
    ("Thanks for reaching out! We'll get back to you soon.", "auto_reply"),
    # --- commitment
    ("Yes please send the abstract. Also draft the patient WhatsApp.", "commitment"),
    ("yes", "commitment"),
    ("Ok, let's do it. What's next?", "commitment"),
    ("go ahead", "commitment"),
    ("haan ji kar do", "commitment"),
    ("Mujhe magicpin judna hai", "commitment"),
    ("chalega, theek hai", "commitment"),
    ("I want to join", "commitment"),
    ("sure, send it", "commitment"),
    ("proceed", "commitment"),
    # --- later
    ("I'm busy right now, call later", "later"),
    ("baad mein baat karte hain", "later"),
    ("not now, maybe next week", "later"),
    ("kal batata hoon", "later"),
    ("tomorrow please", "later"),
    # --- off topic
    ("Btw can you also help me with my GST filing this month?", "off_topic"),
    ("can you help me get a loan", "off_topic"),
    ("I need help with my income tax return", "off_topic"),
    # --- questions / other
    ("What does the study say exactly?", "question"),
    ("how much will this cost", "question"),
    ("kya ye free hai?", "question"),
    ("Interesting, tell me about aligners", "other"),
    ("", "other"),
]


@pytest.mark.parametrize("msg,expected", CASES)
def test_classification(msg, expected):
    assert classify(msg) == expected


def test_repeated_long_message_is_auto_reply_even_without_known_phrase():
    assert classify("We appreciate your interest in our clinic", seen_before=True) == "auto_reply"
    assert classify("ok", seen_before=True) != "auto_reply"          # short replies repeat legitimately


def test_wait_lengths():
    assert wait_seconds("not now") == 3600
    assert wait_seconds("kal baat karte hain") == 86400
    assert wait_seconds("maybe next week") == 86400
