"""Seed the amenity catalogue from ``catalog.constants.AMENITY_SEED`` (LLD §4.6).

Historical models skip ``AddCommonField.save`` and its managers, so the audit columns are set here: the rows
are created by the system actor, ACTIVE, with the category stored as its SMALLINT value.
"""

from django.db import migrations
from hotel_commons.security.principal import SYSTEM_ACTOR

from catalog.constants import AMENITY_SEED

ACTIVE = 1  # RecordStatus.ACTIVE


def seed_amenities(apps, schema_editor):
    Amenity = apps.get_model("catalog", "Amenity")
    Amenity.objects.using(schema_editor.connection.alias).bulk_create(
        Amenity(code=code, name=name, category=category.value, status=ACTIVE, creation_by=SYSTEM_ACTOR)
        for code, name, category in AMENITY_SEED
    )


def remove_amenities(apps, schema_editor):
    Amenity = apps.get_model("catalog", "Amenity")
    Amenity.objects.using(schema_editor.connection.alias).filter(
        code__in=[code for code, _name, _category in AMENITY_SEED]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_amenities, remove_amenities),
    ]
