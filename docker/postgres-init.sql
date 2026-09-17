-- Runs once, when the local Postgres volume is first created.
-- A separate, disposable database for the integration tests (they empty every table).
CREATE DATABASE sitewatch_test OWNER sitewatch;
