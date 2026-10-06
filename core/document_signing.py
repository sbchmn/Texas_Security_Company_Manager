"""DocuSeal submission lifecycle. Provider claims are re-read through the authenticated API."""

import base64
import hashlib
import json
import os
import re
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.validators import validate_email
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    AuditEvent, DocumentType, Membership, Notification, OnboardingItem, OnboardingTask,
    SignedArtifact, SigningRequest, SigningSettings, record_open_for,
)
from .services import DOCUMENT_MAX_BYTES, queue_notice, store_person_document


def canonical_origin(value):
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValidationError("The DocuSeal origin is invalid.") from exc
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise ValidationError("Use the HTTPS origin of your DocuSeal installation, without a path or credentials.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationError("The DocuSeal port is invalid.") from exc
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def allowed_origin(value):
    origin = canonical_origin(value)
    allowed = {canonical_origin(item) for item in settings.DOCUSEAL_ALLOWED_ORIGINS}
    if origin not in allowed:
        raise ValidationError("The operator must add this DocuSeal origin to DOCUSEAL_ALLOWED_ORIGINS first.")
    return origin


def _cipher():
    key = hashlib.sha256(("docuseal-api-key:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_api_key(value):
    return _cipher().encrypt(value.encode()).decode()


def decrypt_api_key(value):
    try:
        return _cipher().decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError) as exc:
        raise ValidationError("The signing API key cannot be decrypted. Re-enter it in Document signing settings.") from exc


def signing_config(organization):
    config = SigningSettings.objects.filter(organization=organization, enabled=True).first()
    if config is None:
        raise ValidationError("Document signing is not enabled for this company.")
    allowed_origin(config.base_url)
    return config


def docuseal_tls_verify():
    path = settings.DOCUSEAL_CA_BUNDLE
    if not path:
        return True
    if not os.path.isfile(path):
        raise ValidationError("DOCUSEAL_CA_BUNDLE does not name a readable certificate file in the web and worker containers.")
    return path


class SubmissionRejected(ValidationError):
    """A definitive POST rejection, unlike a timeout or unreadable creation response."""


class DocuSealClient:
    def __init__(self, config):
        self.base_url = allowed_origin(config.base_url)
        self.api_key = decrypt_api_key(config.encrypted_api_key)
        self.verify = docuseal_tls_verify()

    @staticmethod
    def _read(response, limit):
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            payload.extend(chunk)
            if len(payload) > limit:
                raise ValidationError("DocuSeal returned a response larger than the permitted limit.")
        return bytes(payload)

    def api(self, method, path, *, params=None, payload=None):
        try:
            with requests.request(
                method, self.base_url + "/api" + path, headers={"X-Auth-Token": self.api_key},
                params=params, json=payload, timeout=(5, 30), allow_redirects=False, stream=True,
                verify=self.verify,
            ) as response:
                if not 200 <= response.status_code < 300:
                    error = (
                        SubmissionRejected if method == "POST" and path == "/submissions"
                        and response.status_code in (400, 401, 403, 404, 422) else ValidationError
                    )
                    raise error(f"DocuSeal API returned HTTP {response.status_code}. Check the signing configuration.")
                raw = self._read(response, 2 * 1024 * 1024)
            return json.loads(raw)
        except requests.exceptions.SSLError as exc:
            raise ValidationError("DocuSeal certificate verification failed. Configure trusted certificates in the web and worker containers.") from exc
        except requests.RequestException as exc:
            raise ValidationError("DocuSeal could not be reached. Check status before attempting another send.") from exc
        except (ValueError, UnicodeError) as exc:
            raise ValidationError("DocuSeal returned an invalid JSON response.") from exc

    def template(self, template_id):
        data = self.api("GET", f"/templates/{template_id}")
        if not isinstance(data, dict) or data.get("id") != template_id:
            raise ValidationError("DocuSeal returned a different or invalid template.")
        return data

    def find_submitter(self, request):
        data = self.api("GET", "/submitters", params={"external_id": str(request.pk), "limit": 2})
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValidationError("DocuSeal returned an invalid submitter list.")
        if len(rows) > 1:
            raise ValidationError("Multiple DocuSeal submissions match this request. The office must investigate; nothing was filed.")
        return rows[0] if rows else None

    def pdf(self, url):
        # Document URLs never receive the API key, and redirects cannot leave the configured origin.
        try:
            for _ in range(4):
                try:
                    parts = urlsplit(url)
                except ValueError as exc:
                    raise ValidationError("DocuSeal returned an invalid file URL.") from exc
                if parts.scheme != "https":
                    raise ValidationError("Set DocuSeal's Settings > Account > App URL to the configured HTTPS signing origin.")
                origin = canonical_origin(f"{parts.scheme}://{parts.netloc}")
                if origin != self.base_url or parts.username or parts.password:
                    raise ValidationError("Signed files must be served from the configured DocuSeal origin.")
                with requests.get(url, timeout=(5, 30), allow_redirects=False, stream=True, verify=self.verify) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise ValidationError("DocuSeal returned a file redirect without a destination.")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise ValidationError(f"DocuSeal file download returned HTTP {response.status_code}.")
                    content = self._read(response, DOCUMENT_MAX_BYTES)
                    if not content.startswith(b"%PDF-"):
                        raise ValidationError("DocuSeal did not return a PDF. Nothing was filed.")
                    return content
            raise ValidationError("DocuSeal returned too many file redirects.")
        except requests.exceptions.SSLError as exc:
            raise ValidationError("Signed-file certificate verification failed. Configure trusted certificates in the web and worker containers.") from exc
        except requests.RequestException as exc:
            raise ValidationError("The signed PDF could not be downloaded. The request will be checked again.") from exc


def validate_template(template):
    submitters = template.get("submitters")
    fields = template.get("fields")
    schema = template.get("schema")
    if (template.get("archived_at") or not isinstance(submitters, list) or len(submitters) != 1
            or not isinstance(submitters[0], dict) or not submitters[0].get("uuid")
            or not submitters[0].get("name")):
        raise ValidationError("The MVP needs an active template with exactly one signer.")
    if not isinstance(fields, list) or not any(
        isinstance(field, dict) and isinstance(field.get("uuid"), str) and field["uuid"]
        and field.get("type") == "signature" and field.get("required") is not False
        and field.get("readonly") is not True
        and field.get("submitter_uuid") == submitters[0]["uuid"] for field in fields
    ):
        raise ValidationError("The template must require a signature from its signer.")
    if (not isinstance(schema, list) or not 1 <= len(schema) <= 20
            or any(not isinstance(row, dict) or not row.get("name") for row in schema)):
        raise ValidationError("The template must contain between one and twenty named documents.")
    return {
        "id": template["id"], "name": str(template.get("name") or "")[:255],
        "updated_at": template.get("updated_at"), "role": submitters[0]["name"],
        "documents": [str(row["name"]) for row in schema],
        "signature_fields": [
            field["uuid"] for field in fields if isinstance(field, dict) and field.get("uuid")
            and field.get("type") == "signature" and field.get("required") is not False
            and field.get("readonly") is not True
            and field.get("submitter_uuid") == submitters[0]["uuid"]
        ],
    }


def _assert_signer(row, request):
    if (not isinstance(row, dict) or row.get("external_id") != str(request.pk)
            or str(row.get("email") or "").casefold() != request.signer_email.casefold()
            or row.get("role") != request.template_snapshot["role"]
            or not isinstance(row.get("id"), int) or isinstance(row.get("id"), bool)
            or row["id"] <= 0
            or not isinstance(row.get("submission_id"), int) or isinstance(row.get("submission_id"), bool)
            or row["submission_id"] <= 0):
        raise ValidationError("DocuSeal signer identity does not match this request. Nothing was filed.")
    if request.submission_id and row["submission_id"] != request.submission_id:
        raise ValidationError("DocuSeal submission identity changed. Nothing was filed.")
    if request.submitter_id and row["id"] != request.submitter_id:
        raise ValidationError("DocuSeal submitter identity changed. Nothing was filed.")
    slug = row.get("slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", slug):
        raise ValidationError("DocuSeal returned an invalid signing link.")


def _queue_invitation(request):
    link = f"{request.base_url}/s/{request.signing_slug}"
    body = (f"Please sign {request.task.item.name} for {request.organization.display_name}.\n"
            f"{link}\nThe step completes after the signed document is filed in your personnel file.")
    key = f"onboarding-signing:{request.pk}"
    if request.task.person.user_id:
        queue_notice(
            organization=request.organization, recipients={request.task.person.user_id},
            subject_user_ids={request.task.person.user_id}, event_type="onboarding.signature_requested",
            subject="Document ready to sign", body=body, dedup_key=key, mandatory=True,
        )
        # Keep the email destination bound to the signer, without overriding channel rules.
        Notification.objects.filter(
            organization=request.organization, recipient_id=request.task.person.user_id,
            channel=Notification.Channel.EMAIL, deduplication_key=f"{key}:email", destination="",
        ).update(destination=request.signer_email)
    else:
        Notification.objects.get_or_create(
            organization=request.organization, destination=request.signer_email,
            channel=Notification.Channel.EMAIL, deduplication_key=f"{key}:email",
            defaults={"event_type": "onboarding.signature_requested", "subject": "Document ready to sign",
                      "body": body, "mandatory": True},
        )


def issue_signing_request(task, actor, *, return_url=None):
    if not Membership.objects.filter(
        organization=task.organization, user=actor, active=True,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.HR],
    ).exists():
        raise ValidationError("Only this company's personnel-record staff may issue signing requests.")
    config = signing_config(task.organization)
    if (task.item.kind != OnboardingItem.Kind.SIGNATURE or not task.item.active
            or not task.item.signing_template_id or not task.item.document_type_id):
        raise ValidationError("This step has no active signing template and personnel record type.")
    document_type = task.item.document_type
    if (document_type.organization_id != task.organization_id or not document_type.active
            or document_type.audience != DocumentType.Audience.PERSON
            or not record_open_for(document_type, task.person_id, Membership.Role.OFFICER, task.person_id)):
        raise ValidationError("The signed record must belong to this company and be readable by its signer.")
    email = (task.person.email or (task.person.user.email if task.person.user_id else "")).strip()
    validate_email(email)
    client = DocuSealClient(config)
    snapshot = validate_template(client.template(task.item.signing_template_id))
    with transaction.atomic():
        current_config = SigningSettings.objects.select_for_update().get(pk=config.pk)
        if (not current_config.enabled or current_config.base_url != config.base_url
                or current_config.encrypted_api_key != config.encrypted_api_key):
            raise ValidationError("The signing configuration changed. Reload the checklist before sending.")
        locked = OnboardingTask.objects.select_for_update().select_related("item", "person").get(pk=task.pk)
        if (locked.item.kind != OnboardingItem.Kind.SIGNATURE or not locked.item.active
                or locked.item.owner != OnboardingItem.Owner.PERSON
                or locked.item.signing_template_id != task.item.signing_template_id
                or locked.item.document_type_id != document_type.pk
                or locked.person.email != task.person.email):
            raise ValidationError("The signing step or signer changed. Reload the checklist before sending.")
        if locked.status != OnboardingTask.Status.OPEN:
            raise ValidationError("Only an outstanding step can be sent for signature.")
        previous = locked.signing_requests.order_by("-attempt").first()
        if previous and previous.status in (SigningRequest.Status.PREPARING, SigningRequest.Status.SENT):
            raise ValidationError("This step already has a signing request. Check its status instead of sending another.")
        request = SigningRequest.objects.create(
            organization=task.organization, task=locked, attempt=previous.attempt + 1 if previous else 1,
            document_type=document_type, template_id=task.item.signing_template_id,
            template_snapshot=snapshot, signer_email=email, signer_name=task.person.full_name[:255],
            base_url=client.base_url, issued_by=actor,
            # Reserve before POST; an ambiguous timeout must never cause an automatic second POST.
            next_check_at=timezone.now() + timedelta(minutes=1),
        )
        AuditEvent.objects.create(
            organization=task.organization, actor=actor, action="signing.requested",
            target_type="signing_request", target_id=str(request.pk),
            metadata={"task": str(task.pk), "template": request.template_id, "attempt": request.attempt},
        )
    try:
        data = client.api("POST", "/submissions", payload={
            "template_id": request.template_id, "send_email": False, "send_sms": False,
            "submitters": [{"name": request.signer_name, "email": email, "role": snapshot["role"],
                            "external_id": str(request.pk),
                            # Only a convenience hop back to TSCM; completion is still confirmed by
                            # reconciling against the DocuSeal API, never by the browser arriving.
                            **({"completed_redirect_url": return_url} if return_url else {})}],
        })
        if not isinstance(data, list) or len(data) != 1:
            raise ValidationError("DocuSeal returned an invalid creation response. Check status before sending again.")
        _assert_signer(data[0], request)
        with transaction.atomic():
            request = SigningRequest.objects.select_for_update().get(pk=request.pk)
            request.submission_id = data[0]["submission_id"]
            request.submitter_id = data[0]["id"]
            request.signing_slug = data[0]["slug"]
            request.status = SigningRequest.Status.SENT
            request.save()
            _queue_invitation(request)
        return request
    except SubmissionRejected as exc:
        SigningRequest.objects.filter(pk=request.pk).update(status=SigningRequest.Status.REJECTED)
        _record_error(request.pk, exc)
        raise
    except ValidationError as exc:
        _record_error(request.pk, exc)
        raise
    except DatabaseError as exc:
        error = ValidationError("The submission response could not be saved. Check signing status; do not send another request.")
        _record_error(request.pk, error)
        raise error from exc


def _record_error(request_id, exc):
    with transaction.atomic():
        request = SigningRequest.objects.select_for_update().get(pk=request_id)
        request.failures += 1
        request.last_error = " ".join(exc.messages)[:500]
        request.last_checked_at = timezone.now()
        request.next_check_at = timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** min(request.failures, 6)))
        request.save(update_fields=["failures", "last_error", "last_checked_at", "next_check_at"])


def reconcile_signing_request(request_id):
    created_files = []
    try:
        with transaction.atomic():
            request = SigningRequest.objects.select_for_update().select_related(
                "organization", "task__person__user", "task__item", "document_type",
            ).get(pk=request_id)
            if request.status in (
                SigningRequest.Status.COMPLETED, SigningRequest.Status.DECLINED, SigningRequest.Status.REJECTED,
            ):
                return request
            config = signing_config(request.organization)
            if config.base_url != request.base_url:
                raise ValidationError("The signing backend changed since this request was issued.")
            client = DocuSealClient(config)
            row = client.find_submitter(request)
            if row is None:
                raise ValidationError("DocuSeal has not confirmed submission creation. Review this request in DocuSeal; do not send a duplicate.")
            _assert_signer(row, request)
            submission = client.api("GET", f"/submissions/{row['submission_id']}", params={"include": "fields"})
            if (not isinstance(submission, dict) or submission.get("id") != row["submission_id"]
                    or not isinstance(submission.get("template"), dict)
                    or submission["template"].get("id") != request.template_id):
                raise ValidationError("DocuSeal returned a different template or submission.")
            signers = submission.get("submitters")
            if not isinstance(signers, list) or len(signers) != 1:
                raise ValidationError("The submission no longer has exactly one signer.")
            _assert_signer(signers[0], request)
            if signers[0]["id"] != row["id"]:
                raise ValidationError("DocuSeal returned inconsistent signer identities.")
            request.submission_id = row["submission_id"]
            request.submitter_id = row["id"]
            request.signing_slug = row["slug"]
            status = submission.get("status")
            if status in ("declined", "expired") or signers[0].get("declined_at"):
                request.status = SigningRequest.Status.DECLINED
                AuditEvent.objects.create(
                    organization=request.organization, actor=None, action="signing.declined",
                    target_type="signing_request", target_id=str(request.pk), metadata={"task": str(request.task_id)},
                )
            elif status == "completed":
                _file_completed(request, submission, client, created_files)
            elif status in ("pending", "awaiting"):
                request.status = SigningRequest.Status.SENT
                _queue_invitation(request)
            else:
                raise ValidationError("DocuSeal returned an unknown submission status.")
            request.last_checked_at = timezone.now()
            request.next_check_at = timezone.now() + timedelta(minutes=1)
            request.failures = 0
            request.last_error = ""
            request.save()
            return request
    except ValidationError as exc:
        for document in created_files:
            document.file.delete(save=False)
        _record_error(request_id, exc)
        raise
    except (DatabaseError, OSError) as exc:
        for document in created_files:
            document.file.delete(save=False)
        error = ValidationError("Signed records could not be committed to the personnel file. The request will be checked again.")
        _record_error(request_id, error)
        raise error from exc


def _file_completed(request, submission, client, created_files):
    signer = submission["submitters"][0]
    completed = submission.get("completed_at")
    try:
        signed_at = parse_datetime(completed) if isinstance(completed, str) else None
        signer_completed = signer.get("completed_at")
        signer_signed_at = parse_datetime(signer_completed) if isinstance(signer_completed, str) else None
    except ValueError as exc:
        raise ValidationError("DocuSeal returned an invalid completion timestamp.") from exc
    if (not signed_at or timezone.is_naive(signed_at) or signer.get("status") != "completed"
            or not signer_signed_at or timezone.is_naive(signer_signed_at) or signer_signed_at != signed_at):
        raise ValidationError("DocuSeal has not confirmed completion by the expected signer.")
    signed_fields = signer.get("fields")
    expected_fields = set(request.template_snapshot["signature_fields"])
    if (not expected_fields or not isinstance(signed_fields, list)
            or not expected_fields.issubset({
                row.get("uuid") for row in signed_fields if isinstance(row, dict)
                and isinstance(row.get("uuid"), str) and isinstance(row.get("value"), str) and row["value"]
            })):
        raise ValidationError("DocuSeal has not supplied the required signature evidence.")
    documents = submission.get("documents")
    if (not isinstance(documents, list) or len(documents) != len(request.template_snapshot["documents"])
            or any(not isinstance(row, dict) or not isinstance(row.get("url"), str) for row in documents)
            or sorted(str(row.get("name") or "").removesuffix(".pdf") for row in documents)
            != sorted(name.removesuffix(".pdf") for name in request.template_snapshot["documents"])):
        raise ValidationError("The signed documents do not match the issued template.")
    audit_url = submission.get("audit_log_url")
    if not isinstance(audit_url, str) or not audit_url:
        raise ValidationError("DocuSeal has not supplied its completion audit PDF yet.")
    artifacts = [(f"document:{index}", row["name"], row["url"]) for index, row in enumerate(documents)]
    artifacts.append(("audit", "Signing audit certificate", audit_url))
    for key, name, url in artifacts:
        if request.artifacts.filter(key=key).exists():
            continue
        payload = client.pdf(url)
        filename = f"signed-{request.pk}-{key.replace(':', '-')}.pdf"
        upload = SimpleUploadedFile(filename, payload, content_type="application/pdf")
        document = store_person_document(
            organization=request.organization, person=request.task.person,
            document_type=request.document_type, upload=upload, actor=request.issued_by,
        )
        created_files.append(document)
        SignedArtifact.objects.create(request=request, document=document, key=key, name=str(name)[:255])
    request.status = SigningRequest.Status.COMPLETED
    request.completed_at = signed_at
    task = OnboardingTask.objects.select_for_update().get(pk=request.task_id)
    if task.status == OnboardingTask.Status.OPEN:
        task.status = OnboardingTask.Status.DONE
        task.decided_by = None
        task.decided_at = timezone.now()
        task.note = f"Signed by {request.signer_name}; verified DocuSeal submission {request.submission_id}, filed with its audit certificate."
        task.save(update_fields=["status", "decided_by", "decided_at", "note", "updated_at"])
    AuditEvent.objects.create(
        organization=request.organization, actor=None, action="signing.completed",
        target_type="signing_request", target_id=str(request.pk),
        metadata={"task": str(task.pk), "submission": request.submission_id,
                  "signed_at": signed_at.isoformat(), "template": request.template_snapshot,
                  "artifacts": [{"document": str(item.document_id), "sha256": item.document.sha256,
                                 "key": item.key} for item in request.artifacts.select_related("document")]},
    )
    if task.person.user_id:
        queue_notice(
            organization=request.organization, recipients={task.person.user_id},
            event_type="onboarding.signed", subject=f"{task.item.name} — signed and filed",
            body="Your signed document and its signing audit certificate are now in your personnel file.",
            dedup_key=f"onboarding-signed:{request.pk}",
            subject_user_ids={task.person.user_id},
        )


def signing_evidence(task):
    request = task.signing_requests.order_by("-attempt").first()
    if request is None:
        return {"state": "not sent for signature", "detail": "the office must send this step", "satisfied": False}
    filed = request.status == SigningRequest.Status.COMPLETED and request.artifacts.filter(
        key__startswith="document:", document__deleted_at__isnull=True, document__archived_at__isnull=True,
    ).count() == len(request.template_snapshot["documents"])
    return {"state": request.get_status_display(), "detail": request.last_error, "satisfied": filed}
