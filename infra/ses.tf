# Amazon SES sends the alert emails. Two identities:
#
#   - the DOMAIN obwebdesign.ca, so mail can come from tideline@obwebdesign.ca
#     and be signed with DKIM (three CNAME records go at the DNS host)
#   - the ADDRESS the alerts go to, because SES stays in its sandbox, where
#     every recipient must be verified. Staying in the sandbox is deliberate:
#     an account that can only mail verified addresses cannot mail a client.

resource "aws_sesv2_email_identity" "domain" {
  email_identity = "obwebdesign.ca"
}

resource "aws_sesv2_email_identity" "owen" {
  email_identity = var.alert_email
}

# A dedicated configuration set records what happened to each message.
resource "aws_sesv2_configuration_set" "main" {
  configuration_set_name = "tideline"

  delivery_options {
    tls_policy = "REQUIRE"
  }

  reputation_options {
    reputation_metrics_enabled = true
  }
}

output "ses_dkim_records" {
  description = "Add these three CNAMEs at the DNS host to finish DKIM signing."
  value = [
    for token in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens : {
      name  = "${token}._domainkey.obwebdesign.ca"
      type  = "CNAME"
      value = "${token}.dkim.amazonses.com"
    }
  ]
}

output "ses_verification_note" {
  value = "Check ${var.alert_email} for an SES verification email and click the link. Until then SES will not send to it."
}

moved {
  from = aws_sesv2_configuration_set.sitewatch
  to   = aws_sesv2_configuration_set.main
}
