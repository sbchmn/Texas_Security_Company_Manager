import hashlib
import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@require_safe
@never_cache
def manifest(request):
    response = JsonResponse({
        "id": "/", "name": settings.PWA_NAME, "short_name": settings.PWA_SHORT_NAME,
        "description": "Workforce scheduling, secure time capture and personnel workflows.",
        "start_url": "/?source=pwa", "scope": "/", "display": "standalone",
        "background_color": "#F4F7FB", "theme_color": "#16324F",
        "lang": "en", "categories": ["business", "productivity"],
        "icons": [
            {"src": static(f"icons/icon-{size}.png"), "sizes": f"{size}x{size}",
             "type": "image/png", "purpose": "any"} for size in (192, 512)
        ] + [{"src": static("icons/icon-maskable-512.png"), "sizes": "512x512",
              "type": "image/png", "purpose": "maskable"}],
        "shortcuts": [
            {"name": "Time clock", "url": "/clock/"},
            {"name": "My shifts", "url": "/my-shifts/"},
        ],
    })
    response["Content-Type"] = "application/manifest+json"
    return response


@require_safe
def service_worker(request):
    assets = [static(name) for name in (
        "css/app.css", "css/timesheets.css", "js/app.js", "js/pwa.js",
        "icon.svg", "icons/icon-180.png", "icons/icon-192.png", "icons/icon-512.png",
    )]
    revision = hashlib.sha256(json.dumps(assets).encode()).hexdigest()[:12]
    body = render_to_string("core/service_worker.js", {
        "assets": json.dumps(assets), "revision": revision, "stylesheet": static("css/app.css"),
    })
    return HttpResponse(body, content_type="application/javascript", headers={
        "Service-Worker-Allowed": "/", "Cache-Control": "no-cache",
    })


@require_safe
def asset_links(request):
    links = []
    if settings.ANDROID_SHA256_FINGERPRINTS:
        links.append({
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {"namespace": "android_app", "package_name": settings.ANDROID_PACKAGE_ID,
                       "sha256_cert_fingerprints": settings.ANDROID_SHA256_FINGERPRINTS},
        })
    return JsonResponse(links, safe=False, headers={"Cache-Control": "public, max-age=300"})


@require_safe
def install_app(request):
    return render(request, "core/install_app.html", {"install_name": settings.PWA_NAME})
