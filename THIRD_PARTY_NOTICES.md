# Third-Party Notices

LayerCache incorporates code from the following open-source projects. This includes both **vendored** files (copied into the repository) and **pip dependencies** (installed at build/runtime and bundled into the Docker image).

---

## Vendored Libraries

### Chart.js

- **Source**: https://www.chartjs.org/
- **Version**: 4.4.7
- **License**: MIT
- **Copyright**: Copyright (c) 2014-2024 Chart.js Contributors
- **Vendored at**: `layercache/static/vendor/chart.umd.min.js`

### HTMX

- **Source**: https://htmx.org/
- **Version**: 2.0.4
- **License**: BSD 2-Clause
- **Copyright**: Copyright (c) 2020 - 2024 Big Sky Software
- **Vendored at**: `layercache/static/vendor/htmx.min.js`

---

## Runtime Dependencies (pip)

These packages are installed via `pip` at build time and bundled into the Docker image. Their licenses are reproduced below in abbreviated form.

### aiosqlite — MIT

- **Source**: https://github.com/omnilib/aiosqlite
- **Copyright**: Copyright (c) 2018 John Reese and contributors

### FastAPI — MIT

- **Source**: https://fastapi.tiangolo.com/
- **Copyright**: Copyright (c) 2018 Sebastián Ramírez

### FastEmbed — Apache 2.0

- **Source**: https://github.com/qdrant/fastembed
- **Copyright**: Copyright (c) Qdrant

### HTTPX — BSD 3-Clause

- **Source**: https://www.python-httpx.org/
- **Copyright**: Copyright (c) 2023, Encode OSS Ltd

### itsdangerous — BSD 3-Clause

- **Source**: https://palletsprojects.com/p/itsdangerous/
- **Copyright**: Copyright (c) 2011 by Armin Ronacher and contributors

### Jinja2 — BSD 3-Clause

- **Source**: https://palletsprojects.com/p/jinja/
- **Copyright**: Copyright (c) 2007 by the Pallets project

### LiteLLM — MIT

- **Source**: https://github.com/BerriAI/litellm
- **Copyright**: Copyright (c) 2023, BerriAI

### NumPy — BSD 3-Clause

- **Source**: https://numpy.org/
- **Copyright**: Copyright (c) 2005-2025, NumPy Developers
- **Note**: NumPy bundles several third-party libraries under their own licenses (see `site-packages/numpy/LICENSE.txt`).

### Prometheus Client — Apache 2.0

- **Source**: https://github.com/prometheus/client_python
- **Copyright**: Copyright (c) 2024 Prometheus project

### Pydantic — MIT

- **Source**: https://docs.pydantic.dev/
- **Copyright**: Copyright (c) 2017-2025, Samuel Colvin and contributors

### Pydantic Settings — MIT

- **Source**: https://github.com/pydantic/pydantic-settings
- **Copyright**: Copyright (c) 2022, Samuel Colvin and contributors

### python-multipart — Apache 2.0

- **Source**: https://github.com/andrew-d/python-multipart
- **Copyright**: Copyright (c) 2012 Andrew Dunham and contributors

### PyYAML — MIT

- **Source**: https://pyyaml.org/
- **Copyright**: Copyright (c) 2017-2021 Ingy döt Net
- **Copyright**: Copyright (c) 2006-2016 Kirill Simonov

### Uvicorn — BSD 3-Clause

- **Source**: https://www.uvicorn.org/
- **Copyright**: Copyright (c) 2017-present, Encode OSS Ltd

### Watchdog — Apache 2.0

- **Source**: https://github.com/gorakhargosh/watchdog
- **Copyright**: Copyright (c) 2020-2024, Yesudeep Mangalapilly, Google, contributors

---

## Development Dependencies (pip, optional)

These are only installed in development environments and are not bundled into production Docker images.

| Package | License | Source |
|---------|---------|--------|
| mypy | MIT | https://github.com/python/mypy |
| pytest | MIT | https://github.com/pytest-dev/pytest |
| pytest-asyncio | Apache 2.0 | https://github.com/pytest-dev/pytest-asyncio |
| pytest-cov | MIT | https://github.com/pytest-dev/pytest-cov |
| ruff | MIT | https://github.com/astral-sh/ruff |
| vcrpy | MIT | https://github.com/kevin1024/vcrpy |

---

**Transitive dependencies** not listed here inherit their licenses from their respective upstream projects and are available in the `.dist-info` directories after `pip install`.

This project is licensed under the MIT License — see [LICENSE](LICENSE).
