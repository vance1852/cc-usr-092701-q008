"""患者联系偏好与门诊内部通知待办。

本模块只生成内部任务并登记工作人员的触达结果，不接入任何短信或消息平台。
任务是否允许触达取决于患者按用途授予的渠道、静默时段、时区以及当前有效的
随访联系授权（purpose=followup_contact）；授权撤回会阻止未领取任务，已领取
任务在登记结果时须重新核对授权版本。
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
from .validation import choice, object_value, parsed_timestamp, require_match, text, timestamp

CONTACT_PURPOSES = {"appointment_change", "followup_reminder"}
CONTACT_CHANNELS = {"phone", "sms", "message", "in_person"}
CONSENT_PURPOSE = "followup_contact"
_SOURCE_TYPES = {"appointment", "followup", "milestone", "manual"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 任务按该顺序选择患者允许的渠道。
_CHANNEL_PRIORITY = ("sms", "phone", "message", "in_person")


class NotificationService:
    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ 偏好

    def set_preference(self, clinic_id: str, actor_id: str, patient_id: str, timezone_name: str,
                       channels: dict, *, quiet_hours: dict | None = None, reason: str = "首次登记",
                       expected_version: int | None = None) -> dict:
        timezone_name = text(timezone_name, "时区", maximum=80)
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("时区无效") from exc
        channels = self._normalized_channels(channels)
        quiet_start, quiet_end = self._normalized_quiet(quiet_hours)
        reason = text(reason, "变更原因", maximum=600)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            patient = connection.execute("SELECT state FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            if patient["state"] != "active":
                raise Conflict("非在诊患者不能登记联系偏好")
            existing = connection.execute("SELECT * FROM contact_preferences WHERE patient_id=?", (patient_id,)).fetchone()
            if existing is None:
                version = 1
                connection.execute(
                    "INSERT INTO contact_preferences(id,patient_id,timezone,channels_json,quiet_start,quiet_end,"
                    "recorded_by,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,1)",
                    (new_id("prf"), patient_id, timezone_name, encode_json(channels), quiet_start, quiet_end,
                     actor_id, now, now))
                action = "contact_preference.recorded"
            else:
                require_match(existing["version"], expected_version or 0, "联系偏好")
                version = existing["version"] + 1
                connection.execute(
                    "UPDATE contact_preferences SET timezone=?,channels_json=?,quiet_start=?,quiet_end=?,"
                    "recorded_by=?,updated_at=?,version=? WHERE id=?",
                    (timezone_name, encode_json(channels), quiet_start, quiet_end, actor_id, now, version,
                     existing["id"]))
                action = "contact_preference.updated"
            connection.execute(
                "INSERT INTO contact_preference_revisions(preference_id,revision,timezone,channels_json,"
                "quiet_start,quiet_end,changed_by,change_reason,created_at) "
                "SELECT id,?,?,?,?,?,?,?,? FROM contact_preferences WHERE patient_id=?",
                (version, timezone_name, encode_json(channels), quiet_start, quiet_end, actor_id, reason, now, patient_id))
            self._apply_preference_change(connection, clinic_id=clinic_id, patient_id=patient_id,
                                          channels=channels, zone=zone, quiet_start=quiet_start,
                                          quiet_end=quiet_end, now=now, actor_id=actor_id)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="contact_preference", aggregate_id=patient_id, action=action,
                               occurred_at=now, payload={"timezone": timezone_name, "channels": channels,
                                                         "quiet_hours": self._quiet_payload(quiet_start, quiet_end),
                                                         "version": version})
        return self.get_preference(clinic_id, actor_id, patient_id)

    def get_preference(self, clinic_id: str, actor_id: str, patient_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            return self._preference_payload(connection, clinic_id, patient_id, required=True)

    def preference_revisions(self, clinic_id: str, actor_id: str, patient_id: str) -> list[dict]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            pref = connection.execute("SELECT id FROM contact_preferences WHERE patient_id=?", (patient_id,)).fetchone()
            if pref is None:
                raise NotFound("患者联系偏好尚未登记")
            rows = connection.execute(
                "SELECT r.* FROM contact_preference_revisions r JOIN contact_preferences p ON p.id=r.preference_id "
                "JOIN patients pat ON pat.id=p.patient_id WHERE p.id=? AND pat.clinic_id=? ORDER BY revision",
                (pref["id"], clinic_id)).fetchall()
            return [{"revision": row["revision"], "timezone": row["timezone"],
                     "channels": decode_json(row["channels_json"]),
                     "quiet_hours": self._quiet_payload(row["quiet_start"], row["quiet_end"]),
                     "changed_by": row["changed_by"], "change_reason": row["change_reason"],
                     "created_at": row["created_at"]} for row in rows]

    @staticmethod
    def _normalized_channels(value: object) -> dict[str, list[str]]:
        object_value(value, "渠道选择")
        normalized: dict[str, list[str]] = {}
        for purpose, channel_list in value.items():
            if purpose not in CONTACT_PURPOSES:
                raise ValidationError("联系用途取值无效", details={"allowed": sorted(CONTACT_PURPOSES)})
            if not isinstance(channel_list, list):
                raise ValidationError(f"{purpose} 的渠道必须是列表")
            chosen: list[str] = []
            for channel in channel_list:
                if channel not in CONTACT_CHANNELS:
                    raise ValidationError("联系渠道取值无效", details={"allowed": sorted(CONTACT_CHANNELS)})
                if channel not in chosen:
                    chosen.append(channel)
            normalized[purpose] = chosen
        return normalized

    @staticmethod
    def _normalized_quiet(value: object) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        object_value(value, "静默时段", allowed={"start", "end"})
        start, end = value.get("start"), value.get("end")
        if start is None or end is None:
            raise ValidationError("静默时段需要同时提供开始和结束时间")
        if not isinstance(start, str) or not _HHMM.fullmatch(start):
            raise ValidationError("静默开始时间必须为 HH:MM")
        if not isinstance(end, str) or not _HHMM.fullmatch(end):
            raise ValidationError("静默结束时间必须为 HH:MM")
        if start == end:
            raise ValidationError("静默开始与结束时间不能相同；无需静默时请省略该字段")
        return start, end

    @staticmethod
    def _quiet_payload(start: str | None, end: str | None) -> dict | None:
        return {"start": start, "end": end} if start else None

    def _preference_payload(self, connection, clinic_id: str, patient_id: str, *, required: bool) -> dict | None:
        row = connection.execute(
            "SELECT p.* FROM contact_preferences p JOIN patients pat ON pat.id=p.patient_id "
            "WHERE p.patient_id=? AND pat.clinic_id=?", (patient_id, clinic_id)).fetchone()
        if row is None:
            if required:
                raise NotFound("患者联系偏好尚未登记")
            return None
        return {"patient_id": patient_id, "timezone": row["timezone"],
                "channels": decode_json(row["channels_json"]),
                "quiet_hours": self._quiet_payload(row["quiet_start"], row["quiet_end"]),
                "recorded_by": row["recorded_by"], "updated_at": row["updated_at"], "version": row["version"]}

    def _apply_preference_change(self, connection, *, clinic_id: str, patient_id: str, channels: dict,
                                 zone: ZoneInfo, quiet_start: str | None, quiet_end: str | None,
                                 now: str, actor_id: str) -> None:
        """渠道收窄立即阻止未领取任务；静默时段或时区变化重排可联系时间。"""
        rows = connection.execute(
            "SELECT * FROM notification_tasks WHERE patient_id=? AND state='queued'", (patient_id,)).fetchall()
        now_dt = parsed_timestamp(now)
        for row in rows:
            allowed = channels.get(row["purpose"], [])
            if row["channel"] not in allowed:
                self._block_locked(connection, task=row, reason="channel_no_longer_allowed", now=now,
                                   actor_id=actor_id, note="患者联系偏好不再允许该渠道")
                continue
            next_due = timestamp(self._next_contact_time(now_dt, zone, quiet_start, quiet_end))
            connection.execute("UPDATE notification_tasks SET due_at=?,updated_at=?,version=version+1 WHERE id=? AND state='queued'",
                               (next_due, now, row["id"]))

    # ---------------------------------------------------------- 静默时段解释

    @staticmethod
    def _next_contact_time(now_dt: datetime, zone: ZoneInfo, quiet_start: str | None,
                           quiet_end: str | None) -> datetime:
        """按患者时区解释静默窗口，正确处理跨午夜窗口与夏令时日期。

        静默窗口以患者本地墙上时间表示：结束时间不晚于开始时间时视为跨午夜。
        夏令时导致的不存在或重叠时刻由 zoneinfo 按患者时区规则解释，调用方无需
        自行换算 UTC 偏移。
        """
        if not quiet_start or not quiet_end:
            return now_dt
        start_hour, start_minute = (int(part) for part in quiet_start.split(":"))
        end_hour, end_minute = (int(part) for part in quiet_end.split(":"))
        start_clock = time(start_hour, start_minute)
        end_clock = time(end_hour, end_minute)
        local = now_dt.astimezone(zone)
        day = local.date()
        # 只需检查从前一天开始（跨午夜窗口）、今天开始以及明天开始的窗口。
        for offset in (-1, 0, 1):
            window_day = day + timedelta(days=offset)
            window_start = datetime.combine(window_day, start_clock, tzinfo=zone)
            end_day = window_day if end_clock > start_clock else window_day + timedelta(days=1)
            window_end = datetime.combine(end_day, end_clock, tzinfo=zone)
            if window_start <= local < window_end:
                return window_end.astimezone(UTC)
        return now_dt

    # -------------------------------------------------------------- 任务生成

    def generate(self, clinic_id: str, actor_id: str, patient_id: str, purpose: str,
                 source_type: str, source_id: str, source_event: str, *, title: str, detail: str,
                 event_at: str | None = None) -> dict:
        """供工作人员手工建立内部通知任务（例如口头预约改期补录）。"""
        purpose = choice(purpose, "联系用途", CONTACT_PURPOSES)
        source_type = choice(source_type, "来源类型", _SOURCE_TYPES)
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            result, created = self.create_task_conn(
                connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id, purpose=purpose,
                source_type=source_type, source_id=source_id, source_event=source_event,
                title=title, detail=detail, now=timestamp(event_at or self.clock.now()))
            return result

    def create_task_conn(self, connection, *, clinic_id: str, actor_id: str | None, patient_id: str,
                         purpose: str, source_type: str, source_id: str, source_event: str,
                         title: str, detail: str, now: str) -> tuple[dict, bool]:
        """在调用方事务内建立任务；同一来源事件只建立一次（天然幂等）。

        返回 (任务结果, 是否新建)。授权、偏好和静默窗口在创建时求值一次并快照，
        领取后的结果登记仍会重新核对授权版本。
        """
        title = text(title, "通知标题", maximum=160)
        detail = text(detail, "通知内容", maximum=1000)
        source_event = text(source_event, "来源事件", maximum=80)
        self._require_source(connection, clinic_id=clinic_id, patient_id=patient_id,
                             source_type=source_type, source_id=source_id)
        dedupe_key = f"{clinic_id}:{source_type}:{source_id}:{source_event}"
        existing = connection.execute("SELECT * FROM notification_tasks WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if existing is not None:
            if existing["clinic_id"] != clinic_id or existing["patient_id"] != patient_id:
                raise Conflict("通知来源键已被其他患者或诊所使用")
            return self._task_payload(existing, replayed=True), False

        pref = connection.execute(
            "SELECT p.* FROM contact_preferences p JOIN patients pat ON pat.id=p.patient_id "
            "WHERE p.patient_id=? AND pat.clinic_id=?", (patient_id, clinic_id)).fetchone()
        patient = connection.execute("SELECT state FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
        if patient is None:
            raise NotFound("患者不存在")

        consent = connection.execute(
            "SELECT * FROM consents WHERE patient_id=? AND purpose=? ORDER BY revision DESC LIMIT 1",
            (patient_id, CONSENT_PURPOSE)).fetchone()
        now_dt = parsed_timestamp(now)
        state = "queued"
        blocked_reason = None
        channel = None
        due_dt = now_dt
        consent_id = consent["id"] if consent else None
        consent_revision = consent["revision"] if consent else None
        preference_version = pref["version"] if pref else None

        if patient["state"] != "active":
            state, blocked_reason = "blocked", "patient_inactive"
        elif pref is None:
            state, blocked_reason = "blocked", "no_preference"
        elif not decode_json(pref["channels_json"]).get(purpose):
            state, blocked_reason = "blocked", "no_allowed_channel"
        elif consent is None:
            state, blocked_reason = "blocked", "consent_missing"
        elif consent["state"] == "withdrawn":
            state, blocked_reason = "blocked", "consent_withdrawn"
        elif consent["state"] != "granted" or (consent["expires_at"] and parsed_timestamp(consent["expires_at"]) <= now_dt):
            state, blocked_reason = "blocked", "consent_expired"
        else:
            allowed = decode_json(pref["channels_json"])[purpose]
            channel = next((candidate for candidate in _CHANNEL_PRIORITY if candidate in allowed), allowed[0])
            zone = ZoneInfo(pref["timezone"])
            due_dt = self._next_contact_time(now_dt, zone, pref["quiet_start"], pref["quiet_end"])

        task_id = new_id("ntk")
        due_at = timestamp(due_dt)
        connection.execute(
            "INSERT INTO notification_tasks(id,clinic_id,patient_id,purpose,channel,source_type,source_id,"
            "source_event,dedupe_key,title,detail,due_at,state,blocked_reason,blocked_at,consent_id,"
            "consent_revision,preference_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, clinic_id, patient_id, purpose, channel, source_type, source_id, source_event,
             dedupe_key, title, detail, due_at, state, blocked_reason, now if blocked_reason else None,
             consent_id, consent_revision, preference_version, now, now))
        self._event(connection, task_id, "created", actor_id, blocked_reason or "", now,
                    consent_id=consent_id, consent_revision=consent_revision)
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="notification_task", aggregate_id=task_id, action="notification.created",
                           occurred_at=now, payload={"purpose": purpose, "channel": channel,
                                                     "source_type": source_type, "source_id": source_id,
                                                     "source_event": source_event, "state": state,
                                                     "blocked_reason": blocked_reason, "due_at": due_at})
        row = connection.execute("SELECT * FROM notification_tasks WHERE id=?", (task_id,)).fetchone()
        return self._task_payload(row, replayed=False), True

    def sweep_due(self, clinic_id: str, actor_id: str, *, limit: int = 200) -> dict:
        """为已到期的随访和随访类计划节点生成内部任务，可重复执行不会重复建。"""
        if not 1 <= limit <= 1000:
            raise ValidationError("处理数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        created = blocked = 0
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            followups = connection.execute(
                "SELECT f.* FROM followups f JOIN patients p ON p.id=f.patient_id WHERE p.clinic_id=? "
                "AND f.state='pending' AND f.due_at<=? ORDER BY f.due_at,f.id LIMIT ?",
                (clinic_id, now, limit)).fetchall()
            for row in followups:
                result, was_created = self.create_task_conn(
                    connection, clinic_id=clinic_id, actor_id=None, patient_id=row["patient_id"],
                    purpose="followup_reminder", source_type="followup", source_id=row["id"],
                    source_event="due", title="随访到期提醒", detail=row["reason"], now=now)
                created += int(was_created)
                blocked += int(was_created and result["state"] == "blocked")
            milestones = connection.execute(
                "SELECT m.*,p.patient_id FROM plan_milestones m JOIN plans p ON p.id=m.plan_id "
                "WHERE p.clinic_id=? AND m.kind='followup' AND m.state IN ('pending','deferred') "
                "AND m.due_at<=? ORDER BY m.due_at,m.id LIMIT ?", (clinic_id, now, limit)).fetchall()
            for row in milestones:
                result, was_created = self.create_task_conn(
                    connection, clinic_id=clinic_id, actor_id=None, patient_id=row["patient_id"],
                    purpose="followup_reminder", source_type="milestone", source_id=row["id"],
                    source_event="due", title=row["title"], detail="计划随访节点已到期", now=now)
                created += int(was_created)
                blocked += int(was_created and result["state"] == "blocked")
        return {"clinic_id": clinic_id, "as_of": now, "created": created,
                "blocked_on_creation": blocked}

    # -------------------------------------------------------------- 领取与登记

    def claim_tasks(self, clinic_id: str, actor_id: str, *, limit: int = 20, lease_minutes: int = 15) -> list[dict]:
        if not 1 <= limit <= 100 or not 1 <= lease_minutes <= 120:
            raise ValidationError("领取数量或租约时长超出范围")
        now = timestamp(self.clock.now())
        until = timestamp(parsed_timestamp(now) + timedelta(minutes=lease_minutes))
        claimed = []
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT * FROM notification_tasks WHERE clinic_id=? AND due_at<=? "
                "AND (state='queued' OR (state='claimed' AND claim_until<=?)) ORDER BY due_at,id LIMIT ?",
                (clinic_id, now, now, limit)).fetchall()
            for row in rows:
                changed = connection.execute(
                    "UPDATE notification_tasks SET state='claimed',assigned_to=?,claim_token=?,claim_until=?,"
                    "claim_number=claim_number+1,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND (state='queued' OR (state='claimed' AND claim_until<=?))",
                    (actor_id, new_id("clm"), until, now, row["id"], row["version"], now)).rowcount
                if not changed:
                    continue
                updated = connection.execute("SELECT * FROM notification_tasks WHERE id=?", (row["id"],)).fetchone()
                self._event(connection, row["id"], "claimed", actor_id,
                            f"第 {updated['claim_number']} 次领取，租约至 {until}", now)
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                                   aggregate_type="notification_task", aggregate_id=row["id"],
                                   action="notification.claimed", occurred_at=now,
                                   payload={"claim_number": updated["claim_number"], "claim_until": until,
                                            "previous_version": row["version"]})
                claimed.append(self._task_payload(updated))
        return claimed

    def record_attempt(self, clinic_id: str, actor_id: str, task_id: str, claim_token: str,
                       result: str, note: str, expected_version: int) -> dict:
        result = choice(result, "触达结果", {"delivered", "failed"})
        note = text(note, "结果说明", maximum=2000)
        now = timestamp(self.clock.now())
        # 拦截结论必须先落库再向调用方报错，否则撤回授权的阻止效果会随回滚丢失。
        fenced_reason: str | None = None
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            row = self._require_task(connection, clinic_id, task_id)
            require_match(row["version"], expected_version, "通知任务")
            if row["state"] != "claimed" or row["assigned_to"] != actor_id or row["claim_token"] != claim_token:
                raise Conflict("任务租约已失效或不属于当前人员")
            if row["claim_until"] <= now:
                raise Conflict("任务租约已过期")
            # 已领取任务在登记前重新核对授权版本与患者当前渠道选择。
            block_reason = self._authorization_blocker(connection, row, now)
            if block_reason is not None:
                self._block_locked(connection, task=row, reason=block_reason, now=now, actor_id=actor_id,
                                   note="登记结果时复核未通过")
                fenced_reason = block_reason
            else:
                updated = self._apply_attempt(connection, row=row, actor_id=actor_id, result=result, note=note, now=now)
        if fenced_reason is not None:
            raise Conflict("患者授权或联系偏好已变化，不能登记本次触达",
                           details={"task_state": "blocked", "reason": fenced_reason})
        return updated

    def _apply_attempt(self, connection, *, row, actor_id: str, result: str, note: str, now: str) -> dict:
        task_id = row["id"]
        if result == "delivered":
            connection.execute(
                "UPDATE notification_tasks SET state='succeeded',final_outcome=?,completed_by=?,"
                "completed_at=?,claim_token=NULL,claim_until=NULL,updated_at=?,version=version+1 WHERE id=?",
                (note, actor_id, now, now, task_id))
            self._event(connection, task_id, "attempted", actor_id, note, now,
                        attempt_result="delivered", consent_id=row["consent_id"],
                        consent_revision=row["consent_revision"])
            audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="notification_task", aggregate_id=task_id,
                               action="notification.delivered", occurred_at=now,
                               payload={"note": note, "consent_revision": row["consent_revision"]})
        else:
            pref = connection.execute("SELECT * FROM contact_preferences WHERE patient_id=?", (row["patient_id"],)).fetchone()
            zone = ZoneInfo(pref["timezone"])
            # 每次失败后增加退避（5、15、30、60 分钟……封顶 4 小时），再叠加静默窗口约束，
            # 避免同一名工作人员反复立即重试。
            failures = connection.execute(
                "SELECT COUNT(*) FROM notification_task_events WHERE task_id=? AND event_type='attempted' "
                "AND attempt_result='failed'", (task_id,)).fetchone()[0]
            backoff_minutes = min(300, 5 * (2 ** min(failures, 6)))
            retry_base = parsed_timestamp(now) + timedelta(minutes=backoff_minutes)
            next_due = timestamp(self._next_contact_time(retry_base, zone,
                                                         pref["quiet_start"], pref["quiet_end"]))
            connection.execute(
                "UPDATE notification_tasks SET state='queued',assigned_to=NULL,claim_token=NULL,"
                "claim_until=NULL,due_at=?,updated_at=?,version=version+1 WHERE id=?",
                (next_due, now, task_id))
            self._event(connection, task_id, "attempted", actor_id, note, now, attempt_result="failed",
                        consent_id=row["consent_id"], consent_revision=row["consent_revision"])
            audit.append_event(connection, clinic_id=row["clinic_id"], actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="notification_task", aggregate_id=task_id,
                               action="notification.attempt_failed", occurred_at=now,
                               payload={"note": note, "retry_due_at": next_due,
                                        "consent_revision": row["consent_revision"]})
        return self._task_payload(connection.execute("SELECT * FROM notification_tasks WHERE id=?", (task_id,)).fetchone())

    def resolve_manually(self, clinic_id: str, actor_id: str, task_id: str, outcome: str,
                         expected_version: int) -> dict:
        """人工终结：不再通过该任务触达患者（例如当面告知或注销号码），结论保留在任务上。"""
        outcome = text(outcome, "人工处理结论", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "followup:manage", clinic_id=clinic_id)
            row = self._require_task(connection, clinic_id, task_id)
            require_match(row["version"], expected_version, "通知任务")
            if row["state"] in {"succeeded", "cancelled"}:
                raise Conflict("任务已结束，不能重复人工处理")
            if row["state"] == "claimed" and row["assigned_to"] != actor_id and principal.role not in {"owner", "clinician"}:
                raise Conflict("任务已被其他人员领取，请先与其确认")
            connection.execute(
                "UPDATE notification_tasks SET state='cancelled',final_outcome=?,completed_by=?,completed_at=?,"
                "claim_token=NULL,claim_until=NULL,updated_at=?,version=version+1 WHERE id=?",
                (outcome, actor_id, now, now, task_id))
            self._event(connection, task_id, "resolved_manually", actor_id, outcome, now)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="notification_task", aggregate_id=task_id,
                               action="notification.resolved_manually", occurred_at=now,
                               payload={"outcome": outcome, "previous_state": row["state"]})
            updated = connection.execute("SELECT * FROM notification_tasks WHERE id=?", (task_id,)).fetchone()
            return self._task_payload(updated)

    def reopen(self, clinic_id: str, actor_id: str, task_id: str, reason: str, expected_version: int) -> dict:
        """解除阻止并重新按当前授权与偏好排队；授权或渠道仍不满足时拒绝。"""
        reason = text(reason, "解除原因", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            row = self._require_task(connection, clinic_id, task_id)
            require_match(row["version"], expected_version, "通知任务")
            if row["state"] != "blocked":
                raise Conflict("只有被阻止的任务可以解除阻止")
            patient = connection.execute("SELECT state FROM patients WHERE id=?", (row["patient_id"],)).fetchone()
            if patient is None or patient["state"] != "active":
                raise Conflict("患者档案当前不可用", details={"reason": "patient_inactive"})
            pref = connection.execute("SELECT * FROM contact_preferences WHERE patient_id=?", (row["patient_id"],)).fetchone()
            if pref is None:
                raise Conflict("患者联系偏好缺失", details={"reason": "no_preference"})
            allowed = decode_json(pref["channels_json"]).get(row["purpose"], [])
            if not allowed:
                raise Conflict("患者未允许该用途的联系渠道", details={"reason": "no_allowed_channel"})
            # reopen 接受患者重新授予的新版本并重新快照。
            consent = self._latest_valid_consent(connection, row["patient_id"], now)
            if isinstance(consent, str):
                raise Conflict("当前授权仍不满足，不能解除阻止", details={"reason": consent})
            channel = row["channel"] if row["channel"] in allowed else next(
                (candidate for candidate in _CHANNEL_PRIORITY if candidate in allowed), allowed[0])
            zone = ZoneInfo(pref["timezone"])
            next_due = timestamp(self._next_contact_time(parsed_timestamp(now), zone,
                                                         pref["quiet_start"], pref["quiet_end"]))
            connection.execute(
                "UPDATE notification_tasks SET state='queued',blocked_reason=NULL,blocked_at=NULL,"
                "channel=?,consent_id=?,consent_revision=?,preference_version=?,due_at=?,"
                "claim_token=NULL,claim_until=NULL,updated_at=?,version=version+1 WHERE id=?",
                (channel, consent["id"], consent["revision"], pref["version"], next_due, now, task_id))
            self._event(connection, task_id, "reopened", actor_id, reason, now,
                        consent_id=consent["id"], consent_revision=consent["revision"])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="notification_task", aggregate_id=task_id,
                               action="notification.reopened", occurred_at=now,
                               payload={"reason": reason, "consent_revision": consent["revision"],
                                        "due_at": next_due})
            updated = connection.execute("SELECT * FROM notification_tasks WHERE id=?", (task_id,)).fetchone()
            return self._task_payload(updated)

    def _latest_valid_consent(self, connection, patient_id: str, now: str):
        """返回患者随访联系授权的最新有效行；不存在或无效时返回阻止原因。"""
        latest = connection.execute(
            "SELECT * FROM consents WHERE patient_id=? AND purpose=? ORDER BY revision DESC LIMIT 1",
            (patient_id, CONSENT_PURPOSE)).fetchone()
        if latest is None:
            return "consent_missing"
        if latest["state"] == "withdrawn":
            return "consent_withdrawn"
        if latest["state"] != "granted":
            return "consent_expired"
        if latest["expires_at"] and parsed_timestamp(latest["expires_at"]) <= parsed_timestamp(now):
            return "consent_expired"
        return latest

    def _authorization_blocker(self, connection, task, now: str) -> str | None:
        """登记结果前复核：患者状态、当前渠道选择以及授权身份/版本/有效期。

        允许触达的条件是最新授权仍然有效，并且其版本与领取时快照的版本一致；
        患者撤回或签署新版本都会使旧租约失效。
        """
        patient = connection.execute("SELECT state FROM patients WHERE id=?", (task["patient_id"],)).fetchone()
        if patient is None or patient["state"] != "active":
            return "patient_inactive"
        pref = connection.execute("SELECT * FROM contact_preferences WHERE patient_id=?", (task["patient_id"],)).fetchone()
        if pref is None:
            return "no_preference"
        if task["channel"] not in decode_json(pref["channels_json"]).get(task["purpose"], []):
            return "channel_no_longer_allowed"
        latest = self._latest_valid_consent(connection, task["patient_id"], now)
        if isinstance(latest, str):
            return latest
        if latest["revision"] != (task["consent_revision"] or 0):
            return "consent_superseded"
        return None

    def block_pending_for_consent_withdrawal(self, connection, *, patient_id: str, consent_id: str,
                                             now: str, actor_id: str | None) -> int:
        """撤回随访联系授权时阻止尚未领取的对应任务；已领取任务留待登记时拦截。"""
        rows = connection.execute(
            "SELECT * FROM notification_tasks WHERE patient_id=? AND state='queued'", (patient_id,)).fetchall()
        for row in rows:
            self._block_locked(connection, task=row, reason="consent_withdrawn", now=now, actor_id=actor_id,
                               note=f"授权 {consent_id} 已撤回")
        return len(rows)

    def _block_locked(self, connection, *, task, reason: str, now: str, actor_id: str | None,
                      note: str) -> dict:
        connection.execute(
            "UPDATE notification_tasks SET state='blocked',blocked_reason=?,blocked_at=?,"
            "claim_token=NULL,claim_until=NULL,updated_at=?,version=version+1 WHERE id=? AND state!='succeeded' AND state!='cancelled'",
            (reason, now, now, task["id"]))
        self._event(connection, task["id"], "blocked", actor_id, note or reason, now,
                    consent_id=task["consent_id"], consent_revision=task["consent_revision"])
        audit.append_event(connection, clinic_id=task["clinic_id"], actor_id=actor_id,
                           patient_id=task["patient_id"], aggregate_type="notification_task",
                           aggregate_id=task["id"], action="notification.blocked", occurred_at=now,
                           payload={"reason": reason, "previous_state": task["state"], "note": note})
        updated = connection.execute("SELECT * FROM notification_tasks WHERE id=?", (task["id"],)).fetchone()
        return self._task_payload(updated)

    # ------------------------------------------------------------------ 查询

    def list_tasks(self, clinic_id: str, actor_id: str, *, state: str = "open", limit: int = 200) -> dict:
        state = choice(state, "任务状态筛选", {"open", "queued", "claimed", "blocked", "succeeded",
                                              "cancelled", "all"})
        if not 1 <= limit <= 1000:
            raise ValidationError("查询数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            if state == "open":
                condition, params = "state IN ('queued','claimed')", []
            elif state == "all":
                condition, params = "1=1", []
            else:
                condition, params = "state=?", [state]
            rows = connection.execute(
                "SELECT * FROM notification_tasks WHERE clinic_id=? AND " + condition +
                " ORDER BY due_at,id LIMIT ?", [clinic_id, *params, limit]).fetchall()
            return {"clinic_id": clinic_id, "as_of": now, "returned": len(rows),
                    "items": [self._task_payload(row) for row in rows]}

    def get_task(self, clinic_id: str, actor_id: str, task_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            return self._task_payload(self._require_task(connection, clinic_id, task_id))

    def task_history(self, clinic_id: str, actor_id: str, task_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "followup:manage", clinic_id=clinic_id)
            task = self._require_task(connection, clinic_id, task_id)
            rows = connection.execute("SELECT * FROM notification_task_events WHERE task_id=? ORDER BY sequence,id",
                                      (task_id,)).fetchall()
            return {"task": self._task_payload(task),
                    "events": [{"sequence": row["sequence"], "type": row["event_type"],
                                "actor_id": row["actor_id"], "note": row["note"],
                                "attempt_result": row["attempt_result"], "consent_id": row["consent_id"],
                                "consent_revision": row["consent_revision"], "occurred_at": row["occurred_at"]}
                               for row in rows]}

    def _require_task(self, connection, clinic_id: str, task_id: str):
        row = connection.execute("SELECT * FROM notification_tasks WHERE id=? AND clinic_id=?",
                                 (task_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("通知任务不存在")
        return row

    @staticmethod
    def _require_source(connection, *, clinic_id: str, patient_id: str, source_type: str, source_id: str) -> None:
        """非手工任务必须引用真实且属于该患者的来源实体，保证任务来源明确可溯。"""
        source_id = text(source_id, "来源编号", maximum=80)
        if source_type == "manual":
            return
        if source_type == "appointment":
            row = connection.execute("SELECT patient_id FROM appointments WHERE id=? AND clinic_id=?",
                                     (source_id, clinic_id)).fetchone()
        elif source_type == "followup":
            row = connection.execute("SELECT patient_id FROM followups WHERE id=?", (source_id,)).fetchone()
        elif source_type == "milestone":
            row = connection.execute("SELECT p.patient_id FROM plan_milestones m JOIN plans p ON p.id=m.plan_id "
                                     "WHERE m.id=? AND p.clinic_id=?", (source_id, clinic_id)).fetchone()
        else:
            raise ValidationError("来源类型无效")
        if row is None or row["patient_id"] != patient_id:
            raise NotFound("通知来源不存在")

    @staticmethod
    def _event(connection, task_id: str, event_type: str, actor_id: str | None, note: str, now: str, *,
               attempt_result: str | None = None, consent_id: str | None = None,
               consent_revision: int | None = None) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM notification_task_events WHERE task_id=?", (task_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO notification_task_events(id,task_id,sequence,event_type,actor_id,note,attempt_result,"
            "consent_id,consent_revision,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new_id("nte"), task_id, sequence, event_type, actor_id, note, attempt_result,
             consent_id, consent_revision, now))

    @staticmethod
    def _task_payload(row, *, replayed: bool = False) -> dict:
        payload = {"id": row["id"], "patient_id": row["patient_id"], "purpose": row["purpose"],
                   "channel": row["channel"], "source_type": row["source_type"], "source_id": row["source_id"],
                   "source_event": row["source_event"], "title": row["title"], "detail": row["detail"],
                   "due_at": row["due_at"], "state": row["state"], "assigned_to": row["assigned_to"],
                   "claim_token": row["claim_token"], "claim_until": row["claim_until"],
                   "claim_number": row["claim_number"], "blocked_reason": row["blocked_reason"],
                   "final_outcome": row["final_outcome"], "completed_by": row["completed_by"],
                   "completed_at": row["completed_at"], "consent_id": row["consent_id"],
                   "consent_revision": row["consent_revision"], "preference_version": row["preference_version"],
                   "created_at": row["created_at"], "version": row["version"]}
        if replayed:
            payload["replayed"] = True
        return payload
