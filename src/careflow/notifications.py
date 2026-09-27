"""患者联系偏好与门诊通知待办。

联系类通知统一依赖 followup_contact 授权：任务创建时记录授权版本，
领取与结果登记时重新核对；授权撤回在同一事务内阻止尚未领取的任务。
系统只维护内部发送任务，由工作人员线下完成触达，不接入任何短信或外呼平台。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import choice, parsed_timestamp, require_match, text, timestamp

NOTIFICATION_PURPOSES = {"appointment_updates", "followup_reminders"}
CONTACT_CHANNELS = ("phone", "message")
TASK_STATES = {"pending", "claimed", "done", "closed_manual", "cancelled"}
OUTCOMES = {"reached", "no_answer", "busy", "failed"}
DISPOSITIONS = {"done", "retry", "manual"}
CONSENT_PURPOSE = "followup_contact"
DEFAULT_CHANNELS = ["phone", "message"]

_QUIET = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _quiet_time(value, field: str) -> time:
    if not isinstance(value, str) or not _QUIET.fullmatch(value):
        raise ValidationError(f"{field}必须为 HH:MM 本地时间")
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def quiet_hours_end(moment: datetime, zone: ZoneInfo, quiet_start: time | None, quiet_end: time | None) -> datetime | None:
    """按患者选择的时区解释静默时段。

    moment 落在患者本地静默时段内时返回静默结束对应的 UTC 时刻，否则返回 None。
    跨午夜时段（如 22:00-07:00）按患者本地日期解释；夏令时切换日的本地边界
    由 zoneinfo 按切换前偏移确定性解析（fold=0）。
    """
    if quiet_start is None or quiet_end is None or quiet_start == quiet_end:
        return None
    local = moment.astimezone(zone)
    start_today = datetime.combine(local.date(), quiet_start, zone)
    end_today = datetime.combine(local.date(), quiet_end, zone)
    if quiet_start < quiet_end:
        if start_today <= local < end_today:
            return end_today.astimezone(UTC)
        return None
    # 跨午夜：时段为当天 start 至次日 end。
    if local >= start_today:
        return datetime.combine(local.date() + timedelta(days=1), quiet_end, zone).astimezone(UTC)
    if local < end_today:
        return end_today.astimezone(UTC)
    return None


class NotificationService:
    """联系偏好与通知任务队列；领取、结果与取消都写入审计链。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ---------- 联系偏好 ----------

    def set_preference(self, clinic_id: str, actor_id: str, patient_id: str, purpose: str,
                       channels: list, quiet_start: str | None, quiet_end: str | None,
                       timezone_name: str) -> dict:
        purpose = choice(purpose, "通知用途", NOTIFICATION_PURPOSES)
        if not isinstance(channels, list) or not channels or len(channels) > len(CONTACT_CHANNELS):
            raise ValidationError("联系渠道须为一至两项，按患者意愿排序")
        normalized: list[str] = []
        for item in channels:
            item = choice(item, "联系渠道", set(CONTACT_CHANNELS))
            if item in normalized:
                raise ValidationError("联系渠道不能重复")
            normalized.append(item)
        if (quiet_start is None) != (quiet_end is None):
            raise ValidationError("静默时段需要同时提供开始和结束")
        start_t = _quiet_time(quiet_start, "静默开始") if quiet_start is not None else None
        end_t = _quiet_time(quiet_end, "静默结束") if quiet_end is not None else None
        if start_t is not None and start_t == end_t:
            raise ValidationError("静默时段开始与结束不能相同；不需要静默时段请同时留空")
        timezone_name = text(timezone_name, "时区", maximum=80)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("时区无效") from exc
        now = timestamp(self.clock.now())
        preference_id = new_id("cpr")
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "contact:manage", clinic_id=clinic_id)
            patient = connection.execute("SELECT state FROM patients WHERE id=? AND clinic_id=?",
                                         (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能更新联系偏好")
            latest = connection.execute(
                "SELECT MAX(revision) FROM contact_preferences WHERE patient_id=? AND purpose=?",
                (patient_id, purpose)).fetchone()[0]
            revision = (latest or 0) + 1
            connection.execute(
                "INSERT INTO contact_preferences(id,patient_id,purpose,revision,channels_json,quiet_start,quiet_end,timezone,recorded_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (preference_id, patient_id, purpose, revision, encode_json(normalized),
                 quiet_start, quiet_end, timezone_name, actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="contact_preference", aggregate_id=preference_id,
                               action="contact_preference.set", occurred_at=now,
                               payload={"purpose": purpose, "revision": revision, "channels": normalized,
                                        "quiet_start": quiet_start, "quiet_end": quiet_end, "timezone": timezone_name})
        return {"id": preference_id, "patient_id": patient_id, "purpose": purpose, "channels": normalized,
                "quiet_start": quiet_start, "quiet_end": quiet_end, "timezone": timezone_name,
                "revision": revision, "created_at": now}

    def current_preferences(self, clinic_id: str, actor_id: str, patient_id: str) -> list[dict]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "patient:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM patients WHERE id=? AND clinic_id=?",
                                  (patient_id, clinic_id)).fetchone() is None:
                raise NotFound("患者不存在")
            rows = connection.execute(
                "SELECT p.* FROM contact_preferences p "
                "WHERE p.patient_id=? AND p.revision=(SELECT MAX(q.revision) FROM contact_preferences q "
                "WHERE q.patient_id=p.patient_id AND q.purpose=p.purpose) ORDER BY p.purpose",
                (patient_id,)).fetchall()
            return [self._preference_view(row) for row in rows]

    @staticmethod
    def _preference_view(row) -> dict:
        return {"purpose": row["purpose"], "channels": list(decode_json(row["channels_json"])),
                "quiet_start": row["quiet_start"], "quiet_end": row["quiet_end"],
                "timezone": row["timezone"], "revision": row["revision"],
                "recorded_by": row["recorded_by"], "created_at": row["created_at"]}

    def _effective_preference(self, connection, clinic_id: str, patient_id: str, purpose: str) -> dict:
        """当前偏好；患者尚未选择时回退到诊所时区、无静默时段、电话优先。"""
        row = connection.execute(
            "SELECT * FROM contact_preferences WHERE patient_id=? AND purpose=? ORDER BY revision DESC LIMIT 1",
            (patient_id, purpose)).fetchone()
        if row is not None:
            return {"channels": list(decode_json(row["channels_json"])),
                    "quiet_start": time.fromisoformat(row["quiet_start"]) if row["quiet_start"] else None,
                    "quiet_end": time.fromisoformat(row["quiet_end"]) if row["quiet_end"] else None,
                    "zone": ZoneInfo(row["timezone"]), "id": row["id"], "revision": row["revision"]}
        clinic = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        return {"channels": list(DEFAULT_CHANNELS), "quiet_start": None, "quiet_end": None,
                "zone": ZoneInfo(clinic["timezone"]), "id": None, "revision": None}

    # ---------- 授权核对 ----------

    @staticmethod
    def _current_consent(connection, patient_id: str, now: str):
        return connection.execute(
            "SELECT * FROM consents WHERE patient_id=? AND purpose=? AND state='granted' "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY revision DESC LIMIT 1",
            (patient_id, CONSENT_PURPOSE, now)).fetchone()

    @staticmethod
    def _authorization(connection, task, now: str) -> tuple[bool, int | None, str | None]:
        """核对任务记录的联系授权版本是否仍然有效（撤回、被新版本取代、到期均失效）。"""
        recorded = connection.execute("SELECT state,expires_at,revision FROM consents WHERE id=?",
                                      (task["consent_id"],)).fetchone()
        if recorded is None or recorded["state"] == "withdrawn":
            return False, None, "consent_withdrawn"
        if recorded["state"] != "granted":
            return False, recorded["revision"], "consent_superseded"
        if recorded["expires_at"] and recorded["expires_at"] <= now:
            return False, recorded["revision"], "consent_expired"
        return True, recorded["revision"], None

    # ---------- 任务生成 ----------

    def _create_task(self, connection, clinic_id: str, actor_id: str, patient_id: str, purpose: str,
                     source_kind: str, source_id: str, source_event: str, summary: str, now: str):
        """同一来源事件只生成一个任务；无有效联系授权时不生成。"""
        consent = self._current_consent(connection, patient_id, now)
        if consent is None:
            return None, "no_consent"
        existing = connection.execute(
            "SELECT id FROM notification_tasks WHERE clinic_id=? AND source_kind=? AND source_id=? AND source_event=?",
            (clinic_id, source_kind, source_id, source_event)).fetchone()
        if existing:
            return None, "duplicate"
        pref = self._effective_preference(connection, clinic_id, patient_id, purpose)
        quiet_end = quiet_hours_end(parsed_timestamp(now), pref["zone"], pref["quiet_start"], pref["quiet_end"])
        not_before = timestamp(quiet_end) if quiet_end else now
        task_id = new_id("ntf")
        connection.execute(
            "INSERT INTO notification_tasks(id,clinic_id,patient_id,purpose,channel,summary,source_kind,source_id,source_event,"
            "consent_id,consent_revision,preference_id,preference_revision,state,not_before,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (task_id, clinic_id, patient_id, purpose, pref["channels"][0], summary, source_kind, source_id,
             source_event, consent["id"], consent["revision"], pref["id"], pref["revision"], not_before, now, now))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="notification_task", aggregate_id=task_id, action="notification.generated",
                           occurred_at=now,
                           payload={"purpose": purpose, "source_kind": source_kind, "source_id": source_id,
                                    "source_event": source_event, "channel": pref["channels"][0],
                                    "consent_revision": consent["revision"],
                                    "preference_revision": pref["revision"], "not_before": not_before})
        return task_id, None

    def generate_for_appointment_event(self, connection, clinic_id: str, actor_id: str,
                                       appointment, action: str, now: str) -> str | None:
        """预约确认或取消时生成带来源的联系任务；由预约状态机在同一事务内调用。"""
        event = {"book": "booked", "cancel": "cancelled"}[action]
        patient = connection.execute("SELECT state FROM patients WHERE id=?",
                                     (appointment["patient_id"],)).fetchone()
        if patient is None or patient["state"] != "active":
            return None
        label = "预约确认通知" if event == "booked" else "预约取消通知"
        summary = f"{label}：{appointment['kind']} {appointment['starts_at']}"
        task_id, _ = self._create_task(connection, clinic_id, actor_id, appointment["patient_id"],
                                       "appointment_updates", "appointment", appointment["id"], event,
                                       summary, now)
        return task_id

    def generate_due_followup_tasks(self, clinic_id: str, actor_id: str, *, limit: int = 100) -> dict:
        """扫描已到期的随访并生成提醒任务；重复扫描不会重复建任务。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValidationError("处理数量必须为 1 至 500")
        now = timestamp(self.clock.now())
        created: list[str] = []
        duplicates = no_consent = inactive = 0
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT f.id,f.patient_id,f.reason,p.state AS patient_state FROM followups f "
                "JOIN patients p ON p.id=f.patient_id WHERE p.clinic_id=? AND f.state='pending' AND f.due_at<=? "
                "ORDER BY f.due_at,f.id LIMIT ?", (clinic_id, now, limit)).fetchall()
            for row in rows:
                if row["patient_state"] != "active":
                    inactive += 1
                    continue
                task_id, skip = self._create_task(connection, clinic_id, actor_id, row["patient_id"],
                                                  "followup_reminders", "followup", row["id"], "due",
                                                  f"随访到期提醒：{row['reason']}", now)
                if task_id:
                    created.append(task_id)
                elif skip == "duplicate":
                    duplicates += 1
                else:
                    no_consent += 1
        return {"clinic_id": clinic_id, "as_of": now, "created": len(created), "task_ids": created,
                "skipped_duplicate": duplicates, "skipped_no_consent": no_consent, "skipped_inactive": inactive}

    def block_pending_for_withdrawal(self, connection, clinic_id: str, patient_id: str,
                                     consent, now: str, actor_id: str) -> int:
        """授权撤回时在同一事务内阻止尚未领取的任务；已领取任务在结果登记时重新核对。"""
        rows = connection.execute(
            "SELECT id FROM notification_tasks WHERE clinic_id=? AND patient_id=? AND state='pending'",
            (clinic_id, patient_id)).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE notification_tasks SET state='cancelled',cancelled_reason='consent_withdrawn',"
                "updated_at=?,version=version+1 WHERE id=?", (now, row["id"]))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="notification_task", aggregate_id=row["id"],
                               action="notification.cancelled", occurred_at=now,
                               payload={"reason": "consent_withdrawn", "consent_id": consent["id"],
                                        "consent_revision": consent["revision"]})
        return len(rows)

    # ---------- 领取与结果登记 ----------

    def claim_tasks(self, clinic_id: str, actor_id: str, *, limit: int = 20, lease_minutes: int = 5) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("领取数量必须为 1 至 100")
        if not isinstance(lease_minutes, int) or isinstance(lease_minutes, bool) or not 1 <= lease_minutes <= 60:
            raise ValidationError("租约时长必须为 1 至 60 分钟")
        now = timestamp(self.clock.now())
        now_dt = parsed_timestamp(now)
        until = timestamp(now_dt + timedelta(minutes=lease_minutes))
        claim_token = new_id("claim")
        claimed: list[dict] = []
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            candidates = connection.execute(
                "SELECT * FROM notification_tasks WHERE clinic_id=? AND not_before<=? "
                "AND (state='pending' OR (state='claimed' AND claim_until<=?)) "
                "ORDER BY not_before,created_at,id LIMIT ?",
                (clinic_id, now, now, max(limit * 5, 50))).fetchall()
            for row in candidates:
                if len(claimed) >= limit:
                    break
                ok, _, reason = self._authorization(connection, row, now)
                if not ok:
                    # 授权已失效的任务立即取消，不再出现在任何待联系名单。
                    connection.execute(
                        "UPDATE notification_tasks SET state='cancelled',cancelled_reason=?,updated_at=?,"
                        "version=version+1 WHERE id=?", (reason, now, row["id"]))
                    audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id,
                                       patient_id=row["patient_id"], aggregate_type="notification_task",
                                       aggregate_id=row["id"], action="notification.cancelled", occurred_at=now,
                                       payload={"reason": reason, "consent_id": row["consent_id"],
                                                "consent_revision": row["consent_revision"]})
                    continue
                pref = self._effective_preference(connection, clinic_id, row["patient_id"], row["purpose"])
                if quiet_hours_end(now_dt, pref["zone"], pref["quiet_start"], pref["quiet_end"]) is not None:
                    continue  # 患者静默时段内不领取，留待可联系时间
                channel = row["channel"] if row["channel"] in pref["channels"] else pref["channels"][0]
                changed = connection.execute(
                    "UPDATE notification_tasks SET state='claimed',assigned_to=?,claim_token=?,claim_until=?,"
                    "channel=?,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND (state='pending' OR (state='claimed' AND claim_until<=?))",
                    (actor_id, claim_token, until, channel, now, row["id"], row["version"], now)).rowcount
                if changed:
                    audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id,
                                       patient_id=row["patient_id"], aggregate_type="notification_task",
                                       aggregate_id=row["id"], action="notification.claimed", occurred_at=now,
                                       payload={"claim_until": until, "channel": channel,
                                                "channel_remapped_from": row["channel"] if channel != row["channel"] else None,
                                                "previous_version": row["version"]})
                    claimed.append({"id": row["id"], "patient_id": row["patient_id"], "purpose": row["purpose"],
                                    "channel": channel, "summary": row["summary"], "source_kind": row["source_kind"],
                                    "source_id": row["source_id"], "source_event": row["source_event"],
                                    "claim_token": claim_token, "claim_until": until,
                                    "version": row["version"] + 1})
        return claimed

    def register_result(self, clinic_id: str, actor_id: str, task_id: str, claim_token: str,
                        expected_version: int, outcome: str, disposition: str, note: str,
                        *, retry_after_minutes: int = 30) -> dict:
        outcome = choice(outcome, "联系结果", OUTCOMES)
        disposition = choice(disposition, "处置方式", DISPOSITIONS)
        note = text(note, "结果说明", maximum=1000)
        if disposition == "done" and outcome != "reached":
            raise ValidationError("只有已触达的任务才能登记完成")
        if disposition == "retry" and outcome == "reached":
            raise ValidationError("已触达的任务不能登记为重试")
        if not isinstance(retry_after_minutes, int) or isinstance(retry_after_minutes, bool) \
                or not 0 <= retry_after_minutes <= 1440:
            raise ValidationError("重试间隔必须为 0 至 1440 分钟")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM notification_tasks WHERE id=? AND clinic_id=?",
                                     (task_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("通知任务不存在")
            require_match(row["version"], expected_version, "通知任务")
            if row["state"] != "claimed" or row["assigned_to"] != actor_id or row["claim_token"] != claim_token:
                raise Conflict("通知任务租约已失效或不属于当前人员")
            if not row["claim_until"] or row["claim_until"] <= now:
                raise Conflict("通知任务租约已过期")
            ok, current_revision, reason = self._authorization(connection, row, now)
            if not ok:
                raise Conflict("联系授权已变更，不能登记该任务结果",
                               details={"reason": reason, "recorded_revision": row["consent_revision"]})
            sequence = row["attempt_count"] + 1
            connection.execute(
                "INSERT INTO notification_attempts(id,task_id,sequence,outcome,disposition,note,actor_id,consent_revision,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("nat"), task_id, sequence, outcome, disposition, note, actor_id, current_revision, now))
            if disposition == "retry":
                state = "pending"
                not_before = timestamp(parsed_timestamp(now) + timedelta(minutes=retry_after_minutes))
                closed_outcome = None
            else:
                state = "done" if disposition == "done" else "closed_manual"
                not_before = row["not_before"]
                closed_outcome = outcome
            connection.execute(
                "UPDATE notification_tasks SET state=?,not_before=?,assigned_to=NULL,claim_token=NULL,claim_until=NULL,"
                "attempt_count=attempt_count+1,closed_outcome=COALESCE(?,closed_outcome),updated_at=?,version=version+1 "
                "WHERE id=?", (state, not_before, closed_outcome, now, task_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="notification_task", aggregate_id=task_id,
                               action="notification.result_registered", occurred_at=now,
                               payload={"outcome": outcome, "disposition": disposition, "sequence": sequence,
                                        "consent_revision": current_revision, "note": note,
                                        "version": expected_version + 1})
        return {"id": task_id, "state": state, "outcome": outcome, "disposition": disposition,
                "attempt_sequence": sequence, "not_before": not_before, "version": expected_version + 1}

    # ---------- 队列查询 ----------

    def list_tasks(self, clinic_id: str, actor_id: str, *, state: str | None = None,
                   patient_id: str | None = None, limit: int = 100) -> list[dict]:
        if state is not None:
            state = choice(state, "任务状态", TASK_STATES)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValidationError("查询数量须为 1 至 500")
        now = timestamp(self.clock.now())
        now_dt = parsed_timestamp(now)
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            clauses = ["clinic_id=?"]
            params: list = [clinic_id]
            if state is not None:
                clauses.append("state=?")
                params.append(state)
            else:
                clauses.append("state IN ('pending','claimed')")
            if patient_id is not None:
                clauses.append("patient_id=?")
                params.append(patient_id)
            rows = connection.execute(
                "SELECT * FROM notification_tasks WHERE " + " AND ".join(clauses) +
                " ORDER BY not_before,created_at,id LIMIT ?", (*params, limit)).fetchall()
            items = []
            for row in rows:
                blocked = None
                if row["state"] in {"pending", "claimed"}:
                    ok, _, _ = self._authorization(connection, row, now)
                    if not ok:
                        continue  # 授权已失效的患者不出现在待联系名单
                    pref = self._effective_preference(connection, clinic_id, row["patient_id"], row["purpose"])
                    if quiet_hours_end(now_dt, pref["zone"], pref["quiet_start"], pref["quiet_end"]) is not None:
                        blocked = "quiet_hours"
                    elif row["state"] == "pending" and row["not_before"] > now:
                        blocked = "retry_wait"
                view = self._task_view(row)
                view["contactable_now"] = row["state"] == "pending" and blocked is None
                view["blocked_reason"] = blocked
                items.append(view)
            return items

    def task_detail(self, clinic_id: str, actor_id: str, task_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM notification_tasks WHERE id=? AND clinic_id=?",
                                     (task_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("通知任务不存在")
            attempts = connection.execute(
                "SELECT * FROM notification_attempts WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()
            return {"task": self._task_view(row),
                    "attempts": [{"sequence": item["sequence"], "outcome": item["outcome"],
                                  "disposition": item["disposition"], "note": item["note"],
                                  "actor_id": item["actor_id"], "consent_revision": item["consent_revision"],
                                  "created_at": item["created_at"]} for item in attempts]}

    @staticmethod
    def _task_view(row) -> dict:
        return {"id": row["id"], "patient_id": row["patient_id"], "purpose": row["purpose"],
                "channel": row["channel"], "summary": row["summary"],
                "source": {"kind": row["source_kind"], "id": row["source_id"], "event": row["source_event"]},
                "consent_id": row["consent_id"], "consent_revision": row["consent_revision"],
                "preference_revision": row["preference_revision"], "state": row["state"],
                "not_before": row["not_before"], "assigned_to": row["assigned_to"],
                "claim_until": row["claim_until"], "attempt_count": row["attempt_count"],
                "closed_outcome": row["closed_outcome"], "cancelled_reason": row["cancelled_reason"],
                "created_at": row["created_at"], "updated_at": row["updated_at"], "version": row["version"]}
