from django.http import JsonResponse
from . import service


def avisos_todos(request):
    try:
        dados = service.scrape_todos()
        return JsonResponse(dados, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"status": "erro", "mensagem": str(e)}, status=500)


def avisos_compete(request):
    try:
        dados = service.scrape_compete()
        return JsonResponse(
            {"total": len(dados), "avisos": dados},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"status": "erro", "mensagem": str(e)}, status=500)


def avisos_portugal(request):
    try:
        dados = service.scrape_portugal()
        return JsonResponse(
            {"total": len(dados), "avisos": dados},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"status": "erro", "mensagem": str(e)}, status=500)


def avisos_prr(request):
    try:
        dados = service.scrape_prr()
        return JsonResponse(
            {"total": len(dados), "avisos": dados},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"status": "erro", "mensagem": str(e)}, status=500)
