from django.urls import path
from . import views

urlpatterns = [
    path("list/", views.grants_list, name="grants_list"),              # LEITURA — pública (todos)
    path("<int:pk>/edit/", views.grants_edit, name="grants_edit"),     # EDIÇÃO — admin/commercial
    path("", views.grants_all, name="grants_all"),                     # scrape — ABERTO (sem auth)
    path("compete/", views.grants_compete, name="grants_compete"),
    path("portugal/", views.grants_portugal, name="grants_portugal"),
    path("prr/", views.grants_prr, name="grants_prr"),
]
