# Privacy audit

Status: evidence record, October 22, 2026. The enforceable rules are in
[privacy](privacy.md); this page keeps the findings and their disposition. It
is not a security certification or approval to upload machine data. Review
covered the application at the October 21 trial revision and the transfer
candidate on its separate branch. The transfer component (log upload and
feedback) is not in this repository yet.

Independent review findings and disposition:

| Finding | Disposition |
| --- | --- |
| P1 router identity HMAC keyed by the exported device id | Fixed: private `identity.salt` keys the hash |
| P2 `unit`, `rate_unit` and `reason` admitted any short text | Fixed: closed `UNITS` set; `reason` is a code |
| P3 feedback `context` is unbounded JSON | Deferred by the operator; no submitter attaches context until a schema exists |
| P4 untrusted chunk SQLite parsed without integrity checks | Fixed on the transfer branch, not merged here (`cell_size_check`, `integrity_check`) |
| P5 409 response fields persisted unvalidated | Fixed on the transfer branch, not merged here |
| P6 exported identifiers are hardware fingerprints | To be documented with the transfer component |
| Router identities recorded before the salt switch are keyed by the device id | Open: they stay on Big Mac under the upload hold; any later export must start after the switch or withhold those fields |
| A private application name under an allowed application directory passes `admit_label` | Open: real-data uploads wait on an application-label review |

The requirement is to prevent sensitive machine data from leaving through
measurements, diagnostics or agent feedback. Permission to inspect a machine
does not, by itself, authorize sending its data elsewhere.

## Current boundary

The [storage contract](storage-approach.md#privacy) separates the scanner's
private names from recorded measurements. Export constructs a new SQLite file
from collector records and run metadata; it does not open the scanner store.
The transfer component sends that exported file unchanged. Hashes and identities
protect transfer integrity; they do not establish that its content is safe.

Real-data uploads are on hold. The transfer component's maintainer reports that no chunk,
manifest or feedback has reached Cloudflare. The Worker is not deployed;
existing local checks used synthetic fixtures. That report is deployment
evidence from the owner, not an independent inspection of the cloud account.

## Findings reproduced with synthetic data

| Boundary | Finding | Consequence |
| --- | --- | --- |
| `model.py` | Label and state admission checks syntax. Units accept path-shaped strings; `rate_unit` lacks equivalent validation. | Permitted characters do not establish non-sensitive content. |
| macOS process labels | An application basename under an allowed application directory can pass admission. | A custom application name may identify private work. |
| `store.py` export | Field values and run payloads survive export. A separate scanner canary is excluded. | Scanner exclusion works; collector export is not a content scrubber. |
| Transfer | The real send path carries synthetic canaries in an exported record unchanged. The test intercepted the request before any network call. | Content admission must occur before transmission, including staged retries. |
| Feedback Worker | Free-form text and arbitrary JSON context are stored verbatim. Manifest validation does not close every field. | Agent-authored feedback and extra metadata can carry sensitive material. |

No real files, process labels or credentials were used in these reproductions.
No production collection, upload or service change was performed for the audit.

## Requirements to settle and enforce before real uploads

Keep one content policy shared by source admission and export. Preserve useful
numeric measurements, source units, clocks, coverage and opaque identities.
Define which labels and metadata are approved, with unknown fields rejected or
explicitly withheld. A syntax filter or secret-pattern detector is insufficient
as the only control. Previously stored records and staged retries also need an
explicit disposition; changing future collection alone does not fix them.

The scanner's named map stays local unless the machine owner separately approves
its transfer. Do not include file contents, command lines, environment values,
credentials, private paths, URLs, network names, raw exception descriptions,
conversation transcripts or private screenshots in telemetry or feedback.
These exclusions also apply to error output and test fixtures.

Agent feedback must be deliberately authored and sanitized. Give structured
context a small documented set of keys and types. Free-form text cannot be
certified safe by a character filter; its sending policy must acknowledge that
limit. Share this policy with the receiving agent before issuing its device key.

Before enabling a device, record the approved data inventory, destination,
reader access and retention policy. Verify permitted measurements still arrive,
and synthetic forbidden canaries do not escape through records, manifests,
feedback, errors or retries. Keep real-data transfer disabled until the required
controls and their review are complete. Publishing this document alone does not
clear the hold.

## Next review

Reconcile these findings with the independent reviewer, then turn the agreed
policy into small changes at the owning boundaries. The transfer can remain a
verbatim carrier once its input has a reliable admission contract. Avoid
duplicating speculative secret filters across every layer. The live local
filesystem trial continues separately; its success would not prove export
privacy or Ubuntu readiness.
