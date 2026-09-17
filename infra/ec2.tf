# The one instance. Amazon Linux 2023 on Graviton (arm64), Docker installed by
# user-data. Nothing application-specific is baked into the machine: a deploy
# pulls the image from ECR and the compose file from S3, so the instance can be
# destroyed and recreated at any time.

data "aws_ssm_parameter" "al2023_arm64" {
  # AWS publishes the current AMI id here, so this never pins a stale image.
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region      = var.region
    bucket      = aws_s3_bucket.data.bucket
    ecr_repo    = aws_ecr_repository.app.repository_url
    domain      = var.dashboard_domain
    alert_email = var.alert_email
  })
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  user_data              = local.user_data
  # Replacing the box on a user-data change is deliberate: it proves the machine
  # can be rebuilt from code, which is the point of writing it down.
  user_data_replace_on_change = true

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
    tags        = { Name = "sitewatch-root" }
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  monitoring = false # detailed monitoring costs extra; basic is enough here

  tags = { Name = "sitewatch" }

  lifecycle {
    # A new AMI release should not silently replace the running box. Rebuild on
    # purpose: `terraform apply -replace=aws_instance.app`.
    ignore_changes = [ami]
  }
}

# A fixed address, so the DNS record for status.obwebdesign.ca keeps working
# when the instance is replaced.
resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = "sitewatch" }
}
