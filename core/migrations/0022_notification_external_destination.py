import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[("core","0021_credentialtype_control_fields"),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[
        migrations.AlterField(model_name="notification",name="recipient",field=models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.CASCADE,related_name="workforce_notifications",to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="notification",name="destination",field=models.CharField(blank=True,help_text="Explicit email address or phone number for recipients without an account.",max_length=320)),
    ]
