from django.contrib import admin

from .models import PilotFeedback, ReportTemplate


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'is_active', 'created_at', 'updated_at')
    search_fields = ('name',)
    list_filter = ('report_type', 'is_active')


@admin.register(PilotFeedback)
class PilotFeedbackAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'priority', 'status', 'screen', 'created_by', 'created_at')
    search_fields = ('title', 'screen', 'description', 'decision')
    list_filter = ('category', 'priority', 'status', 'created_at')
    readonly_fields = ('created_at', 'updated_at')
