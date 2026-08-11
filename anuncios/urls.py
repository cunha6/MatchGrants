from django.urls import path

from . import views

urlpatterns = [
    path("list/", views.list_notices, name="list_notices"),                  # LISTAGEM — exige sessão
    path("filters/", views.notice_filters, name="notice_filters"),           # opções dos selects — exige sessão
    path("<int:pk>/edit/", views.notice_edit, name="notice_edit"),           # EDIÇÃO — admin/commercial (PUT/PATCH)
    path("<int:pk>/detail/", views.notice_ai_detail, name="notice_ai_detail"),  # DETALHE IA — POST, cacheado
    path("<int:pk>/document/cadernoEncargos/", views.serve_notice_specifications,
        name="notice_specifications"),                                      # exige sessão
    path("<int:pk>/document/programaConcurso/", views.serve_notice_program,
        name="notice_program"),                                             # exige sessão
    path("<int:pk>/", views.notice_detail, name="notice_detail"),            # DETALHE — exige sessão (não faz parte do match)
    path("", views.import_notices, name="import_notices"),                   # importar — ABERTO (POST, ?num_days=), automação
]
