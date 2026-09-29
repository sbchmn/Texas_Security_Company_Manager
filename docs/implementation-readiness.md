# Implementation readiness

## Decision

There is enough accepted discovery information to begin development of the platform
foundation and the first vertical slice. Development does not need to wait for every
future feature decision.

## Work that can begin now

1. repository conventions, automated checks, environment configuration, and secret
   handling;
2. production Docker image and Compose development topology using MySQL;
3. DigitalOcean App Platform component/configuration design;
4. organization-scoped data model, tenant isolation, entitlements, and audit events;
5. local identity foundation, role/scope authorization, and future OIDC connection
   points;
6. responsive PWA shell, semantic design system, accessibility baseline, and organization
   branding/custom-domain model;
7. personnel directory and secure document pipeline skeleton;
8. provider interfaces for object storage, email, SMS, and asynchronous jobs; and
9. automated unit, integration, tenant-isolation, accessibility, and container smoke
   tests from the first slice.

## Work that remains gated

The application shell and neutral data structures may begin, but the following cannot be
claimed complete until their governing artifacts are approved:

- Texas enforcement behavior requires the source-cited, effective-dated control matrix;
- document disposition requires the record-type retention matrix;
- payroll totals require approved overtime and rounding policy definitions;
- production recovery claims require a designed topology and restore evidence against
  RPO 5 minutes and RTO 1 hour;
- SSO release requires provider registrations and tested redirect/domain policies; and
- SMS production use requires consent, sender-registration, opt-out, and messaging-policy
  decisions for the selected provider and message category.

## Recommended first vertical slice

Deliver a thin, end-to-end foundation rather than building every module horizontally:

1. create an organization and its immutable slug;
2. configure and preview company name, logo, and accessible theme;
3. invite an owner/admin and enforce organization-scoped authorization;
4. create a branch and an officer/person record;
5. upload and malware-scan one custom document type through the storage abstraction;
6. record every mutation in the append-only audit stream;
7. render the experience responsively as an installable PWA; and
8. run the same application image under Compose and an App Platform-compatible
   configuration.

This slice proves tenancy, identity, branding, documents, auditability, portability, and
the UI foundation before regulatory and timekeeping complexity is layered on top.
