# Contributing

Thanks for helping. A few ground rules keep the project useful and safe.

## Getting set up

1. Fork and clone the repository.
2. `cp .env.example .env` and fill in local values. Never commit `.env`.
3. `docker compose up -d`, then `make help` to see every target.
4. Run the tests with `pytest`.

## Pull requests

- Keep each PR to one change, and explain the football reason as well as the code.
- Add or update tests in `tests/` for pipeline, warehouse or agent changes.
- New agent queries go in `agent/tools/sql_registry.py` with an eval question.
- Do not commit scraped data, match output, club crests, video or API keys.

## Good first contributions

- A new tactical panel in `visualizer/`
- Another data source in `scraper/`
- Evaluation questions in `agent/eval/`
- Improvements to the tracking module in `tracking/`
