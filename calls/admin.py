from django.contrib import admin

from .models import Call, CallType, CodeLabel, ProgrammeType


@admin.register(CodeLabel)
class CodeLabelAdmin(admin.ModelAdmin):
    list_display = ("facet", "code", "label")
    list_filter = ("facet",)
    search_fields = ("code", "label")


@admin.register(ProgrammeType)
class ProgrammeTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "total_count")
    search_fields = ("code", "name")


@admin.register(CallType)
class CallTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "total_count")


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = ("identifier", "title", "status", "call_type", "programme", "deadline_date")
    list_filter = ("status", "call_type", "programme")
    search_fields = ("identifier", "reference", "title", "call_identifier")
    # `raw` é um JSON grande; editá-lo à mão no admin não faz sentido (é reescrito pelo sync).
    readonly_fields = ("raw", "created_at", "updated_at", "last_seen_at")
