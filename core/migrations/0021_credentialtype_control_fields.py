import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[("core","0020_membershipinvitation"),migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations=[
        migrations.AddField(model_name="credentialtype",name="jurisdiction",field=models.CharField(default="Texas",max_length=80)),
        migrations.AddField(model_name="credentialtype",name="authority_url",field=models.URLField(blank=True)),
        migrations.AddField(model_name="credentialtype",name="authority_reference",field=models.CharField(blank=True,max_length=180)),
        migrations.AddField(model_name="credentialtype",name="interpretation",field=models.TextField(blank=True)),
        migrations.AddField(model_name="credentialtype",name="effective_from",field=models.DateField(blank=True,null=True)),
        migrations.AddField(model_name="credentialtype",name="effective_until",field=models.DateField(blank=True,null=True)),
        migrations.AddField(model_name="credentialtype",name="approved_at",field=models.DateTimeField(blank=True,null=True)),
        migrations.AddField(model_name="credentialtype",name="approved_by",field=models.ForeignKey(blank=True,null=True,on_delete=django.db.models.deletion.PROTECT,related_name="approved_credential_rules",to=settings.AUTH_USER_MODEL)),
    ]
