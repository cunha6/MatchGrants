from django.http import JsonResponse
from . import service


def compete(request):
    return JsonResponse({"status": "ok", "mensagem": "COMPETE 2030 API"})


def avisos_abertos(request):
    try:
        dados = service.scrape_compete2030()
        return JsonResponse(
            {"total": len(dados), "avisos": dados},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"status": "erro", "mensagem": str(e)}, status=500)
