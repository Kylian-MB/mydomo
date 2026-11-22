"""What a call to a model costs, in US dollars, and how much of it can be known before the call."""

from decimal import Decimal
from typing import Final

from domo_core.llm.catalog import Prices
from domo_core.llm.types import ChatUsage

CHARS_PER_TOKEN: Final = 3
_PER_MILLION: Final = Decimal(1_000_000)


def estimate_tokens(chars: int) -> int:
    """The tokens a text of ``chars`` characters is taken to hold (rounded up)."""
    return -(-chars // CHARS_PER_TOKEN)


def cost_usd(prices: Prices, usage: ChatUsage) -> Decimal:
    """The cost of a call, from the usage the provider reported.

    ``input_tokens`` counts the whole input: what is neither read from nor written to the cache is
    priced as input (never a negative amount), the cache parts at their own prices.
    """
    plain_input = max(0, usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens)
    total = (
        prices.input * plain_input
        + prices.cache_read * usage.cache_read_tokens
        + prices.cache_write * usage.cache_write_tokens
        + prices.output * usage.output_tokens
    )
    return total / _PER_MILLION


def max_cost_usd(prices: Prices, *, input_tokens: int, max_output_tokens: int) -> Decimal:
    """The most a call can cost: all its input at the higher of the input and cache-write prices,
    and the largest output it may produce."""
    input_price = max(prices.input, prices.cache_write)
    return (input_price * input_tokens + prices.output * max_output_tokens) / _PER_MILLION
