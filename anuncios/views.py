from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services


@csrf_exempt
@require_POST
def import_notices(request, num_days=15):
    """Query the base.gov.pt API, filter by keywords and store notices in the DB.

    POST only — it has side effects (writes to the DB and spawns the extraction process),
    so it must not be a safe GET. Registers the notices immediately (fast) and then kills
    any previous extraction and (re)launches the tender-specifications download in a
    SEPARATE process (survives the runserver reloads). num_days comes from the route:
    /anuncios/importar/ (15 by default) or /anuncios/importar/<n>/.
    """
    try:
        # 1) Fast notice registration (no specifications download) — quick response.
        summary = services.import_notices(num_days, download_specs=False)
        # 2) Kill the previous extraction and (re)launch the specifications download in a
        #    separate process (no second API fetch — the notices are already registered).
        services.spawn_specifications_download()
        summary["specifications"] = (
            "extraction (re)started in a separate process — progress in the runserver console"
        )
        return JsonResponse(summary, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except services.BaseGovError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def list_notices(request):
    """List the stored notices (not expired), with filters and ordering.

    Query params: act_type, procedure_type, contract_type, order_by.
    See services.filter_notices / services.ORDERING.
    """
    qs = services.filter_notices(request.GET)
    notices = [services.serialize_notice(n) for n in qs]
    return JsonResponse(
        {"total": len(notices), "notices": notices},
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )
