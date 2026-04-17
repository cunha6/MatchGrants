from django.urls import path
from . import views

urlpatterns = [
    path('', views.compete, name='compete'),
    path("avisos/", views.avisos_abertos),
]