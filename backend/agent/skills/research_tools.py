def fetch_webpage_text(url: str) -> str:
    """
    Downloads and extracts the raw text from any webpage URL.
    Use this when you need deep research on a specific article, blog post, or documentation page beyond just search engine snippets.
    Args:
        url (str): The full HTTP link to the webpage.
    """
    from shared import fetch_url          # blocks internal addresses and checks every redirect
    return fetch_url(url, max_chars=10000)
