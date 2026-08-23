from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("journal", "0017_article_cover"),
    ]

    operations = [
        migrations.CreateModel(
            name="DoubakImporter",
            fields=[],
            options={
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("journal.ndjsonimporter",),
        ),
    ]
