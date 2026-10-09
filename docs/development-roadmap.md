# Remaining development items

**Purpose:** the outstanding work, organised by domain, each item tied to the research that
justifies it.
**Status:** forward-looking list. It does not restate what is built — [feature-status.md](feature-status.md)
is the authority on that, and every item below assumes the "Not yet implemented" list there is
current.
**Research recorded:** 2026-09-29 (`research-recommendations.md`) and the peer/vertical benchmark
pass recorded in feature-status.md under "Peer and vertical benchmark, then the gaps it exposed"
(2026-09-30). The link index is at the end of this document.

Read the items as *gaps with a done-criterion*, not as specifications. Requirement sources are
abbreviated: **DD §** = an accepted answer in [discovery-decisions.md](discovery-decisions.md),
**RB §** = a researched default in [research-recommendations.md](research-recommendations.md),
**PB** = [product-brief.md](product-brief.md), **IR** = [implementation-readiness.md](implementation-readiness.md).

"Builds on" names code that already exists so nobody re-plans work that is done. Several
plausible-looking gaps are *not* gaps: TOTP MFA by role, Google/Entra OIDC, SES/Mailjet/Postmark
email, SNS/Twilio SMS, S3-compatible storage, CSV/XLSX/PDF payroll snapshots, seven-entity CSV
import with preview and downloadable row errors, per-document-type retention, legal hold, and
two-approver disposition all exist. They are omitted here deliberately.

---

## 1. Scheduling

| Item | Gap | Builds on |
| --- | --- | --- |
| SCH-3 | ~~Hold-over and split-tour cases~~ **Done 2026-10-03** | `shift_advisories`, `projected_week_hours` |
| SCH-4 | ~~Cross-person coverage validation at assignment~~ **Done 2026-10-03** | `coverage_state`, `uncovered_windows` |

**SCH-1 (recurring templates) and SCH-2 (shift swaps) shipped on 2026-10-01**, and the same date's
follow-up closed the four limits that slice stated on its face: a rotating N-day cycle (2-2-3,
4-on/4-off, 4-on/3-off — the weekday-anchored rule could not express a drifting cycle at all), a
two-way **trade** as its own object decided once (`ShiftExchange`; `ShiftSwap` is what peers call an
offer/handoff), a series end date with one-click generation to it plus an opt-in unattended fill,
and manager-raised moves — with the approver barred from deciding a move that names them. See
feature-status.md §"Rotations, trades, terms, and who may ask". Their IDs stay reserved here so the
cross-references below and in §9 keep pointing somewhere.

What that follow-up **deliberately did not** build is now the scheduling frontier, and it belongs to
SCH-4 rather than to a new item: nothing yet asks whether removing an officer leaves the **post**
uncovered, and the industry's own coverage unit — 168 hours a week for one standing 24/7 post, 336
for two, before a shift-relief factor of roughly 3.3–4.5 for absence — is computed nowhere. One
vendor states the trap exactly: "Patterns such as 4-on/4-off or 2-2-3 can create a clean rotation,
but **a pattern alone does not prove the roster works**."

- **SCH-3 — done 2026-10-03, and the schema decision was "neither of the obvious two".** The
  vertical's own vocabulary (glossaries at [cguardpro.com](https://cguardpro.com/en/glossary) and
  [finetuneus.com](https://finetuneus.com/glossary)) names *relief*, *pass-down*, *hold-over*, *DAR*: a
  hold-over is a tour extended past its end because relief did not arrive, and a split tour is one post
  covered by two officers in the same day. `HoldOver` now stores the reason against the post — the
  published end time, the hour the officer actually came off, the relief who was meant to stand the next
  tour, and who recorded it — and `payroll_rows` puts that sentence on the worked row's exception field,
  which is the done-criterion and the place a dispute looks.
  It is **not** a punch edit and **not** a pay rule: the held-over hours were already inside `raw_hours`
  because the clock recorded them, so the row adds an explanation and no time, and one test compares every
  figure before and after precisely because a hold-over that moved `total_hours` would be paying the same
  stretch twice. `overrun_prompt` measures the accepted clock-out against the scheduled end and asks on the
  page that can answer it, as an observation — a post with no clock-out prompts nothing, because inventing
  an end would convert the gap `punch.missing` exists to report into a claimed fact.
  The split tour became a **link, not a new kind of object**: two officers on one post must be two posts
  (each needs something to clock onto and the client is billed per officer), so `Shift.relief_for` names the
  tour a half takes over, refusing a "relief" at a different site or one starting outside the parent's
  window. Adjacent halves and a mid-tour takeover both count.
  Three decisions carry the item. The scheduled end is **copied**, not read back, because a dispatcher who
  extends `Shift.ends_at` to match the pay erases the evidence that the tour ran long — the row survives
  that edit, which is what `test_the_prompt_measures_against_the_recorded_end_not_the_one_the_dispatcher_moved`
  pins. `relief` is **refused when it is the officer who stayed**, and they are kept out of the picker rather
  than left for the form to reject: a no-show must not hide inside the record of the tour it emptied. And an
  open hold-over is closed by the clock in the worker loop (`close_stale_hold_overs`, beside
  `expire_stale_moves`), never by guessing.
  Note how this item was picked: `set_shift_designation` refuses an oversized designation with "Split the
  post or record a hold-over instead" — the payroll slice was already telling clerks to do a thing the
  product could not do.
  What was deliberately **not** built: no notice is queued (a recipient rule for "the officer who stayed"
  and "the dispatcher who ordered it" is NTF-1/NTF-2's shape, and the audit event carries the fact in the
  meantime); the unrecorded-overrun prompt lives on the post rather than in the review queue, so it reaches
  somebody who opens it; and the 168/336/504 arithmetic SCH-4 also left unbuilt is still unbuilt.
- **SCH-4 — done 2026-10-03.** `coverage_state(shift)` is now the single answer to "does this post put
  a qualified officer on the ground", and `coverage_report` builds its three buckets from it instead of
  from its own inline test. The new half is `uncovered_windows`, which measures a cancelled or declined
  post's *hours* against the other published posts at the same site, so the answer is a length of dark
  time rather than a row that vanished. That framing was forced by a real defect in the obvious
  approach: cancelling a filled post removes it from `coverage_report` entirely, so the report shows no
  unfilled row for the hole it just opened — which is why the measurement happens before the mutation,
  and `test_cancelling_removes_the_post_from_the_report_that_could_otherwise_hide_the_hole` pins it.
  `shift_cancel` records the hours in its audit event, names them in the confirmation message, and
  notifies the dispatch recipients who can still fill the post (never the actor who pressed the
  button); declining a claim states what stays uncovered and stays quiet when relief already covers it.
  An **at-risk** neighbour closes nothing and neither does a **draft** — the report's own rule, because
  an officer who may not stand the post is not coverage, which is the §1702.302(a) case.
  Two assertions carry the item: the advisory and the report are set-equal about which posts are
  filled (one definition, checked through two call paths), and a fixture's relief officer must be
  somebody *else* — two overlapping posts for one person trip the double-booking rule, and the test
  would then be measuring that instead of coverage.
  What was deliberately **not** built: the 168/336-hour weekly coverage arithmetic and the
  shift-relief factor. `uncovered_windows` answers "is this span covered right now", which is the
  dispatcher's question at the moment of decision; a standing-coverage model is a larger design and
  belongs to no item yet, and this says so rather than implying the industry's unit is now computed.

Shift bidding stays excluded (owner ruling, restated in DD §Scheduling).

## 2. Timekeeping, payroll, and the export baseline

| Item | Gap | Builds on |
| --- | --- | --- |
| PAY-2 | ~~Categorized hours (break, holiday, training, travel, differential, double-time) and whether each is paid~~ **Done 2026-10-03, leave basis included** | regular/overtime split in `payroll_rows` |
| PAY-4 | ~~Lock granularity: per-branch/per-client, partial re-lock~~ **Done 2026-10-03** | `PayrollRun.reopen_*`, `TimePolicy.allow_reopen` |
| PAY-5 | ~~Configurable overtime *policy* beyond one threshold~~ **Done 2026-10-03** | `TimePolicy.overtime_after_hours`, hardcoded 1.5× |

**PAY-1 (pay code) and PAY-3 (leave as a category) shipped 2026-10-01**; see feature-status.md
§"Rule history, the payroll handoff, and a clock that opens offline". Their IDs stay reserved so the
cross-references below keep pointing somewhere. `PayCode` inherits post → site → contract exactly as
`post_requirements` does, the export carries `pay_category`/`pay_code`/`pay_code_name`/`cost_centre`
/`pay_code_source`, `payroll_totals` groups the generated snapshot, and approved leave arrives as its
own clipped row. **PAY-2 landed on 2026-10-03 and left that one row where it was** — paid-ness is now a
rule for every designated kind, but leave still carries no money, for the hours-basis reason recorded in
its bullet below rather than because the question was never asked.

**RB §Payroll export baseline** is the column checklist; **DD §Time calculation** requires that
"pay periods, workweek boundary, timezone, overtime rules, rounding rules, payroll locks, columns,
grouping, and export formats are configurable" and that reports "group by employee, branch, site,
and pay code".

- **PAY-2 — done 2026-10-03, from a slice that had been left half-built.** Six kinds (`break`,
  `holiday`, `training`, `travel`, `double_time`, `differential` — the whole list RB §Payroll export
  baseline names), each carrying a firm's own answer to *paid or unpaid*, at what multiple, and whether
  the hours count toward the overtime threshold; a designation made by a human on the post it applies to,
  with a mandatory reason and the writer recorded, because this is the one place in the payroll path where
  a person asserts a fact the punches cannot show. The three switches are not independent and the
  combinations are the design: an unpaid meal takes its hours out of the tour *before the week is
  totalled*, so it can un-charge overtime on a later post (that case has its own test, and it is the only
  shape that matters — deduct at the line and the firm pays premium for hours nobody was paid for);
  paid-but-excluded hours pay at their own multiple and cannot push the week over; a premium category
  leaves its hours in the worked line and prices only the excess over straight time, so an hour is neither
  counted twice nor quietly lost. `test_the_worked_hours_and_the_category_lines_reconcile_to_the_punched_span`
  holds the invariant over all three.
  The roadmap's premise that this had to land with §3's evidence work turned out to be about *provenance*,
  not sequencing: the categories arrived, and what actually had to be finished was the stamp.
  `PayCategory.save()` moves `revision` on any watched change (so no door can edit a multiplier silently)
  while the timecard line prints `rule v2 at 1.50×` — with no `RuleRevision` row behind it, which named a
  version nobody could read back. `revise_pay_category` + `RuleRevision.Kind.PAY_CATEGORY` close it on the
  §4 mechanism; version 1 is written at seeding, since a stamp pointing before the first recorded row is
  reported as unresolvable rather than guessed at.
  **Its last open question closed the same day, by ruling: leave is paid on the hours it displaced.**
  The row used to carry the *calendar* overlap and no money, which was honest but useless — a
  fortnight's vacation spans 336 hours nobody worked, and multiplying those by a rate would invent a
  sum no contract promises. Now each of the officer's scheduled posts inside the approved span
  contributes its overlap minutes, **less anything already paid for that post**, priced at that post's
  own resolved rate; nothing is billed on, and a leave that displaced nothing says which kind of
  nothing it is (nothing scheduled, all worked, unpaid by rule, or a company that never configured a
  leave rule at all — four different answers, and collapsing them is how a missing setting reads as a
  decision). `leave` is a pay category so paidness and multiple are versioned rules like every other
  kind, and it is refused as a designation on a post, because leave already arrives from a decided
  request. Recorded in DD §Time calculation.
  Note also what the item did *not* become: `payroll_rows` prices a designation with the multiplier in
  force at generation, not at the moment the hours were marked. A generated run keeps its snapshot, so an
  approved period does not move; a draft regenerated after an edit does, and the settings message now says
  that instead of implying the opposite.
- **PAY-4 — done 2026-10-03, as the ruling said: a slice that states an exception.** One `PayrollRun`
  stays one period for the company and `PayrollLockSegment` covers a branch, a contract, or "everywhere
  else" beneath it. **No segment ⇒ the run's status governs everything**, which is what keeps every
  existing installation behaving exactly as before. A `locked` slice in a draft period agrees one branch
  while another is still worked; an `open` slice in an approved period is the correction that used to
  require re-locking everybody. `payroll_lock_state` is the single answer the former
  `PayrollRun.status` gates now ask — the clock, the correction request, the correction approval — with
  specific-first precedence (branch, contract, catch-all, run) and a conservative default: a row that
  cannot be placed at a branch or contract takes the run's answer, because an unattributable punch is
  not a licence to edit an approved period. Locking is the payroll approver's act; **opening** a slice
  inside an approved period is owner/admin, since it undoes an approval — the line the whole-period
  reopen already draws. A period with any open slice **cannot be exported**, named rather than merely
  refused, because handing a client a file while part of the period is still moving is the specific
  mistake this feature makes possible.
  Not built, deliberately: no per-site or per-employee slice (the axes come from DD §Sites, and the two
  the product already fences by are the two pay sequences divide on), no notification, and no protection
  against a draft regeneration re-deriving a locked slice's figures — "locked" means the time cannot be
  changed, not that a number cannot be recomputed.
- **PAY-5 — done 2026-10-03.** The premium left the arithmetic: `payroll_rows` multiplies by
  `TimePolicy.overtime_premium` instead of a literal `Decimal("1.5")`, so a contract paying double time
  over its own trigger sets 2.00 on the same screen as the threshold. 1.50 stays the default for the
  reason this file gave — private security contractors do not get §7(k), so the FLSA weekly multiple is
  what a firm without a negotiated rate should start from — and no jurisdiction's numbers are encoded:
  it is a field, with the provenance stamping the clock policy got.
  The item caught a defect in its own landing. `bump_policy_revision` decides *when* the policy's version
  moves and was given `overtime_premium`; `RULE_WATCHED[CLOCK_POLICY]` decides *what a stored version
  contains* and was not. So a stub could resolve to a snapshot carrying every rounding field and no
  multiple — the one number the stamp exists to protect, and a watched-but-unstored value is worse than an
  unwatched one because it is a promise. Both lists now agree, and the test resolves the stamp through
  `resolve_rule_version` rather than reading the live row, asserting version 1 still says 1.50 after the
  row moves to 1.75.

## 3. Clock evidence

| Item | Gap | Builds on |
| --- | --- | --- |
| ~~CLK-1~~ | ~~Selfie / photo capture at punch~~ **Done 2026-10-04** | `Punch`, `TimePolicyOverride` tri-state fields |
| ~~CLK-2~~ | ~~Shared-kiosk PIN~~ **Done 2026-10-03** | `Punch.device_id`, `OfflineClockDevice` |
| CLK-3 | NFC checkpoints | `Checkpoint` + QR path (now end to end) |
| ~~CLK-4~~ | ~~Mock-location / spoofing-risk signal~~ **Done 2026-10-03** | geofence validation in `record_punch`, `Punch.risk_flags` |
| CLK-6 | Supervisor approval of a punch — the last DD §Sites evidence option with no code | nearest existing shape is the correction-request queue, which is a different thing |

**CLK-5 (offline launch) shipped 2026-10-01**: the worker now caches `/clock/` as a document,
refreshes it on every online load, and tells the page when what it rendered came from cache, so the
banner can admit staleness and the page reloads itself on reconnect. The sync half was already
honoured (IndexedDB, non-exportable AES key, sequence and duplicate detection, 12-hour window) — the
queue always survived a cold start; the *page* did not. What remains open is the part a test suite
cannot supply: a real cold start on a real device, which stays a production gate with PLT-4.

**CLK-2 (shared kiosk with PIN) shipped 2026-10-03.** `ClockKiosk` is the station, `Person.clock_pin` is
the credential, and the station's UUID lands in the `Punch.device_id` column that already existed — so no
artefact store was added and the punch table needed no trigger rewrite. The split is the point: the
station says *where*, only a verified PIN says *who*, and a station can never clock anyone in on its own.
Three things it owed beyond the happy path. **A bound that matches how the lookup works**: a refused
guess names no officer (that is what the digest index is for), so nothing can be charged to a person and
the limit sits on the station — twenty refusals in five minutes pause it, one audit row is written when
the window trips rather than one per guess, and a supervisor clears it. The per-officer lockout that
`verify_clock_pin` does implement is for the path where identity comes first, and it is deliberately
**not** transactional: the refusal *is* the write, and a caller catching the raise inside its own
`transaction.atomic` would roll the attempt count back and leave a guesser unlimited tries.
**Uniqueness, because a shared PIN destroys the evidence**: `set_clock_pin` refuses a collision in
application code (MySQL does not enforce a conditional unique) and `unique_clock_pin_in_org` holds it on
the hermetic leg; the PIN is stored as a stretched hash and the fast lookup is an HMAC over the
deployment secret, so a table dump yields nothing and a match is still confirmed against the hash.
**The ownership rule, stated at the boundary**: `record_punch` now refuses a punch linked to a post
assigned to somebody else, which is what makes the PIN mean anything at a shared station — and was
already reachable on the online and offline paths, both of which took a `shift_id` from the page.
Two drift defects surfaced in the same landing: `bump_policy_revision` kept a hand-written copy of the
watched-field list and now reads `RULE_WATCHED[CLOCK_POLICY]` (the PAY-5 story, inverted), and the
override editor's tri-state mapping became a `TRISTATE_FIELDS` tuple so a new nullable boolean cannot be
left out and render an explicit `False` as "inherit". Not delivered: offline PINs (a PIN cannot be
verified without the server, and an unverified queue at a shared device is the buddy-punching hole back),
any device proof of the pad, and a forced six-digit minimum — a station a visitor could reach wants six
or eight digits, which the page advises rather than imposes.

**DD §Sites, geofences, clocks** enumerates the supported evidence options: "punch location,
selfie/photo, shared kiosk PIN, QR checkpoints, NFC checkpoints, supervisor approval, and
mock-location/spoofing-risk signals", and states registered-device binding is *not* required.
Four of the seven are now built — punch location, the shared-kiosk PIN (CLK-2), the offline launch
(CLK-5), the mock-location / spoofing-risk signal (CLK-4), and the selfie (CLK-1, **shipped
2026-10-04**) — and QR checkpoints are end to end. NFC was ruled out on 2026-10-03. That leaves
**supervisor approval (CLK-6)**, which is a decision record rather than a capture and has no code at
all today; the correction-request queue is its nearest neighbour and is not the same object, so it
wants its own store before it wants a screen.

- **CLK-4 — done 2026-10-03**, and the item's premise had to be corrected before it could be built
  honestly: nothing a browser reports can *prove* a phone forged its position, because the flag that
  would say so (`Location.isFromMockProvider` on Android) is not exposed to the web. So this measures
  implausibility and routes it to a human. Three signals, each with a control case in the test suite:
  a fix claimed good to **2 m or less** (a phone does not produce that; mock providers report 0 or 1
  because they do not fill the radius in), a fix **older than five minutes** than the punch it
  accompanies — **online punches only**, because an offline reading is old by design and applying the
  test to it would write up every post with no signal — and **impossible travel**, over 25 km at more
  than 200 km/h from the officer's previous located punch. The distance floor and the speed ceiling
  together are what keeps a legitimate double-header out of review; a signal that fires on GPS jitter
  trains reviewers to ignore signals.
  Two decisions carry the item. **Measured always, raised only if the rule says so**: the verdict codes
  are stored in `Punch.risk_flags` on every located punch whatever the policy, and the resolved
  `flag_spoof_risk` decides only whether an exception is raised — so turning the switch off leaves the
  readings in place, and the audit event records `flagged: false` beside them, which is the difference
  between a policy decision and a missed detection. And **the clock always takes the time**: nothing
  here raises `ValidationError`, because a guard with a suspicious phone still worked the tour and still
  has to be paid for it. The measurements go into CLK-2's `evidence=` extension point with their own
  source and version, so no artefact column was added to the raw punch. `flag_spoof_risk` joined
  `RULE_WATCHED` on both sides the day it was written — the PAY-5 drift, caught before it could recur —
  and `_location_claims` keeps a missing accuracy `None` rather than `0`, because absence must not read
  as the most suspicious value possible.
- **CLK-1 — done 2026-10-04**, ruled and built the same day. The mechanism it needed had already been
  laid by its neighbours: `record_punch(evidence=)` as the one artefact extension point, the per-field
  company → contract → site resolver with a version stamp, the OWASP upload path with the magic check,
  scan and SHA-256, and a retention column where blank already means permanent — exactly the
  configurable window the owner asked for. So the build added no new field and no new screen:
  `ensure_clock_selfie_type` seeds `clock_selfie` as an ordinary `DocumentType` at 90 days on first need,
  which puts the selfie on the same page that edits every other type's window and reader list, and inherits
  the disposition review, the legal hold and the disclosure ladder without a second copy of any of them.
  The four decisions that are the item:
  a frame at **clock-in and clock-out and not at breaks**, which is one boolean rather than an event-kind
  matrix — a break is an annotation on a tour, not a punch, so there is nothing to exempt and no setting
  to hide it behind; required only **when no other identity method covers that punch**, which is
  `selfie_owed()` and a station PIN, tested in both directions; openable by **the subject, owner,
  administrator, HR and dispatcher**, which cannot reuse the `restricted` rung — `restricted` is
  owner/admin/HR, and widening it would disclose every existing claim, accommodation, leave and screening
  record to dispatch as a side effect, so `biometric` is a fourth rung with exactly those four staff roles
  and the auditor left out; and **the frame is single-use and stale in two minutes**, because one photo
  that can evidence both ends of an eight-hour tour is not evidence about either.
  Three things the landing settled that the ruling could not. **There is no station camera**: the
  exemption says a frame is owed where nothing else proves who, and a station punch is by definition
  PIN-verified, so a camera there would be a rule asking for evidence its own exemption supplies.
  **The checkpoint branch is unreachable and is therefore not built** — the owner's sentence names
  "other identity methods" and a checkpoint scan is one, but `_resolve_checkpoint` refuses a checkpoint
  code on any event kind but `checkpoint`, and a checkpoint is its own kind, so a scan can never also be
  a clock-in; an allowance nobody can invoke is not a safety margin, it is a rule that reads stricter
  than it is. Recorded here rather than buried because it is the one place the implementation is
  narrower than the ruling's wording, and it is narrower by necessity and not by choice. **A retake
  deletes the abandoned frame** (`discard_unattached_selfie`), because a face with no punch behind it is
  not evidence and would otherwise age on the retention clock the owner set *for evidence*; it refuses
  when the frame is attached to a punch or belongs to somebody else, so a crafted `replaces` id is not an
  API for erasing records.
  Offline stays refused: a frame that arrives hours after the moment it shows cannot be distinguished from
  one manufactured during that window — the same reasoning that made CLK-2 refuse offline PINs — so an
  offline punch at a selfie-required post carries the exception, names the reason in
  `exception_reason`, and refuses the photo outright rather than accepting a replayed one.
  Verified on MySQL 8.4: migration 0055 installs `core_punch_selfie_tenant_insert/update` with the
  `IS NOT NULL` prefix on the nullable FK, and 54 triggers are present after 0056.
- **The metadata that outlived the upload — done 2026-10-04, found by CLK-1's own landing.** A frame
  goes through `store_person_document`, and that function stored whatever bytes arrived. Which meant
  every personnel image upload kept its EXIF — GPS coordinates, device make and model, the camera's own
  clock — at `standard` sensitivity by default, a rung whose staff list includes **auditor**, a role this
  schema's own comment describes as often an outside accountant, and in the bytes `personnel_file_zip`
  hands out. The cheapest fix was the pattern already proven in the same file for logos: re-encode.
  `strip_document_metadata` does it before the scan, the digest and the store, so **what is hashed is
  what is kept** — a record whose SHA-256 describes bytes that were thrown away is a digest nobody can
  re-derive. Container preserved rather than normalised, because `verified_type` is the column the preview
  allowlist trusts and a `.jpg` stored as PNG bytes would make it a lie; `Orientation` applied to the
  pixels instead of carried as a tag that is then dropped; nothing resized, because the legibility of a
  photographed certificate is part of the evidence; **fail-closed**, because an image Pillow cannot decode
  is an image whose metadata cannot be *proved* gone, and filing it anyway would record a stripped file
  that still carries a location. That refusal is a real cost and is stated: it also refuses an image a
  browser could render, and it broke eight synthetic `b"\xff\xd8\xff\xe0" + zeros` fixtures in the selfie
  suite, which are now real JPEGs. PDFs are untouched — their bytes are the record an inspection reads —
  and a PDF's XMP block can carry a location, so that is a disclosed limit, not a covered case.
  `SECURITY.md` claimed a general EXIF strip that only the logo path performed; the claim now matches the
  code, and the SSRF row describes the one fetch this pass added.
- **Watch the tri-state trap.** An explicit `False` must not render as "inherit" in an editor;
  `time_policy_override` already had that defect and its test pins it. New evidence fields inherit
  that requirement.

## 4. Evidence policy durability

| Item | Gap | Builds on |
| --- | --- | --- |
| POL-2 | Branch level in the chain (if a customer asks) | contract → site resolver |
| POL-3 | Per-post rule (explicitly *not* in discovery) | same |

**POL-1 shipped 2026-10-01**, and CMP-2 with it, on the single mechanism this section asked for:
`RuleRevision` — append-only, `(kind, rule_id, revision)`, holding the whole watched value set per
version, made tamper-proof by a database trigger in the same shape as the audit chain, resolved by
`services.resolve_rule_version` from the `policy_version` stamp a punch or timecard already carries.
See feature-status.md §"Rule history, the payroll handoff, and a clock that opens offline".
`/settings/time/history/` is the human-facing surface. One limit is on the page rather than in a
commit message: versions older than a rule's first recorded row cannot be reconstructed, and such a
stamp resolves to nothing rather than to the current text dressed up as history.

The inheritance model resolves each consumed field along company → contract → site, stamps every
punch and every payroll row with `policy_source` + `policy_version`, and that is correct — the gap
that was open is closed.

**POL-2/POL-3** are open decisions, not work: DD §Sites only
requires global/client/site, so a branch or post level needs a stated customer need first.

## 5. Notifications, reminders, and delivery lifecycle

| Item | Gap | Builds on |
| --- | --- | --- |
| ~~NTF-1~~ | ~~Per-audience channel selection~~ **Done 2026-10-03** | `ChannelRule`, `queue_notice(..., subject_user_ids=)` |
| ~~NTF-2~~ | ~~Escalation after a missed rung~~ **Done 2026-10-03** | `CredentialType.escalate_after_days`, `queue_compliance_reminders` ladder |
| NTF-3 | ~~Event families~~ **Done 2026-10-01** for timekeeping, payroll state, retention, and operations; identity/security and onboarding tasks remain | `queue_notice`, `record_punch`, `queue_missing_punch_reports` |
| NTF-4 | ~~Bounce/complaint/unsubscribe/suppression handling~~ **Done 2026-10-03** — with the consent that had to precede it | `MessageConsent`, `Suppression`, `DeliveryEvent`, `provider_callback` |
| NTF-5 | Editable HTML templates with brand inheritance + safe variables | `branded_email_html` |

**DD §Email, text messaging, and templates** requires notification coverage "explicitly cataloged
across account, personnel, compliance, scheduling, timekeeping, payroll, document, import, and
system workflows", configurable per-audience recipients, "multiple reminder levels, lead times,
repeat intervals, delivery channels, and escalations", editable templates, and that "delivery,
bounce, complaint, unsubscribe, and suppression events are processed and retained" with mandatory
messages categorised separately from optional ones. **RB §Notification event catalog** is the
enumerated list to check against; **RB §Email and SMS providers** covers the provider docs.

- **NTF-3 — shipped 2026-10-01** for the four families whose state changes already exist, and left
  open where they do not. Queued `event_type` values before this were `membership.invitation`,
  `shift.published` / `changed` / `cancelled` / `open` / `claim_requested` / `claim_<status>`,
  `timeoff.requested` / `approved` / `declined`, `credential.reminder` / `credential.missing`,
  `training.reminder`, and `document.acknowledgment_requested`. Added: **timekeeping** —
  `punch.exception` (fired from `record_punch` whenever a punch lands in review, addressed to the
  dispatcher whose authority reaches the post and *not* to the officer who just punched),
  `punch.missing` (`queue_missing_punch_reports`, run by the new `report_missing_punches` command in
  the worker loop: a published post that closed with no clock-in or clock-out produces no punch row
  at all, which is exactly the defect the benchmark found for credentials), and
  `punch.correction_requested` / `punch.correction_approved` / `punch.correction_rejected`;
  **payroll state** — `payroll.locked`, `payroll.reopened`, `payroll.exported`, all addressed to the
  roles that stand behind the number and never to the actor who pressed the button; **retention and
  holds** — `retention.disposition_requested` (to the *second* approver, since a request that
  notifies its own author is not a second approval), `retention.disposition_executed` (to the
  requester), `retention.hold_changed`; **operations** — `import.completed` carrying the applied and
  parsed counts. Every one of them is deduplicated, and `NotificationEventFamiliesTest` asserts that
  no notice is queued with a blank `deduplication_key` (the tenant unique constraint is conditional
  on a non-empty key, so a blank key silently disables the re-send guard) and that none selects SMS
  while **NTF-4** consent is unmet.
  **Still missing, for a reason:** the identity/security family (password and MFA recovery,
  suspicious access, role changes) has no call site to hang on — there is no in-app role editor
  (`team/` only invites, and the only role assignment happens during `invitation_accept`, which
  already notifies) and authentication events belong to the allauth pipeline rather than a domain row;
  onboarding *tasks* have no model at all (`Person.status` is a single value, not a checklist);
  evidence *rejection* has no persisted state to announce — a failed scan deletes the file and rolls
  the transaction back, so there is no rejected row to point at; and post-order acknowledgment has no
  artifact (a `Checkpoint` tour is completed, not acknowledged). Each of those wants its store before
  it wants a notice, which is the same order §4 established for the compliance matrix.
- **NTF-1 — done 2026-10-03**, on the one line this section insisted on: a rule *selects*, it does not
  *permit*. `ChannelRule` maps (audience, family[, exact event_type]) → outbound channels, `queue_notice`
  resolves the channels **per recipient** rather than per notice, and `send_block_reason` still runs first
  and outranks every rule — so the load-bearing test is the one that puts SMS on a family for a number
  that never opted in and asserts the row comes back `BLOCKED` with **zero attempts spent**. Consent
  belongs to the person and their phone, channel preference belongs to this company, and neither is
  allowed to overrule the other; a setting that could would be the regulatory failure NTF-4 exists to
  prevent, wearing a UI. **In-app is added back whatever the rule says**, because the notification
  centre is the durable copy of *we told you* and a text a carrier dropped leaves no evidence.
  The audience is who a recipient is *for this notice*, not their job title, and being the subject is
  checked before the role map and wins — the case that forced it is a site supervisor whose own
  registration is lapsing, who is one of the "managers" the ladder tells and would otherwise be texted in
  the voice of somebody chasing a colleague. Call sites pass `subject_user_ids=`, which is optional, so a
  company that configures nothing gets exactly the channels each call site always asked for. Families are
  a fixed choice list read from the `event_type` prefixes the code already writes, because free text in a
  channel rule means a notice nobody receives, discovered only when somebody says they were not told.
  The unique constraint is on `(organization, audience, family, event_type)` with `event_type` stored as
  `""` rather than NULL so MySQL enforces it — the conditional uniques elsewhere in this schema are
  skipped (W036) and have to be re-checked in app code; here the database does it, and the form checks
  first so an operator gets a message instead of an `IntegrityError` — measured on 8.4 rather than argued
  from the migration: a second identical rule raises `1062 Duplicate entry`, an exact `event_type` beside
  its family rule does not, and the 0053 cross-tenant consent guard still answers `1644` after 0054 ran,
  with all 50 triggers installed. `/settings/messaging/` shows the
  reach beside each rule (`audience_reach`, counted from the same ledger the send path consults, newest
  answer per destination winning in two queries rather than one per officer), because "text officers
  about credentials" is not a decision an owner can make without knowing how many officers that is.
- **NTF-2 — done 2026-10-03**, hanging where this file said it should: `escalate_after_days` and
  `escalate_to` sit on the `CredentialType` row beside `reminder_days_before`, so a duty's warning and
  its escalation travel together instead of drifting apart in two settings objects. The miss is measured
  by **the state of the record, not by whether the notice was opened** — `Notification.Status.READ`
  exists only for in-app rows, so a read-based rule would escalate an officer who never saw a text and
  stay silent about one who marked the app row read and did nothing. The ladder firing is itself the
  proof the earlier rungs changed nothing: a renewal recorded at 90 days means the 30-day rung never
  becomes a missed one. Escalation means *somebody new* — the escalation audience is subtracted from the
  recipients already addressed and the notice goes out under its own `credential.escalated` event type,
  which NTF-1 can then route on its own. That subtraction earned its keep during the landing: the first
  fixture escalated to a scheduler and produced **nothing**, because a scheduler with no authority scopes
  manages the whole company and was in the reminder audience from the start, so the test now pins that
  fact (`test_a_manager_who_already_stands_in_the_audience_is_not_told_the_same_thing_twice`) instead of
  the assumption that made it. `CredentialType.clean` refuses an escalation lead above the first rung —
  escalating before anything was warned is a wider first notice, not an escalation — and the escalated
  body names the rungs that passed, because an escalation that does not say it is an escalation is a
  second copy of a reminder somebody already read. Scope kept honest: `ComplianceRule` rows and training
  records still warn with nobody to escalate to, and a `credential.missing` notice has no earlier rung.
- **NTF-4 — done 2026-10-03**, on the shape this file asked for rather than the shape that was
  convenient. The consent record is a **ledger keyed on the destination** (`MessageConsent`), not a flag
  on `Person`, because consent belongs to the phone: a carrier reissues numbers, and a flag follows the
  person off the phone and onto whoever holds it next. Every decision is a row and the newest wins, so a
  revoked answer sits *beside* the grant it ended — the pair is the defence, and an overwrite would
  destroy it. `wording` is stored as shown, because consent to a sentence nobody can reproduce is
  consent to nothing.
  Capture is at the account's own start, per the owner's ruling: `invitation_accept` carries the number
  and the tick (`source=signup`, surface and IP in `evidence`), first sign-in asks once on the dashboard
  with the number prefilled and *yes*/*no* both recorded, and `/notifications/text/` is the officer's
  page for changing it. An unticked box with a number writes **no row** — silence is not a refusal, and
  inventing a "no" would make a later real "yes" look like a reversal.
  `Suppression` is the list the send path consults, and it is the "processed" half that a log table alone
  would not be: `send_block_reason` runs **before any provider call**, gives the notice
  `Status.BLOCKED` with **no retry spent**, and encodes RB's warning in one place rather than at forty
  call sites — text needs a current grant and a STOP ends it for *everything*, while email treats an
  unsubscribe as a rule about optional mail only, so a pay notice still goes, and a dead mailbox stops
  even that. `Notification.mandatory` is inferred from the event family when nobody declares it, so no
  notice can be born uncategorised.
  `DeliveryEvent` is the retention half, and it is written for every callback including the ones the code
  does not understand: an unmapped event becomes a `STATUS` row carrying the provider's own name as
  detail, never a guess, because guessing is how a payload rename blocks a reachable officer. A bounce
  suppresses the *next* message and never rewrites the one that bounced.
  Two things this deliberately does **not** do. It never follows an SNS `SubscribeURL`
  automatically: that is a server-side request to an address that arrived inside an unauthenticated
  payload, so the host is extracted and the event is kept pending — and the operator's confirm button
  **landed on 2026-10-04** (§3's sibling work), gated behind `sns_confirmation_target`, an allow-list on
  the *parsed* host that refuses `169.254.169.254`, the userinfo trick, a port, and a path before any
  socket opens. Until somebody clicks it, an SNS deployment's email callbacks still do not flow, which is
  why the messaging page lists the pending subscription instead of reporting success. And it never claims a
  callback was *proven* when it was only addressed: Twilio's signature is required when
  `TWILIO_AUTH_TOKEN` is set, and where a provider documents nothing to verify (SNS, Mailjet) the event
  is stored `verified=False` so a later reader can tell the two apart.
  Still open here, and unchanged by this item: **no rule is seeded**, so nothing selects SMS until an
  owner writes one on `/settings/messaging/` — NTF-1 built the chooser, not a default. Quiet
  hours, number validation, and rate/cost controls from RB's SMS list remain unbuilt; numbers are
  canonicalised to E.164 on a North-American assumption, which is normalisation rather than validation.
- **NTF-5** — templates are generated in code today. Any administrator-editable template needs the
  safe-variable allow-list spelled out in DD, because template editing is an injection surface.

## 6. Records, retention, and the personnel file

| Item | Gap | Builds on |
| --- | --- | --- |
| REC-1 | ~~Bulk export of one personnel file~~ **Done 2026-10-02** | `personnel_file_bundle`, `person_export` |
| REC-2 | ~~Recovery after a disposition executes~~ **Done 2026-10-02** | `restore_disposition`, `DispositionRequest.Status.RESTORED` |
| REC-3 | ~~Cross-revision signature report~~ **Done 2026-10-02** | `signature_lineage`, `document_lineage` |
| REC-4 | ~~Enforcement of `Organization.audit_retention_days`~~ **Done 2026-10-03** — seal, then purge | `AuditSeal`, seal-aware `core_audit_no_delete` |
| REC-5 | Sensitive-record segregation | **Done 2026-10-01** — see below |

**DD §Documents and imports** requires "version history, browser preview, bulk export,
soft deletion/recovery, and owner/admin approval for permanent deletion". Every clause of that sentence
is now implemented — the last of them, browser preview, on 2026-10-03. §6 is therefore closed as a gap
list; what remains in this domain is the signing work in §13.

One loose end from the records set closed on 2026-10-04, and it is recorded here rather than as a new item
because it was never a feature: `core_dispositionrequest` was the only tenant-linked table with no
cross-tenant reference guard — it FKs a `PersonDocument` and was never in the list 0012/0018/0019/0025/0055
policed. The application cannot create the bad state (the view resolves the document through the actor's
own organization), which is why it stood as a disclosed gap rather than an incident. Migration 0056 installs
the INSERT and UPDATE guards; measured on MySQL 8.4 they refuse a raw cross-tenant insert with
`cross-tenant disposition reference`, refuse an UPDATE that walks a request onto another tenant's file, and
still allow the same-tenant case. `document_id` is `NOT NULL` under `on_delete=PROTECT`, so there is no
`IS NOT NULL` prefix to forget here — the 0045 and 0055 defect could not recur on this table by shape.
**Total triggers installed after 0056: 54.**

- **REC-1 — done 2026-10-02.** `personnel_file_bundle` / `personnel_file_zip` produce a ZIP of
  `README.txt`, `dossier.json`, `records-manifest.csv` and the permitted files, and the route is
  reachable two ways with one rule: a record reader within their authority scope, or the officer whose
  file it is. The ladder from REC-5 decides the contents — this is the "and any personnel export built
  from them" path that item's done-criterion named, so it is enforced by the same
  `record_visibility_filter` the register and the download route use, with its own tests.
  Three decisions are recorded rather than assumed. A record the reader's rung does not open is **not
  listed even as withheld**: annotating it would itself disclose its existence to the person it is
  about. Wage and hour figures are **not copied in** — the payroll export is their single source, and a
  personnel file gets the raw events and the posts instead (RPT-4's reasoning applied to exports). And
  the audit event says what left: record types, file count, withheld count, `self_service`.
- **REC-2 — done 2026-10-02, and it required fixing REC-2's premise first.** `archived_at` was written
  by `execute_disposition` and read by nothing, so an archive had no effect and there was nothing to
  recover. Archived records now leave the register, the personnel tab, `My documents`, the compliance
  queue and the acknowledgment reminders, while staying downloadable — retention is not destruction —
  and `/documents/?archived=1` plus a count on the tab mean a moved record is not a missing one.
  Restore is modelled as a state on the `DispositionRequest` (`RESTORED`, `restored_by`,
  `restored_at`, a 10-character reason) rather than as clearing a field, so the record book shows both
  that the disposition executed and that it was reversed. A deletion refuses to be restored and names
  the reason: the bytes are gone, the row and its SHA-256 remain as the evidence of absence.
- **REC-3 — done 2026-10-02.** `signature_lineage` reports the chain a record belongs to (up through
  `supersedes`, down through `revisions`, including versions filed after the row being viewed) and
  splits the roster into *never signed any version*, *signed this text*, and *signed only a superseded
  text*. The second and third are the distinction an inspection turns on: a 2024 signature is evidence
  of what was agreed in 2024 and is not evidence that anyone read the 2026 wording. The panel appears
  only when a chain has more than one version, and both it and the outstanding list are computed from
  one roster so they cannot disagree about who was ever asked.
- **REC-4 — done 2026-10-03**, and the decision it needed was taken by the owner the same day: "seal the
  period, then purge". It is *not* a bulk `DELETE`, which was the trap this line warned about: every
  event stores the hash of the event before it, so trimming the oldest rows leaves the first survivor
  claiming a predecessor that no longer exists, and the daily verification — an insurer, a court — would
  be right to read that as tampering. `AuditSeal` records one closed period: `first_previous`,
  `first_hash`, `last_hash`, `event_count`, the archive's `archive_sha256` and size, and the
  `retention_days` that made it due. Two seals chain into each other — a new period must start exactly
  where the last one ended — so the history stays one line even when the table holds a week, and
  `verify_audit_chain` continues through a *purged* seal's head instead of failing on it. Migration 0052
  replaces `core_audit_no_delete` with a guard that refuses every delete unless the session names a seal
  (`SET @audit_purge_seal`) whose **own period covers the row** and whose organization matches, so a query
  console cannot remove one inconvenient event: the whole period has to have been sealed first, and
  naming a seal unlocks nothing outside it. The service re-checks every precondition in Python too — the
  archive must read back and re-derive hash for hash, the live chain must verify, the live count must
  equal the sealed count, and a period holding a redaction is refused because `AuditRedaction.event` is
  `PROTECT`, the database agreeing that a legal decision and the record it governs stay together.
  Enforced, not destroyed, and said out loud: the bytes remain in storage, addressable by the tenant —
  which is why the archive is the chain *as written*, why export redaction does not apply to it, and why
  `audit_seal_download` is owners/administrators only while the NDJSON export is auditors too. A window
  below 90 days is refused with an explanation rather than silently raised, because a setting that short
  is a wipe, not a policy.
  Two things the item caught in its own landing. The first run of the command test exposed a real hole:
  an archived-but-untrimmed period (from `--no-purge`, a redaction block, or a run that died between the
  steps) made the *next* pass report "nothing due" forever, because the due-date walk continues after the
  last seal — retention would have been silently unenforced by the very thing that makes it resumable;
  both the command and the page now run the purge pass first and `audit_retention_state` names the
  pending ones. And `AuditEvent.save` and `verify_audit_chain` each built the hashed JSON by hand, in
  agreement only by luck — drift there makes every existing row read as tampered — so
  `audit_event_payload`/`audit_event_hash` in `models.py` is now the one definition, used by the writer,
  the reader, and the archive verifier. `seal_audit_history` runs per tenant with `--dry-run`, `--no-purge`
  and `--organization`; wiring it into the worker loop is left as an operator decision, because a nightly
  job that deletes audit rows on its own is not something to switch on silently.
- **REC-5 — done 2026-10-01**, and this was the one item here that was a read-authorization hole
  rather than a missing feature. `DocumentType.audience` says who a record is *issued to*; it cannot
  say who may *open* one, and every family-4 record — workers' compensation, accommodation and leave,
  drug and background screening, investigation and discipline — is attached to one person exactly
  like a payroll receipt is, so a three-value audience choice never separates them. Secrecy is now
  its own ladder (`DocumentType.sensitivity`: `standard` → `restricted` → `sealed`), and the two
  rungs narrow **different axes**: `restricted` takes the read-only auditor (often an outside
  accountant) out of the room and leaves owners, administrators and HR; `sealed` keeps the record
  from the officer it is about as well, which is why its staff list matches `restricted` — the rung is
  about the worker, not the office. Being the subject *denies* even a role that reads sealed files,
  because the staff list exists to let HR open other people's records, not to route around "this one
  is about you".
  Enforced on every path the item named, each with its own test: the person's documents tab, the
  `/documents/` register, the download route, the acknowledgment roster screen, the acknowledge route
  (so guessing a UUID is not a read), `My documents`, and the compliance queue's documents section —
  where the row filter and the **rate computed from it** both take the reader's ladder, so a
  denominator can no longer count a file the reader was never allowed to open. Listing and row-level
  checks are two functions over the same rule (`record_visibility_filter`, `record_readable`), and one
  test asserts set-equality between them for every role, because the regression here is silent: a
  list that stops filtering still renders, and a route that stops checking still serves.
  **The roadmap's premise was stale in one respect and it is worth recording:** it asserted that
  `MANAGERS` reaches `PRIVILEGED + (HR, SCHEDULER, SUPERVISOR)` and so "a scheduler or a field
  supervisor sees every filed record". That hole had already been closed by an uncommitted
  `can_read_records = is_self or role in RECORD_READERS` gate on the documents tab and the queue's
  documents section — the live leak was the *absence of a rung below RECORD_READERS*, i.e. an auditor
  reading a claim or a discipline file, not a dispatcher doing so. `RecordSegregationTest` re-pins the
  scheduler case so the earlier gate cannot quietly widen.
- **Browser preview — done 2026-10-03, having been ruled the same day.** The owner's answer to the
  contradiction was DD's side of it: *"if the person has permission to view a document, there is no
  reason to not let them view it in the page, whether that is inline or in a modal with a thumbnail on
  the original page."* So `document_preview` exists and the standing "uploads are never served inline"
  posture is superseded — with the refusal's protections carried across rather than dropped: the same
  `_record_open` decision the download makes (one rule, and a test that compares the two routes' status
  codes per reader/record pair rather than checking that preview "works"), a content type taken from
  `PersonDocument.verified_type` — written from inside `validate_document_upload`, so the only way to
  earn one is to pass the signature check — never from the uploader's declared `content_type`, an
  allowlist narrower than what may be stored and with nothing scriptable on it, `script-src 'none'` /
  `object-src 'none'` / `nosniff` / `no-store`, a digest-built response filename so no uploaded string
  reaches a header, and a head-bytes re-read before rendering. Where the object store can truly sign,
  the route redirects to a 90-second link that **overrides** the object's own content type; where it
  cannot — local disk, or a `custom_domain` with no CloudFront signer, whose `url()` comes back as a
  bare public link — it streams instead, because an unsigned URL would replace a per-request permission
  check with an unauthenticated read.
  Still owed, and named rather than buried: no live-bucket run (PLT-3 — the signed branch is tested
  against the decision, not against Spaces), no browser-by-browser render check (PLT-4), and this is the
  one item that should carry a **security review** rather than a green suite, since it deliberately
  widens what uploaded bytes may do inside the product.

## 7. Authorization, identity, and tenancy

| Item | Gap | Builds on |
| --- | --- | --- |
| AUTH-1 | ~~Scope inherited from the person's own `Person.branch`~~ **Done 2026-10-03** | `AuthorityScope`, `ActorScope.for_membership` |
| AUTH-2 | Client portals | `Client`, `AuthorityScope.client` |
| AUTH-3 | ~~Per-organization approved-identity-domain rules~~ **Done 2026-10-03** (domains; tenant ids still owed) | global `MICROSOFT_OIDC_TENANT` env only |
| AUTH-4 | Passkey quick sign-in for the PWA -- **Planned 2026-10-09; not implemented** | Existing login/MFA, account settings, [mobile rollout](mobile-rollout.md) |

**DD §Authentication and authorization** states "Authorization can be scoped by organization,
branch, client, and site" and "A company may restrict organizational roles to an approved Entra
tenant while allowing officers to link approved personal Google identities". The first is done for
the two field roles (unscoped = company-wide, by the owner's ruling recorded in feature-status.md
§Bounded manager authority), and AUTH-1 added the inherited reading of it; the second was configured
at deployment level only, and is now a tenant field the acceptance path checks — see AUTH-3 below,
whose residual gap is the difference between an email domain and a tenant id.
**AUTH-3 done when** the allowed-domain rule is a tenant field that the pipeline checks, and identity
linking still grants no role on an email match alone — **met 2026-10-03**: `Organization
.approved_role_domains` is enforced in `invitation_accept`, the only role-granting path, before any of
its side effects.

- **AUTH-1 — done 2026-10-03, as an option rather than a replacement.** `AuthorityScope.follows_own_branch`
  makes a grant mean "the branch on this person's file, wherever it goes": resolved at request time from
  `Person.branch`, so a reassignment moves the reach with no edit to the grant — the question the explicit
  list dodged, ruled as *follows*, and ruled **not to be the default**, because an unattended change of
  reach when somebody moves branch is a worse failure than one extra click at grant time. A named branch
  grant still does not move (`test_an_explicit_branch_grant_still_does_not_move_when_the_file_moves`), so
  the two options cannot quietly converge.
  Two states carry the design. A file with **no branch on it covers nothing** and the authority screen says
  `their own branch — no branch on the file yet, so this covers nothing`; reading an unanswered question as
  company-wide authority would turn a data-entry gap into the widest reach in the product. And a grant is
  read by **two doors** — `ActorScope` (what a supervisor may list and decide) and
  `dispatch_recipients_for_shift` (who is told about an open post) — which are separate code, so moving one
  without the other would leave a person who cannot open a post but is still notified about it. One test
  performs the same reassignment and asserts the same result through both; `manager_recipients_by_person`
  needed no change because it builds an `ActorScope` rather than re-deriving the answer.
  The duplicate-guard is refused in `clean()` rather than by a constraint: uniqueness here would hang on a
  column that is NULL in every such row, and MySQL treats NULLs as distinct inside a unique index (§11), so
  this is application-side dedup like the rest of the product's.
- **AUTH-3 — done 2026-10-03 for domains; the tenant-id half is still owed.**
  `Organization.approved_role_domains` is read by `role_domain_gate` at the one place a role is ever
  assigned, and **before any of that path's side effects** — a refusal creates no membership, does not
  mark the mailbox verified, and leaves the invitation live so the right account can still accept it.
  That ordering is the item: acceptance verifies the address precisely so an MFA-required role can
  enrol, so a gate placed after it would hand a rejected identity a verified email anyway. Domains are
  normalized at read, not write, because the field is a JSON list a fixture can also fill and a stored
  `" GuardCo.COM "` that failed to match would refuse the right person. Empty means no restriction, so
  every install behaves as before, and **Officer is never gated** — that is DD's "officers may link
  personal identities" clause, not a loophole.
  What remains is the stronger claim: a **tenant id**, not a domain. allauth's Microsoft provider hands
  back Graph's `/me` payload, which carries no `tid`, so reading it means a custom provider adapter and
  id-token handling that cannot be verified without a live directory — and a rule nobody has watched
  fail is a worse artifact than no rule. Two consequences stated plainly: custom-UPN tenants whose
  domain and tenant disagree get the domain answer, and anyone who can read the invited mailbox still
  gets the role, because the invitation link *is* the proof of the address. The follow-up reuses this
  gate with a second list; the enforcement point does not change.
- **AUTH-2 (client portals)** is the biggest single item on this list: a client sees coverage,
  invoices and post orders for their own sites and nothing else. It needs `AuthorityScope`-like
  bounding for a non-employee principal, which does not exist yet — no Membership shape for an
  external viewer. Sequence it after §9 reporting, since a portal is largely reports with a fence.
- **AUTH-4 -- passkey quick sign-in.** After normal sign-in and any required MFA, let a user
  opt into quick sign-in by enrolling a WebAuthn passkey from account settings. On subsequent
  online visits, offer "Unlock / Sign in" using the device's biometric or device-PIN prompt.
  The OS/browser verifies the user; the application never receives the device PIN or biometric
  data. The server verifies the signed challenge before establishing a session, rather than
  accepting an app PIN that unlocks a stored bearer token. Feasibility is high on compatible
  devices/browsers; estimated implementation complexity is moderate, subject to confirming
  support in the installed authentication stack.
  - Define how a user-verified passkey satisfies the existing MFA policy, including admin
    access, without weakening required assurance or unnecessarily repeating TOTP.
  - Keep normal login/MFA and an authorized recovery path for unsupported devices or lost
    credentials. Account settings must support viewing and removing enrolled credentials;
    removal must prevent their subsequent use.
  - Treat reauthentication after inactivity as a separate, server-enforced policy decision.
    Do not promise an unlock prompt on every PWA launch/resume: browser lifecycle behavior
    varies. Offline authentication/unlock is outside this item's scope.
  - **Done when:** enrollment and quick sign-in work on supported Android and iOS PWA
    surfaces over the production HTTPS origin; user verification is required; invalid,
    replayed, and revoked credentials are refused; existing account/company authorization,
    MFA requirements, fallback login, and recovery remain enforced. Verify physical-device
    behavior and document browser support and any native-wrapper limitations before rollout.
- Scope choices on `/open-posts/` stay deliberately firm-wide (a guard may offer for any post they
  lawfully stand); that is a ruling, not a gap.

## 8. Compliance control matrix content

The mechanism is complete: `CredentialType` carries jurisdiction, `authority_url`,
`authority_reference`, `interpretation`, `effective_from/until`, `blocks_scheduling`,
`blocks_clock_in`, `applies_to`, `reminder_days_before`, and approval by a named actor — and
`/settings/compliance` renders it as a matrix. What remains is **the content itself, plus two
additions to the mechanism**:

- **CMP-0 — done 2026-10-01.** `ComplianceRule` is the generalised subject: an obligation names its
  `evidence` kind (a filed record, a posted notice, training hours, a credential), *what has to hold
  it* (`applies_to_subject`: the company, officers in the named categories, each site), the record
  type that closes it, its authority and interpretation and effective dates, its renewal window and
  reminder ladder, and its approval — the same columns the credential requirement already carried,
  rendered in the **same table** rather than a second screen, via `_compliance_matrix`. It reuses the
  CMP-2 mechanism rather than adding one: `RuleRevision.Kind.COMPLIANCE_RULE`, watched values
  excluding `name`/`code`, seeded by `ensure_rule_history`, and `/settings/time/history/` filters by
  duty like any other rule.
  What the slice deliberately does **not** claim: no eligibility check consults a duty, so the
  matrix's Enforcement column reads "Queue only" for a duty row rather than borrowing the
  credential row's "Schedule / Clock-in". And `compliance_duties` scores only the two shapes whose
  evidence store exists — a record the company holds and a record each applicable officer holds. A
  posting duty bound to *sites* has nowhere to be filed against (the personnel register has no site
  column), and training hours have no course matcher, so both are listed as **"Entered, not
  measured"** with the reason and are excluded from the denominator instead of diluting a rate. A
  duty whose evidence type the reader may not open under REC-5 reports the same way rather than
  answering "no certificate filed" about a certificate that is filed and sealed from them — a
  permission decision dressed up as a compliance gap. **CMP-1** is now unblocked for those rule
  shapes; the site- and hours-shaped duties need their evidence store first.
- **CMP-1 — drafted 2026-10-04, and still nothing but a draft until the owner approves it row by row.**
  `core/texas_rules.py` holds five obligations, each with its primary text quoted in `interpretation` and
  the page it was read from on `authority_url`; `/settings/compliance/` offers them as a button and
  `manage.py seed_texas_obligations` writes the same rows headless, with `--dry-run`. Every row lands with
  `approved_by`/`approved_at` empty, so `is_approved` is false and `unevaluated_reason` reads *"not
  approved, so not enforced yet"* — which is what makes this reviewable work rather than encoded law, and
  it is asserted by comparing the queue's own per-kind figures before and after the seed: they do not
  move. Idempotent by code, and an existing row is never rewritten, because a draft somebody has started
  editing — or an approved obligation with versions behind it — is that company's record.
  **Reading the primary text corrected three things this file had carried from the benchmark pass**, and
  the corrections sit on the rows rather than being smoothed over:
  - §1702.124(c) is **$100,000 per occurrence for bodily injury and property damage, $50,000 per
    occurrence for personal injury, $200,000 aggregate** — not the $1M/$2M named in the previous version
    of this bullet, which is the commercial-market and client-contract figure. (f) separately requires
    coverage "sufficient to cover all of the business activities… related to private security"; (e) keeps
    a filed certificate in effect until the insurer gives the department 10 days' notice.
  - **37 TAC §35.8 is "Consumer Information and Signage", not a posting duty.** The posting duty is
    §1702.128 — license posted conspicuously at the principal place of business *and each branch office*.
    §35.8 is a different set of acts: client notice of the license number and the Regulatory Services
    Division's contact details, 10-point minimum type in writing, a sign at each office, and the license
    number at least **one inch high on each side** of any vehicle carrying the company name.
  - **37 TAC §35.22 is captioned "Renewal Individual License Applications"**, and (b) is the sentence the
    hard stop rests on: unless a complete renewal arrives *before* expiry, "no regulated services may be
    performed until a complete renewal application is submitted". §1702.302(a) says it from the license
    side, and (b)-(d) are the ladder the reminder windows are built on — **1-1/2× the fee at 90 days or
    less expired, 2× beyond that under a year, no renewal at all at a year or more** (original application,
    examination included). (e) has the department writing no later than the 30th day before expiry, which
    is why the drafted ladder starts at 180: waiting for DPS's letter means already being late.
  The firearm proficiency certificate is sourced to the **department's published procedure** (the DPS
  individual-license FAQ, read 2026-10-04), not to a statute or rule section, and its row says so; the
  data test refuses any row whose reference names no `§` unless it is labelled `Tex. DPS` and its
  interpretation admits it is not the statute. That is the one drafted duty whose authority is
  administrative guidance, and where a legal review should start.
  **Two duties stay honestly unmeasured after approval** — posting at each office, and consumer signage —
  because both attach to a *place* and the personnel register has no site column to file evidence against;
  they report entered-and-not-measured and stay out of the denominator rather than scoring "no record
  found" as compliance. The two filed-record duties become measurable the moment they are approved, which
  is why their `DocumentType`s are seeded with them.
  **The finding this item surfaced is now closed (ruled 2026-10-05, DD §Compliance status and
  evidence).** Credential and schedule eligibility used to read `blocks_clock_in` / `blocks_scheduling`
  and `active` while ignoring `is_approved`, so a requirement with no source, no reference and no named
  approver could refuse a clock-in, while a drafted `ComplianceRule` refused nothing — one matrix, two
  meanings for "approved". The ruling is that **approval gates enforcement**, and the part that made it
  safe is that it does not reach backwards by switching existing rows off:
  `CredentialType.enforcement_grandfathered` (migration 0057, default **True**) keeps a row that has
  been enforcing for months enforcing, and the matrix labels it **"Schedule Clock-in · grandfathered"**
  with the sub-label *"Enforcing without approval — approve it or clear the flags"*, so the debt is a
  visible list on the screen the approver already works from rather than a silent exemption. The
  settings screen clears the flag for every row it **creates** (a new requirement gates nothing until
  approved) and for every row it **approves** (one reason to enforce, not two), and deliberately does
  *not* clear it on a plain edit — a rename must not stop enforcing the obligation it has been
  enforcing. `may_enforce` is the single predicate all three gates use: the clock's
  `prohibited_credentials`, `shift_eligibility`, and the open-post candidate filter.
  Legal review remains a production gate before any of it is relied on. Sources:
  [§1702.124](https://texas.public.law/statutes/tex._occ._code_section_1702.124),
  [§1702.128](https://texas.public.law/statutes/tex._occ._code_section_1702.128),
  [§1702.302](https://texas.public.law/statutes/tex._occ._code_section_1702.302),
  [37 TAC §35.8](https://www.law.cornell.edu/regulations/texas/37-Tex-Admin-Code-SS-35-8),
  [37 TAC §35.22](https://www.law.cornell.edu/regulations/texas/37-Tex-Admin-Code-SS-35-22),
  [DPS individual-license FAQ](https://www.dps.texas.gov/section/private-security/faq/individual-license-questions).
- **CMP-2 — evaluation history. Done 2026-10-01**, on the shared mechanism §4 asked for rather than
  a second one: `CredentialType` now carries a `revision`, and every version of its watched values
  is appended to `RuleRevision`, so a reminder computed against a 60-day lead time can still be read
  back after somebody edits it to 30. `name`/`code` are deliberately not watched — a rename is not a
  new rule, and a version number that moves for nothing stops meaning anything.
- **CMP-3 — done 2026-10-03, as the manual record it has to be.** `CredentialRegistryCheck` appends a
  dated, attributed look-up — what the registry showed, the reference it displayed, who recorded it and
  when — and `CredentialType.registry_check_within_days` says how often one is owed, empty meaning no
  periodic check for that requirement. The person's **Credentials** tab shows the newest check and its
  age, and an adverse finding reaches the compliance queue through the row's attention flag and note
  text; the queue *table* has no registry column yet, so a lapsed-only row looks like any other
  satisfied row there (the state is on the row, waiting for that column). The history stays in the
  table, because "when did we last look" is answered by the dates and not by the conclusion. No call is made to `tops.portal.texas.gov`: it is a search UI, and an automated check
  would be a claim this product cannot support.
  **The line that makes this a control rather than a checkbox is what does *not* count.** A check that
  has simply gone unrepeated is `lapsed`, and `lapsed` never joins the attention set, so it cannot
  move the compliance rate or the stored history: a registration the registry confirmed valid and nobody
  has looked up since June is a gap in *our filing*, and counting it as a failed credential would let
  paperwork cancel a post the way an expired registration does. A check that came back
  expired/not-found/mismatch is `adverse`, and that **does** count, because we looked. Both states are
  shown; only one moves the number. Two of the new tests exist solely to hold that boundary, and one of
  them failed for the wrong reason first — a fixture built on an `unverified` credential was already
  attention for its own cause, which is the same "vacuous assertion" trap the records slice met.
  Recording a clean check also writes `Credential.verified_at`, a column that has existed since
  migration 0003 and that **nothing in the codebase ever wrote** — the same dead-column shape
  `archived_at` had before REC-2 — and promotes a credential out of `unverified`/`pending`. It never
  demotes, and it never overrules a status the office set: an active row is left alone when the registry
  page and the licence in front of somebody disagree, and that finding is surfaced for a person to
  decide, not applied silently. Adverse results notify the office *and* the officer, never the recorder.
  Still open here: no provider-side automation (there is no API to automate), and no obligation on the
  compliance-rule side to *require* a check — a duty can name a credential as its evidence without
  demanding registry verification, which is a `ComplianceRule` field if a customer ever asks.

## 9. Reporting and analytics

`/reports/` shows compliance, coverage, and tour-completion rates with denominators and exclusions
on screen. **All four items in this section were open at the last review; RPT-1, RPT-2 and RPT-3
shipped 2026-10-02** and RPT-4 is a constraint the new code is written against rather than a task.
What the section asked for, and where each landed:

- **RPT-1 — done 2026-10-02.** `ReportSnapshot` stores a figure with the denominator it was divided
  by, the kinds it counted, what it excluded (drafts, non-applicable categories, inactive personnel),
  the reader basis it was taken on, and the items that made up "needs action". A `capture_reports`
  command runs in the worker loop and stores the company plus **every branch and every contract** each
  day; `/reports/` gains a "Save today's figures" action, a per-panel breakdown and a stored-history
  table, and `/reports/saved/` lists, filters and downloads any period as CSV (`snapshot_csv`, which
  passes every cell through `safe_cell` for the same reason the payroll exporter does).
  Two decisions are recorded in the schema rather than in a comment. **A day is captured once and
  never rewritten** — a record that updated whenever evidence changed underneath it would record
  nothing, and `captured_at` is what makes "92% on 12 June" a defensible sentence; the early
  `exists()` test that enforces it is also what keeps a 60-second worker loop from recomputing three
  figures per subject 1,440 times a day. And **`subject_key` exists instead of a nullable branch/client
  pair** because MySQL treats NULLs as distinct inside a unique index, so a
  `(organization, date, NULL, NULL)` constraint would accept a second company-level row for the same
  day — the one duplicate that breaks a trend. The constraint is unconditional, and verified refusing
  a duplicate on a real server.
- **RPT-2 — done 2026-10-02, as stored history rather than a line.** Two things about the figures are
  load-bearing: a past rate can only come from a stored row (recomputing answers the question about
  today), and a supervisor holding two branches *and* a contract has subjects whose denominators
  overlap — an officer in branch A who stands a site under contract X is counted in both. Summing them
  into one series would print a number that was never measured anywhere, so the history is a table by
  subject and date, and the page says why. `ReportSnapshotTest` pins both halves: it captures a day,
  fixes the evidence the day reported, and asserts the stored figure did **not** move while the live
  figure **did** — a test that would pass silently if both were recomputed from the same rows.
- **RPT-3 — done 2026-10-02.** Each panel splits by branch, and coverage and tours split by contract
  too, bounded by the actor's own grants. The split groups the rows the panel above was already built
  from rather than re-running each computation per subject, so the parts add up to the whole by
  construction — and a person with no branch recorded gets their own line instead of disappearing,
  because the officer nobody filed a branch for is the one most likely to be the gap. An obligation
  has no contract split and the page says so: a credential attaches to a person and their branch, and
  guessing a contract from where they happen to work would be a second, weaker definition of the same
  question.
- **RPT-4 — held, and it caught something.** `compliance_attendance` remains the only definition of
  "needs attention": the panel, the breakdown and the capture all read it (or the single
  `report_figures` that wraps it). The first run of the new breakdown test failed for a real reason —
  grouping *all* queue rows counted the duties that CMP-0 lists but does not measure, so the split
  summed to more than the rate above it. `_all_rows` now excludes unmeasured rows for the same reason
  `compliance_summary` does.
  One honest seam stays open and is labelled on the screen: a stored figure is taken on a
  **company-wide reader basis** (every record type counted), because a business record is not one
  person's view — so an auditor's live rate, narrowed by the REC-5 ladder, can differ from the saved
  series. The history table states its basis rather than letting two numbers on the same page appear
  to disagree.

## 10. Platform and commercial

- **PLT-1 — tiers, entitlements, metering.** Absent, and **PB** requires they stay separate from
  business records. Do not bolt features onto `Organization` booleans.
- **PLT-2 — capacity validation** at the DD profile (100 guards, 6 sites, 200 punches/day, 30
  concurrent users, 15 years of records). Profiling, not a feature. It interacts with the accepted
  N+1 costs in §11 — measure before optimizing.
- **PLT-3 — storage adapters.** S3-compatible storage is wired in `config/settings.py`; the DigitalOcean
  Spaces path named in DD §Documents needs an actual run against a real bucket, since
  `querystring_auth` signed URLs and the malware-scan path have only ever been exercised locally.
- **PLT-4 — accessibility and security review** are open production gates
  (feature-status.md §Production acceptance gates, SECURITY.md); automated landmark checks pass,
  human/AT testing has not been done. Two things this pass added to that review's scope, because a green
  suite cannot stand in for either: **the first outbound URL fetch in the product** (the SNS
  confirmation click — its allow-list is a host pattern, so post-resolution IP validation and an egress
  policy are the reviewer's questions, not this file's answers), and a **deliberate widening of what
  uploaded bytes may do inside the product** in two directions — served inline since browser preview,
  and now re-encoded on the way in since the EXIF strip.
- **PLT-5 — restore evidence.** [backup-and-restore.md](backup-and-restore.md) records the shipped
  configuration's RPO against the accepted target and an empty restore log. The timed restore test
  has never been run.

## 11. Accepted technical debt (do not "fix" without a reason)

Recorded so a future contributor meets it as a decision rather than an oversight. Details in
feature-status.md §Known cost of the inheritance model.

- `coverage_report` and `effective_clock_policy` resolve per row (N+1) and per punch (two extra
  queries). Correctness of provenance was chosen over query count deliberately.
- Conditional unique constraints are silently skipped on MySQL (`models.W036`), so dedup is
  application-side only — read the live leg's system-check line for the current count rather than
  trusting a remembered number.
- Report ordering must stay deterministic (`order_by("starts_at","site__name","pk")`) or SQLite and
  MySQL disagree on screen. Any new list inherits this.
- `TransactionTestCase` cannot run against MySQL while the audit guards exist (the teardown `flush`
  is blocked by the immutability trigger), and a streamed `FileResponse` must never be `close()`d
  inside a test.
- Cross-tenant SQL guards are hand-written in migrations; migration parity checks are in CI.
- `tour_completion(organization, scope, start, end, now=None)` **ignores `end`** — its window is
  `ends_at__lte=now` with `starts_at__gte=start`. Both call sites pass a value there, and the live
  page reads correctly because the value passed is the moment of measurement. Left alone deliberately:
  narrowing the window to `end` would change which tours a stored figure counts. Anyone tempted to
  "fix the unused parameter" should re-read `report_figures`, whose stored `window_end` is `now` for
  exactly this reason.
- `assignment_impact` yields one row per **(person, affected workweek)**, and `views.schedule` renders
  one **Monday-to-Sunday** window. Any fixture asserting either must place its posts through
  `services.schedule_week_start` / `week_offset_for`, not `now + n days`: three tests in this suite
  passed mid-week and failed on a Saturday or a late Sunday for exactly that reason, twice in two
  different classes. `ScheduleWindowTest` pins the helpers across ±60 days.
- **A fixture that derives a *date* from `timezone.now()` is anchoring to UTC, and the product's windows
  are local.** `timezone.now().date()` and `timezone.localdate()` disagree for the last hours of every
  day in a UTC-5/6 deployment — which is when this suite is usually run. Found 2026-10-04 as two failures
  that passed at 13:15 and failed at 22:05 on the *same Sunday*: `PayCategoryTest`'s schedule-page check
  (the fixture's "Tuesday" was next week's Tuesday, outside the window the page rendered) and
  `HoldOverTest`'s `close_stale_hold_overs` sweep. Both classes now set `self.now =
  timezone.localtime()`.
  **…and the local-time anchor alone was not enough, which is the part worth keeping.** `HoldOverTest`
  failed again on Monday morning for a second, independent reason: its fixture places the post on
  **Tuesday of the running week**, so on a Monday or Tuesday the tour is genuinely still in the future,
  and a sweep called with no reference is *correctly* refusing to close it — the assertion was testing
  the day of the week. Fixed by passing an explicit `reference=` on every call in that test and adding a
  second test for the branch the wall clock can only reach by luck. **The rule generalises:** a fixture
  that places a post on a weekday of the current week makes *past-versus-future* depend on the run day,
  so any assertion about "has the clock reached it yet" must supply its own reference moment rather than
  inherit `timezone.now()`. Anything else is a test that passes Wednesday through Sunday and fails
  Monday and Tuesday, which is indistinguishable from a real regression at 2 a.m. on a deploy branch.
  **So:** any test computing a weekday, a workweek, a payroll period, or a "has the clock reached it
  yet" comparison takes its date from local time — the same clock the views and the worker loop use —
  and supplies its own `reference=` when the assertion is about a moment rather than an instant. A test
  that only needs an instant may keep `timezone.now()`.

## 12. Suggested ordering

Not a commitment — the owner sequences. The dependency logic only:

1. ~~**PAY-2** (categories and whether each is paid) with **CLK-1..4**~~ — **closed 2026-10-03, and the
   clock family closed behind it on 2026-10-04.** PAY-2 and PAY-5 shipped first and the dependency turned
   out to be about provenance rather than sequencing; CLK-2 then settled the family's shared machinery
   (the per-field evidence switch in the resolver, `record_punch(evidence=)` as one extension point, the
   punch-vs-post ownership rule), and **CLK-4** shipped on the same day using exactly that machinery.
   **CLK-1** landed 2026-10-04 on the owner's three rulings — the retention window (configurable,
   permanent down to N days, as a normal record type), the cadence (clock-in and clock-out, never breaks,
   and only where no other identity method covers the punch), and the viewers (subject + owner/admin/HR/
   dispatcher, which needed a `biometric` rung rather than a widened `restricted`). What the family still
   does not have is DD's **supervisor approval**, newly **CLK-6**, which has no store and no screen, and
   the standing-coverage arithmetic SCH-4 left named. Landing the selfie is what exposed that stored
   uploads keep their EXIF geotag — fixed the same day; see §3.
2. ~~**RPT-1/2**, then **AUTH-2**~~ — **RPT-1/2/3 shipped 2026-10-02** (stored figures, their history,
   and the branch/contract split), which leaves **AUTH-2** standing on its own in this step: a client
   portal is reports behind a fence, and the reports half now exists.
3. ~~**NTF-4**, then **NTF-1**, then **NTF-2**~~ — **§5 is closed except NTF-5.** The order this file set
   held and it is worth saying why: a rule that can choose SMS is only safe to write once consent and
   opt-out are enforced, so NTF-4 (2026-10-03) came first, then **NTF-1**'s channel rules, then
   **NTF-2**'s escalation — and the two compose rather than stack, because NTF-2 decides *who else* hears
   an unanswered rung and NTF-1 decides *on what channel* that person hears it, which is why escalation
   needed no settings of its own. The production gate ("provider credential and webhook setup, plus
   consent/opt-out policy before any SMS sends") is still only half closed: the policy, the ledger, the
   ingest and now the chooser are built, and the credentials and the real provider still have to be
   pointed at each other. Nothing is seeded, so no text leaves this system until an owner says so.
4. **CMP-1 — drafted 2026-10-04; the approving is the owner's, and it is now the shortest path in the
   product.** Five Texas duties sit in `core/texas_rules.py` with their primary text quoted and their
   sources named, offered as one button on `/settings/compliance/` or one command — so what was "enter the
   Texas rules" from a blank table became "read a proposal and approve it row by row", which is the same
   control with most of the typing removed. Nothing is active until the owner approves a row, and
   §1702.124(c)'s real numbers corrected what this file had recorded. Duties whose evidence has no store
   yet (a notice at each site, training hours matched to a course) still need that store first, and the
   newly-recorded question — whether `is_approved` should gate credential enforcement — belongs at the
   front of this step, because it decides what "approved" is allowed to mean.
5. **POL-2/POL-3** are all that is left of this step: **AUTH-1 shipped 2026-10-03** (a grant that
   follows the person's own branch), and §1's **SCH-3** went with it. POL-2/POL-3 remain open *decisions*,
   not work — DD §Sites only requires global/client/site, so a branch or post level needs a stated
   customer need first. REC-5 was the prerequisite inside the records set and is done; **REC-1/2/3 shipped
   2026-10-02** and **REC-4 on 2026-10-03** on the hash-chain ruling ("seal the period, then purge"), so
   §6 is closed as a gap list and §4 is the only section still carrying open items — both of them
   decisions rather than work.
6. **§13 (signing and onboarding) no longer starts with a vendor question.** SIG-0 was ruled on
   2026-10-03 (self-host the community edition) and its container wiring is in `compose.yaml` under
   `--profile signing`, verified by booting it. What gates the next step is now two *product* rulings
   that the boot exposed — **SIG-4** (one DocuSeal account per installation versus many `Organization`
   rows: is signing a self-hosted feature or a hosted one?) and **SIG-5** (templates built by hand in
   DocuSeal's UI versus the Pro template API) — plus whether Form I-9 is in scope at all. If it is taken
   up, **ONB-1 precedes SIG-1**: a signing integration without a task store automates nothing, because
   there is no sequence to run. **NTF-4's callback ingest is a hard dependency** for any provider that
   notifies us back, and DocuSeal documents no webhook secret, so the verification half of NTF-4 is
   load-bearing rather than tidy-up. Set `DOCUSEAL_BACKUP_DATABASES` before the first real signature,
   not after.

Shipped since this list was written: **POL-1 + CMP-2** (rule history, one mechanism), **PAY-1 +
PAY-3** (pay code, leave category), **CLK-5** (offline clock launch), and earlier the scheduling
pair **SCH-1 + SCH-2** with its follow-ups (rotations, terms, two-way trades, manager-raised moves),
and **REC-5 + CMP-0 + NTF-3** (record segregation, the matrix's generalised subject, the missing
event families), and **RPT-1 + RPT-2 + RPT-3** (stored figures with their denominators, their history,
and their branch/contract split), and **REC-1 + REC-2 + REC-3** (the personnel file exported, recovered,
and read across versions), and **ONB-1 + CMP-3 + SCH-4** on 2026-10-03 (the onboarding store, the dated
registry check, and the coverage gap measured before the post disappears — with SIG-0's self-host
decision and its Compose wiring arriving the same day), and later the same day **PAY-2 + PAY-5 + SCH-3 +
AUTH-1** (the hour categories finished from a slice that had been left broken at import, the overtime
multiple moved out of the arithmetic, the hold-over as a stored reason on the timecard row, and an
authority grant that follows the person instead of freezing a branch id), and **PAY-2's leave
basis + PAY-4** (leave priced on the hours it displaced, and a lock that can cover one branch of a
period without touching the rest), and the same day's closing set **CLK-2 + REC-4 + NTF-4** (the shared
clock station and the PIN that makes it evidence, a retention setting the audit chain can obey by sealing
before it purges, and the consent ledger with the provider callbacks that end a send path) and
**NTF-1 + NTF-2 + CLK-4** (channel rules that choose per audience without ever outranking consent, an
unanswered reminder rung reaching somebody new, and the three things that can honestly be measured about
a location reading), and on 2026-10-04 the clock family's last capture plus the outliers behind it:
**CLK-1** (the photo at the ends of a tour, its `biometric` rung, and its single-use and staleness rules),
**the EXIF strip** on every stored image (found by that landing, fail-closed, container preserved),
**the SNS confirmation click** (the last open end of NTF-4, behind a host allow-list rather than automatic),
and **migration 0056** giving `core_dispositionrequest` the tenant guard every other tenant-linked table
already had — 54 triggers on a real MySQL 8.4.

## 13. Document signing and onboarding packets

**The application-side MVP integration is implemented; deployment acceptance remains.** DocuSeal
runs under the optional Compose `signing` profile. The owner chose templates authored in DocuSeal's
UI, explicit staff sending, and one request per onboarding step. `SigningSettings`, `SigningRequest`
and `SignedArtifact` connect a step to a provider ceremony and locally retained PDFs. Authenticated
polling verifies completion and imports the signed documents plus audit certificate before closing
the step. There is no unauthenticated completion webhook or browser-return shortcut.

**Nothing in the research sources covers signing, and that is still true.** **DD**, **RB**, **PB** and
**IR** were written from a scheduling/payroll/compliance framing; the only signing language anywhere
in them is **DD §HCRM** ("admin-defined … signature or acknowledgment requirements" for custom record
types) and **RB §Notification event catalog** listing "onboarding tasks, document requests/signatures"
as a family to notify about. Neither is a requirement *to* e-sign. Everything in this section that
looks like a requirement is therefore either an owner ruling or a vendor fact, and the facts are
labelled with where they were read.

**What exists today is an attestation, not a signature.** `DocumentAcknowledgment` binds one
`(document, person)` pair with a typed `signature_name`, a fixed `statement`, the `document_sha256` the
person was shown, an `ip_hash` and a timestamp; `DocumentType.signature_required` makes the typed name
mandatory and `acknowledgment_required` drives the roster, `outstanding_acknowledgments`,
`signature_lineage` and `queue_acknowledgment_reminders`. That answers *"did this person acknowledge
this exact text"* — which is what most of the HCRM records actually need — and it is stronger than most
guard software, because it is bound to a hash rather than to a filename. What it is **not**: a
cryptographic signature on the PDF, a signature a third party can verify without our database, or an
out-of-band signing ceremony a worker can complete without signing in.

**Onboarding had no store; it has one now, and it is still not a packet.** Until 2026-10-03
`Person.Status.ONBOARDING` was one value on one column plus a dashboard count, so "automate onboarding"
had nothing to automate *against* — no sequence, no owner, no deadline, no completion state. That is why
NTF-3 deferred the onboarding-notification family and why **ONB-1 came before any signing integration,
not after**; ONB-1 shipped the same day as this review, so `OnboardingItem`/`OnboardingTask` are the
store now, and the remaining gap in this section is the packet and the signer, not the checklist.

| Item | Gap | Builds on |
| --- | --- | --- |
| SIG-0 | ~~Decide build vs integrate vs hybrid~~ **Decided: self-host the community edition.** Compose/env/edge and application-side wiring implemented | `compose.yaml` `signing` profile, `docker/mysql-init-signing.sh`, `Caddyfile`, `.env.example` |
| SIG-4 | ~~One DocuSeal **account** per installation: decide the hosting model~~ **Ruled 2026-10-03: free self-hosted while one company, licensed Pro above one tenant** | `Organization` tenancy, the `signing` profile |
| SIG-5 | **Ruled: templates authored in DocuSeal UI and selected here; no in-app template authoring** | `OnboardingItemForm`, `GET /templates` |
| SIG-1 | **Implemented:** signed PDFs and audit certificate verified and filed locally before task completion | `core/document_signing.py`, `store_person_document`, `SignedArtifact` |
| SIG-2 | **MVP narrowed:** one submission per step, one signer and record type; multi-step packets deferred | `SigningRequest`, `SignedArtifact`, `PersonDocument` |
| ONB-1 | ~~Onboarding tasks with an owner, a due rule and a completion state~~ **Done 2026-10-03** | `OnboardingItem`, `OnboardingTask`, `onboarding_board`, `onboarding_progress` |
| ONB-2 | **Implemented for the selected MVP:** staff explicitly sends; invitations, reminders and verified automatic completion follow. Automatic initial sending is deliberately excluded | `issue_signing_request`, `reconcile_signatures`, `onboarding_reminders` |
| SIG-3 | Signature-capture strength stated per record type, with the I-9 case separated from the rest | `DocumentType.signature_required`, `SEC`-style posture in feature-status.md |

- **SIG-0 — decided: self-host, and here is what that bought and what it costs.** The owner's answer to
  the per-document fee was no; DocuSeal runs in this project's own compose stack. Shipped on 2026-10-03:
  `docuseal` + `docuseal-cache` under `--profile signing` (the default topology is unchanged and nothing
  but Caddy is published), a second Caddy site block for `TSCM_SIGN_ADDRESS`, its own database and user
  **inside the application's existing MySQL server**, a first-boot provisioning script, and every value
  compose interpolates spelled out in `.env.example`. **Verified by booting it**, not by reading the
  vendor's README: against a real MySQL 8.4 the image applied every migration, created 44 tables, served
  `/setup` with rendered HTML, and attached Sidekiq to the named queue backend. Four consequences to
  know before the next slice, each read out of upstream source rather than guessed:
  - **Migrations run inside the web process at boot.** There is no one-shot migrate to serialise them and
    the queue processor is embedded in Puma, so **one replica is the only correct count** — two containers
    started together race the schema. The application's own shape (a `migrate` service, a separate
    `worker`) does not transfer, and `WEB_CONCURRENCY` means something different here: every Puma worker
    forks another queue processor. compose.yaml maps it separately from the app's gunicorn setting for
    exactly that reason.
  - **A blank value is a value, not an absence.** Compose cannot leave an environment entry out
    conditionally, and `${VAR:-}` yields an empty string, which this app reads as *configured*
    (`if ENV['HOST']`) or coerces to zero (`ENV.fetch(name, default).to_i`). The first boot of this
    service failed on precisely that: mapping `SECRET_KEY_BASE` empty made its own key-generation path
    skip, and Devise aborted with "secret_key_base for production environment must be a type of String".
    `SECRET_KEY_BASE`, `HOST` and the SMTP names are therefore deliberately **absent** from compose and
    belong in an optional `docuseal.env` sidecar the service reads if present and ignores if not.
  - **Licence: AGPLv3 with one Section 7(b) term, and the term is about the interface.** Upstream
    `LICENSE_ADDITIONAL_TERMS` reads, in full: *"In accordance with Section 7(b) of the GNU Affero
    General Public License, a covered work must retain the original DocuSeal attribution in interactive
    user interfaces."* The signing page an officer opens will say DocuSeal; removing that is
    white-labeling, which is Pro. Unmodified self-hosting makes the source-availability obligation
    nearly vacuous, but a network-facing AGPL service still owes its users corresponding source — that,
    not the logo, is what counsel should read before this is sold as a hosted product (→ SIG-4).
  - **Separate mail paths.** DocuSeal uses SMTP rather than provider HTTP APIs. Mailjet SMTP can reuse
    its API key/secret as username/password and supports STARTTLS on port 2525, an alternative to
    DigitalOcean's documented blocked ports 25/465/587; connectivity still needs deployment testing.
    TSCM already sends its signing invitations through its selected provider API with DocuSeal mail
    disabled. The compose file keeps that default; SMTP would additionally enable DocuSeal's own mail.
    A standalone SMTP-to-HTTPS relay remains a design option, not implemented functionality: private
    SMTP ingress, provider API adapters, authenticated submission, a durable spool, retries, MIME
    attachments, and observable failures would be required. Deploy on a TCP-capable host; do not
    assume App Platform's public HTTP ingress can expose SMTP.
    Sources: [DocuSeal SMTP](https://www.docuseal.com/docs/configuring-docuseal-via-environment-variables),
    [Mailjet ports](https://dev.mailjet.com/docs/smtp-relay/configuration),
    [DigitalOcean restrictions](https://docs.digitalocean.com/support/why-is-smtp-blocked/).
  **The tier read, corrected against the repository instead of the marketing pages.** The earlier
  version of this bullet claimed the vendor's pages said "the API/embedding surface is Pro", which was
  too broad and would have shelved a feature that is actually available: the README's own base feature
  list includes *"API and Webhooks for integrations"*, so `GET /templates` and `POST /submissions` are
  in the free self-hosted build — that is what makes the packet flow automatable at all. What the Pro
  list names is narrower: template creation *by API* (HTML, and PDF/DOCX with field tags), embedded
  signing form, embedded form builder, bulk send, user roles, automated reminders, invitation/identity
  verification by SMS, conditional fields, SSO/SAML, and white-label. **Re-confirm on the day with two
  authenticated calls against the running container** — one to `GET /templates`, one to
  `POST /submissions` — and note that the API-key issuance path in the self-hosted UI has not been
  walked yet, and that `api.md` documents **no webhook signature or shared secret**, so an inbound
  callback cannot be trusted without a secret carried in the URL itself (that is NTF-4's job).
- **SIG-4 — ruled 2026-10-03: the free self-hosted build serves one company; more than one tenant
  licenses Pro, still on-premises.** Of the three options below, (a) is the product and (b) is the
  upgrade path, and the important consequence is architectural: because the answer changes with the
  number of tenants rather than with the code, the integration has to read its signing backend —
  token, base URL, template ids — from **tenant settings**, not from environment alone. Then moving an
  installation from the free build to a licensed Pro build is a credential change, not a schema change,
  and nothing already signed has to be re-filed. What the ruling does **not** buy is a different system
  of record: Pro's multi-account capability is about who can hold templates and submissions, while the
  secrecy ladder, per-type retention, legal hold, the audit chain and the personnel file all live here,
  so signed bytes still come back and are stored as a revision (SIG-1).
  The question as it stood, kept because it is the reason the ruling matters: the community edition is
  one account per installation, and this app serves many
  `Organization` rows. State the consequence precisely, because it is easy to overread: it is not a
  user-count limit (users are free) and not a document-count limit. It is that templates, submissions
  and signing history inside DocuSeal belong to *one* workspace, while this product's tenancy lives in
  `organization_id` columns guarded by hand-written triggers. If one deployment serves three companies,
  their packets sit in one signing account unless something keeps them apart. Options in rough order of
  cost: **(a)** signing is a self-hosted / single-company capability and a hosted multi-tenant install
  leaves the profile off — cleanest, and consistent with `.do/app.yaml`, which already describes one app
  per company; **(b)** one DocuSeal container per tenant, multiplying a database, a hostname and a
  weekly image bump per tenant; **(c)** the vendor's hosted multi-tenant mode — and note that upstream's
  `MULTITENANT=true` *disables* the embedded queue and stops drawing the Active Storage routes, so it is
  the vendor's own hosting arrangement, not a switch to flip here. This is a product ruling, not a code
  task, and it gates SIG-1: signing one company's handbook is a different build from signing every
  tenant's.
- **SIG-5 — packets are sent by API, but templates are still made by a human.** The field-tag workflow
  (`{{Field Name;role=Signer1;type=date}}` in a PDF, which the auto-detector reads) is a UI action in the
  free build; the API that would let *our* screens create that template is Pro. So the operator story is:
  build each template once in DocuSeal, select its template id and `DocumentType` on an onboarding step here, and
  let the application send them. That answers the question this section was opened with — yes, PDF
  packets can be built and sent for signature — **with one manual step per document type, in a second
  admin surface, with its own login, in a container whose releases are weekly.** The owner accepted
  that trade for MVP; no paid template-authoring API is needed.
- **The dual-hosting question, answered: a second hostname on the same proxy, and the proxy stays
  Caddy.** Three ways to put two applications behind one edge were on the table and two are real.
  **A path prefix (`app.example.com/sign`) does not work**: neither `config/application.rb` nor
  `config/environments/production.rb` sets `relative_url_root`, and there is no `SCRIPT_NAME` handling,
  so stripping the prefix at the proxy leaves the app generating asset, redirect and download URLs that
  omit it — a broken signing page that reads like a proxy bug. **A second port** on one hostname works
  without DNS but puts a port number in the link a guard opens on a phone, and App Platform publishes
  one HTTPS route per service. **A second hostname on the same 443 listener is the answer**, and that is
  what shipped: Caddy selects the certificate by SNI and forwards `Host`; DocuSeal's Account
  App URL setting names the public signing origin used in file URLs. The integration uses that same trusted HTTPS origin
  for API calls, human-facing links and file downloads. That hostname and certificate must also work
  from web/worker containers; a LAN/internal-CA deployment must configure certificate trust rather
  than disabling verification.
  **Traefik instead of Caddy: evaluated, not adopted.** It buys per-service labels, which is pleasant
  past three backends, and costs two things that matter here. Its Docker provider needs
  `/var/run/docker.sock` mounted — a container that can read that socket can start a privileged
  container, which is effectively root on the host, in a stack holding personnel files and signatures;
  the alternative, Traefik's file provider, gives up the labels and means writing routing config by
  hand anyway. And the headers this edge is documented to enforce (`SECURITY.md`: HSTS,
  `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`) are three lines in the Caddyfile today
  and would become named middleware attached to every router, plus a regression test to prove none is
  missed. Automatic HTTPS per name, `tls internal` for LAN hostnames, and one small text file are
  already doing the job at two backends. Revisit past three hostnames, or if reload-free config change
  starts to matter. This is reasoning about the two configurations this repository would need, not a
  benchmark of the two proxies.
- **Still owed by the SIG-0 wiring: backups.** DocuSeal now shares the application's MySQL *server*, but
  `docker/mysql-backup.sh` dumps the databases it is *named*, and the name it ships with is
  `MYSQL_DATABASE`. A signing install is therefore **not** covered by the documented RPO until
  `DOCUSEAL_BACKUP_DATABASES` is set to the signing database — and the way to get that wrong is quiet: a
  green hourly job that omits every signature. It ships empty because naming a database that does not
  exist yet makes every dump fail. PLT-5 (the restore walk has never been timed) now covers a second
  system of record: whoever runs it should restore a signature, not only a punch.
- **SIG-1 — one system of record, and it is this one.** Signed bytes and their audit certificate
  come back through `store_person_document` as new personnel records linked to the exact signing
  request. Reissues retain earlier signed records rather than silently superseding or erasing them.
  This request lineage is separate from the existing typed acknowledgment roster and its
  `signature_lineage` report. The signer may hold the ceremony; it must not hold the personnel file. Two reasons, both from
  work already in this roadmap: the REC-5 ladder is enforced in this app's read paths, so records parked
  in a vendor are records nobody segregates, retains, holds or exports; and the audit chain plus
  per-type retention are the compliance story, which a vendor's own history page does not substitute for.
- **SIG-2 — a packet is a bundle, not a new document kind.** Today one `PersonDocument` is one file with
  one `DocumentType`, one retention rule and one acknowledgment roster; a handover packet is
  N files + one person + one signing event. The minimum change is a join (packet → ordered documents)
  plus one completion state, and the trap is retention: a packet whose I-9, W-4 and handbook
  acknowledgment are one artefact inherits one retention rule, which is wrong for all three
  (**RB §HCRM record catalog**: "Retention must be calculated by record type and event … not a single
  employee-folder date"). Packets therefore reference documents; they do not merge them.
- **ONB-1 — done 2026-10-03: the store exists, and the tick is not what completes a step.**
  `OnboardingItem` is a company-defined step (kind, owner, `due_within_days`, personnel-category
  applicability, an optional `document_type`/`credential_type` it waits on) and `OnboardingTask` is one
  step owed by one person, stamped with the date it was issued *under*. Surfaces: `/settings/onboarding/`
  (definitions plus who is stuck, in one screen — a checklist editor that never shows its consequences
  is a form, not a control), an **Onboarding** tab on `/people/<id>/`, and `/my-onboarding/` for the
  officer. The worker loop runs `onboarding_reminders`, which issues to anybody with no checklist and
  chases the late steps.
  Four decisions are the substance of the item, each with a test:
  - **A step that names evidence is checked against the file, not against a tick.** Marking
    "Hand-order on file" complete while nothing of that type exists is refused, and an **archived**
    record does not satisfy it either — same `archived_at` the register reads since REC-2. Training is
    deliberately *not* a kind: the course matcher CMP-0 recorded as missing is still missing, so a
    training step is a task with instructions rather than a kind that would compare
    `TrainingRecord.course_name` by string and report a completion nobody verified.
  - **The REC-5 ladder is consulted before existence.** A step whose record type is sealed from the
    reader reports `hidden` with `satisfied: None` — not "not on file" — because answering "nothing
    filed" about a claim that is filed and sealed is a permission decision dressed as a compliance
    gap, which is the exact failure `compliance_duties` already guards. The *step* still shows, because
    owing a record is not the secret; holding it is. And the subject of a sealed record is still denied
    it, so an officer cannot read their own sealed claim through the checklist.
  - **A deadline is stored at issue time.** Editing `due_within_days` from 5 to 30 must not move a date
    a reminder was already sent against, so `due_on` is a record of the plan, not a derivation — and a
    person with **no hire date** gets `due_on = NULL` and a visible "no hire date" state rather than a
    date invented from today, because an invented deadline turns a data-entry gap into an officer being
    chased for being late.
  - **Waiving is not the officer's to grant.** Anyone may complete their *own* person-owned steps; a
    waiver is a ruling that an insurance or licensing condition does not apply, so it needs
    `RECORD_WRITERS` and a reason of at least 10 characters, and reopening a decision belongs to the
    office too. The dashboard-style counts come from `onboarding_progress`, one aggregate query, and a
    test asserts its per-person numbers equal what `onboarding_board` lists — the tile and the page are
    one calculation (RPT-4's argument applied here).
  **ONB-2 follows the owner's explicit-send ruling.** The chase is shipped: `onboarding.assigned` and
  `onboarding.overdue` notices, deduplicated per (task, date), addressed to the step's declared owner,
  never SMS while NTF-4 consent is unmet — which also closes the onboarding family NTF-3 deferred for
  having no store to hang on. Staff now hand a signing request to each selected step, and the worker
  verifies and files completion. Initial automatic sending and combined multi-step packets remain
  deliberately outside the selected MVP.
- **SIG-3 — do not put the I-9 in the same bucket as the handbook.** USCIS permits Form I-9 to be
  completed and retained electronically with an electronic signature, but conditions that on a system
  with integrity controls, controls that **prevent and detect** unauthorised alteration *including of
  the signature*, an inspection and quality-assurance programme with periodic checks, an **indexing
  system**, reproducible legible paper copies, documented business processes, and audit trails
  establishing authenticity (**M-274 §101**, see the link index). Several of those this product already
  has — content hashes, an append-only audit chain backed by database triggers, per-type retention,
  legal hold, per-tenant indexing — and two it plainly does not: the QA programme and the written
  process description. The handbook section governing signature *capture* was truncated in the read used
  here, so its full text must be read before implementation; note also that form editions and
  enforcement posture have changed recently, which makes the edition date a field on the record, not an
  assumption. **Recommendation this item exists to record: keep Section 1/2 of the I-9 out of scope for
  the first signing slice** and sign the acknowledgement/handbook/hand-order documents, where an
  attestation bound to a hash *is* the requirement. Texas registration applications and DPS forms are
  separately outside what any third-party signature makes acceptable, and belong to an owner-approved
  interpretation in the §8 matrix rather than to this section.
- **Dependencies to respect.** Outbound webhooks arriving from a signer need the ingest, verification
  and tenant-mapping path **NTF-4** already owes (no provider callback is ingested anywhere today —
  feature-status.md §Notifications), and any document handed to a signer is PII leaving the tenant, so
  it goes through **RB §Secure document upload defaults** and the disclosure rule the personnel export
  already encodes: a record a signer is not entitled to see must not even be named to them.

**Done when** SIG-0's answer is written and its container boots (both 2026-10-03) *and* the two
decisions that answer exposed are ruled: SIG-4's hosting model and SIG-5's manual-template trade. Then:
ONB-1 stores tasks with owner, due rule and state, and `/people/<id>/` shows a new hire what is
outstanding without a human opening three screens; a packet of our own documents can be offered to one
officer, signed remotely on a phone, and lands as a retained revision in this tenant with its ladder,
retention and audit trail intact — with the I-9 and anything DPS issues explicitly excluded from that
first path, and with the signing database named in the backup set before the first real document goes
in.

### MVP setup and acceptance

1. Rebuild web/worker and apply migrations 0058/0059 using the existing one-shot migrator. Preserve
   existing volumes. Start the `signing` profile; existing MySQL volumes need the signing
   database/user provisioned as described in `.env.example`.
2. Configure the DocuSeal hostname with HTTPS. Set `DOCUSEAL_ALLOWED_ORIGINS` to its exact origin,
   such as `https://sign.example.com`, and restart web/worker. Both services must resolve/reach it
   and trust its certificate. For a private local CA, set `DOCUSEAL_CA_BUNDLE` to its public root
   certificate as shown in `.env.example`; it applies only to DocuSeal requests. API and file
   redirects cannot leave that origin; do not configure
   remote-object-storage redirects for this first slice. In **DocuSeal Settings > Account > App URL**,
   set that same HTTPS origin; this controls the URLs DocuSeal returns for signed files.
3. In DocuSeal's UI, create a template with one signer and a required, writable signature field.
   In application **Document signing** settings, enter the origin and API key and enable signing.
   Saving checks the connection. Stored keys are encrypted and never displayed; changing the
   application's `SECRET_KEY` requires re-entering signing credentials.
4. Define an onboarding step of kind **A document signed in DocuSeal**, employee-owned, with its
   template and an active subject-readable personnel record type. One template's PDFs and audit
   certificate share that type's retention/access rules. Use separate steps for different rules.
5. Issue the employee's checklist, then press **Send signing request** from their onboarding tab.
   Provisioning does not send automatically. The employee can use their checklist or invitation
   link; the existing notification channel and SMS-consent rules still apply.
6. Sign on a phone, then check that the worker (or **Check signing status**) files all signed PDFs
   and the audit certificate in the personnel file and closes the step. Repeat a status check and
   verify it creates no duplicate records. Download the locally stored artifacts to inspect them.
7. Include `DOCUSEAL_BACKUP_DATABASES` and DocuSeal media/key storage in backups; rehearse restoring
   both a completed request and a pending one. See `backup-and-restore.md`.

**Local/internal-CA testing:** prefer a hostname with a publicly trusted certificate when possible.
With Caddy `tls internal`, export only its public root certificate and mount a CA bundle read-only
into web/worker through a deployment override; set `REQUESTS_CA_BUNDLE` to that bundle's path
**inside the containers**. Include the normal public CA roots if other HTTPS integrations use
Requests too. Do not mount the entire `caddy_data` volume into the application: it contains private
CA keys as well. Never disable certificate verification. The signing hostname must resolve to
the proxy from inside the containers (an internal DNS record or Docker network alias can provide
that); `sign.localhost` resolving to a web container's own loopback does not reach Caddy.

**Failure handling:** a rejected creation (HTTP 400/401/403/404/422) is shown as not created and may
be corrected/reissued. A timeout, server error or invalid creation response is ambiguous: its
reserved request remains visible and is looked up by external ID without a second POST. Review
that ID in DocuSeal; never clear the reservation or resend while creation is uncertain. Declined
or expired requests can be reissued as a new attempt. Download, scanner and local commit failures
leave the step open, record a visible error and retry with backoff up to one hour. Disabling the
backend pauses checks. Browser redirects and generic record uploads never complete a signing step.

**Implementation verification:** an isolated DocuSeal 3.3.0 instance was used to author a PDF template
in its UI, issue a submission through the application, and complete the browser signing ceremony.
Authenticated API verification and downloads used HTTPS with explicit local-CA trust, not disabled
certificate checks. The signed PDF and audit certificate were filed locally, their hashes matched,
the step completed, and a repeated check created no duplicates. MySQL migrations and signing
request/artifact INSERT/UPDATE guards were also exercised. This is local integration evidence,
not proof of the deployment's phone UX, live email delivery, production scanner or restore procedure.

---

# Research index

The endpoints below are where the benchmark actually came from, recorded with the access failure
found at the time — the obvious URL for most of these is wrong, which is the useful part.
Re-verify before citing any of them externally.

## Peer help centres (generic scheduling/workforce)

| Peer | Working endpoint | What the obvious URL does |
| --- | --- | --- |
| Homebase | `https://support.joinhomebase.com` | `help.homebase.com` does not resolve; `www.homebase.com` times out. It is a Salesforce Experience Cloud site: a plain HTTP GET returns a JS shell, so article text came from a rendered page read. `help.homebase.ai` is an **unrelated** smart-home company — discard those hits. |
| Deputy | `https://help.deputy.com` and the Zendesk JSON API `https://help.deputy.com/api/v2/help_center/en-au/articles/<id>.json` | `help.usedeputy.com` does not resolve. HTML pages return 403 to direct fetch; the JSON API works, and the **en-AU** locale returns content while en-us categories come back empty. Features are region-labelled in the docs (e.g. "Shift templates (US Only)"). |
| When I Work | `https://help.wheniwork.com` | `support.wheniwork.com` fails the TLS handshake. |
| Connecteam | `https://help.connecteam.com/en/` | As expected. Publishes a "set-up guide for a security company operation" — the closest thing to vertical guidance among the generic tools. |

Findings that drove work (see feature-status.md §Peer and vertical benchmark): Homebase has *no*
requirement-side compliance model, so certificates attach to a person and nothing binds a
requirement to a role or post; every scheduler surveyed has an open-post concept (Homebase "Open
Shifts", When I Work "OpenShift", GuardsPro "Vacant Shift"); reminder ladders ship as 30/7/1 and
90/60/30 patterns; a generic scheduler gets the Texas hard stop wrong because it treats an expired
licence as a warning.

## Guard vertical

- **TrackTik / Silvertrac** — `https://trackforce.com`, `https://support.tracktik.com`.
- GuardsPro, OfficerReports, GuardMetrics, TEAM Software, Belfry — named in the pass as the
  products holding the post/tour/relief vocabulary; no stable help-doc endpoint was recorded for
  them, so treat those mentions as leads, not citations.
- **Standard vocabulary** (post, tour, relief, pass-down, DAR, bill rate vs pay rate, spread,
  168/336/504 coverage): `https://cguardpro.com/en/glossary` and
  `https://finetuneus.com/glossary` — the best source found for the industry's own terms.
- **Not guard software**, despite surfacing as candidates: Trak-N-Lite, eMagic, IANX,
  Silverpush (ad-tech), Arcanum (cybersecurity), IntelliSys.

## Texas regulatory primary sources

- Occupations Code Ch. 1702: `https://statutes.capitol.texas.gov/Docs/OC/htm/OC.1702.htm`
  (§1702.302(a) regulated services; §1702.124 insurance; §1702.128 posting duties).
- DPS Private Security Program: `https://www.dps.texas.gov/section/private-security`.
- 37 TAC Ch. 35 (the administrative rules; §35.22(b) individual registration, §35.8 posting):
  read via `law.cornell.edu` during the pass. The Texas Secretary of State's Texas Register rule
  text is the authoritative copy — re-check the section numbers against it before encoding any rule.
- Licence/registration verification: `https://tops.portal.texas.gov/psp-self-service/search/index`
  (search UI only; no public API found — see CMP-3).

## Federal recordkeeping and standards (as cited in research-recommendations.md)

- [USCIS I-9 retention rule (M-274 §10.0)](https://www.uscis.gov/i-9-central/form-i-9-resources/handbook-for-employers-m-274/100-retaining-form-i-9)
- [DOL WHD Fact Sheet 21 — FLSA recordkeeping](https://www.dol.gov/agencies/whd/fact-sheets/21-flsa-recordkeeping)
- [EEOC recordkeeping requirements](https://www.eeoc.gov/employers/recordkeeping-requirements)
- [IRS employment-tax recordkeeping](https://www.irs.gov/businesses/small-businesses-self-employed/employment-tax-recordkeeping)
- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [MDN Progressive Web Apps overview](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps)
- [Hubstaff time-clock rounding guide](https://hubstaff.com/time-tracking/time-clock-rounding) — the
  product owner's reference example for rounding UX.

## Provider documentation surveyed

Adopted by discovery (**DD §Email, text messaging, and templates**) and already implemented:
Amazon SES, Mailjet, [Postmark API](https://postmarkapp.com/developer) for email; Amazon SNS
([phone number as subscriber](https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-phone-number-as-subscriber.html))
and [Twilio Messaging API](https://www.twilio.com/docs/messaging) for SMS — one provider per
installation, no cross-provider failover.

Surveyed in [research-recommendations.md](research-recommendations.md) and **not** adopted:
[Mailgun Email API](https://www.mailgun.com/products/send/email-api/),
[SendGrid Email API](https://sendgrid.com/en-us/solutions/email-api).

NTF-4 is where the provider docs actually matter: bounce, complaint, unsubscribe and suppression
callbacks all arrive from these services and nothing consumes them today.

## Document signing and onboarding (§13, checked 2026-10-03)

**Provenance differs from the rest of this index.** The benchmark pass of 2026-09-29/30 did not cover
e-signature tooling at all, so the links below were pulled on 2026-10-03 to answer one question — "can
we build PDF packets and have the system send them for signature?" — by reading the vendor's own pages
and USCIS's handbook. Vendor pages describe pricing and tier boundaries that change; treat every
capability claim here as unconfirmed until it is read again on the day of the decision. **The second
pass the same day was better evidence**: the upstream source files named below, plus actually booting
`docuseal/docuseal:3.3.0` against MySQL 8.4. Where the two disagree, the source and the container won,
and §13's SIG-0 bullet records the correction.

- [DocuSeal on GitHub](https://github.com/docusealco/docuseal) — the code, the AGPLv3-plus-Section-7(b)
  licence, self-hosting (Docker, `DATABASE_URL` for PostgreSQL/MySQL) and the Pro feature list.
  Read directly on 2026-10-03, which is what corrected the tier claim: `README.md` (base features include
  *"API and Webhooks for integrations"*; the Pro list names template-creation APIs, embedding, bulk
  send, user roles, automated reminders, SMS identity verification, conditional fields, SSO/SAML,
  white-label), `LICENSE_ADDITIONAL_TERMS` (the 7(b) attribution term, quoted in full in §13),
  `Gemfile` (`pg`, `sqlite3`, `trilogy`), `config/database.yml` (the `mysql|trilogy` branch that rewrites
  the scheme to `trilogy` with utf8mb4 — the evidence that one MySQL server can host both),
  `config/dotenv.rb` (generated `SECRET_KEY_BASE`, the derived Redis password, the `DATABASE_URL`
  underscore quirk), `config/puma.rb` (Sidekiq embedded per worker; Redis embedded only when
  `LOCAL_REDIS_URL` is set), `config/environments/production.rb` (`FORCE_SSL`, `ENCRYPTION_SECRET`,
  `ACTIVE_STORAGE_PUBLIC`, the SMTP block, and no `relative_url_root` anywhere), `Dockerfile` (port
  3000, `/data/docuseal`, puma-only CMD), `Procfile` (web only, no worker), and the vendor's own
  `docker-compose.yml` (app + `postgres:18` + caddy, with the version-pinned data path §13 warns about).
- [DocuSeal signing API](https://www.docuseal.com/signing-api) and
  [environment-variable reference](https://www.docuseal.com/docs/configuring-docuseal-via-environment-variables)
  — REST API, SDKs, and the page that reads as though the whole API/embedding surface is Pro and billed
  per document. **That reading was too broad** and is corrected in SIG-0: the Pro names on closer
  inspection are the two *template-creation* APIs and the embedding SDKs. The env-var page is also the
  only place `HOST`, `PRESIGNED_URLS_EXPIRE_MINUTES` and `GOTENBERG_URL` (Pro) are documented, and it
  does not list `ENCRYPTION_SECRET`, `ACTIVE_STORAGE_PUBLIC`, `REDIS_URL`, `LOCAL_REDIS_URL` or
  `SIDEKIQ_THREADS` — those came from the source files above.
- [DocuSeal API reference](https://www.docuseal.com/docs/api) — templates, `POST /submissions` (plus
  `/submissions/pdf`, `/submissions/docx`), webhooks, and the field-tag attribute list.
- [Field-tag guide](https://www.docuseal.com/guides/use-embedded-text-field-tags-in-the-pdf-to-create-a-fillable-form)
  — `{{Field Name;role=Signer1;type=date}}` inside a PDF, which is the shape a packet author would
  actually work in.
- [USCIS: retaining Form I-9](https://www.uscis.gov/i-9-central/completing-form-i-9/retention-and-storage)
  and [M-274 §101 storage systems](https://www.uscis.gov/i-9-central/form-i-9-resources/handbook-for-employers-m-274/100-retaining-form-i-9/101-form-i-9-and-storage-systems)
  — electronic completion and e-signature are permitted **subject to** integrity controls,
  prevent-and-detect controls covering the signature itself, an inspection and quality-assurance
  programme, an indexing system, legible reproduction, documented business processes and audit trails.
  [8 CFR §274a.2](https://codes.findlaw.com/cfr/title-8-aliens-and-nationality/cfr-sect-8-274a-2/) is the
  regulation behind it. **The signature-capture paragraph was truncated in the read used here** — reopen
  §101 in full before designing SIG-3.

## Where each research finding landed

| Research input | Landed as |
| --- | --- |
| RB §HCRM record catalog | `DocumentType` audiences, per-type retention, legal hold, disposition approval |
| RB §Payroll export baseline | `PAYROLL_EXPORT_FIELDS`, worked vs rounded columns, policy provenance (pay codes/categories still open → PAY-1/2) |
| RB §Secure document upload defaults | MIME/extension/signature validation, malware scan, download-only serving |
| RB §Mobile delivery options | installable PWA with Android/iOS installation help, offline clock root launch, 12-hour sync gate; Android TWA/iOS Xcode package sources prepared (store/device gates: [mobile rollout](mobile-rollout.md)) |
| RB §Email and SMS providers | SES/Mailjet/Postmark + SNS/Twilio adapters, per-organization provider choice |
| RB §Notification event catalog | event families now covered; the rest is NTF-3 |
| E-signature tooling (§13) | Self-hosted DocuSeal plus per-step application signing, authenticated reconciliation and local signed PDF/audit filing implemented; manual templates/explicit sending selected; deployment phone ceremony and restore acceptance remain |
| USCIS M-274 §101 electronic I-9 storage | why SIG-3 excludes Form I-9 from the first signing path, and the requirements list to satisfy when it comes back |
| Peer open-post pattern | `ShiftClaim`, request → approve → decline, announcement to qualified officers only |
| Peer reminder ladders | `CredentialType.reminder_days_before`, once-per-rung dedup, re-arm on date change |
| Texas §1702.302(a) / §35.22(b) | `blocks_clock_in` gate at the clock; **mechanism only** — no jurisdiction's numbers in code |
| Guard vocabulary | screen wording and report labels; deliberately **not** new schema fields |
