"""Owner registration and verification.

Calling spec:
  Inputs:  Store instance, user_id (int)
  Outputs: tuple[bool, bool] — (is_owner, just_registered)
  Side effects: writes "owner_id" to store on first-ever call

Logic:
  1. Read "owner_id" from store (O(1) SQLite lookup).
  2. If no owner registered, register this user as owner.
     Return (True, True).
  3. If owner exists and matches user_id, return (True, False).
  4. If owner exists and does NOT match, return (False, False).

Guarding:
  - First user to message becomes owner (stored via store.set)
  - No mechanism to change owner at runtime
  - Owner check is O(1) lookup, not bypassable
"""

from __future__ import annotations

from onecmd.store import Store

OWNER_KEY = "owner_id"


def check_owner(store: Store, user_id: int) -> tuple[bool, bool]:
    """Check whether *user_id* is the bot owner.

    Returns ``(is_owner, just_registered)``.

    On the very first call (no owner in the store), the caller is
    registered as owner and ``(True, True)`` is returned.  All
    subsequent calls compare against the stored owner ID.
    """
    stored = store.get(OWNER_KEY)

    # First user becomes owner.
    if stored is None:
        store.set(OWNER_KEY, str(user_id))
        return True, True

    is_owner = stored == str(user_id)
    return is_owner, False
