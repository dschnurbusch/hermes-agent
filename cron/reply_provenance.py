"""Persist a confirmed cron delivery after its fire claim has been cleared."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def record_confirmed_delivery(candidate: dict) -> bool:
    required = ("platform", "chat_id", "anchor_kind", "anchor_id", "cron_session_id", "job_id")
    if not isinstance(candidate, dict) or any(not candidate.get(key) for key in required):
        return False
    public = (str(candidate["platform"]).lower() == "buzz" and
              candidate.get("access_policy") == "public_buzz_thread" and
              candidate.get("owner_user_id") == "")
    if not public and not candidate.get("owner_user_id"):
        return False
    if str(candidate["platform"]).lower() == "buzz" and not public and (
        candidate.get("owner_binding") != "captured_origin"
        or re.fullmatch(r"[0-9a-f]{64}", str(candidate["owner_user_id"])) is None
    ):
        return False
    from hermes_cli.profiles import get_active_profile_name
    from hermes_state_registry import acquire, release

    db = acquire()
    try:
        return db.record_cron_delivery(
            profile_name=get_active_profile_name() or "default",
            platform=candidate["platform"], scope_id=candidate.get("scope_id") or "",
            chat_id=candidate["chat_id"], anchor_kind=candidate["anchor_kind"],
            anchor_id=candidate["anchor_id"], thread_root=candidate.get("thread_root") or "",
            cron_session_id=candidate["cron_session_id"],
            job_id=candidate["job_id"], execution_id=candidate.get("execution_id") or "",
            owner_user_id=candidate["owner_user_id"],
            public_thread=public,
        )
    except Exception:
        logger.warning("Failed to persist cron delivery provenance", exc_info=True)
        return False
    finally:
        release(db)
