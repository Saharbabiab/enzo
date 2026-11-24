# Created by S.H.A.R.P. — self-written skill. Safe to edit or delete.
from agent.skill_api import google_call


def gmail_archive(message_ids: str) -> str:
    """Use to archive Gmail emails (remove them from the inbox) by their message ids.

    Args:
        message_ids: One or more Gmail message ids, comma-separated (from email_inbox).
    """
    ids = [i.strip() for i in message_ids.split(",") if i.strip()]
    if not ids:
        return "No message ids given."
    google_call(
        "gmail",
        "users.messages.batchModify",
        userId="me",
        body={"ids": ids, "removeLabelIds": ["INBOX"]},
        description=f"Archive {len(ids)} email(s)",
    )
    return f"Archived {len(ids)} email(s)."


def gmail_mark_read(message_ids: str) -> str:
    """Use to mark Gmail emails as read by their message ids.

    Args:
        message_ids: One or more Gmail message ids, comma-separated (from email_inbox).
    """
    ids = [i.strip() for i in message_ids.split(",") if i.strip()]
    if not ids:
        return "No message ids given."
    google_call(
        "gmail",
        "users.messages.batchModify",
        userId="me",
        body={"ids": ids, "removeLabelIds": ["UNREAD"]},
        description=f"Mark {len(ids)} email(s) as read",
    )
    return f"Marked {len(ids)} email(s) as read."
