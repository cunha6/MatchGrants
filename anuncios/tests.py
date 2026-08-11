"""Tests for the anuncios app.

Network (base.gov API), Selenium/Chrome and file writes are mocked — the suite runs
offline and does not touch pdf_Anuncios/ or the real lock files.
"""

import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from anuncios import services, specifications, specifications_ai
from anuncios.models import Notice
from users.models import UserProfile

# Password dos utilizadores de teste — lida do ambiente (.env), nunca hardcoded. Os testes usam
# force_login, por isso o valor não é autenticado; só não pode ficar no código versionado.
TEST_PASSWORD = os.environ.get("TEST_USER_PASSWORD", "test-only-password")


class FakeResp:
    """Minimal stand-in for a requests.Response used by the specifications helpers."""

    def __init__(self, content=b"", ctype="text/html", url="http://x", cd=None):
        self.content = content
        self.url = url
        self.headers = {"Content-Type": ctype}
        if cd:
            self.headers["Content-Disposition"] = cd


# --- Normalization / keyword matching -------------------------------------

class NormalizationTests(SimpleTestCase):
    def test_normalize_strips_accents_and_case(self):
        self.assertEqual(specifications.normalize("Ção ÁÉÍ"), "cao aei")

    def test_normalize_empty(self):
        self.assertEqual(specifications.normalize(None), "")
        self.assertEqual(specifications.normalize(""), "")

    def test_matched_keywords_accent_insensitive(self):
        # "Serviços" foi removida das KEYWORDS de propósito (demasiado genérica — aparece em
        # quase todos os anúncios de contratação pública e anulava o filtro).
        got = services.matched_keywords("Aquisição de serviços de CONSULTADORIA e avaliação")
        self.assertIn("Consultadoria", got)
        self.assertIn("Avaliação", got)
        self.assertNotIn("Serviços", got)

    def test_matched_keywords_none(self):
        self.assertEqual(services.matched_keywords("Fornecimento de material de limpeza"), [])


# --- Tolerant parsers ------------------------------------------------------

class ParserTests(SimpleTestCase):
    def test_pick_case_insensitive(self):
        # Matches by exact key, case-insensitively (not singular/plural fuzzy).
        raw = {"DataLimitePropostas": "13/07/2026", "PrecoBase": "9950.00"}
        self.assertEqual(services._pick(raw, "datalimitepropostas"), "13/07/2026")
        self.assertEqual(services._pick(raw, "precoBase"), "9950.00")

    def test_pick_skips_empty_and_missing(self):
        raw = {"a": "", "b": None, "c": "x"}
        self.assertEqual(services._pick(raw, "a", "b", "c"), "x")
        self.assertIsNone(services._pick(raw, "z"))
        self.assertIsNone(services._pick({}, "a"))

    def test_parse_date_iso_and_pt(self):
        self.assertEqual(services._parse_date("2026-07-15"), date(2026, 7, 15))
        self.assertEqual(services._parse_date("15/07/2026"), date(2026, 7, 15))
        self.assertEqual(services._parse_date("15-07-2026"), date(2026, 7, 15))
        self.assertIsNone(services._parse_date(""))
        self.assertIsNone(services._parse_date("not-a-date"))

    def test_parse_decimal(self):
        self.assertEqual(services._parse_decimal("9950.00"), Decimal("9950.00"))
        self.assertEqual(services._parse_decimal("1.234.567,89"), Decimal("1234567.89"))
        self.assertEqual(services._parse_decimal("1234,5"), Decimal("1234.5"))
        self.assertIsNone(services._parse_decimal(""))
        self.assertIsNone(services._parse_decimal("abc"))

    def test_parse_int(self):
        self.assertEqual(services._parse_int(13), 13)
        self.assertEqual(services._parse_int("13"), 13)
        self.assertEqual(services._parse_int("13.0"), 13)
        self.assertIsNone(services._parse_int(""))
        self.assertIsNone(services._parse_int("x"))

    def test_parse_bool(self):
        self.assertTrue(services._parse_bool("Sim"))
        self.assertTrue(services._parse_bool("S"))
        self.assertTrue(services._parse_bool(True))
        self.assertFalse(services._parse_bool("Não"))
        self.assertFalse(services._parse_bool(""))

    def test_parse_list(self):
        self.assertEqual(services._parse_list(["a", "b"]), ["a", "b"])
        self.assertEqual(services._parse_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(services._parse_list(None), [])


class MapNoticeTests(SimpleTestCase):
    def test_map_real_payload(self):
        raw = {
            "nAnuncio": "16771/2026", "IdIncm": "419965576", "dataPublicacao": "30/06/2026",
            "nifEntidade": "506362299", "designacaoEntidade": "IPO Porto",
            "descricaoAnuncio": "Serviços de manutenção", "url": "http://dr/x.pdf",
            "numDR": "124", "serie": "2", "tipoActo": "Anúncio de procedimento",
            "tiposContrato": ["Aquisição de serviços"], "PrecoBase": "9950.00",
            "CPVs": ["72267000-4"], "modeloAnuncio": "Concurso público", "Ano": 2026,
            "CriterAmbient": "Não", "PrazoPropostas": 13,
            "PecasProcedimento": "http://vortal/x", "DataLimitePropostas": "13/07/2026",
            "Lotes": None,
        }
        data = services._map_notice(raw)
        self.assertEqual(data["notice_number"], "16771/2026")
        self.assertEqual(data["incm_id"], "419965576")
        self.assertEqual(data["publication_date"], date(2026, 6, 30))
        self.assertEqual(data["proposal_deadline"], date(2026, 7, 13))
        self.assertEqual(data["base_price"], Decimal("9950.00"))
        self.assertFalse(data["environmental_criteria"])
        self.assertEqual(data["proposal_period_days"], 13)
        self.assertEqual(data["contract_types"], ["Aquisição de serviços"])
        self.assertEqual(data["lots"], [])

    def test_map_truncates_and_defaults(self):
        data = services._map_notice({"nifEntidade": "1234567890123"})
        self.assertEqual(data["entity_nif"], "123456789")  # 9 chars max
        self.assertEqual(data["notice_number"], "")


# --- Specifications name matching -----------------------------------------

class SpecificationsMatchTests(SimpleTestCase):
    def test_strong_matches(self):
        for name in ("Caderno de Encargos.pdf", "214CadernoEncargos.pdf",
                     "Caderno_de_Encargos.docx", "CAD_ENC_123.pdf",
                     "5 - CE - CPI + Anexo C.pdf"):
            self.assertTrue(specifications.is_specifications(name), name)

    def test_ce_token(self):
        self.assertTrue(specifications.is_specifications("Anexo V do CE.pdf"))
        self.assertFalse(specifications.is_specifications_strong("Anexo V do CE.pdf"))

    def test_non_matches(self):
        for name in ("Programa do Concurso.pdf", "cadencia_musical.pdf",
                     "Anuncio.pdf", "Anexo III.xlsx"):
            self.assertFalse(specifications.is_specifications(name), name)


class ProgramMatchTests(SimpleTestCase):
    def test_strong_matches(self):
        for name in ("Programa de Concurso.pdf", "Programa Concurso.pdf",
                     "Programa do Procedimento.pdf", "Programa de Procedimento.docx"):
            self.assertTrue(specifications.is_program(name), name)
            self.assertTrue(specifications.is_program_strong(name), name)

    def test_pc_pp_token(self):
        for name in ("PC.pdf", "PP.pdf", "Anexo - PC.pdf"):
            self.assertTrue(specifications.is_program(name), name)
            self.assertFalse(specifications.is_program_strong(name), name)

    def test_non_matches(self):
        # "Programa.pdf" sozinho NÃO é programa de concurso (é apanhado só pelo fallback "junto
        # do CE" no pairing); caderno de encargos e anexos genéricos também não.
        for name in ("Programa.pdf", "Caderno de Encargos.pdf", "Anexo III.pdf", ""):
            self.assertFalse(specifications.is_program(name), name)


class PairDocumentsInZipTests(SimpleTestCase):
    def _zip(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    def _pair(self, files):
        # _save_bytes é mockado para devolver só o nome (não escreve em disco).
        with mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": f"{subdir}/{name}" if subdir else name):
            return specifications._pair_documents_in_zip(self._zip(files))

    def test_both_named(self):
        # Caderno de encargos e programa vão cada um para a sua subpasta própria.
        out = self._pair({"Caderno de Encargos.pdf": b"%PDF a",
                          "Programa de Concurso.pdf": b"%PDF b"})
        self.assertEqual(out, {"specifications": "caderno_encargos/Caderno de Encargos.pdf",
                               "program": "programa_concurso/Programa de Concurso.pdf"})

    def test_program_falls_back_to_other_pdf_next_to_ce(self):
        # Programa sem nome identificável -> o outro PDF junto do caderno de encargos.
        out = self._pair({"Caderno de Encargos.pdf": b"%PDF a", "Documento.pdf": b"%PDF b"})
        self.assertEqual(out["specifications"], "caderno_encargos/Caderno de Encargos.pdf")
        self.assertEqual(out["program"], "programa_concurso/Documento.pdf")

    def test_only_ce_no_program(self):
        out = self._pair({"Caderno de Encargos.pdf": b"%PDF a"})
        self.assertEqual(out, {"specifications": "caderno_encargos/Caderno de Encargos.pdf", "program": ""})

    def test_only_program_named(self):
        out = self._pair({"Programa de Concurso.pdf": b"%PDF a"})
        self.assertEqual(out, {"specifications": "",
                               "program": "programa_concurso/Programa de Concurso.pdf"})

    def test_no_pdf(self):
        self.assertEqual(self._pair({"notas.txt": b"x"}),
                         {"specifications": "", "program": ""})


class FetchDocumentsTests(SimpleTestCase):
    def test_direct_pdf_ce(self):
        resp = FakeResp(b"%PDF-1.7", ctype="application/pdf", url="http://x/ce.pdf",
                        cd='attachment; filename="Caderno de Encargos.pdf"')
        with mock.patch("anuncios.specifications._get", return_value=resp), \
             mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": f"pdf_Anuncios/{subdir + chr(47) if subdir else ''}{name}"):
            out = specifications.fetch_documents("http://x/ce.pdf")
        self.assertEqual(out, {"specifications": "pdf_Anuncios/caderno_encargos/Caderno de Encargos.pdf", "program": ""})

    def test_direct_pdf_program(self):
        resp = FakeResp(b"%PDF-1.7", ctype="application/pdf", url="http://x/pc.pdf",
                        cd='attachment; filename="Programa de Concurso.pdf"')
        with mock.patch("anuncios.specifications._get", return_value=resp), \
             mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": f"pdf_Anuncios/{subdir + chr(47) if subdir else ''}{name}"):
            out = specifications.fetch_documents("http://x/pc.pdf")
        self.assertEqual(out, {"specifications": "",
                               "program": "pdf_Anuncios/programa_concurso/Programa de Concurso.pdf"})

    def test_empty_url_and_get_failure(self):
        self.assertEqual(specifications.fetch_documents(""), {"specifications": "", "program": ""})
        with mock.patch("anuncios.specifications._get", return_value=None):
            self.assertEqual(specifications.fetch_documents("http://x"),
                             {"specifications": "", "program": ""})


# --- Finders (HTML / ZIP) --------------------------------------------------

class FindInHtmlTests(SimpleTestCase):
    def test_finds_ce_link_in_table_row(self):
        html = (
            b"<table><tr>"
            b"<td>Caderno de Encargos.pdf</td>"
            b'<td><a href="http://x/dl?token=1"><svg></svg></a></td>'
            b"</tr></table>"
        )

        def fake_get(url, cookies=None, referer=None):
            return FakeResp(b"%PDF-1.7", ctype="application/pdf", url=url,
                            cd='attachment; filename="Caderno de Encargos.pdf"')

        with mock.patch("anuncios.specifications._get", side_effect=fake_get), \
             mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": f"pdf_Anuncios/{subdir + chr(47) if subdir else ''}{name}"):
            path = specifications._find_in_html(html, "http://x/pecas")
        self.assertEqual(path, "pdf_Anuncios/Caderno de Encargos.pdf")

    def test_ignores_rows_without_ce(self):
        html = b"<table><tr><td>Programa.pdf</td><td><a href='http://x/1'>x</a></td></tr></table>"
        with mock.patch("anuncios.specifications._get") as g:
            path = specifications._find_in_html(html, "http://x/p")
        self.assertEqual(path, "")
        g.assert_not_called()


class FindInZipTests(SimpleTestCase):
    def _zip(self, files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    def test_prefers_ce_pdf(self):
        data = self._zip({"Programa.pdf": b"%PDF a", "Caderno de Encargos.pdf": b"%PDF b"})
        with mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": (name, content)):
            name, content = specifications._find_in_zip(data)
        self.assertEqual(name, "Caderno de Encargos.pdf")
        self.assertEqual(content, b"%PDF b")

    def test_fallback_first_pdf(self):
        data = self._zip({"Programa.pdf": b"%PDF a", "Notes.txt": b"x"})
        with mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content, subdir="": f"{subdir}/{name}" if subdir else name):
            self.assertEqual(specifications._find_in_zip(data), "Programa.pdf")

    def test_no_pdf(self):
        data = self._zip({"a.txt": b"x"})
        self.assertEqual(specifications._find_in_zip(data), "")


# --- Persistence / upsert --------------------------------------------------

def _notice_data(**over):
    base = {
        "notice_number": "1/2026", "entity_name": "Entity", "description": "d",
        "procedure_documents_url": "http://x", "specifications_path": "",
    }
    base.update(over)
    return base


class UpsertNoticeTests(TestCase):
    def test_create_then_unchanged(self):
        status, obj = services._upsert_notice(_notice_data())
        self.assertEqual(status, "created")
        self.assertEqual(obj.notice_number, "1/2026")
        status, _ = services._upsert_notice(_notice_data())
        self.assertEqual(status, "unchanged")
        self.assertEqual(Notice.objects.count(), 1)

    def test_update_changed_field(self):
        services._upsert_notice(_notice_data(entity_name="Old"))
        status, obj = services._upsert_notice(_notice_data(entity_name="New"))
        self.assertEqual(status, "updated")
        self.assertEqual(obj.entity_name, "New")
        self.assertEqual(Notice.objects.get(notice_number="1/2026").entity_name, "New")

    def test_empty_does_not_overwrite(self):
        services._upsert_notice(_notice_data(entity_name="Original"))
        status, _ = services._upsert_notice(_notice_data(entity_name=""))
        self.assertEqual(status, "unchanged")
        self.assertEqual(Notice.objects.get(notice_number="1/2026").entity_name, "Original")

    def test_created_notice_has_scrape_source(self):
        services._upsert_notice(_notice_data())
        notice = Notice.objects.get(notice_number="1/2026")
        self.assertEqual(notice.last_update_source, Notice.SOURCE_SCRAPE)
        self.assertIsNone(notice.last_updated_by)

    def test_updated_notice_reverts_to_scrape_source_even_if_manually_edited(self):
        # A importação re-processa um anúncio que um humano tinha editado antes — a última
        # escrita passa a ser a importação, por isso a origem reflete-a (não o histórico).
        services._upsert_notice(_notice_data(entity_name="Old"))
        editor = User.objects.create_user("editor_upsert", password=TEST_PASSWORD)
        notice = Notice.objects.get(notice_number="1/2026")
        notice.last_update_source = Notice.SOURCE_MANUAL
        notice.last_updated_by = editor
        notice.save()

        services._upsert_notice(_notice_data(entity_name="New"))
        notice.refresh_from_db()
        self.assertEqual(notice.last_update_source, Notice.SOURCE_SCRAPE)
        self.assertIsNone(notice.last_updated_by)

    def test_existing_specifications_path_missing_file(self):
        Notice.objects.create(notice_number="9/2026", specifications_path="pdf_Anuncios/x.pdf")
        self.assertEqual(services.existing_specifications_path("9/2026"), "")  # file doesn't exist
        self.assertEqual(services.existing_specifications_path(""), "")


# --- Filtering / serialization / expiry -----------------------------------

class FilterNoticeTests(TestCase):
    def setUp(self):
        self.today = date.today()
        Notice.objects.create(notice_number="A", proposal_deadline=self.today + timedelta(days=5),
                              act_type="Anúncio de procedimento", base_price=Decimal("100"),
                              contract_types=["Aquisição de serviços"],
                              status=Notice.StatusChoices.ACTIVE)
        Notice.objects.create(notice_number="B", proposal_deadline=self.today - timedelta(days=1),
                              status=Notice.StatusChoices.INACTIVE)
        Notice.objects.create(notice_number="C", proposal_deadline=None, base_price=Decimal("300"),
                              status=Notice.StatusChoices.TO_FIX)

    def test_excludes_inactive_keeps_to_fix(self):
        nums = {n.notice_number for n in services.filter_notices({})}
        self.assertEqual(nums, {"A", "C"})

    def test_filter_status_active(self):
        nums = {n.notice_number for n in services.filter_notices({"status": "active"})}
        self.assertEqual(nums, {"A"})

    def test_filter_status_inactive(self):
        nums = {n.notice_number for n in services.filter_notices({"status": "inactive"})}
        self.assertEqual(nums, {"B"})

    def test_filter_status_to_fix(self):
        nums = {n.notice_number for n in services.filter_notices({"status": "to_fix"})}
        self.assertEqual(nums, {"C"})

    def test_filter_act_type(self):
        nums = [n.notice_number for n in services.filter_notices({"act_type": "Anúncio de procedimento"})]
        self.assertEqual(nums, ["A"])

    def test_filter_contract_type(self):
        nums = [n.notice_number for n in services.filter_notices({"contract_type": "Aquisição de serviços"})]
        self.assertEqual(nums, ["A"])

    def test_order_price_highest(self):
        nums = [n.notice_number for n in services.filter_notices({"order_by": "price_highest"})]
        self.assertEqual(nums[0], "C")  # 300 before 100 / null


class SerializeNoticeTests(TestCase):
    def test_serialize(self):
        n = Notice.objects.create(notice_number="1/2026", base_price=Decimal("100.50"),
                                  proposal_deadline=date(2026, 7, 13),
                                  status=Notice.StatusChoices.ACTIVE)
        out = services.serialize_notice(n)
        self.assertEqual(out["notice_number"], "1/2026")
        self.assertEqual(out["base_price"], 100.5)
        self.assertEqual(out["proposal_deadline"], "2026-07-13")
        self.assertEqual(out["status"], "active")
        self.assertIn("specifications_path", out)

    def test_serialize_includes_pending_ai_detail_by_default(self):
        n = Notice.objects.create(notice_number="AID-1")
        out = services.serialize_notice(n)
        self.assertEqual(out["ai_detail"], {
            "status": "pending", "descricao_detalhada": "",
            "avaliacao": "", "observacoes": [],
        })

    def test_serialize_includes_generated_ai_detail_once_done(self):
        n = Notice.objects.create(
            notice_number="AID-2",
            specifications_ai_status=Notice.AiStatusChoices.DONE,
            specifications_description="Descrição gerada.",
            specifications_evaluation="Monofator – Preço mais baixo (100%)",
            specifications_observations=["Visita ao local obrigatória."],
        )
        out = services.serialize_notice(n)
        self.assertEqual(out["ai_detail"]["status"], "done")
        self.assertEqual(out["ai_detail"]["descricao_detalhada"], "Descrição gerada.")
        self.assertEqual(out["ai_detail"]["avaliacao"], "Monofator – Preço mais baixo (100%)")
        self.assertEqual(out["ai_detail"]["observacoes"], ["Visita ao local obrigatória."])


class DeactivateExpiredTests(TestCase):
    def test_deactivate(self):
        Notice.objects.create(notice_number="X", status=Notice.StatusChoices.ACTIVE,
                              proposal_deadline=date.today() - timedelta(days=1))
        Notice.objects.create(notice_number="Y", status=Notice.StatusChoices.ACTIVE,
                              proposal_deadline=date.today() + timedelta(days=1))
        self.assertEqual(services.deactivate_expired(), 1)
        self.assertEqual(Notice.objects.get(notice_number="X").status, Notice.StatusChoices.INACTIVE)
        self.assertEqual(Notice.objects.get(notice_number="Y").status, Notice.StatusChoices.ACTIVE)


# --- Import / backfill -----------------------------------------------------

class ImportNoticesTests(TestCase):
    def test_register_only_no_specs(self):
        raw = [{"nAnuncio": "9/2026", "descricaoAnuncio": "Serviços de consultoria",
                "pecasProcedimento": "http://x", "DataLimitePropostas": "31/12/2026",
                "PrecoBase": "9950.00"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["with_keywords"], 1)
        self.assertEqual(Notice.objects.get(notice_number="9/2026").specifications_path, "")
        self.assertEqual(Notice.objects.get(notice_number="9/2026").status, Notice.StatusChoices.ACTIVE)

    def test_missing_deadline_sets_to_fix(self):
        raw = [{"nAnuncio": "9b/2026", "descricaoAnuncio": "Serviços de consultoria"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            services.import_notices(1, download_specs=False)
        self.assertEqual(Notice.objects.get(notice_number="9b/2026").status, Notice.StatusChoices.TO_FIX)

    def test_missing_price_with_open_deadline_sets_to_fix(self):
        # Prazo válido/futuro mas SEM preço -> ainda vale a pena corrigir (continua relevante).
        raw = [{"nAnuncio": "9c/2026", "descricaoAnuncio": "Serviços de consultoria",
                "DataLimitePropostas": "31/12/2026"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            services.import_notices(1, download_specs=False)
        self.assertEqual(Notice.objects.get(notice_number="9c/2026").status, Notice.StatusChoices.TO_FIX)

    def test_missing_price_with_expired_deadline_stays_inactive(self):
        # Prazo já passado -> inativo (encerrado); a falta de preço não o "promove" a corrigir,
        # não vale a pena pedir correção de algo que já fechou.
        raw = [{"nAnuncio": "9d/2026", "descricaoAnuncio": "Serviços de consultoria",
                "DataLimitePropostas": "01/01/2020"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            services.import_notices(1, download_specs=False)
        self.assertEqual(Notice.objects.get(notice_number="9d/2026").status, Notice.StatusChoices.INACTIVE)

    def test_keyword_filter_skips(self):
        raw = [{"nAnuncio": "10/2026", "descricaoAnuncio": "Fornecimento de material de limpeza"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False)
        self.assertEqual(summary["with_keywords"], 0)
        self.assertEqual(Notice.objects.count(), 0)

    def test_download_docs_sets_both_paths(self):
        # O import guarda AMBOS os documentos (caderno de encargos + programa de concurso),
        # obtidos numa única passagem por fetch_documents.
        raw = [{"nAnuncio": "11/2026", "descricaoAnuncio": "Consultoria", "pecasProcedimento": "http://x"}]
        docs = {"specifications": "pdf_Anuncios/ce.pdf", "program": "pdf_Anuncios/pc.pdf"}
        with mock.patch("anuncios.services.fetch_notices", return_value=raw), \
             mock.patch("anuncios.services.fetch_documents", return_value=docs) as fd, \
             mock.patch("anuncios.services.connection.close"):
            services.import_notices(1, download_specs=True)
        notice = Notice.objects.get(notice_number="11/2026")
        self.assertEqual(notice.specifications_path, "pdf_Anuncios/ce.pdf")
        self.assertEqual(notice.program_path, "pdf_Anuncios/pc.pdf")
        fd.assert_called_once()

    def test_should_stop_cancels(self):
        raw = [{"nAnuncio": "12/2026", "descricaoAnuncio": "Serviços"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False, should_stop=lambda: True)
        self.assertEqual(summary["with_keywords"], 0)
        self.assertEqual(Notice.objects.count(), 0)


class BackfillTests(TestCase):
    def test_backfill_downloads_missing(self):
        Notice.objects.create(notice_number="13/2026", procedure_documents_url="http://x",
                              specifications_path="", program_path="")
        Notice.objects.create(notice_number="14/2026", procedure_documents_url="",
                              specifications_path="", program_path="")  # no docs link -> skipped
        docs = {"specifications": "pdf_Anuncios/ce.pdf", "program": "pdf_Anuncios/pc.pdf"}
        with mock.patch("anuncios.services.fetch_documents", return_value=docs), \
             mock.patch("anuncios.services.connection.close"), \
             mock.patch("anuncios.services.mark_import_start"), \
             mock.patch("anuncios.services.mark_import_end"), \
             mock.patch("anuncios.services._heartbeat_lock"):
            summary = services.download_missing_specifications()
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["downloaded"], 1)
        notice = Notice.objects.get(notice_number="13/2026")
        self.assertEqual(notice.specifications_path, "pdf_Anuncios/ce.pdf")
        self.assertEqual(notice.program_path, "pdf_Anuncios/pc.pdf")

    def test_backfill_only_fills_missing_program(self):
        # Já tem caderno de encargos, só falta o programa — não sobrescreve o CE existente.
        Notice.objects.create(notice_number="15/2026", procedure_documents_url="http://x",
                              specifications_path="pdf_Anuncios/existing_ce.pdf", program_path="")
        docs = {"specifications": "pdf_Anuncios/other_ce.pdf", "program": "pdf_Anuncios/pc.pdf"}
        with mock.patch("anuncios.services.fetch_documents", return_value=docs), \
             mock.patch("anuncios.services.connection.close"), \
             mock.patch("anuncios.services.mark_import_start"), \
             mock.patch("anuncios.services.mark_import_end"), \
             mock.patch("anuncios.services._heartbeat_lock"):
            services.download_missing_specifications()
        notice = Notice.objects.get(notice_number="15/2026")
        self.assertEqual(notice.specifications_path, "pdf_Anuncios/existing_ce.pdf")  # intacto
        self.assertEqual(notice.program_path, "pdf_Anuncios/pc.pdf")                  # preenchido


# --- Lock ownership --------------------------------------------------------

class LockTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lock = Path(self.tmp) / "lock"
        self.patcher = mock.patch("anuncios.services._lock_path", return_value=self.lock)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_and_running(self):
        self.assertFalse(services.import_running())
        services.mark_import_start()
        self.assertTrue(self.lock.exists())
        self.assertTrue(services.import_running())

    def test_spawn_refuses_while_an_extraction_is_running(self):
        # Uma extração demora dezenas de minutos — um 2º pedido não a pode deitar fora a
        # meio. Sem esta guarda, o spawn matava-a e relançava do zero.
        services.mark_import_start()
        with mock.patch("anuncios.services.subprocess.Popen") as popen:
            self.assertFalse(services.spawn_specifications_download())
        popen.assert_not_called()
        self.assertTrue(self.lock.exists())  # a extração em curso ficou intacta

    def test_spawn_launches_when_nothing_is_running(self):
        pid_file = Path(self.tmp) / "pid"
        with mock.patch("anuncios.services._pid_path", return_value=pid_file), \
             mock.patch("anuncios.services.subprocess.Popen") as popen:
            popen.return_value.pid = 4242
            self.assertTrue(services.spawn_specifications_download())
        popen.assert_called_once()
        self.assertEqual(pid_file.read_text(encoding="utf-8"), "4242")

    def test_spawn_clears_stale_files_without_killing_the_pid(self):
        # Sem extração viva, o PID guardado já não é nosso — o SO pode tê-lo reutilizado.
        # Os ficheiros são limpos, mas NUNCA se mata o processo que lá está.
        pid_file = Path(self.tmp) / "pid"
        pid_file.write_text("999999", encoding="utf-8")
        self.lock.write_text("999999", encoding="utf-8")
        os.utime(self.lock, (0, 0))  # lock velho -> import_running() dá False
        with mock.patch("anuncios.services._pid_path", return_value=pid_file), \
             mock.patch("anuncios.services.subprocess.Popen") as popen:
            popen.return_value.pid = 7
            self.assertTrue(services.spawn_specifications_download())
        self.assertEqual(pid_file.read_text(encoding="utf-8"), "7")  # substituído, não morto

    def test_end_only_removes_own_lock(self):
        self.lock.write_text("999999", encoding="utf-8")  # foreign owner
        services.mark_import_end()
        self.assertTrue(self.lock.exists())  # not removed
        services.mark_import_start()  # our PID
        services.mark_import_end()
        self.assertFalse(self.lock.exists())


# --- Views -----------------------------------------------------------------

class ViewTests(TestCase):
    def setUp(self):
        # Listagem/detalhe/filtros exigem sessão (anúncios não fazem parte do match) — a
        # importação (POST /anuncios/) fica de fora, é automação sem login.
        user = User.objects.create_user("cliente_view_an", password=TEST_PASSWORD)
        self.client.force_login(user)

    def test_import_route_is_post_only(self):
        resp = self.client.get("/anuncios/")
        self.assertEqual(resp.status_code, 405)  # GET not allowed (side-effecting)

    def test_import_route_post_registers_and_spawns(self):
        with mock.patch("anuncios.services.import_notices",
                        return_value={"created": 1, "with_keywords": 1}) as imp, \
             mock.patch("anuncios.services.spawn_specifications_download") as spawn:
            resp = self.client.post("/anuncios/")
        self.assertEqual(resp.status_code, 200)
        imp.assert_called_once_with(15, download_specs=False)
        spawn.assert_called_once()
        self.assertIn("specifications", resp.json())

    def test_import_route_accepts_num_days_query_param(self):
        with mock.patch("anuncios.services.import_notices",
                        return_value={"created": 0, "with_keywords": 0}) as imp, \
             mock.patch("anuncios.services.spawn_specifications_download"):
            resp = self.client.post("/anuncios/?num_days=30")
        self.assertEqual(resp.status_code, 200)
        imp.assert_called_once_with(30, download_specs=False)

    def test_import_route_invalid_num_days_returns_400(self):
        resp = self.client.post("/anuncios/?num_days=abc")
        self.assertEqual(resp.status_code, 400)

    def test_import_route_api_error(self):
        with mock.patch("anuncios.services.import_notices",
                        side_effect=services.BaseGovError("no key")), \
             mock.patch("anuncios.services.spawn_specifications_download"):
            resp = self.client.post("/anuncios/")
        self.assertEqual(resp.status_code, 502)

    def test_list_route(self):
        Notice.objects.create(notice_number="1/2026",
                              proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/list/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["notices"][0]["notice_number"], "1/2026")

    def test_filters_returns_only_values_present_and_browsable(self):
        # Só os valores REALMENTE presentes entre os anúncios navegáveis (não inativos) — sem
        # duplicados, sem vazios, ordenados. Um anúncio inativo (expirado) não contribui valores.
        Notice.objects.create(
            notice_number="F1/2026", act_type=Notice.ActTypeChoices.PROCEDURE,
            contract_types=["Aquisição de serviços", "Locação"],
            proposal_deadline=date.today() + timedelta(days=3),
            status=Notice.StatusChoices.ACTIVE)
        Notice.objects.create(
            notice_number="F2/2026", act_type=Notice.ActTypeChoices.URGENT,
            contract_types=["Aquisição de serviços"],
            proposal_deadline=date.today() + timedelta(days=10),
            status=Notice.StatusChoices.ACTIVE)
        Notice.objects.create(
            notice_number="F3/2026", act_type=Notice.ActTypeChoices.AMENDMENT,
            contract_types=["Empreitada"],
            proposal_deadline=date.today() - timedelta(days=1),
            status=Notice.StatusChoices.INACTIVE)  # inativo — fora

        resp = self.client.get("/anuncios/filters/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(
            body["act_types"],
            sorted([Notice.ActTypeChoices.PROCEDURE, Notice.ActTypeChoices.URGENT]),
        )
        self.assertEqual(body["contract_types"], ["Aquisição de serviços", "Locação"])

    def test_filters_empty_when_no_browsable_notices(self):
        resp = self.client.get("/anuncios/filters/")
        body = resp.json()
        self.assertEqual(body["act_types"], [])
        self.assertEqual(body["contract_types"], [])
        self.assertEqual(
            body["statuses"],
            [{"value": v, "label": label} for v, label in Notice.StatusChoices.choices],
        )

    def test_list_is_summary_only(self):
        # A listagem é enxuta — a descrição completa só vem no detalhe.
        Notice.objects.create(notice_number="2/2026", description="texto longo",
                              proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/list/")
        body = resp.json()
        self.assertEqual(set(body), {"total", "page", "page_size", "num_pages", "notices"})
        row = body["notices"][0]
        self.assertEqual(set(row), {"id", "notice_number", "description", "entity_name",
                                    "act_type", "contract_types", "base_price",
                                    "proposal_deadline", "status"})

    def test_ordering_is_global_not_per_page(self):
        # O anúncio de maior preço foi criado PRIMEIRO (id mais baixo) — se a paginação
        # cortasse antes de ordenar, não apareceria em 1º ao pedir price_highest. A
        # ordenação é feita no SQL (ORDER BY) ANTES do LIMIT/OFFSET da paginação — global.
        Notice.objects.create(notice_number="LOW/2026", base_price=Decimal("100"),
                              proposal_deadline=date.today() + timedelta(days=3))
        Notice.objects.create(notice_number="HIGH/2026", base_price=Decimal("999999"),
                              proposal_deadline=date.today() + timedelta(days=3))
        Notice.objects.create(notice_number="MID/2026", base_price=Decimal("5000"),
                              proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/list/?order_by=price_highest&page_size=2")
        numbers = [n["notice_number"] for n in resp.json()["notices"]]
        self.assertEqual(numbers, ["HIGH/2026", "MID/2026"])

    def test_search_is_global_not_per_page(self):
        # O anúncio que bate na pesquisa é o ÚLTIMO a ser criado (id mais alto) — se a pesquisa
        # só visse a página atual (em vez de filtrar TODOS os anúncios no SQL antes de paginar),
        # um page_size pequeno cortava-o fora antes de chegar à pesquisa.
        for i in range(5):
            Notice.objects.create(notice_number=f"N{i}/2026", description="outro assunto",
                                  proposal_deadline=date.today() + timedelta(days=3))
        Notice.objects.create(notice_number="ALVO/2026", description="requalificação urbana",
                              proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/list/?q=requalificação&page_size=2")
        body = resp.json()
        self.assertEqual(body["total"], 1)   # filtrado no SQL: só 1 resultado no total, não 6
        self.assertEqual([n["notice_number"] for n in body["notices"]], ["ALVO/2026"])

    def test_search_matches_entity_name_and_notice_number(self):
        Notice.objects.create(notice_number="9/2026", entity_name="Câmara Municipal de Loulé",
                              proposal_deadline=date.today() + timedelta(days=3))
        Notice.objects.create(notice_number="OUTRO/2026", entity_name="Outra Entidade",
                              proposal_deadline=date.today() + timedelta(days=3))
        self.assertEqual(self.client.get("/anuncios/list/?q=Loulé").json()["total"], 1)
        self.assertEqual(self.client.get("/anuncios/list/?q=9/2026").json()["total"], 1)
        self.assertEqual(self.client.get("/anuncios/list/?q=inexistente").json()["total"], 0)

    def test_list_pagination(self):
        for i in range(5):
            Notice.objects.create(notice_number=f"P{i}/2026",
                                  proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/list/?page=2&page_size=2")
        body = resp.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["page_size"], 2)
        self.assertEqual(body["num_pages"], 3)
        self.assertEqual(len(body["notices"]), 2)

    def test_detail_route(self):
        n = Notice.objects.create(notice_number="3/2026", description="descrição completa",
                                  proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get(f"/anuncios/{n.pk}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["description"], "descrição completa")  # detalhe traz tudo

    def test_detail_404(self):
        self.assertEqual(self.client.get("/anuncios/999999/").status_code, 404)

    def test_list_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get("/anuncios/list/").status_code, 401)

    def test_filters_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.get("/anuncios/filters/").status_code, 401)

    def test_detail_requires_authentication(self):
        self.client.logout()
        n = Notice.objects.create(notice_number="AUTH/2026",
                                  proposal_deadline=date.today() + timedelta(days=3))
        self.assertEqual(self.client.get(f"/anuncios/{n.pk}/").status_code, 401)


class NoticeEditViewTests(TestCase):
    """Permissões, validação e auditoria da edição de anúncios (/anuncios/<pk>/edit/)."""

    def setUp(self):
        self.notice = Notice.objects.create(
            notice_number="ED-1/2026", entity_name="Câmara Municipal X",
            description="Original", base_price=Decimal("1000"),
            proposal_deadline=date.today() + timedelta(days=5),
        )
        self.commercial = User.objects.create_user(
            "comercial_an", email="c@x.pt", password=TEST_PASSWORD)
        self.commercial.profile.role = UserProfile.COMMERCIAL_PUBLIC
        self.commercial.profile.save()
        self.client_user = User.objects.create_user("cliente_an", password=TEST_PASSWORD)
        # o signal já cria o perfil com role=client

    def _edit(self, payload):
        # Edição é por PUT (por id, inalterável) — POST não é aceite.
        return self.client.put(
            f"/anuncios/{self.notice.pk}/edit/",
            data=json.dumps(payload), content_type="application/json",
        )

    def test_anonymous_gets_401(self):
        self.assertEqual(self._edit({"description": "X"}).status_code, 401)

    def test_client_role_gets_403(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self._edit({"description": "X"}).status_code, 403)

    def test_commercial_grants_role_gets_403(self):
        # commercial_grants não tem acesso nenhum a anúncios (só commercial_public tem).
        commercial_grants = User.objects.create_user(
            "comercial_gr1", email="cg1@x.pt", password=TEST_PASSWORD)
        commercial_grants.profile.role = UserProfile.COMMERCIAL_GRANTS
        commercial_grants.profile.save()
        self.client.force_login(commercial_grants)
        self.assertEqual(self._edit({"description": "X"}).status_code, 403)

    def test_commercial_edits_and_audit_logs_who_and_what(self):
        self.client.force_login(self.commercial)
        with self.assertLogs("anuncios.audit", level="INFO") as logs:
            resp = self._edit({"description": "Novo texto", "id": 999})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["updated"], ["description"])
        self.assertEqual(body["ignored"], ["id"])   # id nunca é editável
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.description, "Novo texto")
        # O log de auditoria diz QUEM alterou e O QUÊ (antigo -> novo).
        self.assertIn("comercial_an", logs.output[0])
        self.assertIn("'Original'", logs.output[0])
        self.assertIn("'Novo texto'", logs.output[0])

    def test_edit_multiple_fields(self):
        self.client.force_login(self.commercial)
        resp = self._edit({"entity_name": "Câmara Y", "status": "inactive"})
        self.assertEqual(resp.status_code, 200)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.entity_name, "Câmara Y")
        self.assertEqual(self.notice.status, Notice.StatusChoices.INACTIVE)

    def test_invalid_value_returns_400_not_500(self):
        self.client.force_login(self.commercial)
        resp = self._edit({"base_price": "não é um número"})
        self.assertEqual(resp.status_code, 400)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.base_price, Decimal("1000"))  # nada foi gravado

    def test_manual_edit_marks_source_and_user(self):
        self.client.force_login(self.commercial)
        self._edit({"description": "Editado à mão"})
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.last_update_source, Notice.SOURCE_MANUAL)
        self.assertEqual(self.notice.last_updated_by, self.commercial)

    def test_last_update_source_and_user_are_not_client_editable(self):
        self.client.force_login(self.commercial)
        other = User.objects.create_user("outro_admin_an", password=TEST_PASSWORD)
        resp = self._edit({
            "description": "X", "last_update_source": Notice.SOURCE_SCRAPE,
            "last_updated_by": other.pk,
        })
        body = resp.json()
        self.assertEqual(set(body["ignored"]), {"last_update_source", "last_updated_by"})
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.last_update_source, Notice.SOURCE_MANUAL)
        self.assertEqual(self.notice.last_updated_by, self.commercial)

    def test_detail_exposes_last_updated_by_as_username(self):
        self.client.force_login(self.commercial)
        self._edit({"description": "X"})
        resp = self.client.get(f"/anuncios/{self.notice.pk}/")
        self.assertEqual(resp.json()["last_updated_by"], "comercial_an")
        self.assertEqual(resp.json()["last_update_source"], Notice.SOURCE_MANUAL)

    def test_duplicate_notice_number_returns_400(self):
        # notice_number é único — a colisão vira 400 (não 500).
        Notice.objects.create(notice_number="OUTRO/2026")
        self.client.force_login(self.commercial)
        resp = self._edit({"notice_number": "OUTRO/2026"})
        self.assertEqual(resp.status_code, 400)

    def test_post_method_not_allowed(self):
        self.client.force_login(self.commercial)
        resp = self.client.post(
            f"/anuncios/{self.notice.pk}/edit/",
            data=json.dumps({"description": "X"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 405)

    def test_unknown_notice_returns_404(self):
        self.client.force_login(self.commercial)
        resp = self.client.put(
            "/anuncios/999999/edit/", data="{}", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_invalid_json_returns_400(self):
        self.client.force_login(self.commercial)
        resp = self.client.put(
            f"/anuncios/{self.notice.pk}/edit/",
            data="isto não é json", content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class SpecificationsServeTests(TestCase):
    """Servir o caderno de encargos local (pdf_Anuncios) e o link no detalhe. BASE_DIR
    temporário (override_settings) para não escrever no repositório."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdf_Anuncios" / "caderno_encargos").mkdir(parents=True)
        self.rel = "pdf_Anuncios/caderno_encargos/20260101_ce.pdf"
        (self.tmp / self.rel).write_bytes(b"%PDF-1.4\ncaderno\n%%EOF")
        self.notice = Notice.objects.create(
            notice_number="CE-1", specifications_path=self.rel,
            proposal_deadline=date.today() + timedelta(days=5),
        )
        user = User.objects.create_user("cliente_ce_an", password=TEST_PASSWORD)
        self.client.force_login(user)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detail_exposes_specifications_url(self):
        with override_settings(BASE_DIR=self.tmp):
            body = self.client.get(f"/anuncios/{self.notice.pk}/").json()
        self.assertEqual(body["specifications_url"], f"/anuncios/{self.notice.pk}/document/cadernoEncargos/")

    def test_serve_returns_pdf_inline(self):
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{self.notice.pk}/document/cadernoEncargos/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4\ncaderno\n%%EOF")

    def test_serve_404_when_no_file(self):
        n = Notice.objects.create(notice_number="NOCE", specifications_path="",
                                  proposal_deadline=date.today() + timedelta(days=5))
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{n.pk}/document/cadernoEncargos/")
        self.assertEqual(resp.status_code, 404)

    def test_serve_blocks_path_traversal(self):
        (self.tmp / "secret.pdf").write_bytes(b"%PDF-1.4\nsegredo")
        self.notice.specifications_path = "pdf_Anuncios/../secret.pdf"
        self.notice.save(update_fields=["specifications_path"])
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{self.notice.pk}/document/cadernoEncargos/")
        self.assertEqual(resp.status_code, 404)


class ProgramServeTests(TestCase):
    """Servir o programa de concurso local (pdf_Anuncios/programa_concurso/) e o link no
    detalhe. BASE_DIR temporário para não escrever no repositório."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdf_Anuncios" / "programa_concurso").mkdir(parents=True)
        self.rel = "pdf_Anuncios/programa_concurso/20260101_pc.pdf"
        (self.tmp / self.rel).write_bytes(b"%PDF-1.4\nprograma\n%%EOF")
        self.notice = Notice.objects.create(
            notice_number="PC-1", program_path=self.rel,
            proposal_deadline=date.today() + timedelta(days=5),
        )
        user = User.objects.create_user("cliente_pc_an", password=TEST_PASSWORD)
        self.client.force_login(user)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detail_exposes_program_url(self):
        with override_settings(BASE_DIR=self.tmp):
            body = self.client.get(f"/anuncios/{self.notice.pk}/").json()
        self.assertEqual(body["program_path"], self.rel)
        self.assertEqual(body["program_url"], f"/anuncios/{self.notice.pk}/document/programaConcurso/")

    def test_serve_returns_pdf_inline(self):
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{self.notice.pk}/document/programaConcurso/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4\nprograma\n%%EOF")

    def test_serve_404_when_no_file(self):
        n = Notice.objects.create(notice_number="NOPC", program_path="",
                                  proposal_deadline=date.today() + timedelta(days=5))
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{n.pk}/document/programaConcurso/")
        self.assertEqual(resp.status_code, 404)

    def test_serve_blocks_path_traversal(self):
        (self.tmp / "secret.pdf").write_bytes(b"%PDF-1.4\nsegredo")
        self.notice.program_path = "pdf_Anuncios/../secret.pdf"
        self.notice.save(update_fields=["program_path"])
        with override_settings(BASE_DIR=self.tmp):
            resp = self.client.get(f"/anuncios/{self.notice.pk}/document/programaConcurso/")
        self.assertEqual(resp.status_code, 404)


class AnunciosEmailHeadlineTests(TestCase):
    """O título do email era construído com `yesno` sobre um |length, que devolve sempre o
    1º argumento para qualquer lista não vazia — ou seja, o <h1> saía VAZIO em todos os
    envios de uma só lista (ex: qualquer edição manual, que envia notify_notices([], [n]))."""

    def setUp(self):
        self.commercial = User.objects.create_user("com_head_an", email="com_head_an@x.pt")
        self.commercial.profile.role = UserProfile.COMMERCIAL_PUBLIC
        self.commercial.profile.save()

    def _html(self, new_count, updated_count):
        from django.core import mail
        from anuncios.notifications import notify_notices
        mail.outbox = []
        notices = [
            Notice.objects.create(notice_number=f"H{i}/2026", description=f"Anúncio {i}")
            for i in range(new_count + updated_count)
        ]
        notify_notices(notices[:new_count], notices[new_count:])
        return mail.outbox[0].alternatives[0][0]

    def test_headline_singular_new(self):
        self.assertIn("Novo anúncio publicado", self._html(1, 0))

    def test_headline_plural_new(self):
        self.assertIn("Novos anúncios publicados", self._html(2, 0))

    def test_headline_singular_updated(self):
        self.assertIn("Anúncio atualizado", self._html(0, 1))

    def test_headline_plural_updated(self):
        self.assertIn("Anúncios atualizados", self._html(0, 2))

    def test_headline_both_lists(self):
        self.assertIn("Anúncios novos e atualizados", self._html(1, 1))


# --- Detalhe IA do caderno de encargos (specifications_ai) ----------------

class TenderAIDetailSchemaTests(SimpleTestCase):
    """TenderAIDetail: defaults e validação do JSON vindo do LLM."""

    def test_defaults_when_empty(self):
        detail = specifications_ai.TenderAIDetail.model_validate({})
        self.assertEqual(detail.descricao_detalhada, "")
        self.assertEqual(detail.avaliacao, "")
        self.assertEqual(detail.observacoes, [])

    def test_parses_full_payload(self):
        detail = specifications_ai.TenderAIDetail.model_validate({
            "descricao_detalhada": "Aquisição de serviços de limpeza.",
            "avaliacao": "Multifator – Preço (40%) + Qualidade técnica (60%: Metodologia "
                         "30% + Equipa 30%)",
            "observacoes": ["Visita ao local obrigatória.", "Caução de 5%."],
        })
        self.assertEqual(
            detail.avaliacao,
            "Multifator – Preço (40%) + Qualidade técnica (60%: Metodologia 30% + Equipa 30%)",
        )
        self.assertEqual(detail.observacoes, ["Visita ao local obrigatória.", "Caução de 5%."])


class CoerceAvaliacaoTests(SimpleTestCase):
    """_coerce_avaliacao: passa strings tal e qual, mas trata o formato ANTIGO (objeto
    {criterios, formula}, de antes de avaliacao passar a ser uma frase única) como vazio
    em vez de rebentar a validação do Pydantic."""

    def test_passes_through_string(self):
        self.assertEqual(
            specifications_ai._coerce_avaliacao("Monofator – Preço mais baixo (100%)"),
            "Monofator – Preço mais baixo (100%)",
        )

    def test_legacy_object_shape_becomes_empty(self):
        self.assertEqual(
            specifications_ai._coerce_avaliacao({"criterios": [], "formula": ""}), "",
        )

    def test_empty_dict_default_becomes_empty(self):
        self.assertEqual(specifications_ai._coerce_avaliacao({}), "")


class GetMarkdownTests(TestCase):
    """_get_markdown: reutiliza o markdown já convertido ou converte e persiste o caminho."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdf_Anuncios").mkdir(parents=True)
        self.pdf_rel = "pdf_Anuncios/20260101_ce.pdf"
        (self.tmp / self.pdf_rel).write_bytes(b"%PDF-1.4\ncaderno\n%%EOF")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reuses_existing_markdown_file(self):
        md_rel = "output/markdown/pdf_Anuncios/20260101_ce.md"
        (self.tmp / "output" / "markdown" / "pdf_Anuncios").mkdir(parents=True)
        (self.tmp / md_rel).write_text("# Caderno já convertido", encoding="utf-8")
        notice = Notice.objects.create(
            notice_number="MD-1", specifications_path=self.pdf_rel,
            specifications_markdown_path=md_rel,
        )
        with override_settings(BASE_DIR=self.tmp), \
             mock.patch("anuncios.specifications_ai.pdf_to_markdown") as pdf_to_md:
            markdown = specifications_ai._get_markdown(notice)
        self.assertEqual(markdown, "# Caderno já convertido")
        pdf_to_md.assert_not_called()  # não reconverte — usa o ficheiro em cache

    def test_converts_and_persists_path_when_not_cached(self):
        notice = Notice.objects.create(notice_number="MD-2", specifications_path=self.pdf_rel)
        with override_settings(BASE_DIR=self.tmp), \
             mock.patch("anuncios.specifications_ai.pdf_to_markdown",
                        return_value=("# Convertido agora", "output/markdown/pdf_Anuncios/x.md")) as pdf_to_md:
            markdown = specifications_ai._get_markdown(notice)
        self.assertEqual(markdown, "# Convertido agora")
        pdf_to_md.assert_called_once()
        notice.refresh_from_db()
        self.assertEqual(notice.specifications_markdown_path, "output/markdown/pdf_Anuncios/x.md")

    def test_reconverts_when_cached_file_missing(self):
        notice = Notice.objects.create(
            notice_number="MD-3", specifications_path=self.pdf_rel,
            specifications_markdown_path="output/markdown/pdf_Anuncios/ja-nao-existe.md",
        )
        with override_settings(BASE_DIR=self.tmp), \
             mock.patch("anuncios.specifications_ai.pdf_to_markdown",
                        return_value=("# Reconvertido", "output/markdown/pdf_Anuncios/x.md")) as pdf_to_md:
            markdown = specifications_ai._get_markdown(notice)
        self.assertEqual(markdown, "# Reconvertido")
        pdf_to_md.assert_called_once()

    def test_raises_when_no_specifications_pdf(self):
        notice = Notice.objects.create(notice_number="MD-4", specifications_path="")
        with override_settings(BASE_DIR=self.tmp):
            with self.assertRaises(specifications_ai.SpecificationsNotFound):
                specifications_ai._get_markdown(notice)

    def test_raises_when_conversion_fails(self):
        notice = Notice.objects.create(notice_number="MD-5", specifications_path=self.pdf_rel)
        with override_settings(BASE_DIR=self.tmp), \
             mock.patch("anuncios.specifications_ai.pdf_to_markdown", return_value=None):
            with self.assertRaises(specifications_ai.SpecificationsNotFound):
                specifications_ai._get_markdown(notice)


class HasSpecificationsTests(TestCase):
    """_has_specifications: verificação rápida (sem converter) usada por
    generate_detail_async para decidir se vale a pena arrancar uma thread de fundo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "pdf_Anuncios").mkdir(parents=True)
        self.pdf_rel = "pdf_Anuncios/20260101_ce.pdf"
        (self.tmp / self.pdf_rel).write_bytes(b"%PDF-1.4\ncaderno\n%%EOF")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_true_when_pdf_exists(self):
        notice = Notice.objects.create(notice_number="HAS-1", specifications_path=self.pdf_rel)
        with override_settings(BASE_DIR=self.tmp):
            self.assertTrue(specifications_ai._has_specifications(notice))

    def test_true_when_markdown_already_cached(self):
        md_rel = "output/markdown/pdf_Anuncios/20260101_ce.md"
        (self.tmp / "output" / "markdown" / "pdf_Anuncios").mkdir(parents=True)
        (self.tmp / md_rel).write_text("# Caderno", encoding="utf-8")
        notice = Notice.objects.create(
            notice_number="HAS-2", specifications_path="", specifications_markdown_path=md_rel,
        )
        with override_settings(BASE_DIR=self.tmp):
            self.assertTrue(specifications_ai._has_specifications(notice))

    def test_false_when_neither_exists(self):
        notice = Notice.objects.create(notice_number="HAS-3", specifications_path="")
        with override_settings(BASE_DIR=self.tmp):
            self.assertFalse(specifications_ai._has_specifications(notice))


class GenerateDetailTests(TestCase):
    """generate_detail: cache em specifications_description/evaluation/observations,
    force=True e chamada ao OpenAI (create_client/call_openai_text mockados — sem
    chamadas reais nem custo)."""

    def setUp(self):
        self.notice = Notice.objects.create(notice_number="AI-1", specifications_path="x.pdf")
        self.ai_json = json.dumps({
            "descricao_detalhada": "Descrição gerada.",
            "avaliacao": "Monofator – Preço mais baixo (100%)",
            "observacoes": ["Visita ao local obrigatória."],
        })

    def test_returns_cached_detail_without_calling_ai(self):
        self.notice.specifications_description = "Já gerado"
        self.notice.specifications_evaluation = ""
        self.notice.specifications_observations = []
        self.notice.save(update_fields=[
            "specifications_description", "specifications_evaluation", "specifications_observations",
        ])
        with mock.patch("anuncios.specifications_ai.create_client") as create_client, \
             mock.patch("anuncios.specifications_ai.call_openai_text") as call_text:
            detail = specifications_ai.generate_detail(self.notice)
        self.assertEqual(detail.descricao_detalhada, "Já gerado")
        create_client.assert_not_called()
        call_text.assert_not_called()

    def test_generates_and_caches_when_not_cached(self):
        with mock.patch("anuncios.specifications_ai._get_markdown", return_value="# Caderno"), \
             mock.patch("anuncios.specifications_ai.create_client"), \
             mock.patch("anuncios.specifications_ai.call_openai_text",
                        new=mock.AsyncMock(return_value=self.ai_json)):
            detail = specifications_ai.generate_detail(self.notice)
        self.assertEqual(detail.descricao_detalhada, "Descrição gerada.")
        self.assertEqual(detail.avaliacao, "Monofator – Preço mais baixo (100%)")
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.specifications_description, "Descrição gerada.")
        self.assertEqual(self.notice.specifications_evaluation, "Monofator – Preço mais baixo (100%)")
        self.assertEqual(self.notice.specifications_observations, ["Visita ao local obrigatória."])

    def test_force_regenerates_even_if_cached(self):
        self.notice.specifications_description = "Antigo"
        self.notice.specifications_evaluation = ""
        self.notice.specifications_observations = []
        self.notice.save(update_fields=[
            "specifications_description", "specifications_evaluation", "specifications_observations",
        ])
        with mock.patch("anuncios.specifications_ai._get_markdown", return_value="# Caderno"), \
             mock.patch("anuncios.specifications_ai.create_client"), \
             mock.patch("anuncios.specifications_ai.call_openai_text",
                        new=mock.AsyncMock(return_value=self.ai_json)) as call_text:
            detail = specifications_ai.generate_detail(self.notice, force=True)
        call_text.assert_called_once()
        self.assertEqual(detail.descricao_detalhada, "Descrição gerada.")

    def test_malformed_ai_json_falls_back_to_empty_detail(self):
        with mock.patch("anuncios.specifications_ai._get_markdown", return_value="# Caderno"), \
             mock.patch("anuncios.specifications_ai.create_client"), \
             mock.patch("anuncios.specifications_ai.call_openai_text",
                        new=mock.AsyncMock(return_value="isto não é JSON")):
            detail = specifications_ai.generate_detail(self.notice)
        self.assertEqual(detail, specifications_ai.TenderAIDetail())

    def test_propagates_specifications_not_found(self):
        with mock.patch("anuncios.specifications_ai._get_markdown",
                        side_effect=specifications_ai.SpecificationsNotFound("sem PDF")):
            with self.assertRaises(specifications_ai.SpecificationsNotFound):
                specifications_ai.generate_detail(self.notice)


class GenerateDetailAsyncTests(TestCase):
    """generate_detail_async: devolve o cache já pronto sem tocar em nada quando DONE,
    arranca uma thread de fundo quando PENDING/ERROR, e não duplica threads quando já
    está GENERATING. threading.Thread mockado — sem threads reais nem custo de OpenAI."""

    def setUp(self):
        self.notice = Notice.objects.create(notice_number="ASYNC-1", specifications_path="x.pdf")

    def test_raises_when_no_specifications_at_all(self):
        n = Notice.objects.create(notice_number="ASYNC-2", specifications_path="")
        with self.assertRaises(specifications_ai.SpecificationsNotFound):
            specifications_ai.generate_detail_async(n)

    def test_returns_cached_done_without_spawning_thread(self):
        self.notice.specifications_description = "Já gerado"
        self.notice.specifications_ai_status = Notice.AiStatusChoices.DONE
        self.notice.save()
        with mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice)
        self.assertEqual(status, "done")
        self.assertEqual(detail.descricao_detalhada, "Já gerado")
        thread_cls.assert_not_called()

    def test_claims_and_spawns_thread_when_pending(self):
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice)
        self.assertEqual(status, "generating")
        self.assertIsNone(detail)
        thread_cls.assert_called_once_with(
            target=specifications_ai._run_and_store_in_thread,
            args=(self.notice.pk, False), daemon=True,
        )
        thread_cls.return_value.start.assert_called_once()
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.specifications_ai_status, Notice.AiStatusChoices.GENERATING)

    def test_does_not_spawn_second_thread_while_already_generating(self):
        self.notice.specifications_ai_status = Notice.AiStatusChoices.GENERATING
        self.notice.save()
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice)
        self.assertEqual(status, "generating")
        thread_cls.assert_not_called()

    def test_reclaims_stale_generating_status(self):
        # Simula um worker que reiniciou a meio: GENERATING há mais de
        # _STALE_GENERATING_AFTER — a próxima chamada tem de relançar, não ficar presa.
        self.notice.specifications_ai_status = Notice.AiStatusChoices.GENERATING
        self.notice.save()
        Notice.objects.filter(pk=self.notice.pk).update(
            updated_at=timezone.now() - specifications_ai._STALE_GENERATING_AFTER * 2)
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice)
        self.assertEqual(status, "generating")
        thread_cls.assert_called_once()

    def test_force_regenerates_even_if_already_done(self):
        self.notice.specifications_description = "Antigo"
        self.notice.specifications_ai_status = Notice.AiStatusChoices.DONE
        self.notice.save()
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice, force=True)
        self.assertEqual(status, "generating")
        thread_cls.assert_called_once()

    def test_error_status_is_retried_automatically(self):
        self.notice.specifications_ai_status = Notice.AiStatusChoices.ERROR
        self.notice.save()
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai.threading.Thread") as thread_cls:
            status, detail = specifications_ai.generate_detail_async(self.notice)
        self.assertEqual(status, "generating")
        thread_cls.assert_called_once()


class RunAndStoreTests(TestCase):
    """_run_and_store/_run_and_store_in_thread: o trabalho de facto executado pela thread
    de fundo — chamados a direito, sem thread real."""

    def setUp(self):
        self.notice = Notice.objects.create(
            notice_number="RUN-1", specifications_path="x.pdf",
            specifications_ai_status=Notice.AiStatusChoices.GENERATING,
        )

    def test_success_sets_done(self):
        with mock.patch("anuncios.specifications_ai.generate_detail") as gen:
            specifications_ai._run_and_store(self.notice.pk, False)
        gen.assert_called_once()
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.specifications_ai_status, Notice.AiStatusChoices.DONE)

    def test_failure_sets_error(self):
        with mock.patch("anuncios.specifications_ai.generate_detail",
                        side_effect=RuntimeError("boom")):
            specifications_ai._run_and_store(self.notice.pk, False)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.specifications_ai_status, Notice.AiStatusChoices.ERROR)

    def test_in_thread_wrapper_closes_db_connection(self):
        with mock.patch("anuncios.specifications_ai._run_and_store") as run, \
             mock.patch("anuncios.specifications_ai.connection") as conn:
            specifications_ai._run_and_store_in_thread(self.notice.pk, True)
        run.assert_called_once_with(self.notice.pk, True)
        conn.close.assert_called_once()

    def test_in_thread_wrapper_closes_connection_even_on_error(self):
        with mock.patch("anuncios.specifications_ai._run_and_store",
                        side_effect=RuntimeError("boom")), \
             mock.patch("anuncios.specifications_ai.connection") as conn:
            with self.assertRaises(RuntimeError):
                specifications_ai._run_and_store_in_thread(self.notice.pk, True)
        conn.close.assert_called_once()


class NoticeAiDetailViewTests(TestCase):
    """POST /anuncios/<id>/detail/ — autenticação, permissões (só admin/commercial_public),
    métodos, 404 e o fluxo não-bloqueante (202 generating / 200 done)."""

    def setUp(self):
        self.notice = Notice.objects.create(notice_number="AIV-1", specifications_path="x.pdf")
        user = User.objects.create_user(
            "comercial_aidet_an", email="c@x.pt", password=TEST_PASSWORD)
        user.profile.role = UserProfile.COMMERCIAL_PUBLIC
        user.profile.save()
        self.client.force_login(user)

    def test_requires_authentication(self):
        self.client.logout()
        resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 401)

    def test_client_role_forbidden(self):
        client_user = User.objects.create_user("cliente_aidet_an", password=TEST_PASSWORD)
        # o signal já cria o perfil com role=client
        self.client.force_login(client_user)
        resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 403)

    def test_get_not_allowed(self):
        resp = self.client.get(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 405)

    def test_404_when_notice_missing(self):
        resp = self.client.post("/anuncios/999999/detail/")
        self.assertEqual(resp.status_code, 404)

    def test_404_when_specifications_not_found(self):
        with mock.patch("anuncios.views.specifications_ai.generate_detail_async",
                        side_effect=specifications_ai.SpecificationsNotFound("sem PDF")):
            resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 404)

    def test_returns_cached_detail_immediately_when_already_done(self):
        detail = specifications_ai.TenderAIDetail(descricao_detalhada="X", observacoes=["Y"])
        with mock.patch("anuncios.views.specifications_ai.generate_detail_async",
                        return_value=("done", detail)) as gen:
            resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["descricao_detalhada"], "X")
        self.assertEqual(resp.json()["status"], "done")
        gen.assert_called_once_with(self.notice, force=False)

    def test_returns_202_while_generating_in_background(self):
        with mock.patch("anuncios.views.specifications_ai.generate_detail_async",
                        return_value=("generating", None)):
            resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"status": "generating"})

    def test_refresh_query_param_forces_regeneration(self):
        with mock.patch("anuncios.views.specifications_ai.generate_detail_async",
                        return_value=("generating", None)) as gen:
            self.client.post(f"/anuncios/{self.notice.pk}/detail/?refresh=true")
        gen.assert_called_once_with(self.notice, force=True)

    def test_unexpected_error_returns_502(self):
        with mock.patch("anuncios.views.specifications_ai.generate_detail_async",
                        side_effect=RuntimeError("boom")):
            resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
        self.assertEqual(resp.status_code, 502)


class AiDetailEndToEndTests(TransactionTestCase):
    """Prova a ligação ponta-a-ponta: POST /anuncios/<id>/detail/ arranca a geração numa
    thread de fundo REAL (threading.Thread não é mockado aqui) e, quando termina, um GET
    /anuncios/<id>/ NUM PEDIDO SEPARADO já mostra o detalhe gerado. Só o que fala com o
    exterior é mockado (Docling/OpenAI) — o resto (threading, DB entre pedidos) é real.

    TransactionTestCase (não TestCase): a thread de fundo usa uma ligação PRÓPRIA à BD —
    um TestCase normal envolve o teste numa transação nunca comitada nessa ligação, por
    isso a thread nunca veria a Notice criada no setUp (DoesNotExist). Isto reproduz
    exatamente a produção: cada pedido HTTP comita as suas escritas normalmente."""

    def setUp(self):
        self.notice = Notice.objects.create(notice_number="E2E-1", specifications_path="x.pdf")
        user = User.objects.create_user(
            "comercial_e2e_an", email="c@x.pt", password=TEST_PASSWORD)
        user.profile.role = UserProfile.COMMERCIAL_PUBLIC
        user.profile.save()
        self.client.force_login(user)

    def test_post_then_get_shows_generated_detail_once_background_finishes(self):
        ai_json = json.dumps({
            "descricao_detalhada": "Descrição E2E.",
            "avaliacao": "Monofator – Preço mais baixo (100%)",
            "observacoes": ["Nota E2E."],
        })
        with mock.patch("anuncios.specifications_ai._has_specifications", return_value=True), \
             mock.patch("anuncios.specifications_ai._get_markdown", return_value="# Caderno"), \
             mock.patch("anuncios.specifications_ai.create_client"), \
             mock.patch("anuncios.specifications_ai.call_openai_text",
                        new=mock.AsyncMock(return_value=ai_json)):
            resp = self.client.post(f"/anuncios/{self.notice.pk}/detail/")
            self.assertEqual(resp.status_code, 202)
            self.assertEqual(resp.json(), {"status": "generating"})

            # A thread de fundo é real: espera (com limite de 5s) que termine, SEM sair
            # do bloco mockado — se saísse antes, a thread podia acabar de correr já sem
            # os mocks e tentar mesmo converter/chamar o OpenAI.
            for _ in range(50):
                self.notice.refresh_from_db()
                if self.notice.specifications_ai_status != Notice.AiStatusChoices.GENERATING:
                    break
                time.sleep(0.1)

        self.assertEqual(self.notice.specifications_ai_status, Notice.AiStatusChoices.DONE)

        get_resp = self.client.get(f"/anuncios/{self.notice.pk}/")
        self.assertEqual(get_resp.status_code, 200)
        ai_detail = get_resp.json()["ai_detail"]
        self.assertEqual(ai_detail["status"], "done")
        self.assertEqual(ai_detail["descricao_detalhada"], "Descrição E2E.")
        self.assertEqual(ai_detail["avaliacao"], "Monofator – Preço mais baixo (100%)")
        self.assertEqual(ai_detail["observacoes"], ["Nota E2E."])
