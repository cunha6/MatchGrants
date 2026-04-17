from django.http import HttpResponse


def index(request):
    return HttpResponse("Django está a funcionar!")