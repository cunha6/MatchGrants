from django.urls import path
from . import views

urlpatterns = [
    path("list/", views.grants_list, name="grants_list"),              # LISTAGEM — pública (todos)
    path("<int:pk>/edit/", views.grants_edit, name="grants_edit"),     # EDIÇÃO (campos+coleções) — admin/commercial
    path("<int:pk>/document/", views.serve_grant_document, name="grant_document"),  # PDF do aviso
    path("<int:pk>/", views.grants_detail, name="grants_detail"),      # DETALHE — público (GET)
    path("", views.grants_all, name="grants_all"),                     # scrape — ABERTO (POST, sem auth)
    path("compete/", views.grants_compete, name="grants_compete"),
    path("portugal/", views.grants_portugal, name="grants_portugal"),
    path("prr/", views.grants_prr, name="grants_prr"),
]
