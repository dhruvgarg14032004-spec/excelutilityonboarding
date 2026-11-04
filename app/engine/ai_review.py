"""AI review of ledgers that the name rules left in the default "other" category.

Claude only SUGGESTS a category (with confidence + reason). Nothing changes until the
user accepts a suggestion. Only ledger names (+ GSTIN yes/no + balance side) are sent -
no amounts, addresses, PAN or GSTIN values.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import anthropic
from pydantic import BaseModel

from . import classifier

MODEL = "claude-opus-5-5"
BATCH_SIZE = 100

CategoryId = Literal["tax", "cash", "bank", "party", "expense", "other"]


class Suggestion(BaseModel):
    index: int
    category: CategoryId
    confidence: Literal["high", "medium", "low"]
    reason: str


class ReviewResult(BaseModel):
    suggestions: list[Suggestion]


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _system_prompt() -> str:
    lines = [
        "You classify ledger account names from an Indian accounting software (SMEs, GST regime) "
        "into Accobiz categories, to help migrate them into Accobiz.",
        "",
        "Categories (id: meaning -> Accobiz annexure heads Dr / Cr):",
    ]
    for c in classifier.categories():
        lines.append(f'- {c["id"]}: {c["label"]} -> {c["annexuredr"]} / {c["annexurecr"]}')
    lines += [
        "",
        "Guidance:",
        "- tax: GST/TDS/TCS/income-tax/VAT/cess ledgers, payable or recoverable.",
        "- cash: cash in hand / petty cash only. bank: bank current/savings/CC/OD accounts "
        "(not bank loans, FDs, bank charges or bank interest).",
        "- party: a trading customer or supplier (firm, company, shop, transporter, person you buy "
        "from or sell to). These ledgers have no GSTIN, so judge from the name.",
        "- expense: an expense head or an expense payable (salary, rent, interest paid, repairs, "
        "freight, insurance, depreciation, discount paid...).",
        "- other: everything else - capital, partners, loans, fixed assets, purchase/sale accounts, "
        "opening stock, income, deposits, advances, suspense, or anything you cannot tell.",
        "- Prefer 'other' with low confidence over guessing. Use high confidence only when the "
        "name is unambiguous.",
        "- Keep each reason under 15 words.",
        "Return exactly one suggestion per input index.",
    ]
    return "\n".join(lines)


def _review_batch(client: anthropic.Anthropic, items: list[dict]) -> list[Suggestion]:
    body = "\n".join(
        f'{it["index"]}. {it["name"]} | GSTIN: {"yes" if it["has_gstin"] else "no"} | '
        f'balance: {it["side"] or "none"}'
        for it in items)
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        system=_system_prompt(),
        messages=[{"role": "user", "content": f"Classify these ledgers:\n{body}"}],
        output_format=ReviewResult,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return []
    wanted = {it["index"] for it in items}
    return [s for s in response.parsed_output.suggestions if s.index in wanted]


def review(items: list[dict], client: anthropic.Anthropic | None = None) -> dict[int, Suggestion]:
    """items: [{index, name, has_gstin, side}] -> {index: Suggestion}. Batches run in parallel."""
    if not items:
        return {}
    client = client or anthropic.Anthropic()
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    out: dict[int, Suggestion] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda b: _review_batch(client, b), batches):
            for s in result:
                out[s.index] = s
    return out
