from django.contrib import admin

from .models import Notice


@admin.register(Notice)
class NoticeAdmin(admin.ModelAdmin):
    list_display = (
        "notice_number", "entity_name", "act_type", "procedure_type",
        "base_price", "publication_date", "proposal_deadline", "status",
        "specifications_path",
    )
    list_filter = ("status", "act_type", "procedure_type", "environmental_criteria", "year")
    search_fields = ("notice_number", "incm_id", "entity_name", "description", "entity_nif")
    list_editable = ("specifications_path",)  # edit the specs path inline in the list
    date_hierarchy = "publication_date"
    ordering = ("-publication_date",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        ("Identification", {
            "fields": ("notice_number", "incm_id", "dr_number", "series",
                       "publication_date", "act_type", "procedure_type", "year"),
        }),
        ("Entity", {"fields": ("entity_name", "entity_nif")}),
        ("Content", {
            "fields": ("description", "url", "contract_types", "cpvs", "lots",
                       "base_price", "environmental_criteria"),
        }),
        ("Specifications (editable)", {
            "fields": ("procedure_documents_url", "specifications_path"),
            "description": "Add/edit/remove the local path of the tender specifications PDF.",
        }),
        ("Deadline & status", {
            "fields": ("proposal_period_days", "proposal_deadline", "status",
                       "created_at", "updated_at"),
        }),
    )