# GunnersAnalysis - developer entry points.
#
# Python work runs inside the `airflow` container by default: it carries the
# analytics stack (pandas, mplsoccer, pandera, selenium), has the repo
# bind-mounted at /opt/gunners, and already knows GUNNERS_DB_URL. Prefix any
# target with LOCAL=1 to run it against your own venv instead.
#
#   make up                          bring the stack up
#   make seed                        load the 2026/27 calendar
#   make report JOB=<job_id>         render the 18 panels and load the match
#   make season                      what has run and what has not
#   LOCAL=1 make ask Q="..."         analyst on the host venv (localhost DSN)

SHELL   := /bin/bash
COMPOSE ?= docker compose

# Prefer the project venv when it exists; Debian and Ubuntu ship python3
# without a bare `python`, so never assume that name. Override at will.
PYBIN   ?= $(shell test -x venv/bin/python && echo venv/bin/python || echo python3)

SEASON  ?= 2026/2027
OUT     ?= output
JOB     ?=
SVC     ?=
AIRFLOW_PORT ?= 8080
PGUSER  ?= gunners
PGDB    ?= gunners
# Host password for LOCAL=1 DSN. Matches compose default when .env omits it.
POSTGRES_PASSWORD ?= gunners

# One club, one schema. Arsenal is `gunners`; other clubs get their own clone
# of db/schema.sql. Every query target below reads $(SCHEMA), so
# `make season SCHEMA=barcelona` reports on Barcelona's warehouse.
SCHEMA  ?= gunners
CLUB    ?=
WS      ?=
SS      ?=
COMP    ?=
MW      ?=

ifdef LOCAL
  PY   := $(PYBIN)
  ROOT :=
  # `postgres` is the compose DNS name — it only resolves inside the network.
  # Host runs (the analyst venv) need localhost. Respect an explicit override.
  ifeq ($(origin GUNNERS_DB_URL),undefined)
    export GUNNERS_DB_URL := postgresql://$(PGUSER):$(POSTGRES_PASSWORD)@localhost:5432/$(PGDB)
  endif
else
  PY   := $(COMPOSE) exec -T airflow python
  ROOT := /opt/gunners/
endif

PSQL := $(COMPOSE) exec -T postgres psql -U $(PGUSER) -d $(PGDB)

.DEFAULT_GOAL := help

OPEN ?= $(shell command -v xdg-open || command -v open || echo echo)

.PHONY: help build up down restart ps logs health ui api \
        schema schema-club seed fixtures \
        render load report match \
        psql season table players \
        test test-cov \
        clean reset

# ---------------------------------------------------------------- stack ----

help:
	@echo "GunnersAnalysis"
	@echo
	@echo "  Stack"
	@echo "    make up          Start postgres, airflow, orchestrator, redis"
	@echo "    make down        Stop them (warehouse volume survives)"
	@echo "    make restart     down + up"
	@echo "    make build       Rebuild the orchestrator and airflow images"
	@echo "    make ps          Service status"
	@echo "    make logs        Follow all logs, or one: make logs SVC=airflow"
	@echo "    make health      Hit the orchestrator health endpoint"
	@echo "    make ui          Wait for Airflow, then open it in a browser"
	@echo "    make api         Open the API docs in a browser"
	@echo
	@echo "  Warehouse"
	@echo "    make schema      Apply db/schema.sql to $(SCHEMA) (idempotent)"
	@echo "    make schema-club Provision another club's schema   CLUB=barcelona"
	@echo "    make seed        Seed the fixture calendar from JSON"
	@echo "    make fixtures    Rebuild the calendar JSON, then seed it"
	@echo "    make psql        Open a psql shell on the $(SCHEMA) schema"
	@echo
	@echo "  One match"
	@echo "    make render      Render the panels        [JOB=<id>] [OUT=$(OUT)]"
	@echo "    make load        Load a match             JOB=<id> required"
	@echo "    make report      render + load            [JOB=<id>]"
	@echo "    make match       Full pipeline from two links:"
	@echo "                       make match CLUB=barcelona WS=<whoscored-url> \\"
	@echo "                                  SS=<sofascore-url> [COMP='Champions League'] [MW=6]"
	@echo
	@echo "  A whole finished season                 [BACKFILL_SEASON=$(BACKFILL_SEASON)]"
	@echo "    make calendar      Derive its calendar from SofaScore"
	@echo "    make backfill-plan What would be ingested, touching nothing"
	@echo "    make backfill      Ingest it, match by match  [ONLY='1,5-9'] [FORCE=1]"
	@echo "    make readiness     Is there enough history to analyse?"
	@echo
	@echo "  Season"
	@echo "    make season      Calendar vs reality, in kick-off order"
	@echo "    make table       League table from v_team_season_summary"
	@echo "    make players     Player per-90s from v_player_season_totals"
	@echo
	@echo "  Tests"
	@echo "    make test        Unit tests, no coverage"
	@echo "    make test-cov    Unit tests with an HTML coverage report"
	@echo
	@echo "  Cleanup"
	@echo "    make clean       Delete scraped and derived files under data/"
	@echo "    make reset       Also drop the Postgres volume (CONFIRM=1)"
	@echo
	@echo "  Airflow UI  http://localhost:$(AIRFLOW_PORT)     API  http://localhost:8000/docs"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d
	@echo "Airflow  http://localhost:$(AIRFLOW_PORT)"
	@echo "API      http://localhost:8000/docs"
	@echo "Postgres postgresql://$(PGUSER)@localhost:5432/$(PGDB)"

down:
	$(COMPOSE) down

restart: down up

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f $(SVC)

health:
	@curl -sf http://localhost:8000/health | $(PYBIN) -m json.tool || \
	  echo "orchestrator not responding on :8000 - try 'make up'"

# The webserver takes ~100s to bind after a restart, and Airflow standalone
# gives up on it silently if gunicorn is slow ("No response from gunicorn
# master within 120 seconds"). Wait for a real 200 before opening a tab.
ui:
	@echo "waiting for the Airflow webserver on :$(AIRFLOW_PORT) ..."
	@for i in $$(seq 1 30); do \
	  if [ "$$(curl -s -o /dev/null -w '%{http_code}' http://localhost:$(AIRFLOW_PORT)/health)" = "200" ]; then \
	    echo "up - opening http://localhost:$(AIRFLOW_PORT)"; $(OPEN) http://localhost:$(AIRFLOW_PORT) >/dev/null 2>&1 & exit 0; \
	  fi; sleep 5; \
	done; \
	echo "still down after 150s. Check:  docker compose logs airflow | grep -i webserver"; exit 1

api:
	@$(OPEN) http://localhost:8000/docs >/dev/null 2>&1 &
	@echo "http://localhost:8000/docs"

# ------------------------------------------------------------ warehouse ----

schema:
	$(PY) $(ROOT)db/connection.py --schema $(SCHEMA)

# Clone the warehouse for another club: make schema-club CLUB=barcelona
schema-club:
	@test -n "$(CLUB)" || { echo "CLUB is required:  make schema-club CLUB=barcelona"; exit 1; }
	$(PY) $(ROOT)db/connection.py --club '$(CLUB)'

seed:
	$(PY) $(ROOT)db/seed_fixtures.py

fixtures:
	$(PY) $(ROOT)airflow/fixtures/build_fixtures.py
	$(PY) $(ROOT)db/seed_fixtures.py

psql:
	$(COMPOSE) exec postgres psql -U $(PGUSER) -d $(PGDB) -c 'SET search_path TO $(SCHEMA), public' -P pager=off

# ------------------------------------------------------------ one match ----

# --job defaults to the most recently scraped match in data/processed/.
render:
	$(PY) $(ROOT)visualizer/match_report.py $(if $(JOB),--job $(JOB),) --out $(OUT)

load:
	@test -n "$(JOB)" || { echo "JOB is required:  make load JOB=<job_id>"; exit 1; }
	$(PY) $(ROOT)db/loader.py --job $(JOB) --season '$(SEASON)' $(if $(CLUB),--club '$(CLUB)',)

report: render load

# Scrape, align with SofaScore for xG, render and load - one match, two links.
match:
	@test -n "$(CLUB)" || { echo "CLUB is required:  make match CLUB=barcelona WS=<url> SS=<url>"; exit 1; }
	@test -n "$(WS)"   || { echo "WS is required (the WhoScored match URL)"; exit 1; }
	@test -n "$(SS)"   || { echo "SS is required (the SofaScore match URL), or use --no-xg directly"; exit 1; }
	$(PY) $(ROOT)orchestrator/run_match.py --club '$(CLUB)' \
	  --whoscored '$(WS)' --sofascore '$(SS)' --season '$(SEASON)' \
	  $(if $(COMP),--competition '$(COMP)',) $(if $(MW),--matchweek $(MW),)

# ------------------------------------------------------- whole seasons ----

# Derive a finished season's calendar from SofaScore, ids and final scores
# included. BACKFILL_SEASON defaults to the season just gone.
BACKFILL_SEASON ?= 2025/2026

calendar:
	$(PY) $(ROOT)airflow/fixtures/build_season_calendar.py --season '$(BACKFILL_SEASON)'

# Ingest it, one match at a time. Resumable: re-run after any failure and it
# picks up only what is missing. ONLY restricts to matchweeks ('1,2,5-9').
backfill:
	$(PY) $(ROOT)orchestrator/backfill_season.py --season '$(BACKFILL_SEASON)' \
	  $(if $(ONLY),--only '$(ONLY)',) $(if $(FORCE),--force,) $(if $(MODE),--mode $(MODE),)

backfill-plan:
	$(PY) $(ROOT)orchestrator/backfill_season.py --season '$(BACKFILL_SEASON)' --dry-run

# Is there enough history in the warehouse to build an analyst on?
readiness:
	$(PY) $(ROOT)db/readiness.py --season '$(BACKFILL_SEASON)'

# ----------------------------------------------------------- the analyst ----

# The layer after Postgres: derived tactical tables, a semantic document
# corpus, and a DSPy agent that answers with cited evidence. docs/AGENT.md.
AGENT_SEASON ?= 2025/2026
TIER ?= wiring

# Derived tables Opta does not provide: possessions, pressing, carries.
tactical:
	$(PY) $(ROOT)db/apply_agent_schema.py
	$(PY) $(ROOT)analytics/build.py --season '$(AGENT_SEASON)'

# Natural-language documents + embeddings, into pgvector.
documents:
	$(PY) -m agent.documents.build --season '$(AGENT_SEASON)' $(if $(REBUILD),--rebuild,)

# Phase 0: the evaluation set. Statistical half generated from SQL, free.
evalset:
	$(PY) -m agent.eval.dataset --build --season '$(AGENT_SEASON)'

# Measure the agent. Always record a baseline before optimizing.
eval:
	$(PY) -m agent.eval.run --split $(or $(SPLIT),dev) --tier $(TIER) \
	  $(if $(LIMIT),--limit $(LIMIT),)

# Phase 2. Spends money: runs on the cheap tier under a hard ceiling.
optimize:
	$(PY) -m agent.optimize --optimizer $(or $(OPTIMIZER),bootstrap) \
	  --budget $(or $(BUDGET),3) --tier $(TIER) \
	  $(if $(TRAIN_LIMIT),--train-limit $(TRAIN_LIMIT),)

# Ask it something.  make ask Q="Was Arsenal a high-pressing side?"
ask:
	@test -n "$(Q)" || { echo 'Q is required:  make ask Q="how many points?"'; exit 1; }
	$(PY) -m agent.ask --tier $(TIER) "$(Q)"

# Technical match report (offline).  make match-report OPPONENT=Leeds
match-report:
	@test -n "$(OPPONENT)" || { echo 'OPPONENT is required:  make match-report OPPONENT=Leeds'; exit 1; }
	$(PY) -m agent.report --tier $(TIER) --opponent "$(OPPONENT)" \
	  --season $(or $(SEASON),2025/2026) $(if $(MATCH_ID),--match-id $(MATCH_ID),)

# Everything the analyst layer needs, in order.
agent-setup: tactical documents evalset readiness

# Web SPA
web-install:
	cd web && npm install

web-dev:
	cd web && npm run dev

web-build:
	cd web && npm run build

analyst-up:
	docker compose up -d --build analyst

# -------------------------------------------------------------- tracking ----
# Host venv only (requirements-tracking.txt). Pilot: Arsenal v Fulham GW35.
#
#   LOCAL=1 make tracking-schema
#   LOCAL=1 make tracking-run VIDEO="..." OFFSET_S=453 START_S=453 DURATION_S=300 DEVICE=cpu
#   LOCAL=1 make tracking-run FULL=1 OFFSET_S=453 DEVICE=cpu
#   LOCAL=1 make tracking-fuse MATCH_ID=1903405

MATCH_ID ?= 1903405
OFFSET_S ?= 453
START_S  ?= 453
DURATION_S ?= 300
VIDEO    ?=
FULL     ?=
DEVICE   ?=
TRACK_PY ?= $(if $(wildcard .venv-tracking/bin/python),.venv-tracking/bin/python,$(PYBIN))

tracking-install:
	$(TRACK_PY) -m pip install -r requirements-tracking.txt

tracking-schema:
	@cat db/schema_tracking.sql | $(COMPOSE) exec -T postgres \
	  psql -U $(PGUSER) -d $(PGDB) -v ON_ERROR_STOP=1 \
	  -c "SET search_path TO $(SCHEMA), public;" -f -
	@echo "tracking tables applied to schema $(SCHEMA)"

tracking-run:
	@test -n "$(VIDEO)" || { echo 'VIDEO is required'; exit 1; }
	$(TRACK_PY) -m tracking.cli --match-id $(MATCH_ID) --video "$(VIDEO)" \
	  --offset-s $(OFFSET_S) \
	  $(if $(DEVICE),--device $(DEVICE),) \
	  $(if $(FULL),--full,--start-s $(START_S) --duration-s $(DURATION_S))

tracking-fuse:
	$(TRACK_PY) -m tracking.cli --match-id $(MATCH_ID) --fuse-only --offset-s $(OFFSET_S)

# ---------------------------------------------------------------- season ----

season:
	@$(PSQL) -P pager=off -c "SELECT matchweek, scheduled_date, venue, opponent, status, \
	  home_score, away_score, panels_rendered FROM $(SCHEMA).v_season_progress LIMIT 40;"

table:
	@$(PSQL) -P pager=off -c "SELECT team_name, played, won, drawn, lost, goals_for, \
	  goals_against, points, total_xg FROM $(SCHEMA).v_team_season_summary ORDER BY points DESC;"

players:
	@$(PSQL) -P pager=off -c "SELECT player_name, team_name, minutes, goals, assists, \
	  prog_passes_per_90, xt_per_90 FROM $(SCHEMA).v_player_season_totals \
	  ORDER BY xt_per_90 DESC NULLS LAST LIMIT 25;"

# ----------------------------------------------------------------- tests ----

test:
	$(PYBIN) -m pytest tests/unit -q

test-cov:
	$(PYBIN) -m pytest tests/unit --cov=. --cov-report=html --cov-report=term-missing
	@echo "Report: htmlcov/index.html"

# --------------------------------------------------------------- cleanup ----

# Scraped HTML and everything derived from it. All of it is regenerable, and
# none of it is the warehouse - `make reset` is what drops that.
clean:
	rm -rf data/raw/* data/processed/* data/quality/* data/visualizations/*
	@echo "Cleared data/raw, data/processed, data/quality, data/visualizations"

reset:
	@test "$(CONFIRM)" = "1" || { \
	  echo "This drops the Postgres volume - every ingested match goes with it."; \
	  echo "Re-run as:  make reset CONFIRM=1"; exit 1; }
	$(COMPOSE) down -v
	$(MAKE) clean
	@echo "Stack down, volumes gone. 'make up && make seed' to start over."
