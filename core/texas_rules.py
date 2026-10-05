"""The Texas obligations the control matrix has had no content for, drafted for approval.

**Why this file exists.** CMP-0 (2026-10-01) built a matrix that can hold *any* duty — jurisdiction,
authority URL and reference, interpretation, effective dates, renewal window, reminder ladder, approval
by a named actor — and CMP-2 put a version behind every one of those values. What the product has never
had is the content: an owner opening `/settings/compliance` in Texas today faces an empty table and has
to know, unprompted, that §1702.124(c) sets $100,000 / $50,000 / $200,000 rather than the $1M figure
every broker and client contract throws around. That is the largest piece of avoidable friction left in
the product, and it is avoidable because the obligations are the same for every Texas licensee.

**What this is not.** These rows are *drafts*. Every one is written with `approved_by` and `approved_at`
left empty, so none of it is enforced or measured until a named person in the company approves it — the
owner's ruling of 2026-10-03 (roadmap §8, DD §Compliance): "nothing becomes active until the owner
approves that row". `ComplianceRule.unevaluated_reason` reports an unapproved rule as *"not approved, so
not enforced yet"*, so an installation that runs this seed and never reviews it gains a visible list and
changes nothing about how posts are assigned or how the clock behaves. That is the whole safety property,
and it is why a drafted obligation is reviewable work rather than encoded law.

**Verification trail.** Each `authority_reference` below is quoted from the primary text, read on
2026-10-04 from the URLs on the rows themselves. The numbers were *not* taken from this project's
earlier research pass, and one of them corrected it: the roadmap recorded "the CGL limits in §1702.124"
alongside the market figure of $1M/$2M that clients and brokers demand, and §1702.124(c) actually says
$100,000 / $50,000 / $200,000. Where the roadmap's citation was right it is kept — §1702.302(a) really
is the sentence that makes an expired registration a hard stop, and 37 TAC §35.22(b) repeats it for the
renewal path. Where it was loose it is corrected: 37 TAC §35.8 is "Consumer Information and Signage",
not a posting duty, and the posting duty is §1702.128.

Statutes and rules change. Nothing here should be relied on as legal advice; the interpretation field on
each row is a *proposed* reading for the owner to approve, edit, or reject, and independent legal review
remains a production gate (feature-status.md §Production acceptance gates).
"""
from .models import ComplianceRule, DocumentType

# ── Record types the drafted duties need as evidence ───────────────────────────────
# Seeded with the rules rather than assumed to exist, because a filed-record duty that names no record
# type reports "no record type named as the evidence" and stays unmeasured forever. Two of the five
# drafted duties can be measured the moment they are approved, and only if their evidence type exists.
# Retention is left blank — permanent — because neither of these is the kind of record a firm shortens
# on a schedule, and the compliance settings page edits the window per type the same way it edits every
# other one.
DOCUMENT_TYPES = (
    {"code": "gl_certificate", "name": "Certificate of general liability insurance",
     "audience": DocumentType.Audience.MANAGEMENT,
     "help": "Filed against the company, not against a person: §1702.124 binds the licensee. Keep the "
             "countersignature page, not just the declarations."},
    {"code": "firearm_proficiency", "name": "Firearm proficiency certificate",
     "audience": DocumentType.Audience.PERSON,
     "help": "File it with an expiry date 90 days after the issue date — DPS will not accept an older "
             "certificate at renewal, and the duty's warning window reads that field."},
)

# ── The duties ─────────────────────────────────────────────────────────────────────
# Personnel-category codes from `models.PERSONNEL_CATEGORIES`; a rule bound to no category is
# unenforceable by design (`unevaluated_reason` says "no personnel category is bound to it"), so every
# people-shaped duty here names the categories the obligation actually falls on.
ALL_OFFICERS = ["unarmed", "commissioned", "ppo"]
ARMED_ONLY = ["commissioned"]

DUTIES = (
    {
        "code": "tx_gl_insurance",
        "name": "General liability insurance at the statutory minimum",
        "evidence": ComplianceRule.Evidence.DOCUMENT,
        "subject": ComplianceRule.Subject.ORGANIZATION,
        "document_type": "gl_certificate",
        "applies_to": [],
        "warning_days": 30,
        "reminder_days_before": [60, 30, 14],
        "authority_url": "https://texas.public.law/statutes/tex._occ._code_section_1702.124",
        "authority_reference": "Tex. Occ. Code § 1702.124(a)-(c), (e)-(f)",
        "interpretation": (
            "Proposed reading, from the statute text as read 2026-10-04.\n\n"
            "§1702.124(c): the policy \"must contain minimum limits of: (1) $100,000 for each occurrence "
            "for bodily injury and property damage; (2) $50,000 for each occurrence for personal injury; "
            "and (3) a total aggregate amount of $200,000 for all occurrences.\"\n\n"
            "(a) requires a certificate of insurance or other documentary evidence of a general liability "
            "policy *countersigned by an insurance agent licensed in this state*, or surplus-lines "
            "coverage under Insurance Code ch. 981. (b) requires it to pay on behalf of the company "
            "license holder for bodily injury, property damage, or personal injury caused by an event "
            "involving the principal or an officer, agent, or employee, in the conduct of licensed "
            "activity. (e) keeps a filed certificate in effect until the insurer gives the department at "
            "least 10 days' notice of intent to terminate. (f) adds that the licensee must also maintain "
            "coverage \"sufficient to cover all of the business activities ... related to private "
            "security.\"\n\n"
            "Two things the office should decide, not inherit from this row. First, these are the "
            "**statutory floor**, and a client contract or a government bid usually demands more "
            "(commonly $1M per occurrence / $2M aggregate) — the number to file here is the one on the "
            "certificate actually held. Second, the duty is measured by whether a current certificate is "
            "filed against the company, so give the record an expiry date at the policy's renewal date; "
            "with none, this row can only ever say \"a certificate exists\"."),
    },
    {
        "code": "tx_license_posted",
        "name": "Company license posted at the office and every branch",
        "evidence": ComplianceRule.Evidence.POSTING,
        "subject": ComplianceRule.Subject.SITES,
        "document_type": None,
        "applies_to": [],
        "warning_days": 0,
        "reminder_days_before": [],
        "authority_url": "https://texas.public.law/statutes/tex._occ._code_section_1702.128",
        "authority_reference": "Tex. Occ. Code § 1702.128",
        "interpretation": (
            "Proposed reading, from the statute text as read 2026-10-04.\n\n"
            "§1702.128: \"A company license holder shall at all times post the person's license in a "
            "conspicuous place in: (1) the principal place of business of the company license holder; and "
            "(2) each branch office of the company license holder.\"\n\n"
            "This row will report **Entered, not measured** even after approval, and that is the honest "
            "answer rather than a gap in the seed: the duty attaches to a *place*, and the personnel "
            "register has no site column to file evidence against, so \"no posting record found\" cannot "
            "be distinguished from \"every office is compliant\". It belongs on the board a manager "
            "physically walks. Recording it anyway is the point — an obligation that is invisible in the "
            "matrix is one nobody is ever asked about."),
    },
    {
        "code": "tx_consumer_signage",
        "name": "Consumer notice, complaint signage, and license number on marked vehicles",
        "evidence": ComplianceRule.Evidence.POSTING,
        "subject": ComplianceRule.Subject.SITES,
        "document_type": None,
        "applies_to": [],
        "warning_days": 0,
        "reminder_days_before": [],
        "authority_url": "https://www.law.cornell.edu/regulations/texas/37-Tex-Admin-Code-SS-35-8",
        "authority_reference": "37 TAC § 35.8(a)-(e) (adopted eff. 2014-05-06, 39 TexReg 3606; amended "
                               "eff. 2022-01-10, 47 TexReg 31)",
        "interpretation": (
            "Proposed reading, from the rule text as read 2026-10-04. The rule is captioned \"Consumer "
            "Information and Signage\" — it is not an insurance rule, which the roadmap's earlier note "
            "loosely implied by pairing it with §1702.128.\n\n"
            "(a) a licensee must notify all clients or recipients of services, orally or in writing, of "
            "the license number and the mailing address, telephone number, and email address of the "
            "department's Regulatory Services Division, for the purpose of directing complaints. (b) if "
            "given in writing, the notice must use \"a type face of the same size as that which appears in "
            "the document as a whole but in no case less than ten (10) point font\". (c) a sign with the "
            "Regulatory Services Division's name, mailing address, telephone number and email address, and "
            "a statement that complaints may be directed there, must be displayed conspicuously at the "
            "principal place of business and in any branch office. (d) the license number must appear on "
            "any vehicle displaying the company name, \"at least one (1) inch high and permanently affixed "
            "or magnetically attached to each side of the vehicle in a color contrasting with the "
            "background color\". (e) no conduct that causes reasonable confusion about the services or "
            "the charges.\n\n"
            "Three separate acts in one row, and the office should split it if it wants to track them "
            "apart: a client-facing notice (which *can* be filed as a record once the register has a "
            "client dimension), signage at each office, and vehicle marking. Like the posting duty, this "
            "reports Entered, not measured — for the same reason, and for the same honest one."),
    },
    {
        "code": "tx_registration_current",
        "name": "No regulated services on an expired personal registration",
        "evidence": ComplianceRule.Evidence.CREDENTIAL,
        "subject": ComplianceRule.Subject.PEOPLE,
        "document_type": None,
        "applies_to": ALL_OFFICERS,
        "warning_days": 180,
        "reminder_days_before": [90, 30],
        "authority_url": "https://law.justia.com/codes/texas/occupations-code/title-10/chapter-1702/"
                         "subchapter-m/section-1702-302/",
        "authority_reference": "Tex. Occ. Code § 1702.302(a)-(e); 37 TAC § 35.22(b)",
        "interpretation": (
            "Proposed reading, from the statute and rule text as read 2026-10-04.\n\n"
            "§1702.302(a): a person otherwise eligible may renew an unexpired license by paying the fee "
            "before the expiration date, and \"A person whose license has expired may not engage in "
            "activities that require a license until the license has been renewed.\" 37 TAC § 35.22(b) "
            "says it from the renewal side: a complete renewal application must be received *before* "
            "expiration for the registration to remain in effect pending approval, and if it is not, "
            "\"no regulated services may be performed until a complete renewal application is submitted\".\n\n"
            "The ladder is why the reminder lead times below are what they are. DPS accepts an online "
            "renewal from **180 days** before expiration (dps.texas.gov, Individual license questions, "
            "read 2026-10-04), and the statute's own late ladder starts the moment the card expires: "
            "§1702.302(b) 1-1/2 times the renewal fee at 90 days or less expired, (c) two times beyond "
            "90 but under a year, and (d) **no renewal at all** at one year or more — the person must "
            "qualify for an original license again, \"including the examination requirements\". (e) has "
            "the department itself sending notice no later than the 30th day before expiration, so a firm "
            "that only acts on DPS's letter is already inside the window where the officer's own "
            "responsibility started.\n\n"
            "This is the one drafted duty the product can *stop a clock-in* on — and it does not do so "
            "from this row. Enforcement of an expired credential lives on a `CredentialType` marked "
            "`blocks_clock_in`, which the office creates deliberately; this row exists so the obligation "
            "is on the matrix with its authority beside it. The reason it is not seeded as a credential "
            "requirement is recorded in the roadmap §8 note on this seed: the clock and schedule gates "
            "read `blocks_clock_in` and `active` but **not** `is_approved`, so a seeded credential type "
            "would enforce an obligation nobody had approved yet. Approving this row should be the moment "
            "the office creates that requirement."),
    },
    {
        "code": "tx_firearm_proficiency",
        "name": "Firearm proficiency certificate current at commission renewal",
        "evidence": ComplianceRule.Evidence.DOCUMENT,
        "subject": ComplianceRule.Subject.PEOPLE,
        "document_type": "firearm_proficiency",
        "applies_to": ARMED_ONLY,
        "warning_days": 30,
        "reminder_days_before": [30],
        "authority_url": "https://www.dps.texas.gov/section/private-security/faq/individual-license-questions",
        "authority_reference": "Tex. DPS, Individual license questions, Q5 (read 2026-10-04)",
        "interpretation": (
            "Proposed reading, from the department's own published procedure as read 2026-10-04.\n\n"
            "\"In order to renew, Commission Security Officers must submit a firearm proficiency "
            "certificate that is no more than 90 days old.\"\n\n"
            "This is a **departmental procedure page, not the statute or the rule**, and it is labelled "
            "as such on the row: cite it to a person who asks why an officer cannot renew, but do not "
            "quote it as §1702 text. Its practical shape is the reason it is worth a row — a firm that "
            "starts the renewal 180 days out as the registration row suggests, and only then schedules "
            "the qualification, will arrive with a certificate older than 90 days and be turned away. "
            "File each certificate with an expiry date 90 days after its issue date and this duty warns "
            "and goes missing on that clock.\n\n"
            "Bound to commissioned/armed officers only. If the company's own practice requires "
            "re-qualification for unarmed officers who carry a company weapon on some posts, say so here "
            "and bind the extra category rather than loosening this row."),
    },
)


def drafted_codes():
    """The codes this module owns, so a caller can tell a draft from a hand-entered duty."""
    return [row["code"] for row in DUTIES]


def missing_texas_duties(organization):
    """The drafted duties this company does not have a row for yet.

    A view cannot offer "add the Texas obligations" honestly from a count of rows it holds, because a
    firm that entered the general liability duty by hand under a different code *does* have that
    obligation — adding a second row for it would put two answers to "is our certificate current" on one
    matrix, and the queue would then be describing a set nobody chose. So the offer is per missing code,
    and an already-covered duty simply is not offered.
    """
    present = set(organization.compliance_rules.filter(code__in=drafted_codes())
                  .values_list("code", flat=True))
    return [row for row in DUTIES if row["code"] not in present]


def seed_texas_obligations(organization, actor=None, *, dry_run=False):
    """Write the drafted Texas duties as **unapproved** rows, and their evidence types.

    Idempotent by code, because a firm that runs this twice, or that edited a draft and then runs it
    again, must not end up with two obligations of the same name answering "who is missing this" two
    different ways. An existing row is left completely alone — a draft somebody has started editing, or
    an owner-approved obligation with a version history behind it, is that company's record, and a
    re-seed silently overwriting it would be worse than never offering the seed at all.

    Nothing here sets `approved_by` / `approved_at`, and nothing here touches a `CredentialType`. Both
    omissions are the safety property described at the top of this file.
    """
    created_types, created_rules = [], []
    for spec in DOCUMENT_TYPES:
        document_type, made = DocumentType.objects.get_or_create(
            organization=organization, code=spec["code"],
            defaults={"name": spec["name"], "audience": spec["audience"]})
        if made:
            created_types.append(document_type)
    for spec in DUTIES:
        if ComplianceRule.objects.filter(organization=organization, code=spec["code"]).exists():
            continue
        if dry_run:
            created_rules.append(spec["name"])
            continue
        document_type = None
        if spec["document_type"]:
            document_type = DocumentType.objects.filter(organization=organization,
                code=spec["document_type"]).first()
        rule = ComplianceRule.objects.create(organization=organization, name=spec["name"],
            code=spec["code"], evidence=spec["evidence"], applies_to_subject=spec["subject"],
            applies_to=list(spec["applies_to"]), document_type=document_type,
            warning_days=spec["warning_days"], reminder_days_before=list(spec["reminder_days_before"]),
            jurisdiction="Texas", authority_url=spec["authority_url"],
            authority_reference=spec["authority_reference"], interpretation=spec["interpretation"],
            active=True)
        created_rules.append(rule.name)
        # Version 1 is written at creation, the same call the hand-entered path makes, so the first
        # approval is version 2 with something behind it to differ from rather than a change with no
        # prior state.
        from .models import RuleRevision
        from .services import record_rule_revision
        record_rule_revision(rule, RuleRevision.Kind.COMPLIANCE_RULE, actor)
    if created_rules and not dry_run:
        from .models import AuditEvent
        AuditEvent.objects.create(organization=organization, actor=actor,
            action="compliance_rules.drafted", target_type="organization", target_id=str(organization.pk),
            metadata={"jurisdiction": "Texas", "rules": created_rules,
                      "record_types": [item.code for item in created_types],
                      "approved": False,
                      "note": "drafted from primary text, unapproved and unenforced until each row is "
                              "approved by a named person"})
    return {"rules": created_rules, "record_types": [item.code for item in created_types],
            "dry_run": dry_run}
