from django.contrib import admin

from .forms import RatingPeriodReferenceForm
from .models import PilotFeedback, RatingPeriod, ReportTemplate


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'is_active', 'created_at', 'updated_at')
    search_fields = ('name',)
    list_filter = ('report_type', 'is_active')


@admin.register(RatingPeriod)
class RatingPeriodAdmin(admin.ModelAdmin):
    form = RatingPeriodReferenceForm
    list_display = (
        'name',
        'starts_on',
        'ends_before',
        'generation_source_label',
        'manual_override_label',
        'is_active',
        'updated_at',
    )
    search_fields = ('name', 'comment')
    list_filter = (
        'is_active',
        'starts_on',
        'ends_before',
        'nominal_starts_on',
    )
    readonly_fields = (
        'nominal_starts_on',
        'generation_source_label',
        'manual_override_label',
        'created_at',
        'updated_at',
    )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PilotFeedback)
class PilotFeedbackAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'priority', 'status', 'screen', 'created_by', 'created_at')
    search_fields = ('title', 'screen', 'description', 'decision')
    list_filter = ('category', 'priority', 'status', 'created_at')
    readonly_fields = ('created_at', 'updated_at')
