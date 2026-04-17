from django.urls import path
from . import views

urlpatterns = [
    path('', views.prr, name='prr'),
    path("avisos/", views.avisos_abertos, name='avisos_abertos'),
]