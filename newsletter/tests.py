"""Testes da app newsletter: agregação semanal (buckets por data) e o endpoint /news/weekly/.

As datas created_at/updated_at (auto_now/auto_now_add) são forçadas via update() para simular
registos novos vs. atualizados sem depender do relógio do teste.
"""

import os
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from anuncios.models import Notice
from avisos.models import Grant
from planned_grants.models import PlannedGrant
from . import services

TEST_PASSWORD = os.environ.get("TEST_USER_PASSWORD", "test-only-password")


class WeeklyNewsletterTests(TestCase):
    def _grant(self, code):
        return Grant.objects.create(
            source="portugal", scraping_url=f"https://x/{code}/", grant_code=code, ai_processed=True)

    def _notice(self, num):
        return Notice.objects.create(
            notice_number=num, entity_name="Câmara X", base_price=Decimal("1000"),
            proposal_deadline=date.today() + timedelta(days=10))

    def _set_times(self, model, pk, created, updated):
        # auto_now/auto_now_add só atuam no save — usa-se update() para forçar as datas do teste.
        model.objects.filter(pk=pk).update(created_at=created, updated_at=updated)

    def setUp(self):
        now = timezone.now()
        old = now - timedelta(days=10)
        self.g_new = self._grant("G-NEW")
        self.g_upd = self._grant("G-UPD")
        self._set_times(Grant, self.g_upd.pk, created=old, updated=now)
        self.n_new = self._notice("N-NEW")
        self.n_upd = self._notice("N-UPD")
        self._set_times(Notice, self.n_upd.pk, created=old, updated=now)
        today = timezone.localdate()
        PlannedGrant.objects.create(plan_id=1, designation="Perto",
                                    expected_start=today + timedelta(days=10))
        PlannedGrant.objects.create(plan_id=2, designation="Longe",
                                    expected_start=today + timedelta(days=40))
        PlannedGrant.objects.create(plan_id=3, designation="Passado",
                                    expected_start=today - timedelta(days=5))
        user = User.objects.create_user("comercial_news", password=TEST_PASSWORD)
        user.profile.role = "commercial_grants"
        user.profile.save()
        self.client.force_login(user)

    def test_buckets(self):
        data = services.weekly_newsletter()
        self.assertIn("generated_at", data)
        self.assertEqual([g["grant_code"] for g in data["new_grants"]], ["G-NEW"])
        self.assertEqual([g["grant_code"] for g in data["updated_grants"]], ["G-UPD"])
        self.assertEqual([n["notice_number"] for n in data["new_notices"]], ["N-NEW"])
        self.assertEqual([n["notice_number"] for n in data["updated_notices"]], ["N-UPD"])
        # Só o previsto dentro dos próximos 30 dias.
        self.assertEqual([p["plan_id"] for p in data["coming_next_30_days"]], [1])

    def test_endpoint_has_all_sections(self):
        resp = self.client.get("/news/weekly/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.json()),
            {"generated_at", "new_grants", "updated_grants", "new_notices",
             "updated_notices", "coming_next_30_days"},
        )

    def test_endpoint_rejects_post(self):
        self.assertEqual(self.client.post("/news/weekly/").status_code, 405)

    def test_endpoint_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get("/news/weekly/").status_code, 401)

    def test_endpoint_rejects_client_role(self):
        self.client.logout()
        user = User.objects.create_user("cliente_news", password=TEST_PASSWORD)
        self.client.force_login(user)  # role=client por omissão (signal)
        self.assertEqual(self.client.get("/news/weekly/").status_code, 403)
