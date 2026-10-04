# Feature implementation status

**Status date:** 2026-10-03 (text consent and provider callbacks, a shared clock station that knows who
is standing at it, a retention setting the audit chain can obey, and — same day, later — who hears which
notice on which channel, an unanswered reminder rung reaching somebody new, and a location reading that
cannot be true)

## Who hears what, and what happens when nobody answers (2026-10-03, later)

**NTF-1 and NTF-2.** DD §Email, text messaging, and templates asks for "multiple reminder levels, lead
times, repeat intervals, **delivery channels, and escalations**". NTF-4 built the channels' permission
and NTF-3 built the events; what was missing was the two words that decide whether any of it reaches a
person — which channel a given audience is addressed on, and who is told when the first audience did
not act. Both landed together because they compose: NTF-2 decides *who else* hears an unanswered rung,
NTF-1 decides *on what channel* that person hears it, and neither needed a second settings object.

- **`ChannelRule` selects; it does not permit.** The whole design rests on that split, and it is tested
  rather than asserted in prose: a rule that puts SMS on `credential.*` for the officer the notice is
  about still produces a `BLOCKED` row with **zero attempts spent** when that number never opted in,
  because `send_block_reason` runs first and outranks every rule. Consent belongs to the person and
  their phone; channel preference belongs to the company; neither gets to overrule the other. In-app is
  added back whatever the rule says — the notification centre is the durable copy of *we told you*, and
  a text a carrier dropped leaves no evidence at all.
- **Audience is who a recipient is *for this notice*, not their job title.** A recipient resolves to
  exactly one of six audiences, and being the person the notice is about is checked before the role map
  and wins. The case that forced it: a site supervisor whose own registration is lapsing is one of the
  "managers" the ladder tells, and a rule that texts managers would text them in the voice of somebody
  chasing a colleague. Call sites name the subject with `subject_user_ids=`, which is optional — so a
  company that configures nothing gets exactly the channels each call site always asked for.
- **Narrowest voice wins, and there is nothing subtler.** An exact `event_type` beats a family rule; the
  unique constraint on `(organization, audience, family, event_type)` makes it arithmetically impossible
  for two rules to claim one notice, and `event_type` is an empty string rather than NULL so MySQL
  actually enforces it — unlike the conditional uniques this schema skips (W036), where app code has to
  check its own collision. Measured on MySQL 8.4 rather than argued from the migration: a second
  identical rule raises `1062 Duplicate entry`, a rule narrowed to `credential.escalated` beside its own
  family rule does not, `Punch.risk_flags` round-trips its code list on a table that carries a BEFORE
  INSERT tenant trigger, and the 0053 consent guard still answers `1644 cross-tenant message consent
  reference` with all 50 triggers installed after 0054. The leg that sqlite cannot see. Families are a fixed choice list read from the `event_type` prefixes the code
  already writes, because a free-text field in a channel rule means a notice nobody ever receives,
  discovered only when somebody says they were not told.
- **The reach column is what makes the page honest.** `audience_reach` answers from the same consent
  ledger the send path consults: "text the person the notice is about about `credential.*` — 1 of that
  audience has given consent". Newest answer per destination wins, in two queries rather than one per
  officer, and a blocked number is not reachable however recently it opted in.
- **Escalation hangs on the requirement row, beside the ladder it extends** — `escalate_after_days` plus
  `escalate_to`, exactly where the roadmap said rather than as a notification-settings object, because a
  duty whose warning and escalation are configured in two places drifts, and a drifted pair reads to an
  inspector as a company that warned nobody. The miss is measured by **the state of the record, not by
  whether the notice was opened**: `Notification.Status.READ` exists only for in-app rows, so a
  read-based rule would escalate an officer who never saw a text and stay silent about one who marked
  the app row read and did nothing. The ladder firing at all is the proof the earlier rungs changed
  nothing — a renewal recorded at 90 days means the 30-day rung never becomes a missed one.
- **Escalation means *somebody new*, not everybody again.** The escalation audience is subtracted from
  the recipients already addressed, and the notice goes out under its own `credential.escalated` event
  type with its own dedup key. That subtraction is not a courtesy: the first fixture built to test this
  escalated to a scheduler and produced **nothing**, because a scheduler with no authority scopes
  manages the whole company and was already in the reminder audience — an escalation onto people who
  were always told is a duplicate wearing a new label, and `test_a_manager_who_already_stands_in_the_
  audience_is_not_told_the_same_thing_twice` now pins that fact rather than the fixture's assumption.
  The escalated body names the rungs that passed, because an escalation that does not say it is an
  escalation is a second copy of a reminder somebody already read.

## What a location reading cannot be (2026-10-03, later)

**CLK-4.** DD §Sites, geofences, clocks lists "mock-location/spoofing-risk signals" among the supported
evidence options. The honest version of that, from a browser, is implausibility: no web API exposes
Android's `isFromMockProvider`, so nothing here can prove a phone lied. Three things can be *measured*,
each with a control case, and the result routes a punch to a human instead of rejecting it.

- **Three signals, all independent, all reported as numbers.** `implausible_accuracy` — a fix claimed
  good to 2 m or less, which a phone does not produce on its own and every mock provider in circulation
  reports because it does not bother filling the radius in. `stale_fix` — a location captured more than
  five minutes before the clock event it accompanies, **online punches only**: an offline reading is old
  by design (that is CLK-5), and applying this test to it would write up every post without a signal.
  `impossible_travel` — the distance from the officer's previous located punch divided by the elapsed
  time exceeding 200 km/h, floored at 25 km so GPS jitter at one post cannot trip it. The floor and the
  ceiling together are what keeps a legitimate double-header out of review, and both numbers are printed
  in the exception sentence so a reviewer can disagree with a measurement rather than a vibe.
- **Measured always, raised only if the rule says so.** The signals are computed for every punch that
  carries coordinates whatever the policy, and stored in `Punch.risk_flags`; the resolved rule decides
  only whether a human is bothered now. So a company that turns the switch off still has the reading,
  and the audit event records `flagged: false` beside it — the difference between a policy decision and
  a missed detection, which is the difference an insurer asks about.
- **The clock always takes the time.** Nothing here raises. An implausible reading makes the punch an
  exception, exactly as a missing location does, because a guard at a gate with a suspicious phone still
  worked the tour and still has to be paid for it.
- **Absence is not suspicion.** `_location_claims` reads the payload for all three clock paths (device,
  offline queue, shared station) and keeps a missing accuracy `None` rather than `0` — a page that never
  sent the field must not turn every officer into a suspect. The verdict code is stored on the row and
  the measurements beside the event that judged them, which is the `evidence=` extension point CLK-2
  left: no artefact column on the raw punch, one queryable decision column, provenance in the chain.
- **One field, three answers, like the two above it.** `flag_spoof_risk` resolves per level through
  company → contract → site and joined `RULE_WATCHED` on both sides the same day, so the value is
  bumpable *and* storable — the PAY-5 lesson about a watched-but-unstored field, applied before it could
  recur here. The override's copy is a tri-state select, and the form test asserts `""`/`yes`/`no` stay
  `None`/`True`/`False` because an explicit No that renders as Inherit is how a waiver becomes two.

**What these three cannot prove.** Nothing routes a message that no rule exists for, and no rule is
seeded — a company that never opens the page keeps in-app plus email forever, which is the old behaviour
rather than a new default. Reach counts numbers on file, not whether a device can receive them. The
escalation only fires on the `CredentialType` ladder: `ComplianceRule` rows and training records still
warn with no escalation, and a `credential.missing` notice has no earlier rung to have missed. CLK-4
cannot detect a well-chosen coordinate a kilometre from the post, a client that omits or fakes its fix
timestamp (that test simply does not run), or anything at all from a browser that refuses location —
and a shared station's tablet is exactly as forgeable as an officer's phone, which the PIN limits to
*who* pressed the button, not *where* they were standing.



## Consent that belongs to a number, and the callbacks that end a send path (2026-10-03)

**NTF-4.** DD §Email, text messaging, and templates requires that "delivery, bounce, complaint,
unsubscribe, and suppression events are **processed and retained**" and that mandatory operational
messages are "categorised separately from optional" ones; RB §Email and SMS providers adds the sentence
this slice is built around — *"Do not assume email unsubscribe rules and mandatory operational SMS rules
are identical."* Nothing ingested a provider callback before today and SMS had no consent behind it,
which is why the roadmap put NTF-4 ahead of channel selection: an SMS adapter with no consent is a
liability with an API key attached.

- **`MessageConsent` is a ledger, not a column.** Every decision is a row and the newest wins, for two
  reasons a flag cannot serve. Consent attaches to the **number**: a carrier reissues a phone and the new
  holder's texts would otherwise be authorised by whoever had it last — so changing an officer's number
  is a new question rather than a setting that follows them, and `Person` gained no field at all. And an
  opt-out that *overwrites* its opt-in destroys the pair of facts a complaint defence is made of: asked,
  agreed, later withdrew. `wording` holds the disclosure as shown, because a recorded "yes" to a sentence
  nobody can reproduce is not evidence of anything.
- **Captured where the account actually begins.** `ACCOUNT_SIGNUP_ENABLED = False`, so "sign-up" in this
  product is `invitation_accept` — which now carries an optional number and tick and writes consent with
  `source=signup`, the surface and IP in `evidence`. A number with no tick stores the number and writes
  **no row**: silence is not a refusal, and inventing a "no" would make a later real "yes" read as the
  reversal of something the person never declined. First sign-in asks once, on the dashboard, with the
  number prefilled and two buttons, and answering *no* is itself a recorded decision — which is what
  stops the prompt returning. `/notifications/text/` lets the officer change it without anyone in an
  office.
- **The gate sits in the send path, and it is asymmetric on purpose.** `send_block_reason` is the one
  function both the delivery code and the tests read. **Text** needs a current grant and a STOP ends it
  for everything, a pay stub included: "it was only operational" is no defence for a number that never
  opted in. **Email** treats an unsubscribe as a rule about *optional* mail, so a required notice still
  goes (CAN-SPAM's transactional carve-out) while a dead mailbox or an administrator's block stops even
  those, being facts about delivery rather than wishes. `Notification.mandatory` is the category —
  inferred from the event family when a caller says nothing, overridable when it does, so a notice queued
  by future code cannot be born uncategorised.
- **A refusal is not a failure.** A blocked notice gets `Status.BLOCKED`, spends **no retry**, and writes
  `message.blocked`; it shows in the messaging page as "not sent — consent or suppression" with its
  reason, and `process_notifications` counts `blocked=` apart from `failed=` because mixing them makes a
  healthy queue look ill and buries the one number an operator would act on.
- **Callbacks are retained whether or not they are understood.** `normalize_provider_callback` reduces
  Twilio's form-encoded statuses, SNS's JSON-inside-JSON envelope, Postmark's records and Mailjet's event
  arrays to destination + kind + why + provider time. An unmapped event becomes a `STATUS` row carrying
  the raw name as detail with `applied=False` — dropping it loses a fact the provider reported, and
  mapping it to a guess could block a reachable officer because somebody's payload shape changed. A
  bounce suppresses the *next* send and never rewrites the notice that bounced: that message was sent,
  and a history edited to say otherwise is worth less than an inconvenient one.
- **`STOP` works with no login, no screen, and no support ticket** — answered back on the same channel in
  TwiML. The part that is easy to get wrong in the friendly direction is `START`: a number with no prior
  grant is *refused* ("ask your supervisor"), because a stranger texting a company's number is not consent
  to text them back. Re-in statement needs a previous grant for that destination, and lifting the block is
  the officer's own act.
- **Two kinds of "verified", kept apart in the data.** Twilio signs, and when `TWILIO_AUTH_TOKEN` is
  configured an unsigned callback is refused with nothing written — asserted by counting rows, not by
  trusting a status code. SNS and Mailjet document nothing equivalent on this path, so their posts are
  accepted on the tenant's unguessable `webhook_token` plus TLS and every event they produce is stored
  `verified=False`. A reader in six months can tell a proven callback from a merely addressed one. An SNS
  subscription is deliberately *not* auto-confirmed: following `SubscribeURL` is a server-side request to
  a URL that arrived inside an unauthenticated payload — an SSRF primitive — so the host is extracted, the
  event is kept pending, and the row is listed as unresolved. **The operator's confirm button is not
  built**, so until it is, an SNS deployment confirms out of band or its email callbacks do not flow; that
  is an open end of this item, not a finished one.
- **The audit chain gets four bullets, not the number.** `mask_destination` writes the last four
  characters, because the chain is append-only and survives every retention rule below, so contact
  details put into it could never be scrubbed; the ledger holds the real value, because that is what the
  proof is about. Four fixed bullets rather than one per hidden character, because a mask that reveals a
  number's length reveals something.
- **Two defects the tests caught that no amount of reading would reveal.** `views.py` defines a view named
  `settings`, which rebinds the module global — so `getattr(settings, "TWILIO_AUTH_TOKEN", "")` written in
  the callback view asked a Python *function* for an attribute, got the default, and the signature refusal
  could never fire. "expect 403, got 200" is what found it; the decision moved to
  `services.callback_requires_signature`, where nothing shadows it. Second, smaller, same family: reading
  `request.POST` before `request.body` raises `RawPostDataException`, so the content type now picks which
  reader runs.
- **MySQL 8.4 checks.** `core_messageconsent` and `core_deliveryevent` carry INSERT and UPDATE guards,
  **11/11** with controls, including the two nullable clauses that must *not* refuse — a consent with no
  person (the ledger outlives the account) and a callback that matches no notice (retained anyway) — and
  both refusing once re-pointed at another company's row. `core_suppression` gets no guard by design: it
  references no child row, so tenant scope *is* the organization column, which is why one number can be
  blocked by two companies independently. The 0018/0052 audit guards were re-confirmed present and
  refusing a real UPDATE and a real DELETE; the run that first reported them absent was the probe's own
  fault, matching trigger *names* against a table name (`core_audit_no_update` does not begin with
  `core_auditevent`).
- **What this cannot prove.** No provider was ever called — no Twilio or SNS credential exists here, so
  the payload shapes are transcribed from documentation rather than captured from an account, and a
  renamed field would land as an unmapped status until someone noticed. Whether SNS confirms in practice,
  since the confirmation is left to a human on purpose. Quiet hours, rate and cost controls, and real
  number validation from RB's SMS list are **not** built: numbers are canonicalised to E.164 on a
  North-American assumption (ten digits gain a `1`; a non-NANP number is stored as typed rather than
  guessed at), which is normalisation, not validation. Consent keys on the destination, so a typo'd
  number carries its own consent and someone must correct it. And `mandatory` is inferred from event-name
  prefixes — a convention stated in the file, not a fact the schema enforces.

## A shared clock station, and the PIN that makes it evidence (2026-10-03)

**CLK-2.** DD §Sites, geofences, clocks lists "shared kiosk PIN" among the supported clock-evidence
options and says registered-device binding is *not* required — which together describe the ordinary case
in this trade: a tablet in a guard shack, used by officers who may not have accepted a sign-in invitation
and would not carry a personal phone onto post. The owner picked it as the highest-value of the three
evidence items because it "adds no storage at all and is the biggest friction win".

- **Two halves, deliberately separate.** `ClockKiosk` says *where*; `Person.clock_pin` says *who*.
  Neither is trusted alone, and that split is the whole defence against the failure the industry calls
  buddy punching. The station is addressed by its random UUID, which makes the page unguessable but
  authorises nobody: every punch still has to carry a PIN that verifies. Revocation is the `active`
  column, read from the database on every request rather than baked into a token, so a tablet that walks
  out stops working mid-keystroke while the tour evidence it already recorded keeps its attribution
  (`close_clock_kiosk` is a deactivation, never a delete).
- **No new artefact store.** The station's id lands in `Punch.device_id`, the plain UUID column the
  offline-device path already used for the same purpose, so the punch table needed no trigger rewrite and
  a retired station never strands its punches. What the review screen needs to say is copied into the
  punch's audit metadata (`evidence: {kiosk, kiosk_name, identified_by: "pin"}`) instead, and
  `record_punch` grew that one `evidence=` extension point because CLK-1 and CLK-4 need to answer the
  same question at the same place.
- **The PIN is a password, because it is a credential.** `Person.clock_pin` holds a stretched hash, never
  the digits; the audit row records `{source, digits}` and nothing else. A stretched hash cannot be
  searched, and searching it by trying every officer turns a lobby tablet into a PBKDF2 bench, so there
  is a second, fast `clock_pin_index` — HMAC over the deployment secret, the organization and the PIN.
  It is not invertible without the secret and the match is still confirmed against the stretched hash,
  and it doubles as the uniqueness key: **two officers on one PIN make a kiosk punch unattributable**, so
  `set_clock_pin` refuses a collision in application code (MySQL does not enforce a conditional unique)
  and `unique_clock_pin_in_org` holds it on the hermetic leg.
- **The bound is on the station, because a wrong PIN names nobody.** A four-digit code is ten thousand
  possibilities, which is not a secret against an unbounded guess, and a refused guess cannot be charged
  to an officer — that is what the digest lookup is *for*. So twenty refusals in five minutes pause the
  station, the pause writes one audit row when it trips (`clock_kiosk.pin_throttled`, not one per guess,
  because a guess that creates database work is a free way to fill a tenant's chain), and a supervisor
  clears it from the station page. The per-officer lockout that `verify_clock_pin` does implement is for
  the path where identity comes first — changing your own PIN — and it is deliberately **not** wrapped in
  a transaction: the refusal *is* the write, and a caller that catches the raise inside its own atomic
  block would roll the attempt count back with it.
- **Friction, counted.** A station shows one pad. Typing the PIN returns the officer's own posts with the
  in-progress one already chosen, and ninety seconds to act — long enough to clock in and scan a patrol
  point without typing again, short enough that a station abandoned mid-interaction hands the next officer
  a blank pad. The page returns itself to the pad on its own, the officer's name never appears before
  their PIN, and no roster is rendered for a visitor to read over the shoulder. `/clock/` says whether
  you have a PIN yet and points at the one screen that sets it; an officer with no account can still be
  issued one from the personnel profile by owner/admin/HR — not by a supervisor, because the rung that
  schedules a post must not be the rung that hands out the credential authenticating time on it.
- **A per-field switch in the resolver, as the item required.** `TimePolicy.allow_kiosk` plus a nullable
  `TimePolicyOverride.allow_kiosk`, resolved by the same `origin_of` walk as the geofence and reported as
  `{allowed, source, version}`. Refused twice: when a manager opens a station at a post the rule forbids,
  and on **every punch**, so a contract that takes the allowance away mid-week stops the tablet that day
  rather than at the next enrolment. The tri-state defect the roadmap warns about was a live risk here —
  an explicit `False` rendered as "inherit" would silently restore a forbidden station — so the edit
  screen maps True/False/None to yes/no/inherit over a `TRISTATE_FIELDS` tuple rather than one field at a
  time, and a test posts `"no"`, reads the row, then posts `""` and reads `None`.
- **Two drift fixes the item caught in its own landing.** `bump_policy_revision` kept its own hand-written
  copy of the watched-field list, which is how PAY-5's version stamp ended up storing fields it never
  bumped for; it now reads `RULE_WATCHED[CLOCK_POLICY]`, so a field cannot be watched in one list and
  silent in the other. And `record_punch` gained the ownership rule — *a punch can only be recorded
  against a post assigned to that officer* — which the kiosk needs and the online path had been missing
  all along: both took a `shift_id` from the page, so any officer with devtools could put their time on
  someone else's post. Stating it at the service boundary is what makes the PIN mean anything.
- **MySQL 8.4 checks.** `core_clockkiosk_tenant_insert/update` **5/5** on a real server, and the control
  that matters is the one migration 0045 got wrong: a station with **no post of its own** inserts
  normally, because the `site_id` clause is preceded by `IS NOT NULL`. A station planted at another
  company's site is refused at INSERT and at UPDATE; an ordinary rename is allowed.
- **What this cannot prove.** The station assumes connectivity: a PIN cannot be verified offline, and the
  offline queue is deliberately not offered at a kiosk, because a queue of *unverified* punches on a
  shared device is the buddy-punching hole back. A four-digit PIN is only as strong as the station
  throttle in front of it, so a tablet reachable from outside the building wants six or eight digits —
  which is advice on the page, not a forced minimum. The throttle lives in the cache, so a deployment
  with locmem pauses a station per process rather than per installation; Redis is the production
  setting. And nothing here has been exercised on a real tablet: the pad, the ninety-second hand-back and
  the geolocation prompt are browser behaviour, which the suite cannot drive.

## Retention the chain can obey: seal the period, then purge (2026-10-03)

**REC-4.** `Organization.audit_retention_days` had been displayed on the audit page for a year and
enforced by nothing, and the roadmap said plainly: do not implement this as a bulk `DELETE`. It cannot
be a bulk `DELETE`. Every audit event stores the hash of the event before it, so trimming the oldest rows
leaves the first survivor claiming a predecessor that no longer exists — and the daily verification, an
insurer, or a court reading it would be **right** to call that tampering. The chain cannot be shortened.
It can be continued from a head that is on record. The owner's ruling — "seal the period, then purge" —
is the only shape that survives contact with that fact.

- **`AuditSeal` is the head, and the seam.** One row per closed period: `first_previous`, `first_hash`,
  `last_hash`, `event_count`, the archive's `archive_sha256` and size, the `retention_days` that made the
  period due, and a per-action `manifest` so a seal reads as something without opening it. Two seals
  chain into each other — a new period must start exactly where the last one ended — so the history stays
  **one line** even when the table holds a week. `verify_audit_chain` consults the seals at the head of
  its walk: a surviving event whose `previous_hash` matches a *purged* seal's `last_hash` continues the
  chain instead of failing it. A forged seal does not excuse a break, because the claim has to come from
  the surviving row and the seal has to name that exact hash.
- **Seal before you may delete, on both layers.** `purge_sealed_audit` re-reads the archive and re-derives
  every row's hash from the fields beside it, refuses on any problem, refuses if the live chain has
  errors ("purging now would destroy the only copy of a period that might explain them"), refuses if the
  live count differs from the sealed count, and refuses if the period holds a redaction — whose
  `on_delete=PROTECT` is the database agreeing that a legal decision and the record it governs cannot be
  pulled apart. The delete itself is raw SQL inside one transaction, and the count is re-read after it: a
  period that is not *exactly* the sealed rows aborts the whole thing rather than leaving a half-trimmed
  chain.
- **The trigger is the last word, and it is narrower than a permission.** Migration 0052 replaces
  `core_audit_no_delete` with a version that refuses every delete unless the session names a seal —
  `SET @audit_purge_seal` — whose **own period covers the row** and whose organization matches. So a query
  console cannot remove one inconvenient event: the whole period has to have been sealed first, and
  naming a seal does not unlock anything outside it. `SET @var` is MySQL syntax with no equivalent on
  sqlite, so the statement is vendor-gated and the Python preconditions above are what guard the other
  leg; the hermetic suite therefore proves the service refuses, and only a real server proves the
  database does.
- **Retention enforcement, not destruction — stated as such.** The bytes of a sealed period still exist,
  in storage, addressable by the tenant. What stops growing is the live queryable log. That is the honest
  reading of the ruling: destroying the rows outright would break every period after them, and a second
  archive that no hash covers would be worse than either. The archive is the chain **as written**, so
  export redaction does not apply to it — which is why `audit_seal_download` is owners/administrators
  only, narrower than the NDJSON export, and audited as `audit.seal_downloaded`.
- **A floor, not a silent raise.** Below 90 days the pass refuses and says why: a window that short is a
  wipe, not a policy, and quietly reading "30" as "90" would tell an owner their history is kept for a
  quarter when they set a month.
- **The half-done state is a real state.** The first run of the command test caught a genuine hole:
  `--no-purge` (or a run that died between the two steps, or a redaction that blocked the delete) leaves
  a period archived-but-live, and because the due-date walk continues *after* the last seal, the next
  pass found "nothing due" forever. Retention would have been silently unenforced by the very mechanism
  that makes it resumable. Both the command and the page now run the purge pass on pending seals first,
  and `audit_retention_state` names them (`pending_purges`) so the page can say "3 archived period(s)
  are still in the live table" rather than "nothing due".
- **A shared helper for the hash payload.** `AuditEvent.save` and `verify_audit_chain` each built the
  hashed JSON by hand, in agreement only by luck; drift there means every row already written reads as
  tampered, which is the worst possible failure for a tamper-evident log. `audit_event_payload` /
  `audit_event_hash` in `models.py` is now the single definition, used by the writer, the verifier, and
  the archive reader that re-derives a period after its rows are gone.
- **MySQL 8.4 checks.** **20/20** in `.qwen/tmp/probe_seal_and_kiosk.py`, with controls. Bare DELETE
  refused with no session seal; a row *after* the sealed period refused **while the seal was named**;
  another tenant's row refused; a made-up and a malformed session value both refused; the 0018 UPDATE
  guard still holds; the sanctioned trim deletes exactly the sealed count; the surviving event continues
  the seal head; two chained seals, both trimmed, still verify with no errors. The probe's first run
  "failed" four checks for a reason worth keeping: a WHERE clause built from a model instance matched no
  row, so the trigger never ran — a guard that is absent and a guard that is unreachable look identical
  until you check the rowcount.
- **What this cannot prove.** Whether the archive is *reachable* in production: `seal.archive` lands on
  the configured storage, so a CloudFront-restricted bucket or a lifecycle rule shorter than the seal's
  own keeping would strand the evidence, and nothing here audits storage. That a purged period can be
  re-imported (there is no restore path — the row and its hash are the evidence of absence, and REC-2's
  restore was about records, not the chain). Whether `occurred_at` ties are safe under real concurrency:
  the writer chains from `(-occurred_at, -id)` and the reader walks `(occurred_at, id)`, so two events in
  the same microsecond can disagree, and the seal walk inherits that limit — microsecond ties were not
  introduced here, but this is where they would first become a false tamper alarm. And the retention pass
  is not yet in the worker loop on the running deployment.

## A record can be read where it was found (2026-10-03)

**Browser preview.** DD §Documents has always asked for it and this document has always refused it —
"uploads are never served inline" was a deliberate posture, and **RB §Secure document defaults** backed
the refusal. The owner has now ruled the other way: *if the person has permission to view a document,
there is no reason not to let them view it in the page.* So the sentence above §Records is superseded,
and what follows is what replaced it.

- **One access rule, two doors.** `document_preview` looks the row up with the same filters as
  `document_download` and calls the same `_record_open` — the audience rule, the `sensitivity` ladder,
  the sealed-from-subject carve-out, the soft-delete and scan-clean conditions. `DocumentPreviewTest`
  does not check that preview "works"; it checks that the two routes return **the same status code for
  the same (reader, record) pair**, including the subject of a sealed record about them and a UUID from
  another tenant. That is the only assertion that would catch the regression that matters, because a
  preview one rung more permissive is a new leak wearing an existing feature's clothes, and a page that
  still renders looks fine.
- **The served type is never the uploader's.** What is displayed is decided from `PersonDocument.verified_type`
  — a new column written from inside `validate_document_upload`, so the only way to obtain a verified MIME
  is to have passed the magic-signature check. The pre-existing `content_type` is what the uploading
  browser *declared*, and a test proves the distinction rather than asserting it in prose: a PDF stored
  with `content_type="text/html"` is served to the browser as `application/pdf`. A signature list kept
  separately from `ALLOWED_DOCUMENT_SIGNATURES` would be a second opinion able to disagree, so
  `PREVIEW_MAGIC` is **derived** from those two tables at import.
- **The allowlist is narrower than what may be stored, and the omissions are the design.** PDF, PNG,
  JPEG and plain text. Word and Excel are zip containers whose real contents cannot be reasoned about
  from sixteen head bytes; anything scriptable is absent outright, and SVG would have been the dangerous
  one — a valid signature of its own and executable content by design. `text/plain` covers csv/txt for a
  different reason, recorded beside the table: their inertness comes from the response (declared type
  plus `nosniff`), not from the bytes.
- **A preview link is short and overrides the object's own metadata.** Where the storage can genuinely
  sign (S3/Spaces with `querystring_auth`), the route answers `302` to a link valid
  `PREVIEW_URL_SECONDS` (90s, tested ≤ 120) carrying `ResponseContentType` and
  `ResponseContentDisposition` — which is what lets the bucket hand bytes to the browser without this
  process in the path: the type the browser is told is ours, not the object's stored claim. **Where it
  cannot, the route streams.** django-storages with a `custom_domain` and no CloudFront signer returns a
  *bare, unsigned, public* URL from `url()`; that configuration is refused explicitly, because taking it
  would trade a permission check per request for an unauthenticated read for anyone who found the link.
- **Bytes are re-read before they are rendered.** On local storage the head signature is checked at
  serve time, so a file whose contents were replaced after upload is not served: the route redirects with
  the reason and the record still downloads. Refusing to render is not the same as destroying a record,
  and one test asserts both halves together — the failure mode of the first without the second is a
  retention bug dressed as a security feature.
- **Old rows are simply not previewable.** `verified_type` is empty on everything filed before this
  column, which yields "download only", not a guess. A backfill that inferred type from the declared
  MIME would be exactly the trust this slice exists to remove; rows gain a preview when they are
  re-filed, and nothing pretends otherwise.
- **The browser policy that justified the old refusal was kept, not waived:** `script-src 'none'`,
  `object-src 'none'`, `base-uri 'none'`, `default-src 'none'`, `nosniff`, `private, no-store`, and the
  response filename built from the digest so nothing the uploader typed reaches a header. The one
  deliberate widening is `frame-ancestors 'self'`, replacing the global `'none'` for this response only —
  the modal is our own page — and the middleware applies its CSP with `setdefault`, so the override
  holds. The iframe is `sandbox=""`.
- **Why this was worth building at all:** `document_acknowledge` said *"Download and review"*. A guard
  asked to sign a handbook on a phone at the gate had to leave the app, open a file, and come back — the
  single highest-friction step in onboarding. It now says **Read it here**, with Download beside it, and a
  test pins that the page offers both.

**What this cannot prove.** The signed-link branch is tested against the *decision* with a stand-in
storage, not against a live bucket — Spaces signed URLs have never been exercised end to end anywhere in
this project, which is already recorded as PLT-3, and a test that patches django-storages would prove
nothing about django-storages. Whether a PDF renders in the modal on each browser is a human check
(PLT-4): a `sandbox=""` frame can suppress plugin-style rendering in some engines, and the honest scope
statement is "permission enforced and type controlled, rendering unverified". And this widens what an
uploaded file can do inside the product, so it is the one item here that should carry a security review
rather than a passing test run.

## Which identities a company may grant a role to (2026-10-03)

**AUTH-3.** DD says a company "may restrict organizational roles to an approved Entra tenant while
allowing officers to link approved personal Google identities". Until now the only such rule in the
product was the deployment-level `MICROSOFT_OIDC_TENANT` — one value for the whole install, so a
multi-tenant host could not express it per company at all.

- **The rule is a tenant field, checked where a role is granted.** `Organization.approved_role_domains`
  is read by `role_domain_gate` inside `invitation_accept` — the only path in the product that ever
  assigns a role. It is **not** a login restriction: refusing a sign-in would lock out an account that
  already exists, and the DD sentence is about who may hold an organizational role.
- **Officer is never gated**, which is the second half of that sentence rather than an exception carved
  for convenience: the person standing a post is the one expected to arrive on a personal address.
- **A refusal writes nothing at all** — no membership, no verified `EmailAddress`, and the invitation
  stays unconsumed. The ordering is load-bearing: accepting an invitation marks the mailbox verified
  (that is what lets an MFA-required role enrol), so a gate placed after that line would hand a
  rejected identity a verified address and a path into MFA enrolment anyway. One test asserts the
  absence of the `EmailAddress` row specifically, and another asserts the invitation is still live, so
  signing in on the right account is enough to retry.
- **Empty means no restriction**, so every install behaves exactly as it did. Domains are normalized
  at *read* (lowercased, `@`-stripped) because the field is a JSON list a fixture or a shell can
  write, and a stored `" GuardCo.COM "` that failed to match a correct address would refuse the right
  person — the direction of error this rule must not have.
- Configured on the existing security screen (`form.as_div`, so no new widget surface), and the audit
  event now records `domains_before`/`domains_after` alongside the MFA diff. A refusal is its own
  audited action, `membership.invitation_refused`, with the role and the reason.

**What this cannot prove, and what is still owed.** It checks **email domains, not Entra tenant ids**.
allauth's Microsoft provider returns Graph's `/me` payload, which carries no `tid` claim, so a tenant
check needs id-token handling that cannot be verified against a live directory from here — a rule
whose failure mode nobody has observed would be worse than no rule. Two consequences to be explicit
about: a company that uses a custom-UPN layout where the domain and the tenant disagree gets the domain
answer, and **an attacker who can read the invited mailbox still gets the role**, because accepting the
link is what proves the address — that is inherent to email invitation and unchanged here. Google
linking is not restricted at all by this field, and DD's "approved personal Google identities" is
therefore only half honoured: officers are simply not gated. The follow-up, if a customer needs it, is
to take the tenant id from the id_token in a custom provider adapter and re-use this same gate with a
second list — the enforcement point will not need to change.

## Leave priced on what it displaced, and a lock that fits one branch (2026-10-03)

**PAY-2 (completed), PAY-4.** Two items the owner ruled on the same day, both built against those
rulings. Each replaced a number that was previously *safe because it was absent*: leave carried no
money because its only available figure was wrong, and a lock covered a whole period because it could
not cover less.

- **Leave pays on the hours it displaced, not on the span it covers.** For each post of that officer
  inside the approved span, clipped to the window being exported, the row takes the scheduled minutes
  and **subtracts what was already paid for that post** — so an absence granted and worked anyway keeps
  its evidence row at zero money rather than paying the hour twice. Each displaced post is priced at
  its **own resolved rate** through the same post → site → contract → officer chain the worked rows
  use, because averaging two properties' rates would invent a figure nobody agreed; the row therefore
  shows no rate at all. `estimated_bill` stays blank: an absence is the firm's cost, and billing it
  would invoice the client for nobody standing the post.
- **Paidness became a rule, so `leave` joined the pay categories** — with the FLSA reasoning recorded
  in its default (no federal rule requires paid leave, and hours not worked do not make a 40-hour week
  out of 30 worked plus 10 granted). It is deliberately **not designatable on a post**: leave arrives
  from a decided request, so a second claim on the same hours is the double-count, and
  `set_shift_designation` refuses it rather than merely hiding it from one screen.
- **Four kinds of "no money", each named.** Nothing scheduled in the span (a hire two periods out);
  everything in the span worked; the firm's rule saying unpaid; and a company that has never opened the
  Hour categories screen, so it has no leave rule at all. The last is the quiet trap — "unpaid" and
  "never configured" are different statements, and collapsing them lets a missing configuration read as
  a decision somebody made.
- **PAY-4 is a segment that states an exception, and nothing else.** `PayrollLockSegment` sits under a
  run and covers a branch, a contract, or "everywhere else" (naming neither). **No segment exists ⇒ the
  run's status governs everything**, asserted rather than assumed
  (`test_a_run_with_no_slice_behaves_exactly_as_it_did`), because every installation in the product's
  history is in that state today. `locked` inside a draft is "agree North, keep working South"; `open`
  inside an approved period is the correction that used to require re-locking the world.
- **Precedence and the conservative default are the design.** Specific first: branch, then contract,
  then the catch-all, then the run. A punch whose post cannot be placed takes the **run's** answer even
  when a slice is open — an unattributable clock event is not a licence to edit an approved period, and
  that direction cannot destroy a figure somebody signed off. `payroll_lock_state` likewise re-reads
  the run from the database so the answer is the same for a worker tick as for the screen in front of a
  person; one test found this out by setting `.status` without saving, which is a property worth
  stating rather than a quirk to paper over.
- **The new mistake a partial lock makes possible is closed at the door.** A period with any open slice
  cannot be exported, and the refusal names the slices — handing a customer a file while part of the
  period is still moving is exactly what the feature enables, so the gate and the screen share one
  function (`open_lock_segments`) rather than asking the question twice. A refused export also leaves
  the run un-`EXPORTED`, the half a success-path-only test would never have caught.
- **Locking a slice is the approver's act; opening one is not.** `PAYROLL` may agree a slice; only
  owner/admin may open part of an approved period, because opening undoes somebody else's approval — the
  same line the whole-period reopen draws. Every decision writes an audit event with its reason and the
  run's status at the time.

### MySQL 8.4 checks (real server, migrations 0047–0048 applied, then torn down)

**9/9 probes behaved as specified, four of them controls.** Refused with the guard's own SQLSTATE 1644
`cross-tenant payroll lock segment reference`: a segment on another tenant's run, one naming another
tenant's branch, one naming another tenant's contract, and an `UPDATE` repointing a segment at another
tenant's run. Accepted: a company-wide segment with **both** subject columns NULL, an own-tenant branch
segment, an own-tenant contract segment, and tenant B's own segment — so the refusals above are not a
trigger rejecting everything. Two guards on `core_payrolllocksegment`, and the `(run, branch)` index
present across both its columns.

**The control that matters most is the first one.** Migration 0045 had shipped a `COUNT(*)` guard clause
on a nullable FK with no `IS NOT NULL` test in front of it, and because `s.id = NULL` matches nothing,
that clause refused **every ordinary post insert** — invisible to 466 passing sqlite tests, since
`install_tenant_guards` returns early off MySQL. 0048 was written with the guard from the start and
probed on the company-wide case specifically, which is the row that shape kills.

**A confirmed limitation, not a finding:** the second company-wide segment inserts cleanly, because both
keys are NULL and MySQL treats NULLs as distinct inside a unique index. `set_payroll_lock_segment`
replaces by `(run, branch, client)` so the application cannot create two, and the probe asserts the
database will — the reason to keep those two answers separate rather than assume they match.

### What these two items cannot prove

- A displaced hour is attributed by **overlap**, and worked time is subtracted per post in total rather
  than per overlapping minute. A guard who worked the first half of a tour granted for the whole day is
  credited correctly; one whose worked minutes fall outside the granted window but inside the same post
  would have the leave credited short — bounded, and in the direction that underpays rather than
  overpays.
- Leave hours never carry overtime, whatever the category's `counts_toward_overtime` says: leave rows
  are emitted after each person's week is totalled, so a firm setting that flag gets labelling, not a
  re-thresholded week. Changing it means folding leave into the week before allocation, which is a
  restructuring of `payroll_rows` rather than a line.
- Partial locks cover branches and contracts only. A firm whose late-paying unit is one **site** or one
  **employee** still re-opens the slice containing them; the axes came from DD §Sites and what the
  product already fences by, not from a survey of pay sequences.
- A locked slice freezes new punches, corrections and approvals, but not a **draft regeneration** — a
  run's snapshot is recomputed wholesale, so "locked" means the underlying time cannot be changed, not
  that a figure cannot be re-derived. Approval remains the gate that makes anything final.
- Lock segments notify nobody. The whole-period reopen does; a single slice opening is visible on the
  payroll screen and in the audit chain, and who should be pushed is NTF-1's channel question.

## A half-finished payroll slice, the reason a tour ran long, and a scope that moves with the file (2026-10-03)

**PAY-2, PAY-5, SCH-3, AUTH-1.** The first of these was not started here: the working tree already
held the pay-category feature — models, migration 0043 with its tenant guards, the resolver, two
screens, fifteen tests — and it was **broken at import**. `core.views` imported
`revise_pay_category` from `core.services`, and nothing defined it, so *every* request in the
application raised `ImportError`, not only the new ones. Recording that plainly because "the feature
is code-complete and its tests pass" would have been a true-sounding sentence about an app that
could not start.

- **The missing function was the missing provenance, not a stub.** `PayCategory.save()` moves
  `revision` whenever a watched value moves — deliberately, so a shell or a data migration cannot
  edit a multiplier behind the settings screen — and the category line on a timecard prints
  `rule v2 at 1.50×`. With nothing writing a `RuleRevision` row, that sentence named a version nobody
  could read back: the exact failure POL-1 exists to prevent, reproduced inside the feature that
  cites it. `revise_pay_category` plus `RuleRevision.Kind.PAY_CATEGORY` close it, on the mechanism
  §4 already established rather than a second one. Version 1 is written **at seeding**, not at first
  edit, because the commonest stamp on a payroll line is v1 and if only edited rules had history then
  the version most designations were priced under would be the one missing; the version being
  *replaced* is written from the caller's pre-save snapshot, since by the time history is recorded
  the old numbers are gone from the row. A relabel still changes nothing (`test_renaming_a_category_is_not_a_new_rule`
  and its companion, which asserts the history table gains no row).
- **PAY-5 had promised a field the history never stored.** `bump_policy_revision`'s watched list was
  given `overtime_premium`; `RULE_WATCHED[CLOCK_POLICY]` — the list that decides *what a stored
  version contains* — was not. So a stub stamped `company:4` could resolve to a snapshot with every
  rounding field in it and no multiple, which is the one number that stamp was there to protect. A
  watched-but-never-stored value is worse than an unwatched one, because it is a promise. The new
  test resolves the stamp through `resolve_rule_version`, the function a reader actually uses, and
  asserts both versions read back their own number.
- **The sixth kind, and the count that stopped being a number.** `differential` is the last hour kind
  RB §Payroll export baseline names; it is premium-shaped on purpose — a night differential is a
  premium *on* the hour, so it prices the excess over straight time, adds no hours, and bills nothing
  because the worked line already bills that hour. The seed test asserted five rows; it now asserts
  that the seeded defaults cover exactly the kinds the model offers, because a kind with no default
  would still be selectable on the post and then silently skipped by `payroll_rows`, which prices a
  designation only when the company has a rule for its kind.
- **The screen nobody could reach.** The designation page had exactly one door: the "Recently
  designated" table on the rules page, which lists only posts somebody has *already* designated on.
  A payroll clerk in a firm that had never used the feature could not discover it, and a control
  nobody can find is a control that is not there. Now every post on `/schedule/` carries **Hours**
  (the dispatcher's moment — they are looking at the post), and `/payroll/` carries **Hour categories**
  in its header, because `PAYROLL` is not in `MANAGERS` and a payroll approver cannot open the
  schedule at all. One test pins both doors.
- **Leave is still unpriced, and the reason changed.** The row used to say "pay category not
  configured", which after PAY-2 is simply no longer the obstacle. The real blocker is the *hours
  basis*: the row carries the calendar overlap, so a two-week approved vacation arrives as 336 hours
  the officer never worked, and multiplying that by an hourly rate would invent a sum no contract
  promises. The row now says what is missing (the hours the leave displaced, or a standard-hours-per-
  workday rule) instead of sending a clerk to a screen that has an answer for everything except this.
  That is an owner ruling, not arithmetic, so it is asked rather than guessed.

**SCH-3 — a hold-over is a coverage event, and it needed its own object.** The vertical's sequence is
relief does not turn up, the officer stays, and the only durable trace is a clock-out at an hour
nobody scheduled. The product could show the consequence and nothing else: `Shift` has one officer and
one start/end pair, so a late punch-out was indistinguishable from a guard leaving by choice. What was
built is the reason, stored against the post, reaching the timecard row — the roadmap's done-criterion,
which is where the question actually gets asked.

- **`HoldOver` copies the scheduled end rather than reading it back.** A dispatcher who extends
  `Shift.ends_at` to make the pay match rewrites the schedule, and after that nothing says the tour
  ran long at all; the row keeps the published hour and the reason together. The clock-out stays the
  evidence of when the officer actually stopped — a hold-over explains a span, it is not a punch edit,
  and it adds no hour: `test_recording_a_hold_over_answers_the_prompt_without_moving_a_figure`
  compares the worked row before and after on every figure, because a hold-over that moved
  `total_hours` would be paying the same stretch twice.
- **The prompt asks while the night is recent.** `overrun_prompt` measures the accepted clock-out
  against the scheduled end and says so on the page that can answer it — an observation, never a
  refusal: the punch stands and no figure changes. It measures from the *clock*, so a post with no
  clock-out prompts nothing; inventing an end for a missing punch would convert the gap
  `punch.missing` exists to report into a claimed fact about the tour.
- **`relief` is what makes it a control instead of a note.** Naming who was meant to stand the next
  tour is the fact SCH-3 says was missing ("no record of who was supposed to relieve whom"), and the
  same non-arrival that holds one officer over is the no-show that will empty the following post. The
  officer who stayed is refused as their own relief — that pairing is how a no-show gets hidden inside
  the record of the tour it emptied — and they are kept out of the picker rather than left for the
  form to reject after typing.
- **The split tour became a link, not a new kind of post.** Two officers standing one post in a day has
  to be two posts — each officer needs something to clock onto and the client is billed per officer —
  so `Shift.relief_for` names the tour a half takes over. The refusal is the interesting half: a
  "relief" at a different site is nonsense (one tour, one post), and one starting outside the parent's
  window is a separate tour mislabelled. Adjacent halves and mid-tour takeover both count.
- **A hold-over nobody closed is answered by the clock, not by a reminder.** `close_stale_hold_overs`
  runs in the worker loop beside `expire_stale_moves` — the same concern, a record waiting on a human
  whom events have overtaken — and it only ever fills in what an accepted clock-out already states.
- **Where the dangling reference was found:** `set_shift_designation` refuses an oversized designation
  with "Split the post or record a hold-over instead." That sentence pointed at a feature that did not
  exist. It is why this item was picked, and why the sweep for references to absent features is worth
  repeating — the only other hits were in the docs, which are allowed to describe gaps.

**AUTH-1 — a grant that follows the person.** DD §Authentication and authorization scopes authority by
organization, branch, client, and site; the bounded-authority slice implemented the first three as
*explicit id lists*. So a supervisor transferred from North to South kept running North until somebody
noticed and edited the grant, and the roadmap's open question was what should happen on a move.

- **The ruling: an option, not a default.** `follows_own_branch` resolves the branch from the member's
  own `Person.branch` **at request time**, which is the entire difference from a frozen id. It is
  opt-in because a reach that changes without anybody deciding is worse than one extra click at grant
  time — and `test_an_explicit_branch_grant_still_does_not_move_when_the_file_moves` pins the other
  half, so the two options cannot quietly converge into one meaning.
- **No branch on the file covers nothing**, and says so on the authority screen
  (`their own branch — no branch on the file yet, so this covers nothing`). Reading an unanswered
  question as company-wide authority would turn a data-entry gap into the widest reach in the product.
- **Two doors, one answer.** A grant is read by `ActorScope` (what a supervisor may list and decide)
  and by `dispatch_recipients_for_shift` (who is told about an open post); they are separate code, so
  moving the file without moving both would leave a person who cannot open a post but is still notified
  about it. One test performs the same reassignment and asserts the *same* result through both.
  `manager_recipients_by_person` needed nothing: it builds an `ActorScope`, so it inherits the fix
  rather than duplicating it.
- A second inherited grant is refused in `clean()`, not by a constraint — the uniqueness would hang on
  a column that is NULL in every such row, and MySQL treats NULLs as distinct inside a unique index
  (§11's known cost), so this is application-side dedup like the rest of the product's.

### Defects this slice's own tests and checks caught

- **`order_by("full_name")` took down the whole pay-category screen.** `full_name` is a `Person`
  property, not a column, so the new relief picker raised `FieldError` — and because every designation
  POST follows the redirect back to that page, **14 already-passing tests** broke on a screen they had
  nothing to do with. Caught on the first run of the new class against its neighbour, which is why the
  two are run together before the full suite is paid for.
- **A wording change that an older test owned.** Rewriting the leave row's explanation broke
  `PayCodeAndLeaveExportTest`'s assertion on the literal `"pay category not configured"`. The fix was
  the older assertion, not the newer prose: that test was pinning a sentence that had become false, and
  it now pins the reason instead.
- **A `PAYROLL` clerk cannot open rule history.** The category rules page is payroll-facing; the screen
  where `rule v2` is readable is `MANAGERS`-gated. So the person told "this was priced under version 2"
  cannot look it up themselves. Left as-is and asserted as-is: widening it is an authorization change
  the owner has not ruled on, and an owner/admin can reach both.

### MySQL 8.4 checks (real server, migrations applied, then torn down)

- **The clause that would have broken every post insert on a production server.** 0045 extends
  `core_shift`'s existing tenant chain (MySQL allows one trigger per timing and event, so the chain is
  replaced, the way 0034 and 0037 replaced it). Written as
  `OR (SELECT COUNT(*) FROM core_shift s WHERE s.id=NEW.relief_for_id AND …)=0`, that fires on an
  **ordinary** post: `s.id = NULL` matches nothing, `COUNT()` is 0, and the guard refuses every insert
  with `cross-tenant shift reference`. Migrations 0043–0046 all applied, the sqlite suite stayed green,
  and nothing was wrong until a real MySQL instance was booted — the trigger is skipped on sqlite, so
  this class of defect is invisible to 466 passing tests. Each nullable clause is therefore now guarded
  by `IS NOT NULL` first, and the probe asserts that a plain post with `relief_for` left NULL still
  inserts.
- **Guards installed:** `information_schema.TRIGGERS` reports 2 guards on `core_holdover` and
  `core_shift`, and 2 on `core_shifthourdesignation` (0043's, re-verified because 0045 rewrote the
  chain around them). `core_paycategory` has none by design: it references nothing but its own
  organization.
- **11/11 probes behaved as specified, three of them controls.** Refused with the guard's own SQLSTATE
  1644: a hold-over filed against another tenant's post, a relief drawn from another tenant's roster
  (INSERT and the UPDATE that repoints it), and `relief_for` naming another tenant's tour (INSERT and
  UPDATE). Accepted: the same-tenant hold-over, the same-tenant `relief_for`, an ordinary post with no
  relief at all, and a designation on its own tenant's post and category. Without those four the eight
  refusals would only show a trigger rejecting everything.
- **A finding worth keeping:** `relief_for` at a row that does not exist is refused by the **trigger**,
  not the foreign key — `COUNT()` is 0 for a missing parent, so the guard answers first. That is 0025's
  lesson working as intended (a dangling reference is a refusal, never a NULL that slips through the OR
  chain), and the probe now asserts the guard's message rather than the FK's.

### What this slice cannot prove

- Payroll recomputes with the rule **in force now**: a designation's category line is priced by the
  multiplier at generation time, not at the moment the hours were marked, and the settings message says
  so rather than implying otherwise. A generated `PayrollRun` keeps its snapshot, so an approved period
  does not move; a *draft* regenerated after an edit does. Freezing the version per designation would
  need a stored stamp, which is a schema decision this item did not make.
- The hold-over's scheduled end is only as good as the entry. If a dispatcher extends the post first
  and records the reason after, the form has to be told the published hour; nothing in the audit chain
  reconstructs it, because `shift.updated` records status changes, not the previous `ends_at`. Stated on
  the screen instead of hidden.
- No notice is queued for a hold-over. The relief no-show is a coverage fact SCH-4 already measures for
  the *cancelled* post, and announcing a hold-over to the officer who stayed and the dispatcher who
  ordered it needs a recipient rule this slice did not design — the audit event carries it, which is
  where a dispute looks first.
- Nothing yet asks whether an unrecorded overrun should reach the review queue. The prompt lives on the
  post, so it only reaches somebody who opens it; folding it into the timecard's exception list is the
  one move that would make it unavoidable, and it risks re-pricing the exception counts the payroll
  tests already pin.
- `differential` is expressed as a multiple. A firm whose contract says "$1.50 an hour on nights" has
  to enter the equivalent multiple; the note column is where the arithmetic is recorded. A flat addend
  would be a second pricing model, and that is a decision, not a field.



## Onboarding, registry checks, and the hours a cancelled post takes with it (2026-10-03)

**ONB-1, CMP-3, SCH-4** — three items that had the same shape: each replaced a thing the product could
assert but not evidence. Onboarding was a status value and a count; a verified licence was a
`verified_at` column that nothing had ever written; a coverage hole was invisible precisely *because*
the report counted posts rather than hours.

- **A checklist step names its evidence, so the tick cannot be the completion.** `OnboardingItem` is a
  company-defined step (`kind`, `owner`, `due_within_days`, category applicability, the record type or
  requirement it waits on); `OnboardingTask` is one step owed by one person, stamped with the date it
  was issued *under*. `decide_onboarding_task` refuses to complete a document- or credential-kind step
  whose evidence is not on the file, and an **archived** record does not satisfy a step either. Surfaces:
  `/settings/onboarding/` (definitions beside who is stuck), an **Onboarding** tab on the person page,
  and `/my-onboarding/`; the worker loop runs `onboarding_reminders` (issue, then chase).
  Three exclusions are decisions, not omissions. **Training is not a kind**: the course matcher CMP-0
  recorded as missing is still missing, so a training step is a task with instructions rather than a
  comparison against `TrainingRecord.course_name` by string. **`due_on` is stored, not derived**, so
  editing a lead time from 5 to 30 days cannot move a date a reminder was already sent against; a person
  with no hire date gets a blank date and a visible "no hire date" state instead of a deadline invented
  from today. And **waiving needs `RECORD_WRITERS` plus a 10-character reason** — an officer may close
  their own person-owned steps, but "this does not apply to me" is a ruling about an insurance or
  licensing condition, and the person it is about is the last party who should be able to remove it.
- **The secrecy ladder is consulted before the file is checked.** A step whose record type is sealed
  from the reader reports `hidden` with `satisfied: None`, never "not on file", because answering
  "nothing filed" about a claim that is filed and sealed turns a permission decision into a compliance
  gap — the same failure `compliance_duties` already guards. The *step* still appears (owing a record is
  not the secret; holding it is), and the subject of a sealed record stays denied it, so an officer
  cannot read their own sealed claim sideways through the checklist.
- **One number per question, again.** `onboarding_progress` is a single aggregate query and
  `onboarding_board` is the list; a test asserts the per-person counts are equal, because a tile
  saying three beside a page listing two is how a missed step becomes a display bug. Provisioning is
  idempotent through the `(person, item)` unique constraint rather than a check-then-create, so the CSV
  import and the worker pass cannot produce the duplicate task that would need ticking twice.
- **A registry check that is merely old does not move the compliance rate.** `CredentialRegistryCheck`
  appends a dated, attributed look-up (what the registry showed, the reference, who recorded it), and
  `CredentialType.registry_check_within_days` says how often one is owed — DPS publishes a search page
  and no API, so this is the strongest claim available and it is deliberately not an automated call.
  `lapsed` (we have not looked) is reported but never joins the attention set; `adverse` (we looked and
  it said expired / not found / mismatch) does. Folding the first into the rate would let a filing gap
  cancel a post the way an expired registration does. Recording a clean check finally writes
  `Credential.verified_at` and promotes a credential out of `unverified`/`pending`; it never demotes,
  and it never overrules a status the office set itself.
- **SCH-4 measured the hours, not the row.** `coverage_state(shift)` is now the single definition of
  "this post puts a qualified officer on the ground", used by `coverage_report` *and* by
  `uncovered_windows`, which subtracts the other published posts at the site from a post's own span.
  Cancelling a filled post deletes it from the report entirely — so the report shows no unfilled row for
  the hole it just opened, which is why the measurement happens before the mutation. The hours land in
  the audit event, in the confirmation message, and in a notice to the dispatch recipients who can still
  fill it (never the actor who pressed the button). An at-risk neighbour closes nothing and a draft
  closes nothing, per the report's own rule.

### Defects this slice's own tests and checks caught

- **A column that nothing wrote.** `Credential.verified_at` has existed since migration 0003 with no
  writer and no reader anywhere in the codebase — the same dead-column shape `archived_at` had before
  REC-2. CMP-3 gave it a writer, which is also what makes "verified" falsifiable instead of decorative.
- **A vacuous fixture, caught the same way as the records slice's.** The first registry test built its
  credential as `unverified`, which is *already* compliance attention for its own reason, so "a lapsed
  check does not move the rate" passed while proving nothing. The fixture moved to `active`; only then
  did the assertion bite.
- **A coverage test that measured the wrong rule.** Using the same officer for a relief post made the
  neighbour ineligible through the double-booking check, so "relief closes the gap" could not pass for
  the reason it was written; the fixture needed a second registered officer. The at-risk test then
  *depended* on that distinction, which is the version worth keeping.
- **A shared method that quietly changed meaning.** `RuleVocabulary.applies_to_person` was reusable for
  checklists, but an empty `applies_to` means "unenforceable" for a duty and "every new hire" for a
  step. Both readings are now explicit (`AppliesToCategories` for the shared half,
  `binds_every_new_hire` for the checklist's), because sharing the boolean would have silently emptied
  somebody's checklist.
- **MySQL 8.4 checks (real server, migration applied, then torn down).** All five cross-tenant probes
  refused with the guard's own SQLSTATE 1644 message (`cross-tenant onboarding task reference`,
  `…item reference`, `…registry check reference`), including the UPDATE probes aimed at rows that exist
  — a probe matching no rows would have "refused" nothing. `information_schema.TRIGGERS` reports 2
  guards on each of the three new tables, `one_onboarding_task_per_person_item` exists as a real
  2-column index and rejected a duplicate, and the control insert with matched tenants reported
  `rowcount=1`, so the refusals above are not a broken trigger rejecting everything.
- **Not proven.** The reminder ladder has one rung (past-due), because a step has one lead time and no
  ladder of its own yet — escalation is NTF-2's shape, not this. `onboarding_overdue` will chase a step
  the office never issues a notice for if nobody has a login and the step is `staff`-owned with no HR
  member in the tenant; that state is unreachable in practice but untested. And the checklist does not
  gate assignment or clock-in: the credential and requirement axes already do that, and duplicating the
  enforcement here would put a second owner on a rule that has one.

## The personnel file, whole (2026-10-02)



**REC-1, REC-2, REC-3.** Three items that were each about the same object from a different side: what
a personnel file *contains* when somebody asks for it, where a record goes when the company decides to
file it away, and what a signature actually attests to once the text it was given for has been
replaced.

- **An export is a read, so it may carry nothing the reader could not open one at a time.**
  `personnel_file_bundle` selects the person's records through `record_visibility_filter` — the same
  ladder as the register, the tab, the download route and the queue — and the ZIP is built from that
  bundle: `README.txt`, `dossier.json`, `records-manifest.csv`, and the files under `records/`. Two
  doors, one rule: a record reader (owner, administrator, HR, auditor) gets any file within their
  authority scope, and an officer gets *their own*, under the same sealed-from-subject carve-out the
  download route applies. Both are audited with the record types that went in, the number of files
  packaged and a `self_service` flag, because "the file was downloaded" does not answer "what did the
  company hand over".
  Two absences are deliberate. Records a reader's rung does not open are **not listed at all, not even
  as withheld** — writing "Investigation file: withheld because you may not read it" into a worker's own
  export would itself disclose that the company holds one on them, so the absence is silent and
  complete, and a test asserts the string appears nowhere in the manifest, the README or the entry
  names. And wage and hour totals are not reproduced inside a personnel file: the payroll export is the
  single source for what was worked, rounded, paid and billed, so the dossier carries the *raw events*
  and the posts, not a second copy of the arithmetic.
- **Archive now means something, which is what made recovery possible.** `PersonDocument.archived_at`
  was written by `execute_disposition` and read by nothing — an archive was a timestamp with no effect,
  so a "recovery path" would have had nothing to recover from. Archived records are now out of the
  register, the personnel tab, `My documents`, the compliance queue and the acknowledgment reminders,
  and still downloadable, because retention is not destruction. The register defaults to the active
  file with `?archived=1` for the shelf, and the personnel tab states how many records moved rather
  than letting a count fall. Restore reverses the decision *on the disposition row*
  (`Status.RESTORED`, `restored_by`, `restored_at`, a required 10-character reason) instead of quietly
  clearing a field, so the record book says both that it ran and that it was undone, by whom, why.
  A deletion is not reversible and the code says which case it is looking at: the stored object is
  deleted, the row stays as a tombstone with its SHA-256, and `restore_disposition` refuses with the
  reason instead of failing mysteriously.
- **Signatures are read across the chain, not per row.** `signature_lineage` answers the sentence an
  inspection asks — these N people have never acknowledged *any* version — and separates it from
  "signed a text that has since been replaced", which is evidence of what was agreed then and not
  evidence that anyone read the current wording. Both rosters on the page come from one population, so
  the outstanding list and the never-signed list cannot disagree about who was ever asked.
  The fixture detail that decides whether this is tested at all: acknowledging a superseded version is
  refused by design, so a worker who signed the 2024 handbook must have signed it *before* the 2026
  one was filed. Building the second version first would make the signature impossible and the report
  trivially empty.

**Defects this slice's own tests and checks caught**

- **`archived_at` was enforced by nothing.** Found while writing the recovery test, not by reading the
  disposition code — the disposition "worked" and the record stayed exactly where it was. This is the
  same defect class the roadmap flags for `Organization.audit_retention_days` (REC-4): a governance
  field that is displayed and never consulted.
- **A sanitiser that strips slashes but keeps dots still loses.** The first version filtered
  `original_name` down to alphanumerics, spaces, dots, hyphens and underscores, which turned
  `../../etc/passwd.pdf` into `....etcpasswd.pdf` — harmless as a path, but it kept a literal `..` in
  the entry name and the assertion caught it. Entry names are now built from a dot-free stem plus a
  validated extension, so nothing user-typed reaches the archive path.
- **A stale in-memory object tested the wrong thing.** A test that captured a `DispositionRequest`,
  drove the execute POST, then called `restore_disposition(request, …)` was passing the *pre-execute*
  copy whose status was still `PENDING`, so the refusal it "proved" came from the guard it was
  supposed to be getting past. Refreshing the row and asserting it really is `EXECUTED` first is what
  makes the deletion-is-not-recoverable test mean anything.
- **The acknowledgment form's `confirm` box is not decoration.** A helper that posted only
  `signature_name` produced no signature row at all, which made three lineage tests fail as if the
  report were broken. The silence was the form declining an unconfirmed acknowledgment.
- **Two more premise errors in my own fixtures**, both worth naming because they looked like product
  bugs: an export of a person with no punches proved nothing about the raw-evidence half until the
  fixture had them, and `outstanding_acknowledgments` needs a queryset (it calls `.exclude()`), so
  feeding it the roster list that `signature_lineage` returns is a harness error, not a defect.
- **The clock-boundary class was not one bug but two, and the second was found by a later run.** After
  anchoring the exchange fixture to the workweek, a full-suite pass still failed — this time in
  `ScopedAuthorityTest`, whose third post sat at `now + 2 days` and left the schedule page's
  Monday-to-Sunday window on a Saturday run. `OperableConfigurationTest` had the same shape at
  `now + 2 hours` (it escapes only on a late Sunday, which is why it survived longer). Both now derive
  their dates from `services.schedule_week_start`, the view's own boundary, and
  `ScheduleWindowTest` pins that helper and `week_offset_for` across ±60 days so a change to the window
  cannot silently invalidate every anchored fixture. Neither failure had anything to do with scope or
  with hours — the assertions were correct and the placement was not.

## Figures that survive the evidence changing under them (2026-10-02)

**RPT-1, RPT-2, RPT-3.** The reports page computed three numbers and discarded them. A compliance
rate is a fact about a moment — file the lapsed registration tomorrow and recomputing 12 June answers
a different question than the one an operator asked — and one company percentage cannot tell a branch
manager which part of the company is the problem. Both halves are now closed by one table and one
computation.

- **A stored figure carries its own justification.** `ReportSnapshot` holds the rate with its
  denominator, the kinds it divided, the exclusions it applied (draft posts, categories that do not
  apply to a person, inactive personnel), the reader basis it was taken on, and the items that made up
  "needs action" — so "we were at 92%" can be read back as a list, without keeping every satisfied
  obligation. `capture_reports` stores the company plus every branch and every contract daily from the
  worker loop; `/reports/` gained a per-panel breakdown, a stored-history table, and a
  "Save today's figures" action for the moment somebody wants the number pinned before it moves;
  `/reports/saved/` filters and downloads any period as CSV through `safe_cell`, for the same
  formula-injection reason the payroll exporter has it.
- **A day is captured once and never rewritten.** A record that quietly updated when evidence changed
  underneath it records nothing, so the first pass of a period wins and `captured_at` says which
  moment the number is from. The same early existence check is what makes it safe to leave in a loop
  that ticks every sixty seconds rather than recomputing three figures per subject 1,440 times a day
  to create nothing — asserted with `assertNumQueries(1)`, because "the worker is idempotent" is a
  claim about cost, not just about rows.
- **`subject_key` is a column, not a nullable pair.** MySQL treats NULLs as distinct inside a unique
  index, so a `(organization, period_date, branch_id, client_id)` constraint with both NULL would
  accept a second company-level row for the same day — the one duplicate that silently corrupts every
  trend. The unconditional constraint is verified refusing a duplicate on a real 8.4 server, alongside
  the two cross-tenant triggers on the new table.
- **History is a table by subject and date, not a merged line.** A supervisor holding two branches and
  a contract has subjects whose denominators overlap — an officer in branch A who stands a site under
  contract X is counted in both — so summing them would print a figure that was never measured
  anywhere. The page says this rather than leaving the shape unexplained.
- **The breakdown groups rows the panel already built** instead of re-running each computation per
  subject, so the parts add up to the whole by construction and there is still exactly one definition
  of "needs attention" (RPT-4). A person with no branch recorded gets their own line; obligations have
  no contract split and the screen states why, because deriving one from where someone happens to work
  would be a second, weaker answer to the same question.

**Defects this slice's own tests and checks caught**

- The first breakdown test failed on a real bug, not a fixture slip: grouping *every* queue row counted
  the duties CMP-0 lists but deliberately does not measure, so the split summed to more than the rate
  above it. `_all_rows` now applies the same "measured only" rule `compliance_summary` uses. This is
  exactly the failure RPT-4 exists to prevent, and it arrived through the constraint rather than
  through review.
- A bounded manager's saved-reports screen listed **every** subject in the company in its filter
  dropdown — including "Whole company" and branches they cannot reach — even though the table behind it
  was correctly scoped. The filter now comes from the same scoped queryset as the rows.
- The same dropdown also repeated each subject once per metric: `.distinct()` on a model with a default
  `ordering` appends `metric` and `period_date` to the SELECT list, so the DISTINCT was over five
  columns, not three. `.order_by()` before `.distinct()` is what collapses it — a Django trap that looks
  like a template bug from the outside.
- A stored `Decimal` and a computed `float` are not equal in Python, so "the snapshot matches the panel"
  failed on the rate alone. The comparisons are made as floats, which is where a rounding difference on
  the way into the database would actually show up.
- Two fixture entanglements produced three misleading failures before being traced: a credential
  requirement that was never *approved* is not enforceable, so the "assigned but ineligible officer"
  case silently became eligible; a draft post sharing an officer and a time with a published one made
  the published one read as double-booked; and "at risk" is evaluated against the requirements a site
  or contract declares, not the company-wide applicable set. Each was a wrong premise in the test, not
  a wrong product behaviour — worth recording because the failures looked like product bugs first.
- The MySQL probe for the new table hit the documented `char(32)` trap in a second place: the UPDATE
  half matched zero rows because the `WHERE id=%s` parameter was a hyphenated UUID, so the trigger never
  fired and the probe printed "cross-tenant UPDATE was ALLOWED". The `rowcount: 0` control line is what
  exposed it, and the guard passed once the id was in hex.
- A latent clock-boundary flake in last slice's own test suite surfaced on this run: `ShiftExchangeTest`
  built its two posts at `now+2d` and `now+3d`, and `assignment_impact` reports hours **per affected
  workweek**, so "both officers keep their hours" only holds while the pair sits inside one week. It
  passed at 01:13 and failed at 04:30 with no code change between the runs. The product was right and
  the fixture was wrong — the posts are now anchored to the next workweek derived from
  `TimePolicy.workweek_start`, which is also why the assertion belongs to the week rather than the run.

## Who may open a record, what a duty can be, and which events speak (2026-10-01)

Three roadmap items, in the order §12 depends on them: **REC-5** first because it is not a missing
feature but a live read-authorization hole and the named prerequisite for the clock-evidence slice,
then **CMP-0** because it unblocks the owner's Texas rule entry, then **NTF-3** because the events it
adds ride on the state the first two already keep.

- **Secrecy is now a ladder, not a label (`DocumentType.sensitivity`).** `audience` answers "who is
  this issued to" — person, workforce, management — and cannot answer "who may open it", which is the
  question **RB §HCRM record catalog** family 4 asks when it wants workers' compensation,
  accommodation and leave, drug and background screening, and investigation/discipline records
  "isolated by stricter permissions rather than exposed in the general personnel file". Every one of
  those is attached to *one person*, exactly like a payroll receipt, so the three-value audience
  choice never separated them. `standard` → `restricted` → `sealed` narrows two different axes: the
  staff list loses the read-only auditor at `restricted` (that role is often an outside accountant),
  and the *subject* loses the record at `sealed` (an investigation is not shown to the person it
  investigates). Being the subject denies even a role that reads sealed files — the staff list exists
  to let HR open **other people's** records, not to route around "this one is about you" — which is
  what an earlier cut of this got wrong by OR-ing the two grants.
  One rule, two functions: `record_visibility_filter(role, subject)` filters the lists, and
  `record_readable(document, role, subject)` answers 404 on the routes. Enforced on the person's
  documents tab, `/documents/`, `My documents`, the download route, the acknowledge route (so
  guessing a UUID is not a read), the acknowledgment roster screen (its "who has not signed" list is
  its own disclosure), and the compliance queue — where the row filter and the rate computed from it
  take the same ladder, so no denominator can count a file the reader was never allowed to open.
  The settings screen prints each rung's reader list beside the choice (`reader_summary`), because an
  administrator who cannot see that they just hid a claim file from the auditor will not believe the
  queue when it says the record does not exist.
- **The matrix has a generalised subject (`ComplianceRule`), and says what it does not measure.**
  Until now the only rule shape was `CredentialType`, so an obligation that is not "a person holds a
  numbered document with an expiry date" could not be entered at all — including the three the
  research pass actually recorded: the certificate of liability the *licensee* holds (§1702.124), the
  posting duty (§1702.128 / 37 TAC §35.8), and owner/shareholder training. A duty names its evidence
  kind, **what has to hold it** (the company, officers in the named categories, each site), the record
  type that closes it, its authority/interpretation/effective dates, its renewal window and reminder
  ladder, and its approval — and `/settings/compliance` renders it in the **same table** as the
  credential requirements via one row adapter, not a second screen with drifting columns. It reuses
  CMP-2's history mechanism rather than adding one (`RuleRevision.Kind.COMPLIANCE_RULE`, `name`/`code`
  unwatched, seeded by `ensure_rule_history`, filterable on the rule-history screen).
  Two honesty rules the implementation is held to: no eligibility check consults a duty, so a duty's
  Enforcement cell reads "Queue only" rather than borrowing "Schedule / Clock-in" from its neighbour;
  and a duty whose evidence store does not exist yet — a notice at each site, since the personnel
  register has no site column; training hours, since nothing matches a course — is listed as
  **"Entered, not measured"** with the reason, excluded from the denominator instead of diluting the
  rate. The same ladder from the first bullet applies here: a duty whose evidence the reader may not
  open reports "not measured", because "no certificate filed for this person" is a false statement
  built out of a permission decision.
- **The event families that have state to report are wired; the ones that do not are named.**
  `punch.exception` fires from `record_punch` when a punch lands in review, to the dispatcher whose
  authority reaches the post and **not** to the officer who just punched. `punch.missing` comes from a
  new pass (`queue_missing_punch_reports`, run by `report_missing_punches` in the worker loop) that
  finds a published post that closed with no clock-in or clock-out — the case that produces *no row*,
  which is the same defect the benchmark found for credentials: a queue built from rows only notices
  what somebody already filed a row about. It waits out the twelve-hour offline sync window, so a
  guard working without signal is not chased. Corrections (`punch.correction_requested` and the
  decision back to the officer), payroll state (`payroll.locked`, `payroll.reopened`,
  `payroll.exported` — never to the actor who pressed the button, always to the roles standing behind
  the number), retention (`retention.disposition_requested` to the *second* approver, `…_executed` to
  the requester, `retention.hold_changed`) and operations (`import.completed` carrying applied vs
  parsed counts) are all queued as durable rows through the existing worker. Two invariants are
  asserted globally rather than per call site: no notice is queued with a blank `deduplication_key`
  (the tenant uniqueness constraint is *conditional* on a non-empty key, so a blank key silently
  turns the re-send guard off), and nothing selects SMS while NTF-4 consent is unmet.
  **Deferred with the reason recorded:** identity/security events (no in-app role editor exists, and
  recovery and suspicious-access belong to the authentication pipeline rather than a domain row),
  onboarding tasks (`Person.status` is one value, not a checklist), evidence rejection (a failed scan
  deletes the file and rolls back, so there is no rejected row to announce), and post-order
  acknowledgment (a checkpoint tour is completed, not acknowledged). Each wants its store before it
  wants a notice.

**Defects this slice's own tests and checks caught**

- The roadmap's own premise was stale, and saying so matters more than the fix: it claimed a
  scheduler or field supervisor "sees every filed record" because `MANAGERS` includes them. That hole
  had already been closed by a `can_read_records = is_self or role in RECORD_READERS` gate on the
  documents tab and the queue's documents section. The live leak was the *missing rung below*
  `RECORD_READERS` — an auditor reading a claim or a discipline file — not a dispatcher. The test
  class re-pins the scheduler case so the earlier gate cannot quietly widen.
- A required form field breaks a hand-built POST: adding `sensitivity` to `DocumentTypeForm` made the
  existing catalog-edit test answer 200 instead of 302, because the fixture's POST omitted a field a
  browser always sends. The fixture now sends it and asserts the rung travels through the shared
  editor.
- `filter(deleted_at__isnull=True, q)` is a `SyntaxError`-adjacent ordering trap: a positional `Q`
  cannot follow keyword arguments in the same call, so the ladder had to move to the front of three
  filter calls.
- The listing filter and the row check are two implementations of one rule and can drift in opposite
  directions. Rather than trust both, `RecordSegregationTest` asserts **set-equality** between them
  for every role over a corpus covering all three rungs, both person-attached and company-issued.
- Passing the wrong subject id into a fixture makes a permission test vacuous: the queue test first
  handed the *officer's* person row to the *auditor*, so the auditor read the claim through the
  subject axis and the denominator became a fixture artifact. Each reader now gets their own person.
- A `TestCase` attribute named `self.client` shadows the HTTP test client and breaks every
  `force_login` in the class. The tenant contract in the notification fixture is `self.contract`.
- A notice addressed to `{PRIVILEGED}` is a set containing a tuple of role names, not a set of user
  ids — it would have queued notifications addressed to nobody. Recipient lists now come from one
  `role_recipients(organization, roles)` helper, because three call sites had written the same query
  and each had answered "who hears about this" slightly differently.
- Counting notification *rows* where the intent was counting *events* makes an assertion depend on
  how many people and channels a notice has. The hold/release test asserts distinct subjects, and the
  missed-punch tests filter by dedup key prefix instead of totals.
- A `--days` option that only prints what it accepted is a UI lie. The lookback window is now a real
  parameter of `queue_missing_punch_reports`, and the grace constant is imported as the default.
- `record_punch` refuses punches older than the twelve-hour sync window, so a test that wants a
  *closed* post (older than the missing-punch grace) cannot punch into it through the clock at all;
  that pair is filed directly, which is the right seam for testing a gap detector.

## Rule history, the payroll handoff, and a clock that opens offline (2026-10-01)

Three roadmap items, taken in the dependency order §12 gives: **POL-1 + CMP-2** first because it is
the only place where evidence the product has *already produced* becomes unreproducible, then
**PAY-1 + PAY-3** because the export is the handoff every customer consumes, then **CLK-5**.

- **One history table for both domains, as §4 and §8 asked.** The roadmap says POL-1 and CMP-2 are
  the *same class of defect* and "should be solved once for both rather than twice", so there is one
  `RuleRevision` keyed by `(kind, rule_id, revision)` serving the company clock policy, contract and
  site clock rules, and credential requirements. `RuleRevision` is append-only, stores the whole
  watched value set rather than a diff (so resolving a stamp needs no other row) plus a `changed`
  diff for the human reading it, and is guarded by a **database** trigger against UPDATE and DELETE,
  the same mechanism the audit chain uses — a history that can be edited to match the present rule
  text is not evidence of anything.
- **What a stamp now resolves to.** `resolve_rule_version(organization, source, version)` answers a
  punch's or a timecard's `policy_version` from history even after the rule row is deleted;
  `/settings/time/history/` shows every version, what changed, who saved it, and rules that no longer
  exist. The done-criterion is met at the level the roadmap stated it: the punch test reads the
  stamp out of the `punch.recorded` audit event, edits the rule, **deletes** it, and asserts the
  stamp still returns the numbers it was punched under.
- **Deliberate limit, stated not hidden:** revisions before this table existed cannot be
  reconstructed. `ensure_rule_history` writes a first row for existing rules *as they stand today*
  and the page says so in its own words; a stamp older than that resolves to `None` rather than to
  the current text dressed up as history.
- **A version moves only when an evaluation would change.** `name` and `code` are excluded from the
  watched set — a renamed requirement is not a new rule — and the test asserting that pins it. The
  corollary bug this caught: creating a rule through the old form bumped it to **version 2 on
  arrival**, because `bump_policy_revision` compared against an empty snapshot and saw every field
  as changed. That both wasted the number and left version 1 — the version most timecards were
  actually produced by — permanently unrecorded.
- **PAY-1: the pay code, inherited the same way the rates already are.** `PayCode` with
  `code`/`cost_centre`, settable on the contract, the site, or the single post, resolved by
  `effective_pay_code` in post → site → contract order and reported with `pay_code_source`, because
  "which level said this" is the answer that ends a client dispute. The export gains
  `pay_category`, `pay_code`, `pay_code_name`, `cost_centre`, `pay_code_source`;
  `payroll_totals(rows, by=...)` groups the generated snapshot, which is what discovery's "group by
  employee, branch, site, and pay code" asks for.
- **A used code is closed, not deleted.** Removing one would silently rewrite what past shifts say
  they were billed under, so `pay_code_remove` retires it when anything references it and says how
  many records that was.
- **PAY-3: approved leave is its own category, with no invented money.** An approved span overlapping
  the period is emitted as a second row — `pay_category="leave"`, its own clipped `raw_hours`, zero
  worked hours, and the exception text *"pay category not configured"*. Whether each category is
  **paid** is a rule this product does not hold, and PAY-2 stays deliberately open with the
  clock-evidence work: a break deducted from paid time is an evidence decision, not a format
  decision, so the column set is not filled in with a guess.
- **Leave is clipped to the period, not reported whole**, and declined or merely-requested leave
  produces no row — the two cases a clerk would otherwise read as the same thing.
- **CLK-5: the clock page is now a cached document.** The offline queue was already sound (punches
  sealed in IndexedDB under a non-exportable AES key, so they survive a cold start); what was missing
  is the page. The worker now precaches `/clock/` at install, refreshes it on every online load,
  serves it network-first, and **messages the client when what it rendered came from cache**, so the
  page says it is stale and reloads itself on reconnect rather than letting a guard act on
  yesterday's post list with a session token nobody has refreshed. Cache names are bumped to `v2`,
  which is what actually evicts the shell-only cache on devices that installed before this.
- **Disclosed trade-off on that cache:** the stored document is the officer's own schedule and
  patrol points, so on a shared kiosk an offline reopen can show the last person's roster. It is
  served only when the network is unreachable. Visible is not the same as permitted — a cached page
  cannot make a punch, and a punch is attributed to the device token that sealed it, never to
  whoever is looking at the screen.
- Coverage arithmetic (168/336/504 and the shift-relief factor) was **not** wired into any of this:
  a recurring pattern proves nothing about whether a post stays stood, and that question belongs to
  `coverage_report` (SCH-4 / RPT-4), not to the generator.

### Defects this slice's own tests and checks caught

- The **create-then-bump-to-2** bug above, caught by the test written to prove a no-op save adds no
  version — the same test-asserts-the-invariant pattern that keeps finding things.
- `payroll_totals` had to read the **stored snapshot** rather than recompute live, or the summary on
  screen and the file the clerk downloads could drift apart; the panel is built off
  `run.snapshot` for that reason.
- On MySQL, `values` and `changed` are column names that raw SQL must quote (they are reserved in
  8.4). Harmless through the ORM, which quotes identifiers itself — worth recording because it bites
  anyone writing a manual query against the history table.
- Three of the four verification-probe failures in this session were the **same harness bug**:
  matching `core_shift.id` with a hyphenated UUID. Recorded once, in memory, and still repeated
  before it was caught — the probe printed `INVALID`/`ALLOWED` for rows it never touched.

## Rotations, trades, terms, and who may ask (2026-10-01)

Four limits were named on the face of the previous entry and closed here. Each was re-researched
rather than invented, and two of the four turned out to be the same underlying mistake: the object
shipped as `ShiftSwap` is, in every peer's own vocabulary, an **offer**, not a swap.

- **Naming, from the source that states it best** (Connecteam): "**Offer Shift: a one-way handoff.**
  The employee gives the shift to a qualified teammate and is no longer scheduled for it" versus
  "**Swap Shift: a two-way trade.** … both stay scheduled and both keep their planned hours for the
  week." `ShiftSwap` is kept as the handoff; **`ShiftExchange` is the trade**, and the officer page
  now offers both from one post.
- **A trade is one decision, so half a trade cannot exist.** Every peer that ships an exchange
  approves the pair once — the reason is coverage, not fairness: approve one leg alone and an
  officer is released from a post while nobody has taken the other, and the schedule still reports
  both as filled. `ShiftExchange` therefore holds both legs and has exactly one approval, and
  partial approval is unrepresentable rather than checked-for. On approval each officer is
  re-checked for **the other's** post and both assignments move in one transaction, or neither
  does (Connecteam's wording is the spec: "**Only then is the original shift removed from the
  employee and given to the other**").
- **The colleague chooses which of their own posts goes in**, not the proposer — Deputy and When I
  Work both have the responder collapse several candidates into one pair.
- **The manager sees hours before and after**, per officer, per affected workweek
  (`services.assignment_impact`), because that is the peer approval-screen invariant ("If approving
  would push an employee over their weekly hours … it is flagged in red. **For a swap, both
  employees keep their hours**"). The queue states that a trade should leave both figures
  unchanged, so a handoff dressed up as a trade is visible rather than plausible.
- **A manager cannot decide a move they are a party to.** When I Work's queue lists requests a
  supervisor can act on "excluding your personal requests"; without that, the one independent
  consent in the chain is spent by the person who benefits from it. Applies to both objects.
- **A manager may raise either kind of move on an officer's behalf** (`raised_by`), and it is
  modelled as a flag on the same state machine rather than a second flow, because the consents
  belong to the object: a manager asking does not let anyone skip the colleague's answer. Deputy
  draws exactly this line — "Manager Initiated Swaps" versus a manager who "already knows exactly
  which team member will replace them", which is a **schedule edit**, recorded as such with the
  displaced officer notified. Reassignment is therefore not available through these objects at all.
- **Eligibility is now filtered at offer time *and* re-checked at approval.** The previous entry
  recorded approval-only as a deliberate choice; it is the minority pattern, and the failure mode
  is real — an officer who offers a post to someone who could never take it spends a colleague's
  attention and fills a manager's queue with an offer that can only be refused. `swap_candidates`
  does it batched (five queries for the whole roster, ~300 if it called `shift_eligibility` per
  person), and the screen lists **who was excluded with the rule that excluded them**, because an
  empty dropdown with no explanation is a dead end.
- **Unanswered moves expire.** When I Work expires a request once the day has passed; Deputy states
  the officer's rule flatly ("Until the approval notification is received, the team member
  originally scheduled to work should assume they're working the shift"). `close_stale_moves` runs
  in the compose worker loop and notifies both parties that the post stayed where it was.
- **Rotating patterns were the actual gap, and weekdays were the wrong unit.** Deputy, Connecteam
  and Homebase offer only weekday-anchored repeat ("every week, up to every 4 weeks"); Deputy's
  help centre has **no** biweekly and no custom interval, and Homebase caps at a weekly-view
  template plus a four-week repeat while its own marketing sells "build the rotation once". A
  14-day 2-2-3 is weekday-locked, but **4-on/4-off is an eight-day cycle that drifts one weekday
  forward every round**, so no fixed set of weekdays describes it — and Guardhouse HQ's own docs
  concede the workaround practitioners use today: "Two recurring templates have to be created
  (week 1, week 2)", i.e. the rotation lives in someone's head. TrackTik, the only guard product
  with a real cycle primitive, stores exactly the shape built here: a 7/14/custom-length cycle, an
  ordered day-index map, and a start date (`startDayIndex`, `-1 = CLOSED`). So `pattern=cycle`
  stores `cycle_days` plus `cycle_work_days`, **day 0 being the first day of the series** — which
  is also how a second crew is stood on the same post: the same cycle, four days later, no second
  pattern.
- **Names are presets, not a vocabulary.** "Pitman" and "Panama" are used for the same grid by
  different vendors and contradicted by others, and "2s and 3s" is a UK adverts term; the offsets
  are the fact, so the free-text `alias` field is where a firm's own name goes. The cycle presets
  ship with the arithmetic in the tests (2-2-3 = day 1,2,5,6,7,10,11 of 14; seven tours a
  fortnight; every other weekend off).
- **A series now has a term** (`series_end`), and `next_window` is measured from **today**, not from
  the last post: `today + horizon`. Measured from the start, the unattended pass would have
  extended the roster a few days further on every run, forever. The generate screen defaults to the
  next unfilled stretch, which is what removes the re-typing. **There is no contract term to
  generate to** — `Client` and `Site` carry no dates anywhere in the model — so the term lives on
  the series, which is where the firm actually stated it.
- **Unattended fill is opt-in per series** (`auto_generate_days`, `generate_recurring_posts` in the
  worker loop). It creates only ordinary rows through the same `apply_recurring_plan`, skips dates
  already made, and **reports every date it could not place** to the covering dispatchers
  (`shift.series_blocked`) rather than leaving the hole to be found on the night.
- **Deliberately not built**, on evidence: per-crew automatic day↔night swapping (nobody in the
  vertical does it; store the phase as data and let a human flip it), holiday in-lieu rules,
  Fair Workweek consent and predictability premiums (zero Texas relevance: **29 CFR §553.211(e)(11)**
  excludes "building guards whose primary duty is to protect the lives and property of persons
  within the limited area of the building" from §7(k), and TWC states "an employer can change an
  employee's hours with or without notice" with no advance-notice law; HB 2127 bars local
  ordinances), and N-party chained swaps (searched five vendor knowledge bases, found in none).
  **168/336/504 coverage arithmetic is left to reporting**, not generation — a pattern alone does
  not prove the roster works, and that question belongs to `coverage_report`.

### Defects this slice's own tests and checks caught

- **`swaps()` appended to the list it was iterating.** `_open_swaps`/`_open_exchanges` had become
  helpers returning a materialised list, and the old accumulator line survived inside
  `for row in exchanges:` — appending while iterating a list never terminates. Every exchange test
  passed in isolation until one actually rendered the queue page with a non-empty list; the
  earlier suite ran green for hours because that page had only ever been rendered empty. Found by
  eliminating possibilities with a stack-sampling probe, after two wrong diagnoses (I first read a
  zero-row match as "the guard is not enforced", then took a buffering-stalled log tail for a
  hang). Recorded because the general lesson is the expensive one: **a view branch that no test
  exercises with data is an untested view**, and `assertContains` on a page is worth more than
  asserting the context.
- `assignment_impact` returned an empty list when the organization had no `TimePolicy` row, so the
  hours panel a manager decides on silently vanished. Now `get_or_create`, the way `payroll_rows`
  already resolves it.
- The blocked-reasons panel was nested inside `{% if eligible %}`, so it disappeared in precisely
  the case where the explanation matters most — nobody is eligible.
- `test_a_trade_is_refused_when_either_direction_fails_qualification` first failed *because the new
  offer filter worked*: it tried to propose a trade to an officer who no longer qualified, which is
  now refused before an exchange row exists. The test premise moved to "the requirement appears
  after both agreed", which is the case approval actually exists for.

## Recurring series and shift swaps (2026-10-01)

Closes the two items `docs/discovery-decisions.md` put in the first release and this file kept
listing as absent: recurring shift templates, and a swap — an officer offering their *own filled*
post to a colleague. Both were built against the roadmap's done-criteria (SCH-1, SCH-2), and the
research note there is that the surveyed products keep one asymmetry this product did not have: an
unfilled post is broadcast and taken, a filled post moves only with both parties plus a manager.

- **A new officer surface came out of it.** `ShiftSwap` cannot be reached from anywhere that
  exists: `/schedule/` is a manager page and `/open-posts/` shows only unfilled posts, so a guard
  had no page listing the shifts they are scheduled to stand. `/my-shifts/` is that page —
  upcoming posts, offers made, offers received — and is where an offer starts.
- **A series stores the shape of a post, never its money.** `ShiftTemplate` has no rate field.
  Each generated `Shift` keeps `pay_rate`/`bill_rate` empty, so the number resolves through
  site → contract → officer on every read; `test_a_generated_post_keeps_resolving_its_own_rate_instead_of_freezing_the_series_number`
  renegotiates the contract after generating and asserts the old post follows. A series that
  carried a rate would bill December's tours at June's number.
- **Removing a series does not remove the roster.** `Shift.template` is `SET_NULL`, and the
  message on removal states how many posts stay. A generated post is a coverage fact that may be
  filled, worked, and paid by the time the pattern behind it is retired. Pausing is the ordinary
  stop; removal is owner/admin only.
- **Generation is preview-then-apply, borrowed from the bulk import.** `recurring_plan` is pure
  and `apply_recurring_plan` re-derives it inside the transaction, so the screen and the write
  share one implementation and a registration that lapses while the preview is open is still
  refused. A blocked date is named with its reason and is *not* created: a silently missing row
  is what a dispatcher finds on the night. Range is capped at 84 days (`SERIES_MAX_DAYS`).
- **The fortnightly phase is anchored on the series, not on the range.** `(week − series_start)
  % 2` decides inclusion, so generating December in March does not move the alternating weekends
  the contract agreed to. `test_an_every_other_week_series_keeps_its_phase_whatever_range_is_generated`
  pins both ranges against the same anchor.
- **Availability is read once per weekday, overtime once per workweek.** The per-post advisory at
  assignment cannot see a batch, and repeating it per occurrence would be both noisy and wrong —
  the earlier rows of a batch are not in the database yet. `_plan_week_hours` attributes a day to
  a week the way `projected_week_hours` and the payroll side already do (from the configured
  `workweek_start`), so the plan's number and the timecard's agree about which week an hour is in.
- **One notice per officer per generation, not one per post.** A guard named on a fourteen-day
  series gets one message naming the first date and the window.
- **A swap's states are its consents.** `offered → agreed → approved`, with `declined` (by the
  colleague) and `refused` (by a manager) kept distinct because they are different facts for the
  dispatcher. Eligibility runs at **approval**, never at offer — the same reason
  `shift_claim_decide` re-checks — and approval additionally refuses when the post no longer
  belongs to the officer who offered it, or has already run. An offer is not blocked because the
  colleague is short a document today; they may renew before the tour.
- **Recorded time never changes owner.** Approval writes `shift.officer` only. `Punch.person` is
  untouched and `payroll_rows` groups on it, so an officer who clocked in before being swapped out
  is still paid for the hours they worked. `test_recorded_time_stays_on_the_officer_who_actually_worked_it`
  asserts the timecard row names the officer who punched, not the one now scheduled.
- **One open offer per post** is enforced in the route, not only by the model constraint: MySQL
  silently skips a conditional unique (`models.W036`, §11), so the database would not have caught
  it. Approving one offer closes any other open offer on that post rather than leaving a manager
  deciding a post that is already filled.
- **The notice goes to the supervisor whose granted authority covers the post**, via
  `scope.dispatch_recipients_for_shift` — the same function the open-post request already used, so
  a branch dispatcher is not asked to approve a tour they cannot staff.
- "Needs attention" for swaps stays one calculation: `_open_swaps(organization, scope)` feeds both
  the approval queue and the overview banner, per the reporting rule in roadmap §9.

### Defects this slice's own tests and checks caught

- **`_plan_series_notes` raised `NameError: name 'rule' is not defined` on every plan for an
  officer who has stated availability.** The comprehension bound `for row in rows` and then read
  `rule.covers(...)` — a free name that only exists in the *other* loop — so the bug was invisible
  until a person had an `AvailabilityRule`, and every earlier test used an officer with none. It
  was caught by the one test written specifically to cover that function. Worth stating plainly:
  a helper with no test of its own is a helper whose first real input crashes.
- `ShiftSwap` was written with a `Status` choices class and **no `status` field**. It survived
  review and failed only at migration time: `FieldError: Cannot resolve keyword 'status'` from the
  conditional unique constraint. A constraint that names a field is the first thing that reads the
  model as the database will see it.
- `ShiftGenerationForm.status` used `choices=(DRAFT, PUBLISHED)` — a pair of strings, not a pair of
  `(value, label)` pairs — which raised `ValueError: too many values to unpack` inside
  `ChoiceField.valid_value` on the **first legitimate submit**. Nothing about the bug was visible
  until a POST reached it; the field now carries explicit labels.
- `swap_respond` and `swap_withdraw` filtered on the linked person before checking that the
  sign-in resolves to a personnel record, so an account with no file raised instead of 404ing.
  Ordering fix, found by reading the two routes against `my_time_off`'s established shape.
- One test asserted a rate change reached an already-generated post and failed for the wrong
  reason: it reused the same `Shift` instance, whose `site`/`client` caches were already loaded.
  Re-read as a fresh query, which is what any later request does, the product was right. Worth
  recording because the failure looked like a product defect and was not one.

## Operational reporting: compliance, coverage, and closed tours (2026-09-30)

The previous entry on this page recorded that "there are still no reports at all — an operator
can export, but cannot see a compliance percentage, a coverage percentage, or a tour-completion
rate on screen". `/reports/` adds those three, bounded by the actor's granted authority like
every other surface.

- **The report shares its arithmetic with the queue.** `_compliance_rows` moved out of
  `views.py` into `services.compliance_attendance`, and `compliance_summary` derives the rate
  from those same rows, so the compliance queue, the dashboard tiles and the report are one
  computation. A second implementation of "what counts" is how a report ends up describing a set
  nobody is looking at; `test_the_report_and_the_queue_cannot_disagree` asserts the two screens
  report the same totals rather than trusting the refactor.
- **Every percentage carries its denominator and its exclusions.** The page states "17 of 24
  tracked obligations across credentials, training", names what is not counted (inactive
  personnel; requirements whose applicability does not touch a person), and coverage states how
  many draft posts were excluded rather than dropping them silently — "I have five more planned"
  and "I have nothing planned" must not both read as the same number.
- **An empty window is None, never 100%.** `coverage_report` and `tour_completion` return
  `rate=None` when the denominator is zero, and the template renders an em dash with the
  sentence "A schedule with no published posts is reported as none, never as 100%." A silent
  zero-denominator 100% is the single most likely way a report like this lies.
- **Coverage counts eligibility, not assignments.** A published post with an officer who no
  longer qualifies is `at_risk` with the refusal reasons attached, not `filled` — the same
  `shift_eligibility` + `post_requirements` path the dispatcher hits when assigning, so a
  credential that lapsed overnight moves the post out of coverage without any new rule. It is
  deliberately gated on what the *post* requires, per the ruling recorded below: an unrelated
  expired licence on the record is not a scheduling stop.
- **Tour completion is an evidence report, not an attendance judgement.** A tour counts as
  closed when it has a clock-in and a clock-out no earlier than it; the unclosed list names the
  post and which side is missing, because an unclosed tour is the reason a timecard is an
  estimate and the operator needs the list, not the percentage alone.
- Windows are `?days=`, clamped to 1–56 rather than rejected: a mistyped or bookmarked value
  answers with a real number and the chosen span on the page.
- The page footer states the limit plainly: these are operational counts, not wage statements —
  rounding, premium rules and the differential categories still need the owner-approved legal
  gate before any figure here is treated as a payroll record.

## Evidence policy inheritance, and the version behind every number (2026-09-30)

`docs/discovery-decisions.md` has required this since discovery: "Clock evidence is configurable
at global, client, and site levels. Authorized client and site policies may strengthen or weaken
the global baseline. The resolved effective policy, source scope, version, and authorizing actor
must be visible and auditable." `TimePolicy` was one row per organization, and the payroll
snapshot stored rounded hours with no record of the rule that rounded them.

- **`TimePolicyOverride` (migration `0033`) states a delta, not a copy.** A row names exactly one
  level — a contract client or a single site — and may set only the geofence requirement and the
  rounding rule. A null field means *inherit*. Copying the company policy into every override
  would have been friendlier to look at and would then silently freeze the baseline: the site
  stops following the company the moment the company moves, with nobody to explain it.
- **The calendar is not overridable, on purpose.** Workweek start, overtime threshold, timezone
  and reopen stay company facts — a pay period cannot begin on two days at once, and the FLSA
  workweek is not a client preference. `ResolvedClockPolicy` delegates those fields to the company
  row unconditionally rather than storing them twice.
- **Inheritance is per field, so the provenance is per value.** A hospital that waives only the
  geofence still takes its rounding from the contract it stands on: `policy.geofence` resolves to
  the site row while `policy.rounding` resolves to the contract row, and each carries its own
  `source` and `version`. The first draft of this resolved "most specific row wins" for the whole
  policy, which quietly dropped such a site's rounding back to the company's — caught by
  `test_a_site_rule_beats_the_contract_which_beats_the_company`, which asserts the two sources
  differ on one post.
- **Every paid figure names the rule that produced it.** Each timecard row now carries
  `raw_hours` (worked) beside the rounded hours, plus `rounding_mode`, `rounding_minutes`,
  `policy_source` and `policy_version` — a `row:N` stamp of the row that supplied the interval, so
  a number can be reproduced after the rule has been edited twice. Each recorded punch carries the
  geofence rule it was judged against in its audit event, since the raw punch is immutable and the
  rule is not.
- **Versions advance only on a real change.** `bump_policy_revision` compares the watched values
  before saving, so re-posting an identical form leaves `revision` alone; a counter that moved on
  every save would make the stamp meaningless.
- **Both surfaces say what a change reaches.** `/settings/time/` lists every rule with its level,
  version and authorizing actor, and counts how many active posts take their *rounding* from each
  level — counted on rounding, because a site that waived only the fence is not exempt from a
  baseline rounding change. The rounding preview (`rounding_preview`) shows worked time against
  paid time for sample tours before anything is saved, on both the baseline form and the override
  form. `/schedule/` stamps each post with the rule that will be applied to it.
- **Only owners and administrators write policy**, and the actor is stored on the row
  (`authorized_by`) as well as in `time_policy.override_created` / `_saved` / `_removed`, because a
  waiver with no name attached is not a waiver anyone can defend. Duplicate rules for one target are
  refused in the view, not by the database: MySQL cannot enforce a conditional unique constraint, so
  the app check is the only thing between two rules for one site and an unresolvable answer.
- **A tri-state select is a data-loss trap.** The override form's `require_geofence` speaks
  `yes`/`no`/`inherit` while the row stores `True`/`False`/`None`; without the explicit initial
  mapping the edit screen rendered "inherit" for a property whose fence was waived, and the next
  save would have quietly put the fence back up. Pinned by
  `test_editing_one_value_leaves_the_untouched_waiver_in_place`.

## Reopening a locked pay period (2026-09-30)

`docs/discovery-decisions.md` allows a locked period to be reopened "when configuration allows
it", requiring "a reason, privileged approval, and an audit event". Until now there was no reopen
path at all: an approved run could only be re-generated as a draft for a period that had never
been locked, so a dispute discovered after export left the operator with no in-product answer.

- **The switch is the company's policy, not a favour.** `TimePolicy.allow_reopen` (migration
  `0031`) is off by default, and `/payroll/` states which way it is set, so "can this period be
  unlocked?" has one answer per installation rather than one per urgent request.
- **The role that locked it is not the role that unlocks it.** `payroll_reopen` is gated to owner
  and administrator, never to the payroll approver who approved the run: undoing an approval is a
  different act from making one, and one person should not be able to do both alone.
- **A reason is required and stays on the row** (`reopen_reason`, `reopened_by`, `reopened_at`,
  `reopen_count`) as well as in the hash-chained `payroll.reopened` audit event. The count is on
  the page because a period unlocked four times a quarter is a fact an operator should see, not
  metadata to dig for.
- **The approval is cleared, and the old numbers are kept.** A draft must not present an
  `approved_by` who no longer stands behind it, so the approval fields go to null; the snapshot is
  left exactly as it was, because it is what the client was billed from. Reopening appends an
  exception row, and approval refuses while any exception is open — so the reopened period cannot
  be re-locked on the stale numbers. It has to be regenerated, which is the point of unlocking it.
- **What the lock was protecting stays protected while it is on:** corrections and punches in a
  locked period are still refused (`adjustment_review`, `record_punch`), and an exported run is
  not re-exportable as a draft.

## Availability, time off, and what a dispatcher is warned about (2026-09-30)

`docs/discovery-decisions.md` requires assignment validation to combine credentials, training,
role, **availability**, site/client requirements, and **overtime policy**, and lists availability
windows, time-off requests, and overtime warnings in the first release. Neither existed: the
schedule page knew who was qualified and nothing about when a person could actually work.

- **Availability is a weekly pattern the officer owns** (`AvailabilityRule`, migration `0030`),
  stated at `/availability/` or by a manager at `/people/<id>/availability/`. A window whose end
  is earlier than its start runs past midnight, because that is how a Friday night is described
  by the person working it — `covers()` therefore tests both the night a window opens and the
  morning it closes, and a window's end minute is inclusive, so a post starting exactly as the
  stated hours close is inside them. A person who has stated nothing is warned about nothing:
  silence is not a refusal.
- **Approved leave blocks the assignment and never blocks the clock**
  (`TimeOffRequest`, `shift_eligibility`). A post inside granted leave is refused with
  *"On approved leave Feb 05 to Feb 06"*, while a punch recorded on that day is accepted and
  left unflagged — time worked on a day granted off is still time worked, and the evidence is
  not the software's to discard.
- **Availability mismatch and the overtime threshold warn, they do not refuse.**
  `services.shift_advisories` runs after a post is saved and hands the dispatcher the sentence
  they need: which window was missed, and *"this post brings the week of 2026-02-02 to about
  50.0 hours, over the 40.0-hour threshold: the excess is premium time"*. Blocking on
  availability would hide the only officer who could fill a gap, and premium hours are lawful —
  they just cost money and have to be seen before the post is stood, not on the invoice.
  `projected_week_hours` counts whole shifts starting inside the configured workweek, so the
  figure is labelled *about*: rounding and breaks are timecard facts.
- **Approving leave names the hole and erases nothing.** `/time-off/` lists every live post
  inside the requested hours before the decision, and approval leaves them published and
  assigned — a dispatcher moves or cancels them deliberately. Silently cancelling a published
  post would take coverage away from a client with no decision anyone made.
- **Requests reach the managers who cover the person**, using the same `AuthorityScope` index as
  the reminders, and the queue itself is bounded: a branch supervisor approves their branch's
  requests and gets 404 on another branch's. The officer is told the decision through the durable
  queue (`timeoff.approved` / `timeoff.declined`), and withdrawal is theirs alone while open.
- The profile's **Schedule & time** tab now carries the availability pattern and the leave
  history beside the posts, because that comparison is what a supervisor is making when they ask
  why someone was not stood.

## Document revisions, and pages that say how big they are (2026-09-30)

**A re-issued handbook is a revision, not a second unrelated record**
(`PersonDocument.supersedes`, migration `0032`). Filing the 2026 text now declares which version
it replaces, and the model refuses anything else: a revision must be the same record type, filed
against the same person or the same company-wide slot, and it cannot supersede itself or a version
that has already been replaced (the chain would fork).

- **Signatures stay with the text they were given for.** `DocumentAcknowledgment` is keyed to the
  document row and stores the hash it was given against, so a new revision starts with zero
  signatures and the whole roster becomes outstanding again — which is the compliance truth, not a
  regression. The old roster still lists who agreed to *that* text and now warns that it has been
  replaced.
- **A worker is never offered two handbooks.** `_workforce_documents()` and the compliance queue
  select current revisions only, and `document_acknowledge` refuses to sign a superseded row with
  a message pointing at the current one — otherwise a bookmarked link from last year records
  agreement with a document nobody is bound to any more.
- **Superseded rows stay downloadable and auditable.** History is superseded, not deleted; the
  upload audit event carries the revision number and what it replaced.

**Every register now paginates, and the three silent caps are gone.** People, records, training,
notifications, the compliance queue, the punch desk, and the audit log page at 50 rows through one
`_page()` helper, with the totals taken from `paginator.count` rather than the rows on the page.
The punch desk used to render `[:250]`, the audit log `[:500]`, and corrections `[:100]` with no
statement that anything was left out — a capped list that reads as a complete list is how an
operator signs off on a number that was never the whole number. The remaining windows (the 100
most recent correction requests, the 100 most recent redaction requests) are now labelled as
windows on their own panels.

Because a client-side filter can only reach the rows in the DOM, the directory, records, and
training search boxes moved from `data-table-search` to a server-side `?q=` (`_search`, ORed
`icontains` over the name, email, employee id, file name, or course as appropriate). Searching a
52-person roster and being told "no matches" about the 49th person was the bug; the search now
runs where the rows are, and a list that fits on one page says so ("52 records … all of them, on
this page"). `get_page` is used rather than `page(number)` so a stale bookmarked `?page=99` shows
the last real page instead of a 404.

## Bounded manager authority (2026-09-30)

`docs/discovery-decisions.md` gives owners, administrators, branch managers, and field
supervisors the power to review and approve out-of-geofence punches "within their assigned
scope", and lists organization, branch, client, and site as the authorization axes. Roles on
their own are organization-wide, so every supervisor's queue, schedule, directory, and punch
desk was the whole company's. `AuthorityScope` (migration `0029`) makes the bound real:

- **A grant names exactly one level** — a branch, a contract client, or a single post — and a
  membership with **no grants keeps company-wide authority**. That is deliberate: a
  single-branch installation must not be locked out of its own data by the arrival of a new
  table, and narrowing is an explicit act by an owner or administrator from
  `/team/<membership>/authority/`, audited as `authority.scope_granted` /
  `authority.scope_revoked`. Only **Scheduler / Dispatcher** and **Site supervisor** carry
  bounded authority; owner, administrator, HR / Compliance, payroll approver, and read-only
  auditor are company-level functions and are never narrowed, so an administrator handed one
  branch does not lose the rest of the firm by accident.
- **Each grant expands to what it actually covers.** A branch grant reaches its personnel and
  every post stood at its sites; a contract grant reaches that client's posts and the officers
  who stand them; a site grant reaches the post and its assigned officers. Contract and site
  grants reach people through the *assignment*, not the personnel file, because a guard on
  another branch's payroll still has to be supervised where they stand.
- **Lists are filtered and per-object routes answer 404.** A supervisor scoped to Austin sees
  the Austin roster in `/people/`, their compliance queue in `/compliance/`, their posts in
  `/schedule/` (plus their own officers' posts at another branch, which is exactly the case
  that needs watching), and their timecards in `/time/review/`; `/people/<id>/`,
  `/credentials/<id>/edit/`, `/schedule/<id>/edit/`, `/schedule/<id>/cancel/`,
  `/schedule/claims/<id>/decide/`, and the punch and correction approvals all refuse an object
  outside the grant. Hiding a row from a list is not an access control on its own routes.
- **Create forms are bounded too**, so a dispatcher cannot invent an out-of-scope assignment:
  `branch`/`client`/`site`/`officer`/`person` choices come from the same scope that filters the
  lists (`_scope_querysets`).
- **The bound changes who is told.** `scope.dispatch_recipients_for_shift` sends an open-post
  request only to the managers who can decide that post — company roles plus the field managers
  whose grants reach it, including any dispatcher with no grants at all — and
  `manager_recipients_by_person` adds a person's covering supervisors to the credential and
  training reminders, which previously went to owner/admin/HR alone.
- **The UI says what it is showing.** A bounded actor gets the grant in the sidebar and a note
  on every filtered page ("Your authority is bounded to *Austin branch*. Personnel, posts, and
  timecards outside it are not listed here"), because a short list that looks like a complete
  list is how a coverage gap survives.

Resolution costs three queries for a bounded user (grants, sites, people), one for a field role
that was never bounded, and none for a company-level role; it is memoised on the request and the
id sets are bounded by the tenant roster. `core/scope.py` holds it.

### Defects this slice's own tests caught

- The authority route reversed on `<uuid:...>` while `Membership` keeps an integer primary key,
  so every grant screen raised `NoReverseMatch`. Now `<int:membership_id>`.
- `manager_recipients_by_person` first built the index only from memberships *with* grants, so
  a dispatcher who had never been scoped silently stopped receiving reminders they used to get —
  a narrowing feature that narrowed nothing but the notices. Company-wide field managers are now
  seeded into every person's recipient set.
- The overview page read `org.audit_events` for any manager, so once authority was bounded a
  branch supervisor still saw company-wide audit actions on the dashboard while `/audit/` answered
  403 for them. The panel is now gated on the same `AUDIT_READERS` set the audit route uses —
  caught by review of the slice, and pinned by a test after the fact.

## Peer and vertical benchmark, then the gaps it exposed (2026-09-30)

The product's help-doc benchmark was rebuilt from the primary documentation of
**Homebase** (`support.joinhomebase.com`), **Connecteam**, **Deputy**, **When I Work**, and the
guard-vertical products **TrackTik/Silvertrac**, **GuardsPro**, **OfficerReports**,
**GuardMetrics**, **TEAM Software**, plus Texas DPS Private Security Program pages, Occupations
Code Ch. 1702, and 37 TAC Ch. 35. Two conclusions drove this round of work:

- **The category's table stakes were missing, not the category's differentiators.** Homebase has
  *no* requirement-side compliance model at all: certificates attach to a person, nothing binds a
  requirement to a role or post, and no product surveyed computes "this guard is missing a
  registration their role requires". That mechanism already existed here. What was missing was the
  everyday operating surface — provisioning a guard's sign-in, recording a renewal, filling an
  unfilled post, seeing who has not signed the handbook.
- **A generic scheduler gets the vertical's hard stop wrong.** §1702.302(a) and 37 TAC §35.22(b)
  mean an expired individual licence is not a warning: the officer may not perform regulated
  services at all. A reminder that only fires when someone remembers to file a `Credential` row
  therefore cannot be the control.

### What was broken by this reading

- **A provisioned workforce could not clock in.** Nothing in production code ever set
  `Person.user`; the officer clock, "My documents", and correction requests all key off it, and
  the only way to set the link was Django admin — which a production host closes to every source
  address by default. `/people/<id>/access/new/` now issues a person-bound invitation
  (`MembershipInvitation.person`), and accepting it links the record and grants a non-privileged
  role. Owner/Administrator are not selectable there: an HR user must not be able to mint one from
  a personnel profile. A stale invitation cannot move an existing link — the record is re-read
  under a row lock and the conflict is audited (`person.signin_conflict`) instead of overwritten.
- **A renewal had no honest path.** `Credential` and `TrainingRecord` were create-only, so filing
  a renewed licence meant a second row, leaving the register showing the same guard both current
  and lapsed. `credential_edit` and `training_edit` update the row, refuse a number already on
  file for that person, and record before/after in the audit trail.
- **One signature acknowledged everybody.** `document_acknowledge` stamped the shared
  `PersonDocument.acknowledged_at`, so after the first worker signed, every other worker lost the
  "Review" action and the record vanished from the documents queue. Acknowledgment state now comes
  from `DocumentAcknowledgment` rows; the shared column is stamped only for a record with exactly
  one possible signer. `/documents/<id>/acknowledgments/` reports who has and has not signed, and
  "Remind N outstanding" queues a notice per signer — including a raised-to-management case for a
  worker with no sign-in, who otherwise cannot be asked at all.
- **The compliance queue's own arithmetic was unauditable.** A workforce record's row now reads
  "1 of 2 have not acknowledged" rather than disappearing on the first signature.
- **Reminders nagged daily and missed the real case.** The queue walked `Credential` rows only, so
  a guard with *no* certificate filed produced no notice — precisely the person who cannot be
  placed. `queue_compliance_reminders` now covers three cases (inside a lead time, invalid state,
  and missing obligation), fires once per rung instead of once per day, and re-arms when the expiry
  date changes, so a renewal does not silence the ladder. `CredentialType.reminder_days_before`
  makes the ladder configuration, matching the 30/7/1 and 90/60/30 patterns the surveyed products
  ship; the level chosen is the smallest lead the date has entered.
- **Publishing was blocked on a contradiction.** A shift could only be published if an officer was
  assigned and eligible, which makes an open post impossible — the opposite of how coverage works
  in this industry, and of every scheduler surveyed (Homebase "Open Shifts", When I Work
  "OpenShift", GuardsPro "Vacant Shift"). A published post with no officer is now a legitimate
  state, is announced only to the officers who currently qualify for it, and can be requested by
  them from `/open-posts/`. A request does not fill the post: a dispatcher approves it, and
  approval re-checks eligibility, because a credential that lapsed overnight is exactly the case a
  Texas post cannot absorb. Declining, withdrawing, and double-request protection are all there.
- **Checkpoint scans proved nothing.** The `Checkpoint` model, its scan codes, and its server-side
  geofence validation existed, but no screen ever produced a code, so the clock's "Checkpoint"
  button recorded an unattributed punch — and the online punch endpoint did not accept a
  checkpoint at all. Patrol points are now created and edited per site, the clock offers only the
  points belonging to the selected post's site, and both the online and offline paths attribute
  the scan or refuse it. A checkpoint punch that names no point is refused.
- **Configuration was create-only.** `Branch`, `Client`, `Site`, `CredentialType`, `DocumentType`,
  and `CustomFieldDefinition` had no edit route, so a mistyped address, a wrong geofence radius, a
  changed applicability set, or a requirement that must be approved *after* creation were all
  admin-database edits. Approval especially: `is_approved` could only be set on the create form, so
  a rule drafted before its source was verified stayed unapproved and unenforceable unless it was
  deleted and re-typed. Each catalog now has an audited editor, and `BranchForm` exposes `active`
  so a branch can be closed. `branch_create` redirects to `/branches/`, which existed but was
  unreachable from the create flow.
- **The dashboard was decoration.** Two of four tiles were hardcoded `—` ("Control matrix
  pending", "Coming next"). Managers now get compliance-attention count, unfilled posts in the
  next 7 days, and timecards pending review in the last 30 days; workers get their next post, the
  records waiting for their signature, and their flagged timecards — plus an explicit statement of
  why the clock is unavailable when no personnel record is linked.
- **The schedule page had no window.** Every shift the tenant ever created rendered in one flat
  table with no edit, cancel, or filter. It is now one week at a time (±26 weeks), grouped by day,
  with per-post requests, edit, and cancel. Cancelling requires a reason, refuses a completed
  shift, and refuses a post that already has punches — the assignment may be wrong, but the time
  evidence is not theirs to erase. Publishing, changing, cancelling, and filling an open post each
  notify the officer through the durable queue, so `Notifications` finally says what it promises.

### What the benchmark deliberately did not change

- **Fee/period facts from the regulator are not encoded.** Renewal windows (180 days early, 1.5×/2×
  late, new application after one year), the CGL limits in §1702.124, and the posting duties in
  §1702.128 / 37 TAC §35.8 are recorded here as research inputs. This product still ships the
  mechanism, not the rules: no jurisdiction's numbers are hardcoded, and the control matrix remains
  owner-entered and owner-approved.
- The terminology research is worth adopting in wording, not in schema: peers say *post*, *tour*,
  *relief*, *open post*, *hold-over*, *bill rate vs pay rate*, *spread*.

### Rulings taken from this benchmark

- **Does an expired clock-gating credential block scheduling company-wide? No** (2026-09-30).
  Scheduling is gated by the credentials the post requires — now including what the contract and
  the site add to it (see below) — and not by an unrelated expired credential elsewhere on the
  record. The clock-in gate (`blocks_clock_in`) and the compliance queue remain the places a
  broader lapse surfaces.
- **Per-site and per-client requirements: build them** (2026-09-30), together with pay rates per
  client/site and an adjustable per-post rate.

## Post-level requirements and the rate model (2026-09-30)

`Client` and `Site` each declare `required_credentials`, `default_pay_rate`, and
`default_bill_rate`; `Shift` declares `pay_rate` and `bill_rate` for the one-off case (holiday
cover, a client-agreed premium).

- **Requirements resolve widest scope first** — `services.post_requirements()` returns the
  contract's set, then the site's, then the post's own, de-duplicated with the origin kept.
  `shift_eligibility` consumes that list, so a dispatcher who leaves the post's checkboxes empty
  still cannot place an unregistered officer, and the refusal says *"Guard registration: missing.
  Required by the contract."* Naming the layer is what makes the refusal actionable: the person
  seeing it cannot otherwise find the rule. The same list gates open-post announcements
  (`open_post_candidates`) and officer claims, so an unqualified guard is never invited to ask for
  an armed post, and the reason is shown rather than the post being silently hidden.
- **Rates resolve post → site → contract → officer**, reported with the winning layer
  (`pay_rate_source`), because "who set this rate" is the first question in a client dispute.
- **Timecards are now per officer per post**, not per officer. This is the only way a split shift
  across two accounts can be paid and billed correctly, and it is what makes the spread visible.
  Overtime stays a *person-workweek* fact: the first `overtime_after_hours` of the week are
  regular whichever post they were stood on, allocated chronologically, so a guard cannot collect
  two half-thresholds or lose the premium entirely. The premium multiplier is FLSA's 1.5× and is
  not configurable — there is no §7(k) work period available to a private contractor (29 CFR
  §553.202), which is why `overtime_after_hours` is a weekly threshold and nothing pretends
  otherwise.
- **Exports carry the money columns.** CSV and XLSX gained `client`, `site`, `post`, `pay_rate`,
  `pay_rate_source`, `bill_rate`, `bill_rate_source`, `estimated_pay`, `estimated_bill`, and
  `margin`; the PDF carries the reduced money view (`PAYROLL_PDF_FIELDS`) because sixteen
  columns do not fit a fixed-width text page. A payroll run generated before this change exports
  with those cells blank rather than failing — the snapshot rows are read with `.get`.

`estimated_pay` and `estimated_bill` are labelled *estimated* on purpose: this product does not run
payroll, tax, or invoicing, and a rounding or premium policy still needs the legal gate below
before any of these numbers are treated as a wage statement under Tex. Lab. Code §62.003.

### Known cost of the inheritance model

`post_requirements` runs three queries per evaluation, and `open_post_candidates` evaluates the
whole roster — so publishing one open post costs `3 + 2 × active officers` queries. It is bounded
by the roster and happens on a publish, not on a page render, and `requirements` can be passed
in; the same caching is not yet done for `record_punch`'s clock-in check.

`effective_clock_policy` adds two queries per resolution (the company row and the site's override
rows), and `record_punch` calls it for every punch it accepts — so an offline synchronisation
burst of twenty punches now costs about forty extra queries on top of the eligibility check it
already made. Memoising it per request would be wrong here, not just premature: a device draining
a queue can span a policy edit, and the stamp written beside a punch has to be the rule that was
in force for that punch. The fix, when the volume justifies it, is to resolve once per
`(organization, site)` *inside* the sync transaction and pass the result down, so the batch shares
one answer while the batch is atomic — not to cache across requests.

`coverage_report` is the same class of cost in a new place: it evaluates `post_requirements`
(three queries) and `shift_eligibility` (one or more) for every post in the window, so a
hundred-post fortnight is several hundred queries to render one page. It is bounded by the window
rather than the roster, and the report is a page an operator opens deliberately, so it is
tolerable; the fix when it is not is to resolve requirements once per site and credentials once
per officer for the whole window, the way `open_post_candidates` already batches. `tour_completion`
was written that way from the start — one query for posts, one for all their punches — because
that report scans every past post in the window and would otherwise be the worst offender.
- **Fee/period facts from the regulator are not encoded.** Renewal windows (180 days early, 1.5×/2×
  late, new application after one year), the CGL limits in §1702.124, and the posting duties in
  §1702.128 / 37 TAC §35.8 are recorded here as research inputs. This product still ships the
  mechanism, not the rules: no jurisdiction's numbers are hardcoded, and the control matrix remains
  owner-entered and owner-approved.
- The terminology research is worth adopting in wording, not in schema: peers say *post*, *tour*,
  *relief*, *open post*, *hold-over*, *bill rate vs pay rate*, *spread*.

## Personnel-centred records and navigation (2026-09-30)

`PersonDocument`, `Credential`, and `TrainingRecord` are children of `Person` in the schema,
but were created from three top-level menus that asked which person the record belonged to,
and `person_detail` showed none of them. The surface now follows the data:

- **The profile is the hub.** `/people/<id>/` renders tabs Profile · Credentials · Training ·
  Documents · Schedule & time · History, each querying only its own section. Records are
  created at `/people/<id>/credentials/new/`, `…/training/new/`, `…/documents/new/`, where the
  person field is removed from the form rather than merely prefilled — a record opened from a
  profile cannot name anyone else.
- **One compliance queue.** `/compliance/` merges credentials, training, and records into a
  single attention list filterable by kind, by person, and by needs-action/everything, with
  the register pages (`/documents/`, `/training/`) kept as read-mostly cross-roster views that
  deep-link back into the profile.
- **Requirements now declare applicability.** `CredentialType.applies_to` lists the personnel
  categories that must hold the credential (`docs/discovery-decisions.md` requires an
  applicability condition for every built-in requirement). Without it the **Missing** state was
  unrepresentable: a guard with no certificate recorded produces no `Credential` row, so a queue
  built by iterating credentials could never show them. Missing obligations are computed from
  the requirement side in `services.credential_obligations`.
- **Company-level records exist.** `PersonDocument.person` is nullable and `DocumentType.audience`
  is `person | workforce | management`. A handbook is uploaded once, appears in every worker's
  My documents, and is acknowledged per person against the stored SHA-256; a management-audience
  record (PSB licence, insurance) is not released to workers. Migration `0024`; migration `0025`
  rewrites `core_document_tenant_insert/update` with an explicit `person_id IS NOT NULL` guard —
  the old body compared a subselect that yields NULL for a NULL person, so it accepted the new
  rows only by NULL-propagation accident.
- **Configuration is no longer orphaned.** `/settings/` is a hub listing only what the actor's
  role can open. Previously `/settings/security/` and `/settings/domains/` were reachable only
  from the branding page, `/settings/time/` only from Payroll, `/documents/retention/` only from
  Documents, `branch_create` only from the dashboard, and `/people/fields/new/` had **no inbound
  link at all** — typed custom fields, their sensitivity control, and their value coercion were
  unconfigurable from the product. Credential and document type catalogs moved out of the record
  registers into `/settings/compliance/`.
- **Branches have a view.** `Branch` had a create route and no list page, so a branch was
  invisible except as a column elsewhere. `/branches/` shows sites and active personnel attached
  to each and flags the empty ones.
- **The sidebar is grouped and filtered.** Twelve links in a flat list (only two carrying an
  active state) became five labelled groups, and links a role would meet `PermissionDenied` on
  are no longer rendered — an officer previously saw Team access, Payroll, and Audit. Role
  tuples live once in `core/views.py` and the `navigation` context processor filters with them,
  so a link cannot be offered for a page its own decorator refuses.
- **Private records stay private while assignment data is shared.** Documents are
  `RECORD_READERS` (owner, administrator, HR, auditor); credentials and training are readable by
  the full manager set, because `shift_eligibility` blocks assignment on them and a scheduler
  could not previously open the Credentials page at all.

### Defects fixed in passing

- **Four create forms could never save.** `site_create`, `credential_create`, `shift_create`,
  and `training_create` built a `ModelForm` without the tenant on the instance and set
  `item.organization` only after `is_valid()`. Django's `ModelForm._post_clean` copies cleaned
  data onto the instance and runs the model's `clean()` **during validation**, so each
  cross-tenant guard compared against `organization_id = None` and rejected correct input with
  "…must belong to the same organization". `_scoped_form` now seeds the instance with the
  organization. Reported by the product owner as a site-creation error; `test_a_foreign_client_is_still_rejected`
  pins that the fix did not loosen the guard.
- **Browser document upload was broken.** `core/form.html` had no
  `enctype="multipart/form-data"`, so the upload form submitted only a filename and `request.FILES`
  arrived empty. The suite missed it because Django's test client posts files without going
  through HTML encoding.

## First vertical slice

The recommended foundation slice is implemented end to end: idempotent bootstrap creates
an organization, immutable slug, owner, and branch; owners and administrators can issue
audited, single-use, 72-hour invitations; branding supports safe image processing,
contrast validation, preview, publication history, and rollback; branches and personnel
are tenant-scoped; custom personnel documents are validated, malware-scanned, stored
through Django's storage abstraction, and audited; audit records are append-only and
hash-chained; the responsive shell is installable and includes keyboard/reduced-motion
accessibility safeguards; and the same image/configuration contract is represented in
Compose and the DigitalOcean App Platform specification.

This is completion of the deliberately thin foundation slice, not completion of every
first-release workflow or any external production certification. The acceptance gates
below remain in force.

## Confirmed first-release outcomes

The seven confirmed software outcomes are now represented end to end:

1. HCRM includes personnel, custom fields, employment history, private records,
   acknowledgments, retention/disposition, credentials, and training.
2. Time and attendance includes online/offline capture, multiple geofenced sites,
   immutable correction evidence, configurable workweeks and rounding, approval/lock,
   and CSV, XLSX, and PDF payroll exports.
3. Management and worker workflows share a responsive, keyboard-accessible web shell.
4. The installable PWA uses an encrypted offline queue and the same server authorization
   and compliance enforcement as online punches.
5. Texas compliance configuration retains jurisdiction, primary-source URL/reference,
   effective dates, owner-approved interpretation, evidence policy, warning window, and
   separate scheduling/clock enforcement controls. Actual Texas rules must still be
   entered and approved under the legal gate below; the product does not invent them.
6. Transactional delivery supports Mailjet, Amazon SES, and Postmark for email and Amazon
   SNS or Twilio for SMS. Invitations are queued through the same durable notification
   path, including recipients who do not have an account yet.
7. Bulk setup supports downloadable templates, dry-run validation, downloadable row
   errors, duplicate-safe application, and all accepted entity types.

## Completed foundation capabilities

| Area | Implemented capability |
| --- | --- |
| Multi-tenancy | Explicit tenant selection with session rotation, membership enforcement, verified custom-host routing, platform superuser overview, tenant-scoped form/query paths, cross-tenant tests (list views **and** per-object routes: person, document download, import apply, disposition, payroll, audit redaction), model validation, and MySQL reference-integrity triggers for high-risk records. A verified hostname pins the tenant: the membership fallback cannot resolve a different organization on that host. |
| Branding | WCAG-oriented contrast validation, scanned and normalized PNG uploads, immutable brand snapshots and rollback, automatic dark palette, DNS TXT hostname verification, and HTML email branding. |
| Personnel/HCRM | Extended employment/contact profile, typed custom fields, sensitive-field visibility controls, change history, private documents, signed acknowledgments bound to a document hash, imports, granular retention, legal holds, archive/delete approvals, and immutable disposition tombstones. |
| Audit | Application append-only controls, per-tenant SHA-256 hash chains, verification UI, MySQL update/delete rejection triggers (installed **and** asserted by the CI MySQL leg), two-person export-redaction workflow (requester cannot approve; only an approved redaction alters an export), NDJSON export, and an organization retention floor that is recorded and displayed but not yet enforced by any purge job. Audit events themselves are never purged. |
| PWA/offline clock | Responsive installable shell, IndexedDB queue encrypted with a non-extractable WebCrypto AES-GCM key (the key lives in the same browser storage as the ciphertext and the device token, so this is tamper-evidence and at-rest hygiene, not a defense against a compromised browser profile), signed 30-day tenant/user/device credentials, monotonic replay protection with sequence-ordered queue draining, idempotent retries, a 12-hour synchronization limit, rejection of punch times ahead of the server clock, unclosed-shift and orphan clock-out detection, and geofence validation bound either to the shift site or to an explicitly named site. |
| Identity/security | Local auth, Google and Microsoft OIDC/account linking, TOTP/recovery-code MFA, configurable role MFA plus enforced MFA for platform (staff) accounts, login throttling on both the tenant and `/admin/` sign-in paths with proxy-aware client identification, `/admin/` source-range restriction that fails closed, CSRF, restrictive browser headers, secure production settings, dependency/SAST/container scanning CI, Redis cache (sessions remain database-backed), and S3-compatible private media support. |
| Records safety | Personnel-document downloads are authorized per actor, streamed as attachments with `nosniff` and `private, no-store`, and audited. Brand logos are streamed from an authenticated same-origin route instead of a public media path. Payroll CSV/XLSX cells are neutralised against spreadsheet formula injection. |

## Wider product capability already present

Clients/sites, credentials and training, credential-aware scheduling, inherited post
requirements, stated availability, time-off requests with an approval queue, pay-period reopen,
bounded manager authority, paginated registers, post orders, punch review and corrections,
configurable rounding, payroll approval/locking/export, document version lineage, provider-backed
email/SMS notifications, automated compliance reminders, and validated CSV imports are implemented
as first-release workflows.

## Not yet implemented

Each gap below is expanded into a domain section — with the research link that justifies it, the
code it builds on, and a done-criterion — in
[development-roadmap.md](development-roadmap.md). That file is the work list; this section stays
the authority on *what* is absent.

Recorded explicitly because `docs/discovery-decisions.md` and `docs/product-brief.md` list
these as first-release scope. Each is absent, not partially present:

**Scheduling.** Assignment, credential-aware eligibility, post orders, overlap detection, weekly
windowing, edit and cancel, open posts a qualified officer can request, dispatcher approval, stated
availability, time-off requests with an approval queue, overtime warnings at assignment, recurring
series that generate dated posts behind a preview (weekly, every-other-week, or a rotating N-day
cycle with presets for 2-2-3 / 4-on-4-off / 4-on-3-off), a series end date with one-click
generation to it and an opt-in unattended fill, an officer's own `/my-shifts/` roster page,
one-way hand-offs and two-way trades (each decided once, with hours before and after shown to the
approver, expiry when a post starts unanswered, and either kind raisable by a manager on an
officer's behalf), **the hold-over and the split tour** (a stored reason for a tour that ran past its
scheduled end, with the relief who was meant to stand the next tour named, reaching the timecard row;
and a post that takes over another officer's tour, recorded as the pair it is) are implemented.
Missing: any *cross-person* check that a post leaves a site uncovered rather than merely this
officer over-hours — including the 168/336/504 coverage arithmetic, which is deliberately left to
reporting rather than to the generator, and the hours-per-leave basis SCH-3's timecard row would need
to say what a held-over tour cost the following one. Automatic per-crew day↔night rotation, N-party
chained swaps, and Fair Workweek consent machinery are excluded on evidence, not by oversight (see
"Rotations, trades, terms, and who may ask"). Shift bidding is intentionally excluded.

**Scoped authorization.** Bounded branch/client/site authority is implemented for the two field
roles that need it (see "Bounded manager authority" above), including a grant that **follows the
branch on the person's own file** and moves with a reassignment (see "A half-finished payroll slice…"
above — AUTH-1). Missing: client portals, which are absent entirely; and per-user inheritance for the
company-level roles, which are deliberately never narrowed. Scope
choices on the officer-facing `/open-posts/` list — deliberately firm-wide, since a guard may
offer for any post they lawfully stand.

**Clock evidence.** Missing the documented evidence option selfie/photo capture (CLK-1), which the
owner unblocked on 2026-10-04 with three rulings — a configurable retention window from permanent down
to N days, a frame at clock-in and clock-out but not at breaks and only when no other identity method
covered the punch, and opening rights for the subject plus owner/administrator/HR/dispatcher — so the
remaining gap is the capture itself, not a decision (see "Who hears what, and what happens when nobody
answers" for the same day's channel and escalation work, and DD §Sites for the rulings). NFC tags are
not built and were ruled out on
2026-10-03. Shared-kiosk PIN is now built (see "A shared clock station, and the PIN that makes it
evidence"), and so are the mock-location / spoofing-risk signals (see "What a location reading cannot
be"): an implausible accuracy radius, a fix captured minutes before the punch it accompanies, and a
distance no vehicle could have covered all put the punch in time review with the numbers printed beside
it, and none of them stops the clock. Supervisor approval remains the third DD option that is not built.
QR checkpoints are end to
end: a patrol point is created per site, the clock offers only that site's points, and both the
online and the offline path attribute the scan or refuse it — a checkpoint punch that names no
point is rejected outright. Pay-period reopen exists (see "Reopening a locked pay period"), and so does
granularity beneath it: a **lock slice** per branch or contract, or "everywhere else", so one part of a
period can be agreed while another is still being corrected (see "Leave priced on what it displaced…"
above — PAY-4). What remains absent is a slice finer than those two axes — no per-site and no
per-employee lock — and no accrual bank that leave could draw down instead of being priced on posts.

**Evidence policy inheritance.** Resolved (see "Evidence policy inheritance, and the version
behind every number"): contract and site rules exist, each consumed value reports its own source
and version, and the payroll row and the punch audit event both carry the ones that were applied.
**The residual gap is closed** (see "Rule history, the payroll handoff, and a clock that opens
offline"): `RuleRevision` keeps every saved version of a policy, a contract/site rule, or a
credential requirement, is append-only down to a database trigger, and `resolve_rule_version` answers
a `policy_version` stamp after the rule row itself has been deleted — the case that used to send a
reader to the audit chain. Still absent, both by discovery's scope rather than by oversight: no
per-post rule and no branch level. One limit is stated rather than papered over — versions that
predate this table cannot be reconstructed, so a stamp older than a rule's first recorded row
resolves to nothing, and `/settings/time/history/` says so on the page.

**Reminders.** The reminder ladder (`reminder_days_before` plus the warning window), the missing
obligation case, the once-per-rung dedup that replaced the daily re-queue, the re-arm when an
expiry date changes, and the event families that have state to report are implemented (see "Who may
open a record, what a duty can be, and which events speak"): timekeeping (`punch.exception`,
`punch.missing` for a post that closed with no punch at all, the correction request and its answer),
payroll state (`payroll.locked`, `payroll.reopened`, `payroll.exported`), retention
(`retention.disposition_requested`, `…_executed`, `retention.hold_changed`), and operations
(`import.completed`), beside the earlier scheduling, invitation, credential, training, leave, and
acknowledgment events. A credential notice reaches the schedulers and supervisors whose granted
authority covers that person, not only owner/admin/HR.
Missing: administrator-editable templates (NTF-5). Per-audience channel selection (NTF-1) and
escalation after a missed rung (NTF-2) are now built — see "Who hears what, and what happens when
nobody answers" — so the sentence above about *nothing selecting SMS* is no longer true in principle:
an owner can now put text on a family for one audience from `/settings/messaging/`. It remains true in
practice until somebody does, because no rule is seeded and a company that never opens that page keeps
the in-app-plus-email behaviour each call site always asked for. Escalation exists only on the
`CredentialType` ladder; `ComplianceRule` rows and training records warn with nobody to escalate to.
The provider webhooks with the bounce /
complaint / unsubscribe / suppression handling and the consent record that must precede any SMS are now
built — see "Consent that belongs to a number, and the callbacks that end a send path" — so SMS has a
gate, a ledger, an inbound path, and now a rule that can choose it. Quiet hours, number validation and
rate or cost controls from RB's SMS list remain unbuilt, and the SNS `SubscribeURL` is still handed to a
human rather than fetched. The four families that have
nothing durable to report yet — identity/security events (there is no in-app role editor, and
recovery or suspicious-access belong to the authentication pipeline rather than a domain row),
onboarding tasks (`Person.status` is one value, not a checklist), evidence rejection (a failed scan
rolls the row back, so there is no rejected record to announce), and post-order acknowledgment (a
checkpoint tour is completed, not acknowledged).

**Reports and exports.** Timecard rows break out client, site, post, pay and bill rate with the
scope that set each rate, estimated pay, estimated bill and margin, and now the worked
(`raw_hours`) and rounded durations side by side with the rounding rule, its interval, and the
`policy_source` / `policy_version` that produced the paid figure — so a row can be reproduced from
the punches. `/reports/` now shows a compliance rate, a coverage rate and a tour-completion rate on
screen, each with its denominator. **Pay-code grouping and the leave category have landed**: a job or
cost-centre code is set at the contract, the site or the post and resolved with its source recorded,
`payroll_totals` groups the generated snapshot by it, and approved leave is emitted as its own
`pay_category` row rather than as a hole in worked time. **Saved and split reporting has landed** (see
"Figures that survive the evidence changing under them"): each of the three rates is stored nightly for
the company and for every branch and contract with its denominator, its exclusions and the items that
made up "needs action", the page breaks each panel down by branch and contract inside the actor's own
authority, and any stored period downloads as CSV from `/reports/saved/`. **The hour categories have
landed** (PAY-2): break, holiday, training, travel, double-time and differential each carry a firm's own
answer to *paid or unpaid*, at what multiple, and whether the hours count toward the overtime threshold,
a human marks them on the post they apply to with a mandatory reason, and the resulting line names the
rule version that priced it; the overtime multiple itself is now configuration rather than arithmetic
(PAY-5). **Approved leave is now priced too**, on the hours it displaced rather than the calendar span it
covers — each displaced post at its own resolved rate, less anything already paid for it — and it is
never billed on to the client (see the section above). Still missing: a payable basis *of the other
kind*, an accrual or PTO bank that leave draws down rather than being inferred from a schedule;
scheduled *emailing* of a report, which waits on NTF-1's per-audience channel rules; a merged
multi-subject trend line, withheld on purpose because subjects' denominators overlap and summing them
would print a figure that was never measured; and a lock slice finer than a branch or a contract
(no per-site, no per-employee).

**Records.** Version lineage now exists: a re-issued record declares the version it replaces, the
current revision is the only one offered for signature, and earlier signatures stay attached to
the text they were given for (see "Document revisions, and pages that say how big they are").
Secrecy is a ladder of its own now (see "Who may open a record, what a duty can be, and which events
speak"): `DocumentType.sensitivity` separates a claim file or a discipline record from a payroll
receipt that happens to be filed against the same person, and every read path honours it — the person
tab, the register, `My documents`, the download route, the acknowledgment routes, and the compliance
queue with the rate computed from it. Bulk export and recovery have since landed: a personnel file
downloads as a ZIP (dossier + manifest + the records the reader's rung opens), and an archived record can
be restored with a reason that is recorded against the disposition. That DD sentence is now fully
covered: **browser preview shipped the same day** (see "A record can be read where it was found" above),
after the owner ruled "permission to read is permission to view", which supersedes this document's
earlier deliberate refusal to serve uploads inline — and it travels with the sandboxing and allowlisting
the refusal existed to provide. A
worker who signed revision 1 is discoverable on revision 2's roster only as "not signed" on that
revision's list — which is exactly why `signature_lineage` (REC-3) exists: it reports "these N people
have never signed *any* version", "these N signed the text shown", and "these N signed only a superseded
text", as one panel. One limit worth stating:
the ladder classifies a *record type*, so segregation is per type rather than per file — an
organization that files both routine and privileged material under one type must split the type.
What the product has is an **attestation, not an e-signature**: `DocumentAcknowledgment` stores a typed
name, the SHA-256 of the text shown, the statement, a hashed IP and a timestamp, bound to one person and
one revision. Nobody signs the PDF, and no third party can verify a signature without this database.
Document signing, PDF packets and onboarding automation are **not implemented as a feature**. What
changed on 2026-10-03 is the infrastructure decision around them: SIG-0 was ruled (self-host DocuSeal's
community edition rather than pay per document) and the signer now boots inside this project's compose
stack under `--profile signing`, sharing the application's MySQL server as its own database and served
on its own hostname through the same Caddy listener. No code in this application talks to it yet — no
API token, no submission call, no webhook ingest, no packet model — so the sentence to report is *the
signer is installed, the signing feature is not*. The questions that come before any code are now
SIG-4 (one DocuSeal account per installation versus many organizations) and SIG-5 (templates built by
hand in DocuSeal's UI versus the Pro template API), plus whether Form I-9 belongs in the first path at
all; see §13 of the roadmap, which also records what the real boot proved and where it contradicted the
vendor's own pages.

**Queue scope.** `/compliance/`, the directory, `/schedule/`, and `/time/review/` are now bounded
by the actor's granted authority, so a supervisor's queue is their branch's, their contract's, or
their post's. The requirement axis exists on the post side (a client or a site declares what every
officer standing it must hold, gating assignment, open-post announcements, and claims) and the
authorization axis exists on the membership side. What remains organization-wide is deliberate:
records retention, audit, payroll runs, and the rule catalogs are company-level functions, and a
workforce-wide document has one queue for the firm rather than one per branch.

**Platform.** Subscription tiers, entitlements, and metering are absent (the product brief
requires them to stay separate from business records). `Organization.audit_retention_days`
is now enforced — by sealing each closed period and then trimming it from the live chain (see
"Retention the chain can obey: seal the period, then purge"); what remains is that the pass is not yet
wired into the worker loop on a running deployment, and nothing audits whether the archive storage itself
is reachable. **The offline clock now launches from a closed tab:** the
service worker caches `/clock/` as a document and refreshes it on each online load, so the
remaining launch gap in the documented PWA contract is closed — with the shared-kiosk caveat that
the cached page shows the last person's roster while there is no signal, stated in the worker and
on the banner. What still has no device proof is the thing no automated test can give it: an actual
cold start on a real phone in a stairwell, which remains a production gate (PLT-4).

## Production acceptance gates

“Implemented” does not equal an external certification. Before handling real employee or
regulatory data, an operator must complete:

- environment-specific TLS and custom-domain setup, including off-host backup copies
  (see [backup-and-restore.md](backup-and-restore.md): the shipped configuration does not
  yet reach the accepted RPO for a whole-host failure);
- a documented, timed restore test, which has never been run — the log table in that file is
  empty;
- provider credential and webhook setup, plus consent/opt-out policy before any SMS sends;
- accessibility testing beyond the automated landmarks check, and capacity validation at
  the 100-guard/30-concurrent profile;
- entry of the Texas control matrix with owner-approved interpretations (the product ships
  the mechanism, not the rules — and the mechanism now accepts duties as well as credentials,
  so §1702.124 certificate holding and the §1702.128 posting duty have a row to go in);
- independent security and legal review.

The MySQL tenant-integrity and audit-immutability triggers are now exercised by CI, so the
former "MySQL trigger verification" gate is automated rather than operator-performed.
Texas compliance rules remain subject to owner-approved legal interpretation. Native
iOS/Android packaging and subscription billing remain intentionally outside the PWA-first MVP.
