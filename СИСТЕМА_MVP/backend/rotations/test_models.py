from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import Employee

from .models import RotationCollectionCycle, RotationResponse, WatchExtensionCase


class RotationModelConstraintTests(TestCase):
    def setUp(self):
        today = timezone.localdate()
        self.employee = Employee.objects.create(
            full_name='Сотрудник Перевахты',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.watch_period = WatchPeriod.objects.create(
            name='Следующая вахта',
            starts_on=today + timedelta(days=10),
            ends_on=today + timedelta(days=40),
            is_active=True,
        )

    def create_cycle(self, *, status=RotationCollectionCycle.Status.DRAFT, name='Сбор данных'):
        now = timezone.now()
        lifecycle = {}
        if status == RotationCollectionCycle.Status.OPEN:
            lifecycle = {
                'opened_by': self.employee,
                'opened_at': now,
            }
        elif status in {
            RotationCollectionCycle.Status.CLOSED,
            RotationCollectionCycle.Status.ARCHIVED,
        }:
            lifecycle = {
                'opened_by': self.employee,
                'opened_at': now - timedelta(hours=1),
                'closed_by': self.employee,
                'closed_at': now,
            }
        return RotationCollectionCycle.objects.create(
            name=name,
            target_watch_period=self.watch_period,
            response_deadline=now + timedelta(days=2),
            status=status,
            created_by=self.employee,
            **lifecycle,
        )

    def create_submitted_extension_response(self):
        cycle = self.create_cycle()
        return RotationResponse.objects.create(
            cycle=cycle,
            employee=self.employee,
            snapshot_full_name=self.employee.full_name,
            state=RotationResponse.State.SUBMITTED,
            intent=RotationResponse.Intent.EXTENSION,
            submitted_by=self.employee,
            submitted_at=timezone.now(),
        )

    def test_only_one_open_cycle_is_allowed_for_watch_period(self):
        self.create_cycle(status=RotationCollectionCycle.Status.OPEN, name='Первый сбор')

        with self.assertRaises(IntegrityError), transaction.atomic():
            self.create_cycle(status=RotationCollectionCycle.Status.OPEN, name='Второй сбор')

    def test_pending_response_cannot_have_intent_or_submission_metadata(self):
        cycle = self.create_cycle()

        with self.assertRaises(IntegrityError), transaction.atomic():
            RotationResponse.objects.create(
                cycle=cycle,
                employee=self.employee,
                snapshot_full_name=self.employee.full_name,
                state=RotationResponse.State.PENDING,
                intent=RotationResponse.Intent.ARRIVAL,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            RotationResponse.objects.create(
                cycle=cycle,
                employee=self.employee,
                snapshot_full_name=self.employee.full_name,
                state=RotationResponse.State.PENDING,
                submitted_by=self.employee,
                submitted_at=timezone.now(),
            )

    def test_extension_end_cannot_be_before_start(self):
        response = self.create_submitted_extension_response()
        extension_start = self.watch_period.ends_on + timedelta(days=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchExtensionCase.objects.create(
                response=response,
                extension_start=extension_start,
                extension_end=extension_start - timedelta(days=1),
            )

    def test_decision_and_documentation_lifecycle_constraints(self):
        response = self.create_submitted_extension_response()
        now = timezone.now()
        extension_start = self.watch_period.ends_on + timedelta(days=1)
        extension_end = extension_start + timedelta(days=10)

        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchExtensionCase.objects.create(
                response=response,
                extension_start=extension_start,
                extension_end=extension_end,
                decision_status=WatchExtensionCase.DecisionStatus.APPROVED,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchExtensionCase.objects.create(
                response=response,
                extension_start=extension_start,
                extension_end=extension_end,
                decision_status=WatchExtensionCase.DecisionStatus.PENDING,
                decision_by=self.employee,
                decision_at=now,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchExtensionCase.objects.create(
                response=response,
                extension_start=extension_start,
                extension_end=extension_end,
                documentation_status=WatchExtensionCase.DocumentationStatus.DATA_READY,
                documentation_by=self.employee,
                documentation_at=now,
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            WatchExtensionCase.objects.create(
                response=response,
                extension_start=extension_start,
                extension_end=extension_end,
                decision_status=WatchExtensionCase.DecisionStatus.APPROVED,
                decision_by=self.employee,
                decision_at=now,
                documentation_status=WatchExtensionCase.DocumentationStatus.DATA_READY,
            )

        extension_case = WatchExtensionCase.objects.create(
            response=response,
            extension_start=extension_start,
            extension_end=extension_end,
            decision_status=WatchExtensionCase.DecisionStatus.APPROVED,
            decision_by=self.employee,
            decision_at=now,
            documentation_status=WatchExtensionCase.DocumentationStatus.DATA_READY,
            documentation_by=self.employee,
            documentation_at=now,
        )
        self.assertEqual(
            extension_case.documentation_status,
            WatchExtensionCase.DocumentationStatus.DATA_READY,
        )
