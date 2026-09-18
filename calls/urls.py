from django.urls import path

from . import views

urlpatterns = [
    path("sync/", views.sync_calls, name="calls_sync"),          # importar — automação, sem autenticação
    path("filters/", views.call_filters, name="call_filters"),   # opções dos selects — exige sessão
    path("<int:pk>/edit/", views.call_edit, name="call_edit"),   # EDIÇÃO — admin/commercial (PUT/PATCH)
    path("<int:pk>/", views.call_detail, name="call_detail"),    # DETALHE — exige sessão
    path("", views.list_calls, name="calls_list"),               # LISTAGEM — exige sessão
]
