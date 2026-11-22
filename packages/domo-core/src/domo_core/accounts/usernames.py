"""Usernames are case-insensitive and ignore the spaces around them."""


def normalize_username(username: str) -> str:
    """The form in which a username is stored and looked up (and counted by the login throttle):
    trimmed, lower case. Every place that reads or writes a username goes through this."""
    return username.strip().lower()
