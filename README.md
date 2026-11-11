# broth-docs-fixture

The docs repository the docs source is tested against on hosted dev (docs source plan D0.2).
Its pages and spec come from docs-ingestion-agent's fixtures, so that project's golden
questions run as the docs bot's eval set. This README is not a page: no rule claims it.

The site at https://proteensheykh.github.io/broth-docs-fixture/ serves every page and API
operation at the URL Broth cites for it. `site/build.py` builds it from `broth.docs.yaml`,
and `.github/workflows/pages.yml` tests, builds, deploys and checks every URL on each push
to `main`. Build locally with `pip install -r site/requirements.txt && python site/build.py`.
The build refuses contract features it cannot publish exactly (`rewrite`, `url_field`,
placeholders other than `path`, `tag` and `operationId`), rather than publish URLs that
differ from Broth's.
