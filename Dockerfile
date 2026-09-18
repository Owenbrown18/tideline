# One image, two Lambda functions (infra/lambda.tf):
#   the scheduled run   CMD tideline.aws_lambda.run_handler
#   the dashboard       CMD tideline.aws_lambda.web_handler (the default below)
# Images are pinned by digest, so a moved tag upstream cannot change what is
# built; Dependabot proposes updates (.github/dependabot.yml).
# Built on AWS's own Lambda Python base image for arm64 (Graviton: cheaper per
# second than x86). Multi-stage: uv resolves and installs the dependencies in
# the build stage; the final image gets only the installed packages.

# ---- build ------------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12-arm64@sha256:6ad7dc750cc64bbebc95a0d87a732f0b9fc6bf27ab119fd686b0d1baacc0c8be AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /build

# Dependencies first, in their own layer, so a code-only change rebuilds in seconds.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --locked --no-dev --no-emit-project --format requirements.txt -o requirements.txt \
 && uv pip install --system --target /packages -r requirements.txt

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --target /packages --no-deps .

# ---- runtime ----------------------------------------------------------------
FROM public.ecr.aws/lambda/python:3.12-arm64@sha256:6ad7dc750cc64bbebc95a0d87a732f0b9fc6bf27ab119fd686b0d1baacc0c8be
COPY --from=build /packages ${LAMBDA_TASK_ROOT}
COPY alembic.ini ${LAMBDA_TASK_ROOT}/
COPY alembic ${LAMBDA_TASK_ROOT}/alembic
ENV TIDELINE_ALEMBIC_INI=${LAMBDA_TASK_ROOT}/alembic.ini \
    TIDELINE_DATABASE_URL=sqlite+aiosqlite:////tmp/tideline.db \
    PYTHONUNBUFFERED=1
CMD ["tideline.aws_lambda.web_handler"]
