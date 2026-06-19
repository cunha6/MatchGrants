from django.http import JsonResponse
from . import service


def grants_all(request):
    try:
        data = service.scrape_todos()
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def grants_compete(request):
    try:
        data = service.scrape_compete()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

def grants_portugal(request):
    try:
        data = service.scrape_portugal()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def grants_prr(request):
    try:
        data = service.scrape_prr()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
