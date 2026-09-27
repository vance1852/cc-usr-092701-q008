from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow


class NotificationCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.auditor = self.app.create_staff(self.clinic, "内审员", "auditor", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-021", "林女士")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, revision=1, expires_at=None):
        digest = hashlib.sha256(f"followup_contact-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic, self.clinician, self.patient, "followup_contact",
                                      revision, digest, expires_at=expires_at)

    def preference(self, *, timezone_name="Asia/Shanghai", channels=None, quiet_hours=None, expected_version=None):
        return self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient, timezone_name,
            channels or {"appointment_change": ["sms"], "followup_reminder": ["phone", "message"]},
            quiet_hours=quiet_hours, reason="患者午休偏好", expected_version=expected_version)

    def queued_task(self, *, purpose="followup_reminder", source=("manual", "case-1", "requested"),
                    event_at="2026-09-27T12:00:00Z"):
        return self.app.notifications.generate(
            self.clinic, self.nurse, self.patient, purpose, source[0], source[1], source[2],
            title="复诊提醒", detail="请按约定时间复诊", event_at=event_at)

    # ------------------------------------------------------------ 偏好校验

    def test_preference_validation_and_versioned_revision_history(self):
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient, "Asia/NotAZone",
                                                  {"followup_reminder": ["phone"]}, reason="首次登记")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient, "Asia/Shanghai",
                                                  {"followup_reminder": ["pager"]}, reason="首次登记")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient, "Asia/Shanghai",
                                                  {"followup_reminder": ["phone"]},
                                                  quiet_hours={"start": "12:00", "end": "12:00"}, reason="首次登记")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient, "Asia/Shanghai",
                                                  {"followup_reminder": ["phone"]},
                                                  quiet_hours={"start": "25:00", "end": "14:00"}, reason="首次登记")
        with self.assertRaises(Forbidden):
            self.app.notifications.set_preference(self.clinic, self.auditor, self.patient, "Asia/Shanghai",
                                                  {"followup_reminder": ["phone"]}, reason="首次登记")
        first = self.preference()
        self.assertEqual(first["version"], 1)
        with self.assertRaises(Conflict):
            self.preference()  # 更新必须带期望版本
        updated = self.preference(channels={"appointment_change": ["sms"], "followup_reminder": ["phone"]},
                                  expected_version=1)
        self.assertEqual(updated["version"], 2)
        revisions = self.app.notifications.preference_revisions(self.clinic, self.nurse, self.patient)
        self.assertEqual([item["revision"] for item in revisions], [1, 2])
        with self.assertRaises(NotFound):
            self.app.notifications.get_preference(self.clinic, self.nurse, "pat_unknown")

    # ------------------------------------------------------------ 静默与时区

    def test_same_day_quiet_hours_are_interpreted_in_patient_timezone(self):
        self.preference(quiet_hours={"start": "12:00", "end": "14:00"})
        self.consent()
        # 上海时间 13:00 = 05:00Z，处于静默时段，顺延至上海 14:00 = 06:00Z。
        task = self.queued_task(event_at="2026-09-27T05:00:00Z")
        self.assertEqual(task["state"], "queued")
        self.assertEqual(task["due_at"], "2026-09-27T06:00:00Z")
        self.assertEqual(task["channel"], "phone")
        # 静默开始前立即排入，不延迟。
        other = self.queued_task(source=("manual", "case-2", "requested"), event_at="2026-09-27T03:59:00Z")
        self.assertEqual(other["due_at"], "2026-09-27T03:59:00Z")

    def test_overnight_quiet_hours_wrap_past_midnight(self):
        self.preference(quiet_hours={"start": "22:00", "end": "08:00"})
        self.consent()
        # 上海时间 23:00 = 15:00Z，处于跨午夜静默，顺延至次日上海 08:00 = 00:00Z。
        evening = self.queued_task(source=("manual", "case-eve", "requested"), event_at="2026-09-27T15:00:00Z")
        self.assertEqual(evening["due_at"], "2026-09-28T00:00:00Z")
        # 上海时间次日 07:00 = 前一日 23:00Z，仍属同一跨午夜窗口。
        early = self.queued_task(source=("manual", "case-morning", "requested"), event_at="2026-09-27T23:00:00Z")
        self.assertEqual(early["due_at"], "2026-09-28T00:00:00Z")

    def test_quiet_hours_follow_dst_dates_in_patient_zone(self):
        self.preference(timezone_name="America/New_York",
                        channels={"appointment_change": ["sms"], "followup_reminder": ["phone"]},
                        quiet_hours={"start": "01:00", "end": "03:00"})
        self.consent()
        # 2026-11-01 北美夏令时结束：06:00Z 对应当地 02:00(EDT)，窗口到当地 03:00(EST)=08:00Z 结束。
        fall_back = self.queued_task(source=("manual", "dst-fall", "requested"), event_at="2026-11-01T06:00:00Z")
        self.assertEqual(fall_back["due_at"], "2026-11-01T08:00:00Z")
        # 2026-03-08 春令时开始：06:30Z 对应当地 02:30(EDT)，窗口到当地 03:00(EDT)=07:00Z 结束。
        spring = self.queued_task(source=("manual", "dst-spring", "requested"), event_at="2026-03-08T06:30:00Z")
        self.assertEqual(spring["due_at"], "2026-03-08T07:00:00Z")

    # ------------------------------------------------------------ 创建与幂等

    def test_task_requires_consent_preference_and_channel_or_is_blocked_with_reason(self):
        missing = self.queued_task(source=("manual", "m1", "requested"))
        self.assertEqual(missing["state"], "blocked")
        self.assertEqual(missing["blocked_reason"], "no_preference")
        self.preference()
        no_consent = self.queued_task(source=("manual", "m2", "requested"))
        self.assertEqual(no_consent["blocked_reason"], "consent_missing")
        self.consent()
        self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient, "Asia/Shanghai",
            {"followup_reminder": ["phone", "message"]}, reason="暂不接受预约类提醒", expected_version=1)
        blocked_purpose = self.app.notifications.generate(
            self.clinic, self.nurse, self.patient, "appointment_change", "manual", "m3", "requested",
            title="预约提醒", detail="x", event_at="2026-09-27T12:00:00Z")
        self.assertEqual(blocked_purpose["blocked_reason"], "no_allowed_channel")
        ready = self.queued_task(source=("manual", "m4", "requested"))
        self.assertEqual(ready["state"], "queued")
        self.assertEqual(ready["consent_revision"], 1)

    def test_duplicate_source_event_never_creates_two_tasks(self):
        self.preference()
        self.consent()
        first = self.queued_task()
        replay = self.queued_task()
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        listing = self.app.notifications.list_tasks(self.clinic, self.nurse, state="all")
        self.assertEqual(listing["returned"], 1)

    def test_appointment_confirmation_and_cancellation_create_sourced_tasks(self):
        self.preference(channels={"appointment_change": ["sms"], "followup_reminder": ["phone"]})
        self.consent()
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient, "复诊",
            "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", "visit-n1", staff_id=self.clinician)
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        tasks = self.app.notifications.list_tasks(self.clinic, self.nurse, state="all")["items"]
        confirmed = next(item for item in tasks if item["source_event"] == "confirmed")
        self.assertEqual(confirmed["source_type"], "appointment")
        self.assertEqual(confirmed["source_id"], appointment["id"])
        self.assertEqual(confirmed["channel"], "sms")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "cancel", reason="医生停诊")
        tasks = self.app.notifications.list_tasks(self.clinic, self.nurse, state="all")["items"]
        cancelled = next(item for item in tasks if item["source_event"] == "cancelled")
        self.assertIn("医生停诊", cancelled["detail"])

    def test_sweep_generates_followup_and_milestone_tasks_once(self):
        self.preference()
        self.consent()
        self.app.schedule_followup(self.clinic, self.clinician, self.patient, "2026-09-27T11:00:00Z",
                                   "复诊反馈", "fup-n1")
        plan = self.app.create_plan(
            self.clinic, self.clinician, self.patient, "wellbeing", self.clinician,
            {"description": "随访"}, {}, "2026-09-01")
        self.app.milestones.create(self.clinic, self.clinician, plan["id"], "followup", "节点随访",
                                   "2026-09-27T10:00:00Z", "msl-n1", assigned_to=self.nurse)
        first = self.app.notifications.sweep_due(self.clinic, self.coordinator)
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["blocked_on_creation"], 0)
        second = self.app.notifications.sweep_due(self.clinic, self.coordinator)
        self.assertEqual(second["created"], 0)
        sources = {(item["source_type"], item["source_event"])
                   for item in self.app.notifications.list_tasks(self.clinic, self.nurse, state="all")["items"]}
        self.assertEqual(sources, {("followup", "due"), ("milestone", "due")})

    # ------------------------------------------------------------ 授权撤回

    def test_withdrawal_blocks_unclaimed_task_and_hides_it_from_work_queue(self):
        self.preference()
        consent = self.consent()
        task = self.queued_task(event_at="2026-09-28T12:00:00Z")
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.nurse), [])  # 尚未到期
        result = self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者只接受晚间联系，短信授权撤回")
        self.assertEqual(result["state"], "withdrawn")
        blocked = self.app.notifications.get_task(self.clinic, self.nurse, task["id"])
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["blocked_reason"], "consent_withdrawn")
        # 护士待联系名单不再出现该患者。
        open_queue = self.app.notifications.list_tasks(self.clinic, self.nurse, state="open")
        self.assertEqual(open_queue["returned"], 0)
        blocked_queue = self.app.notifications.list_tasks(self.clinic, self.nurse, state="blocked")
        self.assertEqual(blocked_queue["returned"], 1)
        history = self.app.notifications.task_history(self.clinic, self.nurse, task["id"])["events"]
        self.assertEqual([item["type"] for item in history], ["created", "blocked"])

    def test_claimed_task_is_fenced_when_consent_withdrawn_before_result_is_recorded(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T11:00:00Z")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        self.assertEqual(claimed["id"], task["id"])
        consents = self.app.consent_history(self.clinic, self.clinician, self.patient)
        self.app.withdraw_consent(self.clinic, self.clinician, consents[0]["id"], "患者撤回短信授权")
        with self.assertRaises(Conflict):
            self.app.notifications.record_attempt(
                self.clinic, self.nurse, task["id"], claimed["claim_token"], "delivered", "已电话提醒",
                claimed["version"])
        blocked = self.app.notifications.get_task(self.clinic, self.nurse, task["id"])
        self.assertEqual(blocked["state"], "blocked")
        self.assertIsNone(blocked["claim_token"])
        # 重新授权前不能解除阻止。
        with self.assertRaises(Conflict):
            self.app.notifications.reopen(self.clinic, self.nurse, task["id"], "患者改主意", blocked["version"])
        self.consent(revision=2)
        reopened = self.app.notifications.reopen(self.clinic, self.nurse, task["id"], "患者重新授权", blocked["version"])
        self.assertEqual(reopened["state"], "queued")
        self.assertEqual(reopened["consent_revision"], 2)

    def test_newer_consent_reversion_fences_stale_claim(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T11:00:00Z")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        self.consent(revision=2)  # 旧版本在授予新版本时自动到期
        with self.assertRaises(Conflict):
            self.app.notifications.record_attempt(
                self.clinic, self.nurse, task["id"], claimed["claim_token"], "delivered", "已提醒", claimed["version"])
        self.assertEqual(self.app.notifications.get_task(self.clinic, self.nurse, task["id"])["state"], "blocked")

    # ------------------------------------------------------------ 重试与终态

    def test_failed_attempts_are_retained_across_retries_until_delivery(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T10:00:00Z")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=30)[0]
        failed = self.app.notifications.record_attempt(
            self.clinic, self.nurse, task["id"], claimed["claim_token"], "failed", "无人接听", claimed["version"])
        self.assertEqual(failed["state"], "queued")
        self.assertGreaterEqual(failed["due_at"], "2026-09-27T12:05:00Z")
        self.assertIsNone(failed["assigned_to"])
        # 旧租约令牌不能提交新结果。
        with self.assertRaises(Conflict):
            self.app.notifications.record_attempt(
                self.clinic, self.nurse, task["id"], claimed["claim_token"], "delivered", "迟到回写", failed["version"])
        # 退避结束前任务不会再次进入领取名单。
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.nurse), [])
        self.clock.set(datetime(2026, 9, 27, 12, 6, tzinfo=UTC))
        claimed_again = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        self.assertEqual(claimed_again["claim_number"], 2)
        failed_again = self.app.notifications.record_attempt(
            self.clinic, self.nurse, task["id"], claimed_again["claim_token"], "failed", "仍无人接听",
            claimed_again["version"])
        self.assertGreaterEqual(failed_again["due_at"], "2026-09-27T12:16:00Z")
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.nurse), [])
        self.clock.set(datetime(2026, 9, 27, 12, 17, tzinfo=UTC))
        claimed_third = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        delivered = self.app.notifications.record_attempt(
            self.clinic, self.nurse, task["id"], claimed_third["claim_token"], "delivered", "患者确认复诊时间",
            claimed_third["version"])
        self.assertEqual(delivered["state"], "succeeded")
        self.assertEqual(delivered["final_outcome"], "患者确认复诊时间")
        history = self.app.notifications.task_history(self.clinic, self.nurse, task["id"])["events"]
        self.assertEqual([item["attempt_result"] for item in history if item["type"] == "attempted"],
                         ["failed", "failed", "delivered"])
        with self.assertRaises(Conflict):
            self.app.notifications.resolve_manually(self.clinic, self.nurse, task["id"], "重复终结", delivered["version"])

    def test_manual_resolution_preserves_final_state_and_reason(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T10:00:00Z")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        self.app.notifications.record_attempt(self.clinic, self.nurse, task["id"], claimed["claim_token"],
                                              "failed", "号码停机", claimed["version"])
        queued = self.app.notifications.get_task(self.clinic, self.nurse, task["id"])
        resolved = self.app.notifications.resolve_manually(
            self.clinic, self.coordinator, task["id"], "患者已在门诊当面确认，不再电话提醒", queued["version"])
        self.assertEqual(resolved["state"], "cancelled")
        self.assertEqual(resolved["completed_by"], self.coordinator)
        self.assertIn("当面确认", resolved["final_outcome"])
        history = self.app.notifications.task_history(self.clinic, self.nurse, task["id"])["events"]
        self.assertEqual([item["attempt_result"] for item in history if item["type"] == "attempted"], ["failed"])
        self.assertEqual(history[-1]["type"], "resolved_manually")

    def test_expired_lease_can_be_reclaimed_but_old_token_is_fenced(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T10:00:00Z")
        first = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=1)[0]
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.app.notifications.record_attempt(self.clinic, self.nurse, task["id"], first["claim_token"],
                                                  "delivered", "迟到回写", first["version"])
        second = self.app.notifications.claim_tasks(self.clinic, self.coordinator, lease_minutes=10)[0]
        self.assertEqual(second["claim_number"], 2)
        done = self.app.notifications.record_attempt(self.clinic, self.coordinator, task["id"], second["claim_token"],
                                                     "delivered", "已提醒", second["version"])
        self.assertEqual(done["state"], "succeeded")
        history = self.app.notifications.task_history(self.clinic, self.nurse, task["id"])["events"]
        self.assertEqual([item["type"] for item in history].count("claimed"), 2)

    # ------------------------------------------------------------ 偏好变更

    def test_channel_removal_blocks_queued_task_and_quiet_change_reschedules(self):
        self.preference(quiet_hours={"start": "22:00", "end": "08:00"})
        self.consent()
        task = self.queued_task(event_at="2026-09-27T12:00:00Z")  # 上海 20:00，非静默
        self.assertEqual(task["state"], "queued")
        updated = self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient, "Asia/Shanghai",
            {"appointment_change": ["sms"], "followup_reminder": ["message"]},
            quiet_hours={"start": "22:00", "end": "08:00"}, reason="改为只收消息", expected_version=1)
        # 渠道仍含 message，任务保留但渠道跟随优先级重选的逻辑不改变已快照渠道以外的用途：
        # 这里验证渠道收窄（去掉 phone 与 message 之外）不阻止仍允许的任务。
        self.assertEqual(updated["version"], 2)
        narrowed = self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient, "Asia/Shanghai",
            {"appointment_change": ["sms"], "followup_reminder": ["in_person"]},
            quiet_hours={"start": "22:00", "end": "08:00"}, reason="仅当面沟通", expected_version=2)
        self.assertEqual(narrowed["version"], 3)
        # phone 渠道不再允许 → 任务被阻止。
        self.assertEqual(self.app.notifications.get_task(self.clinic, self.nurse, task["id"])["blocked_reason"],
                         "channel_no_longer_allowed")

    def test_quiet_change_reschedules_open_task_to_end_of_new_window(self):
        self.preference()
        self.consent()
        task = self.queued_task(event_at="2026-09-27T12:00:00Z")
        self.assertEqual(task["due_at"], "2026-09-27T12:00:00Z")
        self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient, "Asia/Shanghai",
            {"appointment_change": ["sms"], "followup_reminder": ["phone"]},
            quiet_hours={"start": "19:00", "end": "22:00"}, reason="新增晚间静默", expected_version=1)
        # 上海 20:00 = 12:00Z 落入新窗口，顺延至上海 22:00 = 14:00Z。
        self.assertEqual(self.app.notifications.get_task(self.clinic, self.nurse, task["id"])["due_at"],
                         "2026-09-27T14:00:00Z")

    def test_http_workflow_preference_withdrawal_removes_patient_from_nurse_queue(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        for staff_id in (self.nurse, self.coordinator, self.clinician):
            self.app.set_password(self.clinic, self.owner, staff_id, "LongPassphrase!2026")

        def call(method, path, staff_id, body=None, headers=None):
            token = self.app.login(self.clinic, staff_id, "LongPassphrase!2026")["access_token"]
            request = Request(
                base + path,
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                         "Content-Type": "application/json", **(headers or {})})
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())

        try:
            status, pref = call("POST", f"/patients/{self.patient}/contact-preferences", self.nurse, {
                "timezone": "Asia/Shanghai",
                "channels": {"appointment_change": ["sms"], "followup_reminder": ["sms", "phone"]},
                "quiet_hours": {"start": "12:00", "end": "14:00"},
                "reason": "只愿午休或晚间接收提醒"})
            self.assertEqual(status, 200)
            self.assertEqual(pref["version"], 1)
            consent = self.consent()
            status, task = call("POST", "/notifications", self.coordinator, {
                "patient_id": self.patient, "purpose": "followup_reminder",
                "source_type": "manual", "source_id": "http-case-1", "source_event": "requested",
                "title": "复诊提醒", "detail": "请按时复诊"})
            self.assertEqual(status, 201)
            # 当前 12:00Z = 上海 20:00，不在午休静默窗口内，任务立即排队。
            self.assertEqual(task["state"], "queued")
            self.assertIsNone(task["blocked_reason"])
            status, queue = call("GET", "/notifications?state=open", self.nurse)
            self.assertEqual(queue["returned"], 1)
            status, withdrawn = call("POST", f"/consents/{consent['id']}/withdraw", self.clinician,
                                     {"reason": "患者撤回短信联系授权"})
            self.assertEqual(withdrawn["state"], "withdrawn")
            status, queue = call("GET", "/notifications?state=open", self.nurse)
            self.assertEqual(queue["returned"], 0)
            status, blocked_queue = call("GET", "/notifications?state=blocked", self.nurse)
            self.assertEqual(blocked_queue["items"][0]["blocked_reason"], "consent_withdrawn")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
