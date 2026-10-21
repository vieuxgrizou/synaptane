"""Estimated cost of a tidy-up pass, in euros.

Price entered by the organization (€ per million tokens, input and output), or else the
model's public price when known: public amounts are in dollars and counted one for one in
euros, hence "estimated". Input read back from the cache is counted at a tenth of the input
price (less for models with a reduced cache price); input written to the cache, at 1.25 times
the input price (Anthropic's price for the 5-minute duration, the only one Synaptane asks
for: the one-hour duration would cost 2 times). Subscriptions (Claude Code, Codex) have no
pay-per-use cost.

A model with no known price is never counted as zero when a budget is set: it is counted at
the highest price in the table (`prudent_price`, see `budget.run_price`).
"""

from dataclasses import dataclass
from decimal import Decimal

from synaptane.ai.base import SUBSCRIPTION_PROVIDERS, Usage

MILLION = Decimal(1_000_000)
CACHE_READ = Decimal("0.1")  # input read back from the cache, share of the input price
CACHE_WRITE = Decimal("1.25")  # input written to the cache (5 minutes), share of the input price
CACHE_READ_BY_MODEL = {  # models with a reduced cache price
    "claude-opus-5-5": Decimal("0.05"),
    "claude-fable-5-1": Decimal("0.025"),
}

# Public prices (input, output) per million tokens, in dollars.
PUBLIC_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-1": (Decimal(10), Decimal(50)),
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-opus-5": (Decimal(5), Decimal(25)),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "claude-sonnet-5": (Decimal(2), Decimal(10)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
}


# Embeddings: public prices per million input tokens, in dollars.
EMBEDDING_PRICES: dict[str, Decimal] = {
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
    "gemini-embedding-001": Decimal("0.15"),
    "mistral-embed": Decimal("0.10"),
    "voyage-3.5": Decimal("0.06"),
    "voyage-3.5-lite": Decimal("0.02"),
    "voyage-4-large": Decimal("0.12"),
    "voyage-4": Decimal("0.06"),
    "voyage-4-lite": Decimal("0.02"),
}
# Embedding providers billed per use (Ollama and compatible APIs: unknown).
PAID_EMBEDDINGS = ("openai", "gemini", "mistral", "voyage")


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    public: bool  # default public price (estimate), not the one entered
    cache_read: Decimal = CACHE_READ
    cache_write: Decimal = CACHE_WRITE


def public_price(model: str) -> tuple[Decimal, Decimal] | None:
    model = model.strip().lower()
    if model in PUBLIC_PRICES:
        return PUBLIC_PRICES[model]
    # Dated identifiers (claude-haiku-4-5-20251001): longest known prefix.
    for known in sorted(PUBLIC_PRICES, key=len, reverse=True):
        if model.startswith(known + "-"):
            return PUBLIC_PRICES[known]
    return None


def price_for(
    provider: str, model: str, price_input: Decimal | None, price_output: Decimal | None
) -> Price | None:
    """Applicable price, or None: subscription, or unknown model with no price entered."""
    if provider in SUBSCRIPTION_PROVIDERS:
        return None
    cache = next(
        (f for m, f in CACHE_READ_BY_MODEL.items() if model.strip().lower().startswith(m)),
        CACHE_READ,
    )
    if price_input is not None and price_output is not None:
        return Price(price_input, price_output, public=False, cache_read=cache)
    known = public_price(model)
    return Price(known[0], known[1], public=True, cache_read=cache) if known else None


def prudent_price() -> Price:
    """Highest price of the public table (and the cache at the ordinary price): to count a run
    whose model has no known price while a budget is set."""
    highest = max(PUBLIC_PRICES.values(), key=lambda p: (p[1], p[0]))
    return Price(highest[0], highest[1], public=True)


def embedding_cost(provider: str, model: str, tokens: int) -> Decimal | None:
    """Estimated cost of `tokens` embedded tokens, or None: local or unknown provider (Ollama,
    compatible API). A paid model missing from the table is counted at the table's highest
    price, never zero."""
    if provider not in PAID_EMBEDDINGS or tokens <= 0:
        return None
    rate = EMBEDDING_PRICES.get(model.strip().lower(), max(EMBEDDING_PRICES.values()))
    return (Decimal(tokens) * rate / MILLION).quantize(Decimal("0.000001"))


def cost(usage: Usage, price: Price | None) -> Decimal | None:
    if price is None:
        return None
    # Cache writes are already in `input_tokens` (full price): only the surcharge is
    # added.
    total = (
        Decimal(usage.input_tokens) * price.input
        + Decimal(usage.cache_write_tokens) * price.input * (price.cache_write - 1)
        + Decimal(usage.cached_tokens) * price.input * price.cache_read
        + Decimal(usage.output_tokens) * price.output
    ) / MILLION
    return total.quantize(Decimal("0.0001"))
