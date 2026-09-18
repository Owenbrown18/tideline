# GitHub Actions authenticates to AWS with OIDC: GitHub signs a short-lived
# token describing the workflow, AWS trusts that signature, and the workflow
# assumes this role. No AWS access keys exist anywhere, so there is nothing to
# leak or rotate.
#
# The trust policy pins the repo AND the branch: a workflow on another branch,
# or in a fork, cannot assume the role.

data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "github_deploy" {
  name        = "sitewatch-github-deploy"
  description = "Assumed by GitHub Actions on pushes to main. Pushes the image and points the two functions at it."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "sitewatch-github-deploy"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "PushImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:DescribeImages",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid    = "PointTheFunctionsAtTheNewImage"
        Effect = "Allow"
        Action = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:InvokeFunction"]
        Resource = [
          aws_lambda_function.run.arn,
          aws_lambda_function.web.arn,
          aws_lambda_function.demo.arn,
        ]
      },
    ]
  })
}
