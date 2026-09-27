from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.notifications import quiet_hours_end
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
        self.auditor = self.app.create_staff(self.clinic, "稽核员", "auditor", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-101", "林女士")

    def tearDown(self):
        self.temp.cleanup()

    def contact_consent(self, revision=1, expires_at=None):
        digest = hashlib.sha256(f"followup_contact-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], "followup_contact",
                                      revision, digest, expires_at=expires_at)

    def book_appointment(self, key="visit-1", starts="2026-09-29T10:00:00+08:00"):
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "复诊", starts,
            starts.replace("10:00", "10:30"), key, staff_id=self.clinician)
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        return appointment

    def open_tasks(self, **kwargs):
        return self.app.notifications.list_tasks(self.clinic, self.nurse, **kwargs)

    def test_preference_is_versioned_and_validated(self):
        first = self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient["id"], "followup_reminders",
            ["message", "phone"], "22:00", "07:00", "Asia/Shanghai")
        self.assertEqual(first["revision"], 1)
        second = self.app.notifications.set_preference(
            self.clinic, self.nurse, self.patient["id"], "followup_reminders",
            ["phone"], None, None, "UTC")
        self.assertEqual(second["revision"], 2)
        current = self.app.notifications.current_preferences(self.clinic, self.auditor, self.patient["id"])
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["revision"], 2)
        self.assertEqual(current[0]["channels"], ["phone"])
        self.assertIsNone(current[0]["quiet_start"])
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", [], None, None, "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["sms"], None, None, "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["phone", "phone"], None, None, "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["phone"], "22:00", None, "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["phone"], "25:00", "07:00", "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["phone"], "08:00", "08:00", "UTC")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "followup_reminders", ["phone"], None, None, "Mars/Olympus")
        with self.assertRaises(ValidationError):
            self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                                  "marketing", ["phone"], None, None, "UTC")
        with self.assertRaises(Forbidden):
            self.app.notifications.set_preference(self.clinic, self.auditor, self.patient["id"],
                                                  "followup_reminders", ["phone"], None, None, "UTC")
        with self.assertRaises(NotFound):
            self.app.notifications.set_preference(self.clinic, self.nurse, "pat_missing",
                                                  "followup_reminders", ["phone"], None, None, "UTC")

    def test_appointment_events_generate_sourced_tasks_without_duplicates(self):
        self.contact_consent()
        appointment = self.book_appointment()
        tasks = self.open_tasks()
        self.assertEqual(len(tasks), 1)
        booked = tasks[0]
        self.assertEqual(booked["purpose"], "appointment_updates")
        self.assertEqual(booked["source"], {"kind": "appointment", "id": appointment["id"], "event": "booked"})
        self.assertEqual(booked["channel"], "phone")
        self.assertEqual(booked["consent_revision"], 1)
        self.assertTrue(booked["contactable_now"])
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "cancel",
                                        reason="患者改期")
        tasks = self.open_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task["source"]["event"] for task in tasks}, {"booked", "cancelled"})
        # 状态机不允许重复确认，同一事件不会产生第二个任务。
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "book")
        self.assertEqual(len(self.open_tasks()), 2)

    def test_followup_sweep_requires_consent_and_never_duplicates(self):
        followup = self.app.schedule_followup(self.clinic, self.clinician, self.patient["id"],
                                              "2026-09-27T11:00:00Z", "复诊反馈", "fup-n1")
        first = self.app.notifications.generate_due_followup_tasks(self.clinic, self.nurse)
        self.assertEqual(first["created"], 0)
        self.assertEqual(first["skipped_no_consent"], 1)
        self.contact_consent()
        second = self.app.notifications.generate_due_followup_tasks(self.clinic, self.nurse)
        self.assertEqual(second["created"], 1)
        third = self.app.notifications.generate_due_followup_tasks(self.clinic, self.nurse)
        self.assertEqual(third["created"], 0)
        self.assertEqual(third["skipped_duplicate"], 1)
        task = self.open_tasks()[0]
        self.assertEqual(task["purpose"], "followup_reminders")
        self.assertEqual(task["source"], {"kind": "followup", "id": followup["id"], "event": "due"})
        self.assertIn("复诊反馈", task["summary"])

    def test_withdrawal_blocks_unclaimed_tasks_and_hides_patient_from_queue(self):
        self.contact_consent()
        self.book_appointment()
        self.assertEqual(len(self.open_tasks()), 1)
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"],
                                           purpose="followup_contact")[0]
        self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者撤回短信联系授权")
        self.assertEqual(self.open_tasks(), [])
        cancelled = self.open_tasks(state="cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["cancelled_reason"], "consent_withdrawn")

    def test_claimed_task_rechecks_authorization_version_at_result(self):
        self.contact_consent()
        self.book_appointment()
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=60)[0]
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"],
                                           purpose="followup_contact")[0]
        self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者撤回联系授权")
        with self.assertRaises(Conflict) as ctx:
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"],
                                                   "reached", "done", "已电话告知")
        self.assertEqual(ctx.exception.details["reason"], "consent_withdrawn")
        # 租约到期后任务不会被再次领取，而是在核对授权时取消。
        self.clock.set(datetime(2026, 9, 27, 13, 30, tzinfo=UTC))
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.coordinator), [])
        detail = self.app.notifications.task_detail(self.clinic, self.nurse, claimed["id"])
        self.assertEqual(detail["task"]["state"], "cancelled")
        self.assertEqual(detail["task"]["cancelled_reason"], "consent_withdrawn")

    def test_consent_renewal_stales_inflight_task_at_result(self):
        self.contact_consent(revision=1)
        self.book_appointment()
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=60)[0]
        self.contact_consent(revision=2)
        with self.assertRaises(Conflict) as ctx:
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"],
                                                   "reached", "done", "已电话告知")
        self.assertEqual(ctx.exception.details["reason"], "consent_superseded")

    def test_expired_consent_blocks_result_and_lazily_cancels(self):
        self.contact_consent(expires_at="2026-09-27T12:30:00Z")
        self.book_appointment()
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=60)[0]
        self.clock.set(datetime(2026, 9, 27, 12, 45, tzinfo=UTC))
        with self.assertRaises(Conflict) as ctx:
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"],
                                                   "reached", "done", "已电话告知")
        self.assertEqual(ctx.exception.details["reason"], "consent_expired")
        self.clock.set(datetime(2026, 9, 27, 13, 30, tzinfo=UTC))
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.nurse), [])
        detail = self.app.notifications.task_detail(self.clinic, self.nurse, claimed["id"])
        self.assertEqual(detail["task"]["cancelled_reason"], "consent_expired")

    def test_retry_preserves_each_attempt_and_final_manual_status(self):
        self.contact_consent()
        self.book_appointment("visit-retry")
        first = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=30)[0]
        retried = self.app.notifications.register_result(
            self.clinic, self.nurse, first["id"], first["claim_token"], first["version"],
            "no_answer", "retry", "无人接听，稍后再拨", retry_after_minutes=0)
        self.assertEqual(retried["state"], "pending")
        self.assertEqual(retried["attempt_sequence"], 1)
        second = self.app.notifications.claim_tasks(self.clinic, self.coordinator, lease_minutes=30)[0]
        closed = self.app.notifications.register_result(
            self.clinic, self.coordinator, second["id"], second["claim_token"], second["version"],
            "failed", "manual", "两次未达，转为到店沟通")
        self.assertEqual(closed["state"], "closed_manual")
        detail = self.app.notifications.task_detail(self.clinic, self.nurse, first["id"])
        self.assertEqual(detail["task"]["attempt_count"], 2)
        self.assertEqual(detail["task"]["closed_outcome"], "failed")
        self.assertEqual([a["outcome"] for a in detail["attempts"]], ["no_answer", "failed"])
        self.assertEqual([a["disposition"] for a in detail["attempts"]], ["retry", "manual"])
        self.assertEqual([a["consent_revision"] for a in detail["attempts"]], [1, 1])

    def test_done_requires_reached_and_result_needs_valid_lease(self):
        self.contact_consent()
        self.book_appointment("visit-done")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=30)[0]
        with self.assertRaises(ValidationError):
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"],
                                                   "no_answer", "done", "未接通")
        with self.assertRaises(ValidationError):
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"],
                                                   "reached", "retry", "已接通")
        with self.assertRaises(Conflict):
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   "claim_wrong", claimed["version"],
                                                   "reached", "done", "已接通")
        with self.assertRaises(Conflict):
            self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                   claimed["claim_token"], claimed["version"] + 1,
                                                   "reached", "done", "已接通")
        with self.assertRaises(NotFound):
            self.app.notifications.register_result(self.clinic, self.nurse, "ntf_missing",
                                                   claimed["claim_token"], 1, "reached", "done", "已接通")
        done = self.app.notifications.register_result(self.clinic, self.nurse, claimed["id"],
                                                      claimed["claim_token"], claimed["version"],
                                                      "reached", "done", "已电话确认改期")
        self.assertEqual(done["state"], "done")
        self.assertEqual(self.open_tasks(), [])

    def test_lease_fencing_and_role_permissions(self):
        self.contact_consent()
        self.book_appointment("visit-lease")
        first = self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=1)[0]
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        second = self.app.notifications.claim_tasks(self.clinic, self.coordinator, lease_minutes=5)[0]
        with self.assertRaises(Conflict):
            self.app.notifications.register_result(self.clinic, self.nurse, first["id"],
                                                   first["claim_token"], first["version"],
                                                   "reached", "done", "迟到回写")
        done = self.app.notifications.register_result(self.clinic, self.coordinator, second["id"],
                                                      second["claim_token"], second["version"],
                                                      "reached", "done", "已联系")
        self.assertEqual(done["state"], "done")
        with self.assertRaises(Forbidden):
            self.app.notifications.claim_tasks(self.clinic, self.auditor)
        with self.assertRaises(Forbidden):
            self.app.notifications.list_tasks(self.clinic, self.auditor)
        with self.assertRaises(Forbidden):
            self.app.notifications.generate_due_followup_tasks(self.clinic, self.auditor)

    def test_cross_midnight_quiet_hours_defer_claiming(self):
        self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                              "appointment_updates", ["phone"], "22:00", "07:00", "Asia/Shanghai")
        self.contact_consent()
        self.clock.set(datetime(2026, 9, 27, 14, 30, tzinfo=UTC))  # 患者本地 22:30
        self.book_appointment("visit-quiet")
        task = self.open_tasks()[0]
        self.assertEqual(task["not_before"], "2026-09-27T23:00:00Z")  # 次日 07:00 (+08:00)
        self.assertFalse(task["contactable_now"])
        self.assertEqual(task["blocked_reason"], "quiet_hours")
        self.assertEqual(self.app.notifications.claim_tasks(self.clinic, self.nurse), [])
        self.clock.set(datetime(2026, 9, 27, 23, 30, tzinfo=UTC))  # 患者本地次日 07:30
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse)
        self.assertEqual(len(claimed), 1)

    def test_same_day_quiet_hours_and_boundary_instants(self):
        zone = ZoneInfo("Asia/Shanghai")
        start, end = time(12, 0), time(14, 0)
        self.assertEqual(quiet_hours_end(datetime(2026, 9, 27, 5, 0, tzinfo=UTC), zone, start, end),
                         datetime(2026, 9, 27, 6, 0, tzinfo=UTC))  # 本地 13:00 → 14:00
        self.assertEqual(quiet_hours_end(datetime(2026, 9, 27, 4, 0, tzinfo=UTC), zone, start, end),
                         datetime(2026, 9, 27, 6, 0, tzinfo=UTC))  # 边界：进入静默
        self.assertIsNone(quiet_hours_end(datetime(2026, 9, 27, 6, 0, tzinfo=UTC), zone, start, end))  # 边界：恢复可联系
        self.assertIsNone(quiet_hours_end(datetime(2026, 9, 27, 3, 59, tzinfo=UTC), zone, start, end))

    def test_quiet_hours_follow_patient_timezone_across_dst(self):
        zone = ZoneInfo("America/New_York")
        start, end = time(22, 0), time(7, 0)
        # 夏令时期间：本地 07:00 对应 11:00 UTC。
        self.assertEqual(quiet_hours_end(datetime(2026, 10, 26, 5, 30, tzinfo=UTC), zone, start, end),
                         datetime(2026, 10, 26, 11, 0, tzinfo=UTC))
        # 2026-11-01 凌晨回拨冬令时：同一本地窗口对应 12:00 UTC。
        self.assertEqual(quiet_hours_end(datetime(2026, 11, 1, 5, 30, tzinfo=UTC), zone, start, end),
                         datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        # 春季切换日前后同一本地时刻的 UTC 偏移同样按患者时区解释。
        self.assertEqual(quiet_hours_end(datetime(2026, 3, 8, 6, 30, tzinfo=UTC), zone, start, end),
                         datetime(2026, 3, 8, 11, 0, tzinfo=UTC))
        self.assertIsNone(quiet_hours_end(datetime(2026, 10, 26, 15, 0, tzinfo=UTC), zone, start, end))

    def test_dst_aware_task_defers_until_local_quiet_end(self):
        self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                              "appointment_updates", ["message"], "22:00", "07:00",
                                              "America/New_York")
        self.contact_consent()
        self.clock.set(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))  # 回拨日当天本地 01:30
        self.book_appointment("visit-dst", starts="2026-11-03T10:00:00-05:00")
        task = self.open_tasks()[0]
        self.assertEqual(task["not_before"], "2026-11-01T12:00:00Z")  # 本地 07:00 EST
        self.assertEqual(task["channel"], "message")

    def test_channel_preference_change_remaps_at_claim(self):
        self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                              "appointment_updates", ["message"], None, None, "Asia/Shanghai")
        self.contact_consent()
        self.book_appointment("visit-channel")
        self.assertEqual(self.open_tasks()[0]["channel"], "message")
        self.app.notifications.set_preference(self.clinic, self.nurse, self.patient["id"],
                                              "appointment_updates", ["phone"], None, None, "Asia/Shanghai")
        claimed = self.app.notifications.claim_tasks(self.clinic, self.nurse)[0]
        self.assertEqual(claimed["channel"], "phone")

    def test_diagnostics_flag_expired_claim_and_stale_consent(self):
        self.contact_consent()
        self.book_appointment("visit-diag")
        self.app.notifications.claim_tasks(self.clinic, self.nurse, lease_minutes=1)
        self.clock.set(datetime(2026, 9, 27, 12, 5, tzinfo=UTC))
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("notification.expired_claim", {item["code"] for item in report["findings"]})
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"],
                                           purpose="followup_contact")[0]
        self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者撤回联系授权")
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("notification.consent_stale", {item["code"] for item in report["findings"]})


if __name__ == "__main__":
    unittest.main()
