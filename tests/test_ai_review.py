"""AI review tests with a fake Claude client (no network, no cost)."""
import re
from pathlib import Path
from types import SimpleNamespace

from app.engine import ai_review
from app.engine.ai_review import ReviewResult, Suggestion
from app.engine.ledger import LedgerJob

FIX = Path(__file__).parent / "fixtures" / "shakun"

GUESSES = {"Punjab National Bank": None, "Share Capital": "other", "Land": "other",
           "Bank Interest": "expense", "Interest Received": "other"}


class FakeMessages:
    def __init__(self):
        self.calls = []

    def parse(self, **kw):
        self.calls.append(kw)
        lines = kw["messages"][0]["content"].splitlines()[1:]
        sugs = []
        for line in lines:
            idx, rest = line.split(". ", 1)
            name = rest.split(" | ")[0]
            if "Transport" in name or "Carrier" in name or "Roadways" in name:
                cat, conf = "party", "high"
            elif "Interest" in name:
                cat, conf = "expense", "medium"
            else:
                cat, conf = "other", "low"
            sugs.append(Suggestion(index=int(idx), category=cat, confidence=conf, reason="test"))
        return SimpleNamespace(stop_reason="end_turn", parsed_output=ReviewResult(suggestions=sugs))


def job():
    return LedgerJob("shakun").load((FIX / "ledgers.xls").read_bytes(), (FIX / "trial.xls").read_bytes())


def test_only_default_other_sent_in_batches():
    j = job()
    cands = j.ai_candidates()
    assert cands and all(j.records[i].category == "other" for i in cands)
    fake = SimpleNamespace(messages=FakeMessages())
    n = j.run_ai_review(client=fake)
    assert n == len(cands)
    assert len(fake.messages.calls) == -(-len(cands) // ai_review.BATCH_SIZE)
    sent = "\n".join(c["messages"][0]["content"] for c in fake.messages.calls)
    assert "GSTIN: no" in sent
    assert not re.search(r"\d{2}[A-Z]{5}\d{4}[A-Z]", sent)           # no GSTIN / PAN values sent
    assert all(c["model"] == "claude-opus-5-5" for c in fake.messages.calls)


def test_suggestions_need_acceptance():
    j = job()
    j.run_ai_review(client=SimpleNamespace(messages=FakeMessages()))
    r = next(r for r in j.records if r.name == "Guru Nanak Goods Carriers")
    assert r.category == "other" and r.ai["status"] == "pending"     # not applied yet
    before = j.summary()["controls"]
    n = j.accept_ai(min_confidence="high")
    assert n >= 1 and r.category == "party" and r.ai["status"] == "accepted"
    assert j.summary()["controls"] == before                        # balances untouched
    medium = next(r for r in j.records if r.ai and r.ai["confidence"] == "medium")
    assert medium.ai["status"] == "pending"
    confirmed = next(r for r in j.records if r.name == "Share Capital")
    assert confirmed.ai["status"] == "confirmed" and confirmed.category == "other"


def test_user_override_dismisses_suggestion():
    j = job()
    j.run_ai_review(client=SimpleNamespace(messages=FakeMessages()))
    i = next(i for i, r in enumerate(j.records) if r.ai and r.ai["status"] == "pending")
    j.set_override(i, category="tax")
    assert j.records[i].ai["status"] == "dismissed"
    assert j.accept_ai() == j.summary()["ai"]["accepted"]


def test_refusal_returns_no_suggestions():
    class Refuse:
        def parse(self, **kw):
            return SimpleNamespace(stop_reason="refusal", parsed_output=None)
    j = job()
    assert j.run_ai_review(client=SimpleNamespace(messages=Refuse())) == 0
