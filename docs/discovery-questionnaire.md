# Discovery questionnaire status

Rounds 1 and 2 and their follow-up confirmations are complete. Accepted answers are
captured in the [product brief](product-brief.md) and
[Round 2 decision log](discovery-decisions.md). Research sources and accepted technical
recommendations are maintained in
[research recommendations](research-recommendations.md).

## Discovery outcome

The following follow-ups are now resolved:

- invalid credential states block both scheduling and clock-in;
- client/site clock-evidence policies may strengthen or weaken the global policy;
- the first mobile milestone is a responsive, installable PWA spanning phone to desktop;
- Postmark is the third email adapter, and Amazon SNS and Twilio are SMS adapters;
- time rounding is configurable and defaults to exact time when no policy is selected;
- initial recovery targets are RPO 5 minutes and RTO 1 hour; and
- authorized personal-data redaction retains a tamper-evident audit tombstone.

Discovery can now proceed to the bounded MVP requirements, source-cited Texas control
matrix, role/permission matrix, domain model, architecture decisions, and incremental
delivery plan. Any new ambiguity found in those artifacts should be recorded as a
numbered decision rather than silently assumed.
