"""Session-route adoption for authenticated replies to confirmed cron deliveries."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def private_cron_reply_route(source, session_key: str, profile_name: str, *, config=None) -> bool:
    """Check the effective canonical lane; durable ownership is verified at ingress."""
    from gateway.session import build_session_key

    if not source.chat_id or not source.user_id:
        return False
    if source.chat_type != "dm" or source.thread_id:
        # Buzz threads (including DM threads) share a chat/root key across pubkeys.
        # The signed participant must also match the persisted delivery owner.
        if (source.platform.value != "buzz" or not source.thread_id
                or len(str(source.user_id)) != 64
                or any(ch not in "0123456789abcdef" for ch in str(source.user_id))):
            return False
    if config is None:
        return False  # Unknown effective isolation settings cannot establish this lane.
    return session_key == build_session_key(
        source,
        group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
        thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        profile=profile_name,
    )


class SessionCronReplyMixin:
    def adopt_cron_reply(self, session_key: str, *, expected_session_id: str,
                         profile_name: str, platform: str, scope_id: str,
                         chat_id: str, anchor_kind: str, anchor_id: str,
                         user_id: str, thread_id: str = ""):
        """Reserve, CAS and spend while /new is excluded by the route lock."""
        with self._lock:
            current = self._entry_locked(session_key)
            if current is None or current.session_id != expected_session_id:
                return None
            db = self._db_for_key(session_key)
            if db is None:
                return None
            reservation = self.resolve_cron_reply_target(
                session_key, profile_name=profile_name, platform=platform,
                scope_id=scope_id, chat_id=chat_id, anchor_kind=anchor_kind,
                anchor_id=anchor_id, user_id=user_id, thread_id=thread_id,
            )
            if not reservation:
                return None
            target, holder = reservation
            committed = False
            switched = None
            # switch_session reopens the target and ends the old route. Keep both lifecycle
            # snapshots so a rejected/failed anchor commit cannot leave those writes behind.
            before = {}
            try:
                before = {sid: db.get_session(sid) for sid in {current.session_id, target}}
                ancestor = before[target]
                anchor_session_id = target
                while ancestor is not None and ancestor["parent_session_id"]:
                    parent_id = ancestor["parent_session_id"]
                    if parent_id in before:
                        break
                    ancestor = db.get_session(parent_id)
                    before[parent_id] = ancestor
                    anchor_session_id = parent_id
                if any(row is None for row in before.values()):
                    return None
                switched = self.switch_session(
                    session_key, target, expected_session_id=expected_session_id)
                if switched is None:
                    return None
                committed = bool(db.commit_cron_reply_reservation(
                    anchor_session_id, holder, profile_name=profile_name, platform=platform,
                    scope_id=scope_id, chat_id=chat_id, session_key=session_key,
                    thread_id=thread_id, user_id=user_id,
                ))
                if committed:
                    return switched, reservation
                # A false result can follow a durable write in a wrapped DB/backend.
                # Reconcile the persisted marker before compensating the route.
                committed = bool(db.is_consumed_cron_delivery(target))
                if committed:
                    return switched, reservation
                logger.warning("Cron reply anchor commit refused; restoring prior route")
            except Exception:
                logger.warning("Cron reply adoption failed; restoring prior route", exc_info=True)
                # An exception after a successful DB commit is an uncertain outcome. Never
                # restore a spent anchor into an ordinary route or release its turn lease.
                try:
                    committed = bool(db.is_consumed_cron_delivery(target))
                except Exception:
                    committed = True  # Unknown outcome: retain the lease and route, fail closed.
                    logger.error("Cron reply commit outcome unknown; keeping reserved route", exc_info=True)
                    return None
                if committed:
                    return (switched, reservation) if switched is not None else None
            finally:
                if not committed:
                    if switched is not None:
                        # Keep the lease through compensation: another resolver must not
                        # observe a reusable anchor against a half-restored lineage.
                        try:
                            def _restore(conn):
                                for sid, row in before.items():
                                    conn.execute(
                                        """UPDATE sessions SET ended_at = ?, end_reason = ?,
                                           session_key = ?, source = ?, user_id = ?, chat_id = ?,
                                           chat_type = ?, thread_id = ?, display_name = ?,
                                           origin_json = ?, transport_profile = ? WHERE id = ?""",
                                        tuple(row[field] for field in (
                                            "ended_at", "end_reason", "session_key", "source", "user_id",
                                            "chat_id", "chat_type", "thread_id", "display_name",
                                            "origin_json", "transport_profile")) + (sid,),
                                    )
                            db._execute_write(_restore)
                            self._entries[session_key] = current
                            self._save()
                        except Exception:
                            logger.error("Cron reply route rollback failed; retaining reservation", exc_info=True)
                            return None
                    try:
                        db.release_session_turn_lease(target, holder)
                    except Exception:
                        logger.warning("Cron reply reservation release failed", exc_info=True)
            return None

    def resolve_cron_reply_target(
        self, session_key: str, *, profile_name: str, platform: str, scope_id: str,
        chat_id: str, anchor_kind: str, anchor_id: str, user_id: str, thread_id: str = "",
    ) -> Optional[tuple[str, str]]:
        db = self._db_for_key(session_key)
        if db is None:
            return None
        try:
            return db.resolve_cron_delivery_reply(
                profile_name=profile_name, platform=platform, scope_id=scope_id,
                chat_id=chat_id, anchor_kind=anchor_kind, anchor_id=anchor_id,
                user_id=user_id, session_key=session_key, thread_id=thread_id,
            )
        except Exception:
            logger.warning("Cron reply provenance lookup failed closed", exc_info=True)
            return None
