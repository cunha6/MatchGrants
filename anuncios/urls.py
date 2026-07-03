from django.urls import path

from . import views

urlpatterns = [
    path("", views.list_notices, name="list_notices"),
    path("importar/", views.import_notices, {"num_days": 15}, name="import_notices"),
    path("importar/<int:num_days>/", views.import_notices, name="import_notices_days"),
]
