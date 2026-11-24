# Created by S.H.A.R.P. — self-written skill. Safe to edit or delete.
from agent.skill_api import google_call


def gmail_mailbox_stats() -> str:
    """Use when the user asks how many emails they have in total in Gmail, or for overall mailbox counts (total messages, threads, unread in inbox).

    Args:
        None
    """
    try:
        profile = google_call("gmail", "users.getProfile", userId="me")
    except Exception as e:
        return f"Failed to read Gmail profile: {e}"
    total = profile.get("messagesTotal", "unknown")
    threads = profile.get("threadsTotal", "unknown")
    parts = [f"Total messages: {total}", f"Total threads: {threads}"]
    try:
        inbox = google_call("gmail", "users.labels.get", userId="me", id="INBOX")
        parts.append(f"Inbox messages: {inbox.get('messagesTotal', 'unknown')}")
        parts.append(f"Inbox unread: {inbox.get('messagesUnread', 'unknown')}")
    except Exception:
        pass
    return "; ".join(parts)
