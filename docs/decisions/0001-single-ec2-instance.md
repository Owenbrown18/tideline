# 0001: One EC2 instance running docker compose

> **Superseded on 2026-09-18 by [0005](0005-serverless-twice-monthly.md)** (twice-monthly runs on Lambda). Kept as it was decided.

**Status:** accepted, 2026-09-17

**Context.** Tideline's job is making outbound requests to 11 sites. It needs
a database and a long-running scheduler, on a budget of about USD 20/month.

**Options.**
- Lambda + RDS: a Lambda must be inside a VPC to reach RDS, and a VPC Lambda
  needs a NAT gateway (about USD 30+/month) to reach the internet, which is the
  whole job.
- ECS Fargate + RDS + load balancer: about USD 50-70/month before any work.
- One `t4g.small` Graviton instance running compose: about USD 15-20/month.

**Decision.** One instance. It also means operating a real Linux server
(patching, disk, memory, logs, backups and restores).

**Revisit when** there are many more sites, or a second probe region is needed:
move Postgres to RDS and the containers to ECS.
