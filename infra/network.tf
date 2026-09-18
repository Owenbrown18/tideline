# A small VPC of its own rather than the default one: everything the service
# uses is then visible in code and reproducible in another account or region.
#
# One public subnet, no NAT gateway. The instance needs to reach the internet
# (that is the job: checking client sites), and a public subnet with an internet
# gateway does that for free, where a private subnet plus NAT would add about
# USD 30/month. See docs/decisions/0001-single-ec2-instance.md.

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "tideline" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "tideline" }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "tideline-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "tideline-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Only Caddy is reachable from the internet. There is no SSH rule at all:
# shell access goes through SSM Session Manager, which needs no inbound port.
resource "aws_security_group" "instance" {
  name        = "sitewatch-instance"
  description = "HTTP and HTTPS in, everything out. No SSH."
  vpc_id      = aws_vpc.main.id

  ingress {
    description      = "HTTP: ACME challenges and the redirect to HTTPS"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description      = "HTTPS (dashboard and API)"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  egress {
    description      = "Outbound checks, ECR pulls, CloudWatch, S3, SES"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "tideline-instance" }
}
