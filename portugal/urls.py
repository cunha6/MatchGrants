from django.urls import path
from . import views

urlpatterns = [
    path('', views.portugal, name='portugal'),
    path("avisos/", views.avisos_abertos, name='avisos_abertos'),
]