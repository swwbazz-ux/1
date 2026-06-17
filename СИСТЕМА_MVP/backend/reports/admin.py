from django.contrib import admin

from .models import ReportTemplate


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'is_active', 'created_at', 'updated_at')
    search_fields = ('name',)
    list_filter = ('report_type', 'is_active')

# Register your models here.
