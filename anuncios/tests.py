"""Tests for the anuncios app.

Network (base.gov API), Selenium/Chrome and file writes are mocked — the suite runs
offline and does not touch pdf_Anuncios/ or the real lock files.
"""

import io
import shutil
import tempfile
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase

from anuncios import services, specifications
from anuncios.models import Notice


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
        got = services.matched_keywords("Aquisição de serviços de CONSULTADORIA e avaliação")
        self.assertIn("Consultadoria", got)
        self.assertIn("Avaliação", got)
        self.assertIn("Serviços", got)

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
                        side_effect=lambda name, content: f"pdf_Anuncios/{name}"):
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
                        side_effect=lambda name, content: (name, content)):
            name, content = specifications._find_in_zip(data)
        self.assertEqual(name, "Caderno de Encargos.pdf")
        self.assertEqual(content, b"%PDF b")

    def test_fallback_first_pdf(self):
        data = self._zip({"Programa.pdf": b"%PDF a", "Notes.txt": b"x"})
        with mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content: name):
            self.assertEqual(specifications._find_in_zip(data), "Programa.pdf")

    def test_no_pdf(self):
        data = self._zip({"a.txt": b"x"})
        self.assertEqual(specifications._find_in_zip(data), "")


class FetchSpecificationsTests(SimpleTestCase):
    def test_direct_pdf_matching_name(self):
        resp = FakeResp(b"%PDF-1.7", ctype="application/pdf", url="http://x/ce.pdf",
                        cd='attachment; filename="Caderno de Encargos.pdf"')
        with mock.patch("anuncios.specifications._get", return_value=resp), \
             mock.patch("anuncios.specifications._save_bytes",
                        side_effect=lambda name, content: f"pdf_Anuncios/{name}"):
            self.assertEqual(specifications.fetch_specifications("http://x/ce.pdf"),
                             "pdf_Anuncios/Caderno de Encargos.pdf")

    def test_direct_pdf_non_ce_name_skipped(self):
        resp = FakeResp(b"%PDF-1.7", ctype="application/pdf", url="http://x/prog.pdf",
                        cd='attachment; filename="Programa.pdf"')
        with mock.patch("anuncios.specifications._get", return_value=resp):
            self.assertEqual(specifications.fetch_specifications("http://x/prog.pdf"), "")

    def test_empty_url(self):
        self.assertEqual(specifications.fetch_specifications(""), "")

    def test_get_failure(self):
        with mock.patch("anuncios.specifications._get", return_value=None):
            self.assertEqual(specifications.fetch_specifications("http://x"), "")


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
        self.assertEqual(services._upsert_notice(_notice_data()), "created")
        self.assertEqual(services._upsert_notice(_notice_data()), "unchanged")
        self.assertEqual(Notice.objects.count(), 1)

    def test_update_changed_field(self):
        services._upsert_notice(_notice_data(entity_name="Old"))
        self.assertEqual(services._upsert_notice(_notice_data(entity_name="New")), "updated")
        self.assertEqual(Notice.objects.get(notice_number="1/2026").entity_name, "New")

    def test_empty_does_not_overwrite(self):
        services._upsert_notice(_notice_data(entity_name="Original"))
        status = services._upsert_notice(_notice_data(entity_name=""))
        self.assertEqual(status, "unchanged")
        self.assertEqual(Notice.objects.get(notice_number="1/2026").entity_name, "Original")

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
                              contract_types=["Aquisição de serviços"])
        Notice.objects.create(notice_number="B", proposal_deadline=self.today - timedelta(days=1))
        Notice.objects.create(notice_number="C", proposal_deadline=None, base_price=Decimal("300"))

    def test_excludes_expired_keeps_null(self):
        nums = {n.notice_number for n in services.filter_notices({})}
        self.assertEqual(nums, {"A", "C"})

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
                                  proposal_deadline=date(2026, 7, 13), active=True)
        out = services.serialize_notice(n)
        self.assertEqual(out["notice_number"], "1/2026")
        self.assertEqual(out["base_price"], 100.5)
        self.assertEqual(out["proposal_deadline"], "2026-07-13")
        self.assertTrue(out["active"])
        self.assertIn("specifications_path", out)


class DeactivateExpiredTests(TestCase):
    def test_deactivate(self):
        Notice.objects.create(notice_number="X", active=True,
                              proposal_deadline=date.today() - timedelta(days=1))
        Notice.objects.create(notice_number="Y", active=True,
                              proposal_deadline=date.today() + timedelta(days=1))
        self.assertEqual(services.deactivate_expired(), 1)
        self.assertFalse(Notice.objects.get(notice_number="X").active)
        self.assertTrue(Notice.objects.get(notice_number="Y").active)


# --- Import / backfill -----------------------------------------------------

class ImportNoticesTests(TestCase):
    def test_register_only_no_specs(self):
        raw = [{"nAnuncio": "9/2026", "descricaoAnuncio": "Serviços de consultoria",
                "pecasProcedimento": "http://x", "DataLimitePropostas": "31/12/2026"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(summary["with_keywords"], 1)
        self.assertEqual(Notice.objects.get(notice_number="9/2026").specifications_path, "")

    def test_keyword_filter_skips(self):
        raw = [{"nAnuncio": "10/2026", "descricaoAnuncio": "Fornecimento de material de limpeza"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False)
        self.assertEqual(summary["with_keywords"], 0)
        self.assertEqual(Notice.objects.count(), 0)

    def test_download_specs_sets_path(self):
        raw = [{"nAnuncio": "11/2026", "descricaoAnuncio": "Serviços", "pecasProcedimento": "http://x"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw), \
             mock.patch("anuncios.services.fetch_specifications", return_value="pdf_Anuncios/ce.pdf") as fs, \
             mock.patch("anuncios.services.connection.close"):
            services.import_notices(1, download_specs=True)
        self.assertEqual(Notice.objects.get(notice_number="11/2026").specifications_path, "pdf_Anuncios/ce.pdf")
        fs.assert_called_once()

    def test_should_stop_cancels(self):
        raw = [{"nAnuncio": "12/2026", "descricaoAnuncio": "Serviços"}]
        with mock.patch("anuncios.services.fetch_notices", return_value=raw):
            summary = services.import_notices(1, download_specs=False, should_stop=lambda: True)
        self.assertEqual(summary["with_keywords"], 0)
        self.assertEqual(Notice.objects.count(), 0)


class BackfillTests(TestCase):
    def test_backfill_downloads_missing(self):
        Notice.objects.create(notice_number="13/2026", procedure_documents_url="http://x",
                              specifications_path="")
        Notice.objects.create(notice_number="14/2026", procedure_documents_url="",
                              specifications_path="")  # no docs link -> skipped
        with mock.patch("anuncios.services.fetch_specifications", return_value="pdf_Anuncios/ce.pdf"), \
             mock.patch("anuncios.services.connection.close"), \
             mock.patch("anuncios.services.mark_import_start"), \
             mock.patch("anuncios.services.mark_import_end"), \
             mock.patch("anuncios.services._heartbeat_lock"):
            summary = services.download_missing_specifications()
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(Notice.objects.get(notice_number="13/2026").specifications_path, "pdf_Anuncios/ce.pdf")


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

    def test_end_only_removes_own_lock(self):
        self.lock.write_text("999999", encoding="utf-8")  # foreign owner
        services.mark_import_end()
        self.assertTrue(self.lock.exists())  # not removed
        services.mark_import_start()  # our PID
        services.mark_import_end()
        self.assertFalse(self.lock.exists())


# --- Views -----------------------------------------------------------------

class ViewTests(TestCase):
    def test_import_route_is_post_only(self):
        resp = self.client.get("/anuncios/importar/")
        self.assertEqual(resp.status_code, 405)  # GET not allowed (side-effecting)

    def test_import_route_post_registers_and_spawns(self):
        with mock.patch("anuncios.services.import_notices",
                        return_value={"created": 1, "with_keywords": 1}) as imp, \
             mock.patch("anuncios.services.spawn_specifications_download") as spawn:
            resp = self.client.post("/anuncios/importar/")
        self.assertEqual(resp.status_code, 200)
        imp.assert_called_once()
        spawn.assert_called_once()
        self.assertIn("specifications", resp.json())

    def test_import_route_api_error(self):
        with mock.patch("anuncios.services.import_notices",
                        side_effect=services.BaseGovError("no key")), \
             mock.patch("anuncios.services.spawn_specifications_download"):
            resp = self.client.post("/anuncios/importar/")
        self.assertEqual(resp.status_code, 502)

    def test_list_route(self):
        Notice.objects.create(notice_number="1/2026",
                              proposal_deadline=date.today() + timedelta(days=3))
        resp = self.client.get("/anuncios/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["notices"][0]["notice_number"], "1/2026")
