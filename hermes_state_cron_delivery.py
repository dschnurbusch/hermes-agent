"""Durable cron-delivery anchors and fail-closed reply resolution."""

from __future__ import annotations

import time
import os
import uuid
from contextvars import ContextVar
from typing import Optional

cron_reply_reservation: ContextVar[Optional[tuple[str, str]]] = ContextVar("cron_reply_reservation", default=None)


class SessionCronDeliveryMixin:
    """State-DB operations for exact outbound cron reply provenance."""

    def record_cron_delivery(
        self, *, profile_name: str, platform: str, scope_id: str, chat_id: str,
        anchor_kind: str, anchor_id: str, cron_session_id: str, job_id: str,
        execution_id: str = "", owner_user_id: str = "", public_thread: bool = False,
        thread_root: str = "",
    ) -> bool:
        values = tuple(str(v or "").strip() for v in (
            profile_name, platform, scope_id, chat_id, anchor_kind, anchor_id,
            cron_session_id, job_id,
        ))
        profile, platform_name, scope, chat, kind, anchor, session_id, cron_job = values
        if not all((profile, platform_name, chat, kind, anchor, session_id, cron_job)):
            return False
        if public_thread:
            if platform_name != "buzz" or owner_user_id or kind != "message":
                return False
        elif not owner_user_id:
            return False

        def _record(conn) -> bool:
            session = conn.execute(
                "SELECT source, ended_at, end_reason FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if (
                session is None
                or session["source"] != "cron"
                or session["ended_at"] is None
                or session["end_reason"] != "cron_complete"
            ):
                return False
            if conn.execute(
                "SELECT 1 FROM cron_delivery_consumed WHERE cron_session_id = ?",
                (session_id,),
            ).fetchone():
                return True  # Idempotent late receipt; never recreate a spent anchor.
            conn.execute(
                """INSERT OR IGNORE INTO cron_delivery_provenance
                   (profile_name, platform, scope_id, chat_id, anchor_kind, anchor_id,
                    thread_root, cron_session_id, job_id, owner_user_id, execution_id, delivered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile, platform_name, scope, chat, kind, anchor,
                 str(thread_root or anchor) if platform_name == "buzz" else "", session_id, cron_job,
                 str(owner_user_id), str(execution_id or ""), time.time()),
            )
            return True

        return bool(self._execute_write(_record))

    def resolve_cron_delivery_reply(
        self, *, profile_name: str, platform: str, scope_id: str, chat_id: str,
        anchor_kind: str, anchor_id: str, user_id: str, session_key: str = "", thread_id: str = "",
    ) -> Optional[tuple[str, str]]:
        """Return one safe completed cron transcript, else ``None``.

        Resolution, claim, and lease/compression checks share one write transaction. Multiple matching
        sessions, a session delivered to more than one destination, an active turn/compression,
        or a non-complete lifecycle all fail closed.
        """
        match = tuple(str(v or "").strip() for v in (
            profile_name, platform, scope_id, chat_id, anchor_kind, anchor_id,
        ))
        if not all((match[0], match[1], match[3], match[4], match[5], user_id)):
            return None
        if platform == "buzz" and (
            (thread_id and not session_key)
            or (session_key and not thread_id and ":dm:" not in session_key)
        ):
            return None
        now = time.time()
        from hermes_state import _compression_lock_holder_process_is_dead
        from hermes_state_compression import _claim_lease_row
        holder = f"pid={os.getpid()}:cron-reply={uuid.uuid4().hex}"

        def _resolve(conn) -> Optional[tuple[str, str]]:
            rows = conn.execute(
                """SELECT cron_session_id, owner_user_id, thread_root FROM cron_delivery_provenance
                   WHERE profile_name = ? AND platform = ? AND scope_id = ? AND chat_id = ?
                     AND anchor_kind = ? AND anchor_id = ?""",
                match,
            ).fetchall()
            session_ids = {str(row["cron_session_id"]) for row in rows}
            if len(session_ids) != 1:
                return None
            if platform == "buzz" and thread_id and any(
                row["thread_root"] != thread_id for row in rows
            ):
                return None
            if any(row["owner_user_id"] != user_id and not (
                platform == "buzz" and thread_id and
                row["owner_user_id"] == "" and len(user_id) == 64 and
                all(ch in "0123456789abcdef" for ch in user_id)
            ) for row in rows):
                return None
            session_id = next(iter(session_ids))

            # One cron run delivered into multiple destinations must never let the first reply
            # choose which conversation owns its transcript.
            destinations = conn.execute(
                """SELECT DISTINCT profile_name, platform, scope_id, chat_id, anchor_kind, anchor_id
                   FROM cron_delivery_provenance WHERE cron_session_id = ?""",
                (session_id,),
            ).fetchall()
            if len(destinations) != 1:
                return None

            seen = set()
            current_id = session_id
            while current_id not in seen:
                seen.add(current_id)
                current = conn.execute(
                    "SELECT id, source, ended_at, end_reason FROM sessions WHERE id = ?",
                    (current_id,),
                ).fetchone()
                if current is None:
                    return None
                if current["end_reason"] != "compression":
                    break
                children = conn.execute(
                    """SELECT id FROM sessions
                       WHERE parent_session_id = ? ORDER BY started_at ASC""",
                    (current_id,),
                ).fetchall()
                if len(children) != 1:
                    return None
                current_id = str(children[0]["id"])

            if (
                current["source"] != "cron"
                or current["ended_at"] is None
                or current["end_reason"] != "cron_complete"
            ):
                return None
            lease_key = self._session_turn_lease_key_on_conn(conn, current_id)
            active_turn = conn.execute(
                "SELECT 1 FROM session_turn_leases WHERE conversation_id = ? AND expires_at > ?",
                (lease_key, now),
            ).fetchone()
            placeholders = ",".join("?" for _ in seen)
            active_compression = conn.execute(
                f"SELECT 1 FROM compression_locks WHERE session_id IN ({placeholders}) AND expires_at > ?",
                (*seen, now),
            ).fetchone()
            if active_turn is not None or active_compression is not None:
                return None
            if platform == "buzz" and thread_id and conn.execute(
                "SELECT 1 FROM cron_reply_ownership WHERE session_key = ? OR cron_session_id = ?",
                (session_key, session_id),
            ).fetchone():
                return None
            claimed, _ = _claim_lease_row(
                conn, "session_turn_leases", "conversation_id", lease_key, holder,
                now, now + 300,
                lambda h, e: float(e) <= now or _compression_lock_holder_process_is_dead(h),
            )
            if not claimed:
                return None
            # This is only a reservation. The route CAS may still lose to /new;
            # leave the anchor reusable until the exact route has been published.
            return current_id, holder
        return self._execute_write(_resolve, patience_s=self._ACTIVITY_WRITE_PATIENCE_S)

    def commit_cron_reply_reservation(
        self, session_id: str, holder: str, *, profile_name: str, platform: str,
        scope_id: str, chat_id: str, session_key: str, thread_id: str, user_id: str,
    ) -> bool:
        """Spend the anchor once the CAS has installed the route under its route lock."""
        def _commit(conn):
            key = self._session_turn_lease_key_on_conn(conn, session_id)
            lease = conn.execute(
                "SELECT 1 FROM session_turn_leases WHERE conversation_id=? AND holder=? AND expires_at>?",
                (key, holder, time.time()),
            ).fetchone()
            if not lease:
                return False
            rows = conn.execute(
                "SELECT cron_session_id, owner_user_id, thread_root FROM cron_delivery_provenance "
                "WHERE cron_session_id=?", (session_id,),
            ).fetchall()
            if len(rows) != 1 or conn.execute(
                "SELECT 1 FROM cron_delivery_consumed WHERE cron_session_id=?", (session_id,),
            ).fetchone():
                return False
            owner = rows[0]["owner_user_id"]
            if owner != user_id and not (
                platform == "buzz" and thread_id and owner == ""
                and len(user_id) == 64 and not set(user_id) - set("0123456789abcdef")
            ):
                return False
            if platform == "buzz" and thread_id and owner == "" and (
                len(user_id) != 64 or set(user_id) - set("0123456789abcdef")
            ):
                return False
            if platform == "buzz" and thread_id:
                if rows[0]["thread_root"] != thread_id or conn.execute(
                    "SELECT 1 FROM cron_reply_ownership WHERE session_key=? OR cron_session_id=?",
                    (session_key, session_id),
                ).fetchone():
                    return False
                conn.execute(
                    """INSERT INTO cron_reply_ownership
                       (cron_session_id, session_key, profile_name, platform, scope_id,
                        chat_id, thread_id, owner_user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, session_key, profile_name, platform, scope_id, chat_id,
                     thread_id, "" if rows[0]["owner_user_id"] == "" else user_id),
                )
            conn.execute(
                "INSERT INTO cron_delivery_consumed (cron_session_id, consumed_at) VALUES (?, ?)",
                (session_id, time.time()),
            )
            conn.execute("DELETE FROM cron_delivery_provenance WHERE cron_session_id=?", (session_id,))
            return True
        return bool(self._execute_write(_commit, patience_s=self._ACTIVITY_WRITE_PATIENCE_S))

    def owns_cron_reply_route(self, session_id: str, *, session_key: str,
                              profile_name: str, platform: str, scope_id: str,
                              chat_id: str, thread_id: str, user_id: str) -> bool:
        """Authorize a live or compressed cron lineage against its immutable owner lane."""
        def _check(conn):
            return conn.execute(
                """WITH RECURSIVE lineage(id, parent_session_id) AS (
                       SELECT id, parent_session_id FROM sessions WHERE id = ?
                       UNION ALL SELECT s.id, s.parent_session_id FROM sessions s
                       JOIN lineage l ON s.id = l.parent_session_id
                   ) SELECT 1 FROM lineage l JOIN cron_reply_ownership o
                     ON o.cron_session_id = l.id
                   WHERE o.session_key = ? AND o.profile_name = ? AND o.platform = ?
                     AND o.scope_id = ? AND o.chat_id = ? AND o.thread_id = ?
                     AND (o.owner_user_id = ? OR (o.platform = 'buzz' AND o.owner_user_id = ''
                          AND length(?) = 64 AND ? NOT GLOB '*[^0-9a-f]*')) LIMIT 1""",
                (session_id, session_key, profile_name, platform, scope_id, chat_id,
                 thread_id, user_id, user_id, user_id),
            ).fetchone() is not None
        return bool(self._execute_write(_check))

    def has_cron_reply_ownership(self, session_id: str) -> bool:
        """Recognize owner-bound lineages even from a different caller route."""
        def _check(conn):
            return conn.execute(
                """WITH RECURSIVE lineage(id, parent_session_id) AS (
                       SELECT id, parent_session_id FROM sessions WHERE id = ?
                       UNION ALL SELECT s.id, s.parent_session_id FROM sessions s
                       JOIN lineage l ON s.id = l.parent_session_id
                   ) SELECT 1 FROM lineage l JOIN cron_reply_ownership o
                   ON o.cron_session_id = l.id LIMIT 1""", (session_id,),
            ).fetchone() is not None
        return bool(self._execute_write(_check))

    def has_unconsumed_cron_delivery(self, session_id: str) -> bool:
        """Detect a stranded route even when peer metadata replaced its cron source.

        A switch can mutate session metadata before its anchor commit fails. If
        compensation also fails, the still-live provenance must keep ingress closed.
        """
        def _check(conn):
            return conn.execute(
                """WITH RECURSIVE lineage(id, parent_session_id) AS (
                       SELECT id, parent_session_id FROM sessions WHERE id = ?
                       UNION ALL SELECT s.id, s.parent_session_id FROM sessions s
                       JOIN lineage l ON s.id = l.parent_session_id
                   ) SELECT 1 FROM lineage l JOIN cron_delivery_provenance p
                     ON p.cron_session_id = l.id LIMIT 1""", (session_id,),
            ).fetchone() is not None
        return bool(self._execute_write(_check))

    def is_consumed_cron_delivery(self, session_id: str) -> bool:
        """Recognize adopted cron lineage even after gateway peer metadata changes source."""
        def _check(conn):
            return conn.execute(
                """WITH RECURSIVE lineage(id, parent_session_id) AS (
                       SELECT id, parent_session_id FROM sessions WHERE id = ?
                       UNION ALL
                       SELECT s.id, s.parent_session_id FROM sessions s
                       JOIN lineage l ON s.id = l.parent_session_id
                   )
                   SELECT 1 FROM lineage l JOIN cron_delivery_consumed c
                   ON c.cron_session_id = l.id LIMIT 1""",
                (session_id,),
            ).fetchone() is not None
        return bool(self._execute_write(_check))

    def transfer_cron_reply_reservation(self, session_id: str, reservation: str, turn_holder: str) -> bool:
        """CAS the exact live reservation to the agent's normal durable turn lease."""
        def _transfer(conn):
            key = self._session_turn_lease_key_on_conn(conn, session_id)
            return conn.execute(
                "UPDATE session_turn_leases SET holder = ?, expires_at = ? "
                "WHERE conversation_id = ? AND holder = ? AND expires_at > ?",
                (turn_holder, time.time() + 300, key, reservation, time.time()),
            ).rowcount == 1
        return bool(self._execute_write(_transfer))
