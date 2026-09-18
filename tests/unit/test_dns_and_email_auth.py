"""Checks 5 and 6, with a fake resolver so no test touches real DNS."""

import pytest

from sitewatch.checks import dns_drift, email_auth
from sitewatch.checks.dns_drift import diff_records
from sitewatch.checks.email_auth import count_lookups, parse_dmarc, spf_records
from sitewatch.checks.resolver import DnsUnavailable, DomainMissing, _normalise

BASELINE = {
    "example.ca": {
        "A": ["76.76.21.21"],
        "MX": ["10 mx1.forwardemail.net"],
        "NS": ["ns1.hostinger.com", "ns2.hostinger.com"],
        "TXT": ["v=spf1 include:_spf.google.com ~all"],
    },
    "www.example.ca": {"CNAME": ["cname.vercel-dns.com"]},
}


class FakeResolver:
    """Answers from a dict of {(name, type): [values]}. Missing keys mean no records."""

    def __init__(self, answers: dict[tuple[str, str], list[str]], fail: str | None = None) -> None:
        self.answers = answers
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def records(self, name: str, rdtype: str) -> list[str]:
        self.calls.append((name, rdtype))
        if self.fail == "missing":
            raise DomainMissing(f"{name} does not exist (NXDOMAIN)")
        if self.fail == "unavailable":
            raise DnsUnavailable(f"{name}: Timeout")
        return self.answers.get((name, rdtype), [])


def resolver_from(records: dict[str, dict[str, list[str]]], **extra: list[str]) -> FakeResolver:
    answers = {
        (name, rdtype): values
        for name, types in records.items()
        for rdtype, values in types.items()
    }
    for key, values in extra.items():
        name, rdtype = key.rsplit("__", 1)
        answers[(name.replace("_dot_", "."), rdtype)] = values
    return FakeResolver(answers)


# --- DNS drift ----------------------------------------------------------------


async def test_first_run_captures_the_baseline(make_clients):
    clients = make_clients(resolver=resolver_from(BASELINE))
    result = await dns_drift.run({"domain": "example.ca"}, clients)
    assert result.status == "ok"
    assert result.detail["baseline_captured"] is True
    assert result.detail["records"] == BASELINE
    assert "Baseline captured" in result.summary


async def test_unchanged_dns_is_ok(make_clients):
    clients = make_clients(resolver=resolver_from(BASELINE))
    result = await dns_drift.run({"domain": "example.ca", "baseline": BASELINE}, clients)
    assert result.status == "ok"
    assert "Matches the accepted baseline" in result.summary
    assert "baseline_captured" not in result.detail


async def test_changed_a_record_fails_and_says_what_changed(make_clients):
    moved = {**BASELINE, "example.ca": {**BASELINE["example.ca"], "A": ["203.0.113.9"]}}
    clients = make_clients(resolver=resolver_from(moved))
    result = await dns_drift.run({"domain": "example.ca", "baseline": BASELINE}, clients)
    assert result.status == "fail"
    assert "example.ca A: 76.76.21.21 -> 203.0.113.9" in result.detail["changes"]
    assert result.detail["baseline"] == BASELINE


async def test_mx_removed_is_reported(make_clients):
    no_mail = {
        **BASELINE,
        "example.ca": {k: v for k, v in BASELINE["example.ca"].items() if k != "MX"},
    }
    clients = make_clients(resolver=resolver_from(no_mail))
    result = await dns_drift.run({"domain": "example.ca", "baseline": BASELINE}, clients)
    assert result.status == "fail"
    assert any("MX removed" in change for change in result.detail["changes"])


async def test_nxdomain_is_a_failure(make_clients):
    clients = make_clients(resolver=FakeResolver({}, fail="missing"))
    result = await dns_drift.run({"domain": "example.ca", "baseline": BASELINE}, clients)
    assert result.status == "fail"
    assert "NXDOMAIN" in result.summary


async def test_our_own_lookup_failure_gives_no_verdict(make_clients):
    clients = make_clients(resolver=FakeResolver({}, fail="unavailable"))
    assert await dns_drift.run({"domain": "example.ca", "baseline": BASELINE}, clients) is None


async def test_both_apex_and_www_are_checked(make_clients):
    resolver = resolver_from(BASELINE)
    await dns_drift.run({"domain": "example.ca"}, make_clients(resolver=resolver))
    assert {name for name, _ in resolver.calls} == {"example.ca", "www.example.ca"}


async def test_addresses_behind_a_cname_are_ignored(make_clients):
    """Vercel rotates the addresses behind a www CNAME; that is not DNS drift."""
    first = {
        "example.ca": {"A": ["76.76.21.21"]},
        "www.example.ca": {"CNAME": ["x.vercel-dns.com"], "A": ["216.150.1.129"]},
    }
    later = {
        "example.ca": {"A": ["76.76.21.21"]},
        "www.example.ca": {"CNAME": ["x.vercel-dns.com"], "A": ["216.150.1.1"]},
    }
    captured = await dns_drift.run(
        {"domain": "example.ca"}, make_clients(resolver=resolver_from(first))
    )
    assert "A" not in captured.detail["records"]["www.example.ca"]

    result = await dns_drift.run(
        {"domain": "example.ca", "baseline": captured.detail["records"]},
        make_clients(resolver=resolver_from(later)),
    )
    assert result.status == "ok"


async def test_a_record_change_without_a_cname_still_fails(make_clients):
    """The apex has no CNAME, so its addresses are the client's own config."""
    before = {"example.ca": {"A": ["76.76.21.21"]}, "www.example.ca": {}}
    after = {"example.ca": {"A": ["203.0.113.9"]}, "www.example.ca": {}}
    captured = await dns_drift.run(
        {"domain": "example.ca"}, make_clients(resolver=resolver_from(before))
    )
    result = await dns_drift.run(
        {"domain": "example.ca", "baseline": captured.detail["records"]},
        make_clients(resolver=resolver_from(after)),
    )
    assert result.status == "fail"


async def test_a_cname_change_is_still_caught(make_clients):
    """If the CNAME itself moves, someone has repointed the site."""
    before = {"example.ca": {}, "www.example.ca": {"CNAME": ["x.vercel-dns.com"], "A": ["1.1.1.1"]}}
    after = {
        "example.ca": {},
        "www.example.ca": {"CNAME": ["someone-else.wixdns.net"], "A": ["1.1.1.1"]},
    }
    captured = await dns_drift.run(
        {"domain": "example.ca"}, make_clients(resolver=resolver_from(before))
    )
    result = await dns_drift.run(
        {"domain": "example.ca", "baseline": captured.detail["records"]},
        make_clients(resolver=resolver_from(after)),
    )
    assert result.status == "fail"
    assert "wixdns" in result.summary


def test_diff_lists_additions_removals_and_changes():
    changes = diff_records(
        {"a.ca": {"A": ["1.1.1.1"], "TXT": ["old"]}},
        {"a.ca": {"A": ["1.1.1.1", "2.2.2.2"], "MX": ["10 mx.a.ca"]}},
    )
    assert changes == [
        "a.ca A: 1.1.1.1 -> 1.1.1.1, 2.2.2.2",
        "a.ca MX added: 10 mx.a.ca",
        "a.ca TXT removed (was old)",
    ]


def test_records_are_normalised_for_comparison():
    assert _normalise("CNAME", "CNAME.Vercel-DNS.com.") == "cname.vercel-dns.com"
    assert _normalise("MX", "10 MX1.ForwardEmail.net.") == "10 mx1.forwardemail.net"
    assert _normalise("TXT", '"v=spf1 " "include:x ~all"') == "v=spf1 include:x ~all"


# --- email authentication -----------------------------------------------------


def clients_for(make_clients, apex_txt: list[str], dmarc_txt: list[str], **includes: list[str]):
    answers: dict[tuple[str, str], list[str]] = {
        ("example.ca", "TXT"): apex_txt,
        ("_dmarc.example.ca", "TXT"): dmarc_txt,
    }
    for name, values in includes.items():
        answers[(name.replace("_", "."), "TXT")] = values
    return make_clients(resolver=FakeResolver(answers))


async def test_valid_spf_and_dmarc_is_ok(make_clients):
    clients = clients_for(
        make_clients,
        ["v=spf1 include:_spf.google.com ~all", "google-site-verification=abc"],
        ["v=DMARC1; p=quarantine; rua=mailto:owen@example.ca"],
    )
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "ok"
    assert result.detail["dmarc_policy"] == "quarantine"
    assert result.detail["spf_lookups"] == 1


async def test_missing_spf_is_a_warning_not_a_failure(make_clients):
    """A gap that leaves the domain spoofable, but breaks no mail today."""
    clients = clients_for(make_clients, [], ["v=DMARC1; p=none"])
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "warn"
    assert "No SPF record" in result.summary


async def test_two_spf_records_fail_because_mail_is_breaking_now(make_clients):
    clients = clients_for(
        make_clients,
        ["v=spf1 include:_spf.google.com ~all", "v=spf1 include:sendgrid.net ~all"],
        ["v=DMARC1; p=none"],
    )
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "fail"
    assert "2 SPF records" in result.summary


async def test_missing_dmarc_is_a_warning(make_clients):
    clients = clients_for(make_clients, ["v=spf1 -all"], [])
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "warn"
    assert "No DMARC record" in result.summary


async def test_breaking_and_gap_problems_together_are_a_failure(make_clients):
    """Two SPF records AND no DMARC: the worse of the two decides."""
    clients = clients_for(
        make_clients, ["v=spf1 include:a.test ~all", "v=spf1 include:b.test ~all"], []
    )
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "fail"
    assert "2 SPF records" in result.summary
    assert "No DMARC record" in result.summary


async def test_nxdomain_on_dmarc_subdomain_means_no_dmarc(make_clients):
    """A domain with no DMARC record has no _dmarc subdomain either, so the
    lookup returns NXDOMAIN. That is a missing record, not a dead domain."""

    class DmarcMissing(FakeResolver):
        async def records(self, name, rdtype):
            if name.startswith("_dmarc."):
                raise DomainMissing(f"{name} does not exist (NXDOMAIN)")
            return await super().records(name, rdtype)

    clients = make_clients(resolver=DmarcMissing({("example.ca", "TXT"): ["v=spf1 -all"]}))
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "warn"
    assert result.summary == "No DMARC record"
    assert result.detail["spf_records"] == ["v=spf1 -all"]


async def test_dead_domain_still_fails_loudly(make_clients):
    clients = make_clients(resolver=FakeResolver({}, fail="missing"))
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "fail"
    assert "NXDOMAIN" in result.summary


async def test_policy_none_is_ok_but_mentioned(make_clients):
    clients = clients_for(make_clients, ["v=spf1 -all"], ["v=DMARC1; p=none"])
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "ok"
    assert "p=none" in result.summary


async def test_spf_lookup_limit_counts_includes_recursively(make_clients):
    # Four includes at the top, each of which includes two more: over the limit of 10.
    top = "v=spf1 include:a.test include:b.test include:c.test include:d.test ~all"
    nested = "v=spf1 include:x.test include:y.test a mx ~all"
    clients = clients_for(
        make_clients,
        [top],
        ["v=DMARC1; p=reject"],
        a_test=[nested],
        b_test=[nested],
        c_test=[nested],
        d_test=[nested],
    )
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "fail"
    assert "over the limit" in result.summary


async def test_spf_include_loop_does_not_hang(make_clients):
    clients = clients_for(
        make_clients,
        ["v=spf1 include:loop.test ~all"],
        ["v=DMARC1; p=reject"],
        loop_test=["v=spf1 include:loop.test ~all"],
    )
    result = await email_auth.run({"domain": "example.ca"}, clients)
    assert result.status == "ok"


async def test_lookup_failure_gives_no_verdict(make_clients):
    clients = make_clients(resolver=FakeResolver({}, fail="unavailable"))
    assert await email_auth.run({"domain": "example.ca"}, clients) is None


@pytest.mark.parametrize(
    ("record", "lookups"),
    [
        ("v=spf1 -all", 0),
        ("v=spf1 ip4:1.2.3.4 -all", 0),  # ip4 costs nothing
        ("v=spf1 a mx -all", 2),
        ("v=spf1 include:one.test include:two.test ~all", 2),
        ("v=spf1 redirect=other.test", 1),
        ("v=spf1 exists:%{i}.test ptr ~all", 2),
    ],
)
def test_lookup_counting(record, lookups):
    assert count_lookups(record) == lookups


def test_spf_and_dmarc_parsing_ignores_other_txt():
    assert spf_records(["google-site-verification=x", "v=spf1 -all"]) == ["v=spf1 -all"]
    assert parse_dmarc(["random", "v=DMARC1; p=reject"]) == "v=DMARC1; p=reject"
    assert parse_dmarc(["random"]) is None
