# Created by S.H.A.R.P. — self-written skill. Safe to edit or delete.
"""Cryptocurrency price lookup using the free CoinGecko API."""
import httpx

_ALIASES = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin",
    "xrp": "ripple", "ada": "cardano", "bnb": "binancecoin", "ltc": "litecoin",
    "dot": "polkadot", "usdt": "tether", "usdc": "usd-coin", "trx": "tron",
    "avax": "avalanche-2", "link": "chainlink", "matic": "matic-network",
}


def _resolve_id(coin: str) -> str:
    c = coin.strip().lower()
    if c in _ALIASES:
        return _ALIASES[c]
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/search",
                      params={"query": c}, timeout=10)
        coins = r.json().get("coins", [])
        if coins:
            return coins[0]["id"]
    except Exception:
        pass
    return "-".join(c.split())


def get_crypto_price(coin: str = "bitcoin", currency: str = "usd") -> str:
    """Use when the user asks for the current price of a cryptocurrency (bitcoin, ethereum, etc.).

    Args:
        coin: Coin name or ticker, e.g. "bitcoin", "btc", "ethereum", "sol".
        currency: Fiat currency code to price in, e.g. "usd", "ils", "eur".
    """
    coin_id = _resolve_id(coin)
    cur = currency.strip().lower()
    try:
        r = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": cur,
                    "include_24hr_change": "true", "include_market_cap": "true"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get(coin_id)
    except Exception as e:
        return f"Price lookup failed: {e}"
    if not data or cur not in data:
        return f"No price found for '{coin}' in {cur.upper()}."
    price = data[cur]
    change = data.get(f"{cur}_24h_change")
    mcap = data.get(f"{cur}_market_cap")
    out = f"{coin_id}: {price:,.2f} {cur.upper()}"
    if change is not None:
        out += f", 24h change {change:+.2f}%"
    if mcap:
        out += f", market cap {mcap:,.0f} {cur.upper()}"
    return out
