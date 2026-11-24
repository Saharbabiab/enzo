# Adding tools to Enzo

Every public function in a `.py` file in this folder becomes a tool Enzo can use.
Restart the backend after adding a file.

Skills Enzo writes itself (`create_skill`) do not go here: they need tests, are staged in
`data/skills/_proposed/<name>/`, and after the user's approval and passing tests live in `data/skills/<name>/`
(kept across container updates). A routine made of existing tools needs no Python at all: write a playbook,
`skills/<name>/SKILL.md` (see `agent/playbooks.py`).

```python
# agent/skills/crypto.py
import httpx

def get_crypto_price(coin: str = "bitcoin", currency: str = "usd") -> str:
    """Get the current price of a cryptocurrency.

    Args:
        coin: CoinGecko id, e.g. "bitcoin", "ethereum".
        currency: Currency code, e.g. "usd", "ils".
    """
    r = httpx.get("https://api.coingecko.com/api/v3/simple/price",
                  params={"ids": coin, "vs_currencies": currency}, timeout=10)
    price = r.json()[coin][currency]
    return f"{coin} is {price} {currency.upper()}"
```

Rules:
- **Type hints + docstring are required.** The first paragraph tells the model *when* to use
  the tool; the `Args:` section explains each argument. That's all the model sees.
- Return a short **string** (it goes back to the model, max ~4000 chars).
- Arguments with a default are optional.
- Functions starting with `_` are private helpers, not tools.
- Sync functions run in a thread, `async def` functions run directly.
- Need the user's location / device? `from agent.skill_api import user_location` →
  `user_location()` returns latitude, longitude, place, device.
- Self-written skills may import only `agent.skill_api` from Enzo (no tokens, files or internals).
- Names must not clash with built-in tools (`web_search`, `run_command`, `read_file`, ...).

Built-in tools live in `agent/tools.py`.

## Using Google (Calendar / Gmail) from a skill

Skills can't touch credentials. Use the approval-gated helper instead:

```python
from agent.skill_api import google_call

def unread_count() -> str:
    """How many unread emails are in the inbox."""
    res = google_call("gmail", "users.labels.get", userId="me", id="INBOX")
    return f"{res.get('messagesUnread', 0)} unread"
```

Read calls (`list`, `get`, …) run directly. Anything that changes data (`insert`, `patch`,
`delete`, `send`, `modify`, …) asks the user first — pass `description="..."` so the question is clear.
