from django.db import migrations


def move_admin_control(apps, schema_editor):
    leases = apps.get_model('settlement', 'SettlementControlLease').objects.using(schema_editor.connection.alias)
    shared = leases.select_for_update().filter(scope='settlement', owner_access__role__code='admin').first()
    if shared is None:
        return
    # Keep the running admin session valid and fence credentials from earlier clerk sessions.
    shared.scope = f'settlement:admin:{shared.owner_access_id}'
    shared.save(update_fields=['scope'])
    leases.create(scope='settlement', fencing_revision=shared.fencing_revision + 1)


class Migration(migrations.Migration):
    dependencies = [('settlement', '0025_allow_external_resident_without_phone')]
    operations = [migrations.RunPython(move_admin_control, migrations.RunPython.noop)]
