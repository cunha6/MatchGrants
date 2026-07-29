from django.contrib import admin

from .models import PlannedGrant


@admin.register(PlannedGrant)
class PlannedGrantAdmin(admin.ModelAdmin):
    list_display = ("plan_id", "designation", "programme", "fund",
                    "expected_start", "expected_end", "quadrimester")
    search_fields = ("plan_id", "designation", "programme", "specific_objective")
    list_filter = ("programme", "fund", "quadrimester", "nuts")
    ordering = ("expected_start", "plan_id")
    readonly_fields = ("created_at", "updated_at")
