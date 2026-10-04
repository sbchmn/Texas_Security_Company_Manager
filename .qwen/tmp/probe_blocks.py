import os
import shutil

from django.template import Context, Engine

ROOT = os.path.join(".qwen", "tmp", "tpl")
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(os.path.join(ROOT, "allauth", "layouts"), exist_ok=True)

with open(os.path.join(ROOT, "base.html"), "w", encoding="utf-8") as fh:
    fh.write(
        "<html><head><title>{% block title %}T{% endblock %}</title></head>"
        "{% block body_outer %}{% block body %}BODY{% endblock %}{% endblock %}"
        "|{% block content_shell %}{% block content %}BASE{% endblock %}{% endblock %}</html>"
    )

with open(os.path.join(ROOT, "allauth", "layouts", "base.html"), "w", encoding="utf-8") as fh:
    fh.write(
        '{% extends "base.html" %}'
        "{% block title %}{% block head_title %}H{% endblock %} · ORG{% endblock %}"
        '{% block content_shell %}<section class="auth-page">{% block content %}{% endblock %}</section>{% endblock %}'
    )

with open(os.path.join(ROOT, "page.html"), "w", encoding="utf-8") as fh:
    fh.write(
        '{% extends "allauth/layouts/base.html" %}'
        "{% block head_title %}PAGE-TITLE{% endblock %}"
        "{% block content %}HELLO{% endblock %}"
    )

with open(os.path.join(ROOT, "app.html"), "w", encoding="utf-8") as fh:
    fh.write('{% extends "base.html" %}{% block content %}APP{% endblock %}')

engine = Engine(dirs=[ROOT])
print("allauth page ->", engine.get_template("page.html").render(Context({})))
print("plain app page ->", engine.get_template("app.html").render(Context({})))
