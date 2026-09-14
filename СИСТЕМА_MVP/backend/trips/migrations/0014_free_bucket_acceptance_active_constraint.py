from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0013_free_bucket_acceptance'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='freebucketacceptance',
            name='unique_open_free_bucket_acceptance_per_truck',
        ),
        migrations.AddConstraint(
            model_name='freebucketacceptance',
            constraint=models.UniqueConstraint(
                condition=models.Q(status='accepted'),
                fields=('truck',),
                name='unique_open_free_bucket_acceptance_per_truck',
            ),
        ),
    ]
