# One repository for the one image. Old images are cleaned up automatically so
# storage does not creep up: ECR charges per GB stored.

resource "aws_ecr_repository" "app" {
  name                 = "sitewatch"
  image_tag_mutability = "IMMUTABLE" # a tag always means the same image
  force_delete         = true        # this repo is rebuilt from source any time

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images; rollback only ever needs the previous one"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
