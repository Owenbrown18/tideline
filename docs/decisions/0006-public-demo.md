# 0006: A public, read-only demo over invented businesses

**Date:** 2026-09-18.

**Context.** Tideline is a portfolio piece as well as a tool: recruiters and
prospective clients should be able to see it working. The real dashboard can't
be shown: it names real clients and lists their weak spots (domains that can be
spoofed, a broken contact form). Screenshots alone don't show that it is real.

**Decision.**
- **Showcase data made by the product itself.** `tideline showcase` (src/tideline/showcase.py)
  runs every scheduled run of the last six months through the real code, against
  a simulated web: pages, forms, links and the domain registry answer through
  an httpx mock transport, DNS through a fake resolver, following a script (an
  outage and its recovery, a broken link fixed, a DNS change accepted, a missing
  DMARC record, two problems open now). Only the TLS handshake is simulated
  directly; its verdict still comes from the real expiry rules. The eight
  businesses are invented, and their domains were checked to be unregistered.
- **A third Lambda, `tideline-demo`,** from the same image, with the dashboard in
  demo mode: no sign-in, no JSON API, and anything but a read is refused (in the
  app, and at CloudFront, which only allows GET, HEAD and OPTIONS). A banner says
  the data is invented.
- **Its own role,** which can read and write `showcase.db` and nothing else: no
  real database, no secrets, no email.
- **Rebuilt every morning** by the scheduler, so "Checked 3 days ago" and "Next
  check" are always current.
- **Its own address,** tideline.obwebdesign.ca, through its own CloudFront
  distribution.

**Consequences.**
- Anyone can click around the real product without an account, and nothing
  about a real client is exposed.
- Cost stays in the free tier: one minute of Lambda a day for the rebuild, and
  page views.
- A public, unauthenticated function can be flooded. It shares the account's
  Lambda concurrency with the real run until the limit increase requested on
  2026-09-18 is granted; then the demo gets a small cap and the run a reserved
  slot. The "run throttled" alarm covers the gap.
