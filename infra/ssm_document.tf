# The only thing CI is allowed to run on the server: fetch the deploy files
# from S3 and run deploy.sh with the image tag it just pushed. CI cannot run
# arbitrary commands on the box.

resource "aws_ssm_document" "deploy" {
  name            = "sitewatch-deploy"
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "Deploy a Sitewatch image tag: pull, migrate, restart, health-check, roll back on failure."
    parameters = {
      imageTag = {
        type           = "String"
        description    = "ECR image tag to run (the git SHA)."
        allowedPattern = "^[A-Za-z0-9._-]{1,128}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "deploy"
      inputs = {
        timeoutSeconds = "900"
        runCommand = [
          "set -euo pipefail",
          "aws s3 cp s3://${aws_s3_bucket.data.bucket}/deploy/deploy.sh /opt/sitewatch/deploy.sh --region ${var.region}",
          "chmod +x /opt/sitewatch/deploy.sh",
          "/opt/sitewatch/deploy.sh '{{ imageTag }}'",
        ]
      }
    }]
  })
}
