from django.contrib import admin

from .models import Backup, EditJob, EditPlan, JobItem, PlanItem


@admin.register(EditPlan)
class EditPlanAdmin(admin.ModelAdmin):
    list_display = ("id", "operation", "status", "created_by", "created_at", "n_change", "n_nochange", "n_skip")
    list_filter = ("operation", "status")


@admin.register(EditJob)
class EditJobAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "status", "created_by", "created_at", "done", "total")
    list_filter = ("status", "kind")


admin.site.register(PlanItem)
admin.site.register(JobItem)
admin.site.register(Backup)
