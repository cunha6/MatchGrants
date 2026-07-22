from django.urls import path

from . import views

urlpatterns = [
    path("", views.list_notices, name="list_notices"),                       # LISTAGEM — pública
    path("importar/", views.import_notices, {"num_days": 15}, name="import_notices"),
    path("importar/<int:num_days>/", views.import_notices, name="import_notices_days"),
    path("<int:pk>/edit/", views.notice_edit, name="notice_edit"),           # EDIÇÃO — admin/commercial (PUT/PATCH)
    path("<int:pk>/specifications/", views.serve_notice_specifications, name="notice_specifications"),
    path("<int:pk>/", views.notice_detail, name="notice_detail"),            # DETALHE — público
]
