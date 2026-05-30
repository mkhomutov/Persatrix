# Third-Party Notices

Persatrix bundles and redistributes third-party software. This file lists
every dependency pulled into a Persatrix build, together with its license
and source location.

The file is **generated** — do not edit by hand. Regenerate with:

```bash
make notices
```

Policy:

- Allow-list of acceptable licenses lives in
  [`scripts/checks/allowed_licenses.txt`](scripts/checks/allowed_licenses.txt).
  CI enforces the same list via `make check-licenses` (Go + Python + Rust).
- Any row prefixed with `!` denotes a license *outside* the allow-list — a
  reviewer must resolve it (replace the dependency, upgrade, or add a
  justified exception) before release.
- Persatrix itself ships under BUSL-1.1 (see [`LICENSE`](LICENSE) and
  [`NOTICE`](NOTICE)) and is excluded from the tables below.

## Go dependencies

Collected via `go-licenses report ./cmd/... ./internal/...` (38 packages).

| Package | License | Source |
| --- | --- | --- |
| `github.com/cenkalti/backoff/v5` | MIT | [link](https://github.com/cenkalti/backoff/blob/v5.0.3/LICENSE) |
| `github.com/cespare/xxhash/v2` | MIT | [link](https://github.com/cespare/xxhash/blob/v2.3.0/LICENSE.txt) |
| `github.com/dustin/go-humanize` | MIT | [link](https://github.com/dustin/go-humanize/blob/v1.0.1/LICENSE) |
| `github.com/felixge/httpsnoop` | MIT | [link](https://github.com/felixge/httpsnoop/blob/v1.0.4/LICENSE.txt) |
| `github.com/go-logr/logr` | Apache-2.0 | [link](https://github.com/go-logr/logr/blob/v1.4.3/LICENSE) |
| `github.com/go-logr/stdr` | Apache-2.0 | [link](https://github.com/go-logr/stdr/blob/v1.2.2/LICENSE) |
| `github.com/google/uuid` | BSD-3-Clause | [link](https://github.com/google/uuid/blob/v1.6.0/LICENSE) |
| `github.com/grpc-ecosystem/grpc-gateway/v2` | BSD-3-Clause | [link](https://github.com/grpc-ecosystem/grpc-gateway/blob/v2.28.0/LICENSE) |
| `github.com/mattn/go-isatty` | MIT | [link](https://github.com/mattn/go-isatty/blob/v0.0.20/LICENSE) |
| `github.com/ncruces/go-strftime` | MIT | [link](https://github.com/ncruces/go-strftime/blob/v1.0.0/LICENSE) |
| `github.com/oklog/ulid/v2` | Apache-2.0 | [link](https://github.com/oklog/ulid/blob/v2.1.1/LICENSE) |
| `github.com/remyoudompheng/bigfft` | BSD-3-Clause | [link](https://github.com/remyoudompheng/bigfft/blob/24d4a6f8daec/LICENSE) |
| `go.opentelemetry.io/auto/sdk` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go-instrumentation/blob/sdk/v1.2.1/sdk/LICENSE) |
| `go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go-contrib/blob/instrumentation/google.golang.org/grpc/otelgrpc/v0.68.0/instrumentation/google.golang.org/grpc/otelgrpc/LICENSE) |
| `go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go-contrib/blob/instrumentation/net/http/otelhttp/v0.68.0/instrumentation/net/http/otelhttp/LICENSE) |
| `go.opentelemetry.io/otel` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/v1.43.0/LICENSE) |
| `go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/exporters/otlp/otlpmetric/otlpmetrichttp/v1.43.0/exporters/otlp/otlpmetric/otlpmetrichttp/LICENSE) |
| `go.opentelemetry.io/otel/exporters/otlp/otlptrace` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/exporters/otlp/otlptrace/v1.43.0/exporters/otlp/otlptrace/LICENSE) |
| `go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/exporters/otlp/otlptrace/otlptracehttp/v1.43.0/exporters/otlp/otlptrace/otlptracehttp/LICENSE) |
| `go.opentelemetry.io/otel/metric` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/metric/v1.43.0/metric/LICENSE) |
| `go.opentelemetry.io/otel/sdk` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/sdk/v1.43.0/sdk/LICENSE) |
| `go.opentelemetry.io/otel/sdk/metric` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/sdk/metric/v1.43.0/sdk/metric/LICENSE) |
| `go.opentelemetry.io/otel/trace` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-go/blob/trace/v1.43.0/trace/LICENSE) |
| `go.opentelemetry.io/proto/otlp` | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-proto-go/blob/otlp/v1.10.0/otlp/LICENSE) |
| `go.uber.org/multierr` | MIT | [link](https://github.com/uber-go/multierr/blob/v1.10.0/LICENSE.txt) |
| `go.uber.org/zap` | MIT | [link](https://github.com/uber-go/zap/blob/v1.27.0/LICENSE) |
| `golang.org/x/net` | BSD-3-Clause | [link](https://cs.opensource.google/go/x/net/+/v0.52.0:LICENSE) |
| `golang.org/x/sys/windows` | BSD-3-Clause | [link](https://cs.opensource.google/go/x/sys/+/v0.42.0:LICENSE) |
| `golang.org/x/text` | BSD-3-Clause | [link](https://cs.opensource.google/go/x/text/+/v0.35.0:LICENSE) |
| `google.golang.org/genproto/googleapis/api/httpbody` | Apache-2.0 | [link](https://github.com/googleapis/go-genproto/blob/9d38bb4040a9/googleapis/api/LICENSE) |
| `google.golang.org/genproto/googleapis/rpc/status` | Apache-2.0 | [link](https://github.com/googleapis/go-genproto/blob/6f92a3bedf2d/googleapis/rpc/LICENSE) |
| `google.golang.org/grpc` | Apache-2.0 | [link](https://github.com/grpc/grpc-go/blob/v1.80.0/LICENSE) |
| `google.golang.org/protobuf` | BSD-3-Clause | [link](https://github.com/protocolbuffers/protobuf-go/blob/v1.36.11/LICENSE) |
| `gopkg.in/yaml.v3` | MIT | [link](https://github.com/go-yaml/yaml/blob/v3.0.1/LICENSE) |
| `modernc.org/libc` | MIT | [link](https://gitlab.com/cznic/libc/blob/v1.72.0/LICENSE-3RD-PARTY.md) |
| `modernc.org/mathutil` | !Unknown | Unknown |
| `modernc.org/memory` | BSD-3-Clause | [link](https://gitlab.com/cznic/memory/blob/v1.11.0/LICENSE-GO) |
| `modernc.org/sqlite` | BSD-3-Clause | [link](https://gitlab.com/cznic/sqlite/blob/v1.50.0/LICENSE) |

## Python dependencies

Collected via `pip-licenses --from=mixed` against the `agents` extras (116 packages).

| Package | Version | License | Source |
| --- | --- | --- | --- |
| `aiohappyeyeballs` | 2.6.1 | Python Software Foundation License | [link](https://github.com/aio-libs/aiohappyeyeballs) |
| `aiohttp` | 3.13.5 | Apache-2.0 AND MIT | [link](https://github.com/aio-libs/aiohttp) |
| `aiosignal` | 1.4.0 | Apache Software License | [link](https://github.com/aio-libs/aiosignal) |
| `aiosqlite` | 0.22.1 | MIT License | [link](https://aiosqlite.omnilib.dev) |
| `alembic` | 1.18.4 | MIT | [link](https://alembic.sqlalchemy.org) |
| `annotated-types` | 0.7.0 | MIT License | [link](https://github.com/annotated-types/annotated-types) |
| `anthropic` | 0.89.0 | MIT License | [link](https://github.com/anthropics/anthropic-sdk-python) |
| `anyio` | 4.13.0 | MIT | [link](https://anyio.readthedocs.io/en/stable/versionhistory.html) |
| `attrs` | 26.1.0 | MIT | [link](https://www.attrs.org/en/stable/changelog.html) |
| `backports.zstd` | 1.3.0 | PSF-2.0 | [link](https://github.com/rogdham/backports.zstd) |
| `bcrypt` | 4.3.0 | Apache Software License | [link](https://github.com/pyca/bcrypt/) |
| `bidict` | 0.23.1 | Mozilla Public License 2.0 (MPL 2.0) | [link](https://github.com/jab/bidict) |
| `blinker` | 1.9.0 | MIT License | [link](https://github.com/pallets-eco/blinker/) |
| `brotli` | 1.2.0 | MIT | [link](https://github.com/google/brotli) |
| `certifi` | 2026.2.25 | Mozilla Public License 2.0 (MPL 2.0) | [link](https://github.com/certifi/python-certifi) |
| `charset-normalizer` | 3.4.7 | MIT | [link](https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md) |
| `click` | 8.3.1 | BSD-3-Clause | [link](https://github.com/pallets/click/) |
| `colorama` | 0.4.6 | BSD License | [link](https://github.com/tartley/colorama) |
| `coverage` | 7.13.5 | Apache-2.0 | [link](https://github.com/coveragepy/coveragepy) |
| `distro` | 1.9.0 | Apache Software License | [link](https://github.com/python-distro/distro) |
| `dnspython` | 2.8.0 | ISC License (ISCL) | [link](https://www.dnspython.org) |
| `docstring_parser` | 0.17.0 | MIT License | [link](https://github.com/rr-/docstring_parser) |
| `eventlet` | 0.40.4 | MIT License | [link](https://github.com/eventlet/eventlet) |
| `Flask` | 3.1.3 | BSD-3-Clause | [link](https://github.com/pallets/flask/) |
| `Flask-Compress` | 1.24 | MIT License | [link](https://github.com/colour-science/flask-compress) |
| `flask-cors` | 6.0.2 | MIT | [link](https://corydolphin.github.io/flask-cors/) |
| `Flask-Migrate` | 4.1.0 | MIT License | [link](https://github.com/miguelgrinberg/flask-migrate) |
| `Flask-SocketIO` | 5.6.1 | MIT License | [link](https://github.com/miguelgrinberg/flask-socketio) |
| `Flask-SQLAlchemy` | 3.1.1 | BSD License | [link](https://flask-sqlalchemy.palletsprojects.com) |
| `frozenlist` | 1.8.0 | Apache-2.0 | [link](https://github.com/aio-libs/frozenlist) |
| `googleapis-common-protos` | 1.74.0 | Apache Software License | [link](https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos) |
| `greenlet` | 3.3.2 | MIT AND PSF-2.0 | [link](https://greenlet.readthedocs.io) |
| `grpcio` | 1.80.0 | Apache-2.0 | [link](https://grpc.io) |
| `grpcio-tools` | 1.71.2 | Apache Software License | [link](https://grpc.io) |
| `h11` | 0.16.0 | MIT License | [link](https://github.com/python-hyper/h11) |
| `httpcore` | 1.0.9 | BSD-3-Clause | [link](https://www.encode.io/httpcore/) |
| `httpx` | 0.28.1 | BSD License | [link](https://github.com/encode/httpx) |
| `idna` | 3.11 | BSD-3-Clause | [link](https://github.com/kjd/idna) |
| `importlib_metadata` | 8.7.1 | Apache-2.0 | [link](https://github.com/python/importlib_metadata) |
| `iniconfig` | 2.3.0 | MIT | [link](https://github.com/pytest-dev/iniconfig) |
| `itsdangerous` | 2.2.0 | BSD License | [link](https://github.com/pallets/itsdangerous/) |
| `Jinja2` | 3.1.6 | BSD License | [link](https://github.com/pallets/jinja/) |
| `jiter` | 0.13.0 | MIT License | [link](https://github.com/pydantic/jiter/) |
| `jsonschema` | 4.24.1 | MIT | [link](https://github.com/python-jsonschema/jsonschema) |
| `jsonschema-path` | 0.4.5 | Apache Software License | [link](https://github.com/p1c2u/jsonschema-path) |
| `jsonschema-specifications` | 2025.9.1 | MIT | [link](https://github.com/python-jsonschema/jsonschema-specifications) |
| `lazy-object-proxy` | 1.12.0 | BSD-2-Clause | [link](https://python-lazy-object-proxy.readthedocs.io/en/latest/changelog.html) |
| `librt` | 0.9.0 | MIT | [link](https://github.com/mypyc/librt) |
| `Mako` | 1.3.10 | MIT License | [link](https://www.makotemplates.org/) |
| `markdown-it-py` | 4.0.0 | MIT License | [link](https://github.com/executablebooks/markdown-it-py) |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | [link](https://github.com/pallets/markupsafe/) |
| `mdurl` | 0.1.2 | MIT License | [link](https://github.com/executablebooks/mdurl) |
| `multidict` | 6.7.1 | Apache License 2.0 | [link](https://github.com/aio-libs/multidict) |
| `mypy` | 1.20.0 | MIT | [link](https://www.mypy-lang.org/) |
| `mypy-protobuf` | 5.1.0 | Apache License 2.0 | [link](https://github.com/nipunn1313/mypy-protobuf) |
| `mypy_extensions` | 1.1.0 | MIT | [link](https://github.com/python/mypy_extensions) |
| `openai` | 1.109.1 | Apache Software License | [link](https://github.com/openai/openai-python) |
| `openapi-schema-validator` | 0.8.1 | BSD License | [link](https://github.com/python-openapi/openapi-schema-validator) |
| `openapi-spec-validator` | 0.8.4 | Apache-2.0 | [link](https://github.com/python-openapi/openapi-spec-validator) |
| `opentelemetry-api` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api) |
| `opentelemetry-exporter-otlp-proto-common` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-common) |
| `opentelemetry-exporter-otlp-proto-grpc` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-grpc) |
| `opentelemetry-exporter-otlp-proto-http` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-http) |
| `opentelemetry-instrumentation` | 0.62b0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/opentelemetry-instrumentation) |
| `opentelemetry-instrumentation-grpc` | 0.62b0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-grpc) |
| `opentelemetry-instrumentation-system-metrics` | 0.62b0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation/opentelemetry-instrumentation-system-metrics) |
| `opentelemetry-proto` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-proto) |
| `opentelemetry-sdk` | 1.41.0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk) |
| `opentelemetry-semantic-conventions` | 0.62b0 | Apache-2.0 | [link](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions) |
| `orchestr8-agents` | 0.1.0 | Apache-2.0 | UNKNOWN |
| `packaging` | 26.0 | Apache-2.0 OR BSD-2-Clause | [link](https://github.com/pypa/packaging) |
| `pathable` | 0.5.0 | Apache Software License | [link](https://github.com/p1c2u/pathable) |
| `pathspec` | 1.0.4 | Mozilla Public License 2.0 (MPL 2.0) | [link](https://python-path-specification.readthedocs.io/en/latest/index.html) |
| `pluggy` | 1.6.0 | MIT License | UNKNOWN |
| `propcache` | 0.4.1 | Apache Software License | [link](https://github.com/aio-libs/propcache) |
| `protobuf` | 5.29.6 | 3-Clause BSD License | [link](https://developers.google.com/protocol-buffers/) |
| `psutil` | 7.2.2 | BSD-3-Clause | [link](https://github.com/giampaolo/psutil) |
| `pydantic` | 2.12.5 | MIT | [link](https://github.com/pydantic/pydantic) |
| `pydantic-settings` | 2.13.1 | MIT | [link](https://github.com/pydantic/pydantic-settings) |
| `pydantic_core` | 2.41.5 | MIT | [link](https://github.com/pydantic/pydantic-core) |
| `Pygments` | 2.19.2 | BSD License | [link](https://pygments.org) |
| `PyJWT` | 2.10.1 | MIT License | [link](https://github.com/jpadilla/pyjwt) |
| `pytest` | 9.0.2 | MIT | [link](https://docs.pytest.org/en/latest/) |
| `pytest-asyncio` | 1.3.0 | Apache-2.0 | [link](https://github.com/pytest-dev/pytest-asyncio) |
| `pytest-cov` | 7.1.0 | MIT | [link](https://pytest-cov.readthedocs.io/en/latest/changelog.html) |
| `pytest-flask` | 1.3.0 | MIT License | [link](https://github.com/pytest-dev/pytest-flask) |
| `python-dotenv` | 1.2.2 | BSD-3-Clause | [link](https://github.com/theskumar/python-dotenv) |
| `python-engineio` | 4.13.1 | MIT | [link](https://github.com/miguelgrinberg/python-engineio) |
| `python-socketio` | 5.16.1 | MIT | [link](https://github.com/miguelgrinberg/python-socketio) |
| `PyYAML` | 6.0.3 | MIT License | [link](https://pyyaml.org/) |
| `referencing` | 0.37.0 | MIT | [link](https://github.com/python-jsonschema/referencing) |
| `regex` | 2026.4.4 | Apache-2.0 AND CNRI-Python | [link](https://github.com/mrabarnett/mrab-regex) |
| `requests` | 2.33.1 | Apache Software License | [link](https://github.com/psf/requests) |
| `rfc3339-validator` | 0.1.4 | MIT License | [link](https://github.com/naimetti/rfc3339-validator) |
| `rich` | 14.3.3 | MIT License | [link](https://github.com/Textualize/rich) |
| `rpds-py` | 0.30.0 | MIT | [link](https://github.com/crate-py/rpds) |
| `ruff` | 0.15.10 | MIT | [link](https://docs.astral.sh/ruff) |
| `simple-websocket` | 1.1.0 | MIT License | [link](https://github.com/miguelgrinberg/simple-websocket) |
| `six` | 1.17.0 | MIT License | [link](https://github.com/benjaminp/six) |
| `sniffio` | 1.3.1 | Apache Software License; MIT License | [link](https://github.com/python-trio/sniffio) |
| `SQLAlchemy` | 2.0.48 | MIT | [link](https://www.sqlalchemy.org) |
| `structlog` | 25.5.0 | MIT OR Apache-2.0 | [link](https://github.com/hynek/structlog/blob/main/CHANGELOG.md) |
| `tiktoken` | 0.12.0 | MIT License | [link](https://github.com/openai/tiktoken) |
| `tqdm` | 4.67.3 | MPL-2.0 AND MIT | [link](https://tqdm.github.io) |
| `types-grpcio` | 1.0.0.20260408 | Apache-2.0 | [link](https://github.com/python/typeshed) |
| `types-protobuf` | 7.34.1.20260408 | Apache-2.0 | [link](https://github.com/python/typeshed) |
| `types-PyYAML` | 6.0.12.20260408 | Apache-2.0 | [link](https://github.com/python/typeshed) |
| `typing-inspection` | 0.4.2 | MIT | [link](https://github.com/pydantic/typing-inspection) |
| `typing_extensions` | 4.15.0 | PSF-2.0 | [link](https://github.com/python/typing_extensions) |
| `tzdata` | 2026.2 | Apache-2.0 | [link](https://github.com/python/tzdata) |
| `urllib3` | 2.6.3 | MIT | [link](https://github.com/urllib3/urllib3/blob/main/CHANGES.rst) |
| `Werkzeug` | 3.1.7 | BSD-3-Clause | [link](https://github.com/pallets/werkzeug/) |
| `wrapt` | 2.1.2 | BSD-2-Clause | [link](https://github.com/GrahamDumpleton/wrapt) |
| `wsproto` | 1.3.2 | MIT | [link](https://github.com/python-hyper/wsproto/) |
| `yarl` | 1.23.0 | Apache-2.0 | [link](https://github.com/aio-libs/yarl) |
| `zipp` | 3.23.0 | MIT | [link](https://github.com/jaraco/zipp) |

## Rust dependencies

Collected via `cargo license --json` inside `cli/` (222 crates).

| Package | Version | License | Source |
| --- | --- | --- | --- |
| `anstream` | 1.0.0 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `anstyle` | 1.0.14 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `anstyle-parse` | 1.0.0 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `anstyle-query` | 1.1.5 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `anstyle-wincon` | 3.0.11 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `anyhow` | 1.0.102 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/anyhow) |
| `atomic-waker` | 1.1.2 | Apache-2.0 OR MIT | [link](https://github.com/smol-rs/atomic-waker) |
| `base64` | 0.22.1 | Apache-2.0 OR MIT | [link](https://github.com/marshallpierce/rust-base64) |
| `bitflags` | 2.11.0 | Apache-2.0 OR MIT | [link](https://github.com/bitflags/bitflags) |
| `block2` | 0.6.2 | MIT | [link](https://github.com/madsmtm/objc2) |
| `bumpalo` | 3.20.2 | Apache-2.0 OR MIT | [link](https://github.com/fitzgen/bumpalo) |
| `bytecount` | 0.6.9 | Apache-2.0 OR MIT | [link](https://github.com/llogiq/bytecount) |
| `bytes` | 1.11.1 | MIT | [link](https://github.com/tokio-rs/bytes) |
| `cc` | 1.2.60 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/cc-rs) |
| `cfg-if` | 1.0.4 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/cfg-if) |
| `cfg_aliases` | 0.2.1 | MIT | [link](https://github.com/katharostech/cfg_aliases) |
| `clap` | 4.6.0 | Apache-2.0 OR MIT | [link](https://github.com/clap-rs/clap) |
| `clap_builder` | 4.6.0 | Apache-2.0 OR MIT | [link](https://github.com/clap-rs/clap) |
| `clap_derive` | 4.6.0 | Apache-2.0 OR MIT | [link](https://github.com/clap-rs/clap) |
| `clap_lex` | 1.1.0 | Apache-2.0 OR MIT | [link](https://github.com/clap-rs/clap) |
| `colorchoice` | 1.0.5 | Apache-2.0 OR MIT | [link](https://github.com/rust-cli/anstyle.git) |
| `colored` | 2.2.0 | MPL-2.0 | [link](https://github.com/mackwic/colored) |
| `core-foundation` | 0.9.4 | Apache-2.0 OR MIT | [link](https://github.com/servo/core-foundation-rs) |
| `core-foundation` | 0.10.1 | Apache-2.0 OR MIT | [link](https://github.com/servo/core-foundation-rs) |
| `core-foundation-sys` | 0.8.7 | Apache-2.0 OR MIT | [link](https://github.com/servo/core-foundation-rs) |
| `ctrlc` | 3.5.2 | Apache-2.0 OR MIT | [link](https://github.com/Detegr/rust-ctrlc.git) |
| `dirs` | 5.0.1 | Apache-2.0 OR MIT | [link](https://github.com/soc/dirs-rs) |
| `dirs-sys` | 0.4.1 | Apache-2.0 OR MIT | [link](https://github.com/dirs-dev/dirs-sys-rs) |
| `dispatch2` | 0.3.1 | Apache-2.0 OR MIT OR Zlib | [link](https://github.com/madsmtm/objc2) |
| `displaydoc` | 0.2.5 | Apache-2.0 OR MIT | [link](https://github.com/yaahc/displaydoc) |
| `encoding_rs` | 0.8.35 | (Apache-2.0 OR MIT) AND BSD-3-Clause | [link](https://github.com/hsivonen/encoding_rs) |
| `equivalent` | 1.0.2 | Apache-2.0 OR MIT | [link](https://github.com/indexmap-rs/equivalent) |
| `errno` | 0.3.14 | Apache-2.0 OR MIT | [link](https://github.com/lambda-fairy/rust-errno) |
| `fastrand` | 2.4.1 | Apache-2.0 OR MIT | [link](https://github.com/smol-rs/fastrand) |
| `find-msvc-tools` | 0.1.9 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/cc-rs) |
| `fnv` | 1.0.7 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-fnv) |
| `foldhash` | 0.1.5 | Zlib | [link](https://github.com/orlp/foldhash) |
| `foreign-types` | 0.3.2 | Apache-2.0 OR MIT | [link](https://github.com/sfackler/foreign-types) |
| `foreign-types-shared` | 0.1.1 | Apache-2.0 OR MIT | [link](https://github.com/sfackler/foreign-types) |
| `form_urlencoded` | 1.2.2 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-url) |
| `futures-channel` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-core` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-io` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-macro` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-sink` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-task` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `futures-util` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/futures-rs) |
| `getrandom` | 0.2.17 | Apache-2.0 OR MIT | [link](https://github.com/rust-random/getrandom) |
| `getrandom` | 0.4.2 | Apache-2.0 OR MIT | [link](https://github.com/rust-random/getrandom) |
| `h2` | 0.4.13 | MIT | [link](https://github.com/hyperium/h2) |
| `hashbrown` | 0.15.5 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/hashbrown) |
| `hashbrown` | 0.17.0 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/hashbrown) |
| `heck` | 0.5.0 | Apache-2.0 OR MIT | [link](https://github.com/withoutboats/heck) |
| `http` | 1.4.0 | Apache-2.0 OR MIT | [link](https://github.com/hyperium/http) |
| `http-body` | 1.0.1 | MIT | [link](https://github.com/hyperium/http-body) |
| `http-body-util` | 0.1.3 | MIT | [link](https://github.com/hyperium/http-body) |
| `httparse` | 1.10.1 | Apache-2.0 OR MIT | [link](https://github.com/seanmonstar/httparse) |
| `hyper` | 1.9.0 | MIT | [link](https://github.com/hyperium/hyper) |
| `hyper-rustls` | 0.27.7 | Apache-2.0 OR ISC OR MIT | [link](https://github.com/rustls/hyper-rustls) |
| `hyper-tls` | 0.6.0 | Apache-2.0 OR MIT | [link](https://github.com/hyperium/hyper-tls) |
| `hyper-util` | 0.1.20 | MIT | [link](https://github.com/hyperium/hyper-util) |
| `icu_collections` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_locale_core` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_normalizer` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_normalizer_data` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_properties` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_properties_data` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `icu_provider` | 2.2.0 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `id-arena` | 2.3.0 | Apache-2.0 OR MIT | [link](https://github.com/fitzgen/id-arena) |
| `idna` | 1.1.0 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-url/) |
| `idna_adapter` | 1.2.1 | Apache-2.0 OR MIT | [link](https://github.com/hsivonen/idna_adapter) |
| `indexmap` | 2.14.0 | Apache-2.0 OR MIT | [link](https://github.com/indexmap-rs/indexmap) |
| `ipnet` | 2.12.0 | Apache-2.0 OR MIT | [link](https://github.com/krisprice/ipnet) |
| `iri-string` | 0.7.12 | Apache-2.0 OR MIT | [link](https://github.com/lo48576/iri-string) |
| `is_terminal_polyfill` | 1.70.2 | Apache-2.0 OR MIT | [link](https://github.com/polyfill-rs/is_terminal_polyfill) |
| `itoa` | 1.0.18 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/itoa) |
| `js-sys` | 0.3.94 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/js-sys) |
| `lazy_static` | 1.5.0 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang-nursery/lazy-static.rs) |
| `leb128fmt` | 0.1.0 | Apache-2.0 OR MIT | [link](https://github.com/bluk/leb128fmt) |
| `libc` | 0.2.184 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/libc) |
| `libredox` | 0.1.17 | MIT | [link](https://gitlab.redox-os.org/redox-os/libredox.git) |
| `linux-raw-sys` | 0.12.1 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/sunfishcode/linux-raw-sys) |
| `litemap` | 0.8.2 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `log` | 0.4.29 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/log) |
| `memchr` | 2.8.0 | MIT OR Unlicense | [link](https://github.com/BurntSushi/memchr) |
| `mime` | 0.3.17 | Apache-2.0 OR MIT | [link](https://github.com/hyperium/mime) |
| `mio` | 1.2.0 | MIT | [link](https://github.com/tokio-rs/mio) |
| `native-tls` | 0.2.18 | Apache-2.0 OR MIT | [link](https://github.com/rust-native-tls/rust-native-tls) |
| `nix` | 0.31.2 | MIT | [link](https://github.com/nix-rust/nix) |
| `objc2` | 0.6.4 | MIT | [link](https://github.com/madsmtm/objc2) |
| `objc2-encode` | 4.1.0 | MIT | [link](https://github.com/madsmtm/objc2) |
| `once_cell` | 1.21.4 | Apache-2.0 OR MIT | [link](https://github.com/matklad/once_cell) |
| `once_cell_polyfill` | 1.70.2 | Apache-2.0 OR MIT | [link](https://github.com/polyfill-rs/once_cell_polyfill) |
| `openssl` | 0.10.80 | Apache-2.0 | [link](https://github.com/rust-openssl/rust-openssl) |
| `openssl-macros` | 0.1.1 | Apache-2.0 OR MIT |  |
| `openssl-probe` | 0.2.1 | Apache-2.0 OR MIT | [link](https://github.com/rustls/openssl-probe) |
| `openssl-sys` | 0.9.116 | MIT | [link](https://github.com/rust-openssl/rust-openssl) |
| `option-ext` | 0.2.0 | MPL-2.0 | [link](https://github.com/soc/option-ext.git) |
| `papergrid` | 0.17.0 | MIT | [link](https://github.com/zhiburt/tabled) |
| `percent-encoding` | 2.3.2 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-url/) |
| `pin-project-lite` | 0.2.17 | Apache-2.0 OR MIT | [link](https://github.com/taiki-e/pin-project-lite) |
| `pkg-config` | 0.3.32 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/pkg-config-rs) |
| `potential_utf` | 0.1.5 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `prettyplease` | 0.2.37 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/prettyplease) |
| `proc-macro-error-attr2` | 2.0.0 | Apache-2.0 OR MIT | [link](https://github.com/GnomedDev/proc-macro-error-2) |
| `proc-macro-error2` | 2.0.1 | Apache-2.0 OR MIT | [link](https://github.com/GnomedDev/proc-macro-error-2) |
| `proc-macro2` | 1.0.106 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/proc-macro2) |
| `quote` | 1.0.45 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/quote) |
| `r-efi` | 6.0.0 | Apache-2.0 OR LGPL-2.1-or-later OR MIT | [link](https://github.com/r-efi/r-efi) |
| `redox_users` | 0.4.6 | MIT | [link](https://gitlab.redox-os.org/redox-os/users) |
| `reqwest` | 0.12.28 | Apache-2.0 OR MIT | [link](https://github.com/seanmonstar/reqwest) |
| `ring` | 0.17.14 | Apache-2.0 AND ISC | [link](https://github.com/briansmith/ring) |
| `rustix` | 1.1.4 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/rustix) |
| `rustls` | 0.23.37 | Apache-2.0 OR ISC OR MIT | [link](https://github.com/rustls/rustls) |
| `rustls-pki-types` | 1.14.0 | Apache-2.0 OR MIT | [link](https://github.com/rustls/pki-types) |
| `rustls-webpki` | 0.103.13 | ISC | [link](https://github.com/rustls/webpki) |
| `rustversion` | 1.0.22 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/rustversion) |
| `ryu` | 1.0.23 | Apache-2.0 OR BSL-1.0 | [link](https://github.com/dtolnay/ryu) |
| `schannel` | 0.1.29 | MIT | [link](https://github.com/steffengy/schannel-rs) |
| `security-framework` | 3.7.0 | Apache-2.0 OR MIT | [link](https://github.com/kornelski/rust-security-framework) |
| `security-framework-sys` | 2.17.0 | Apache-2.0 OR MIT | [link](https://github.com/kornelski/rust-security-framework) |
| `semver` | 1.0.28 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/semver) |
| `serde` | 1.0.228 | Apache-2.0 OR MIT | [link](https://github.com/serde-rs/serde) |
| `serde_core` | 1.0.228 | Apache-2.0 OR MIT | [link](https://github.com/serde-rs/serde) |
| `serde_derive` | 1.0.228 | Apache-2.0 OR MIT | [link](https://github.com/serde-rs/serde) |
| `serde_json` | 1.0.149 | Apache-2.0 OR MIT | [link](https://github.com/serde-rs/json) |
| `serde_urlencoded` | 0.7.1 | Apache-2.0 OR MIT | [link](https://github.com/nox/serde_urlencoded) |
| `shlex` | 1.3.0 | Apache-2.0 OR MIT | [link](https://github.com/comex/rust-shlex) |
| `signal-hook-registry` | 1.4.8 | Apache-2.0 OR MIT | [link](https://github.com/vorner/signal-hook) |
| `slab` | 0.4.12 | MIT | [link](https://github.com/tokio-rs/slab) |
| `smallvec` | 1.15.1 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-smallvec) |
| `socket2` | 0.6.3 | Apache-2.0 OR MIT | [link](https://github.com/rust-lang/socket2) |
| `stable_deref_trait` | 1.2.1 | Apache-2.0 OR MIT | [link](https://github.com/storyyeller/stable_deref_trait) |
| `strsim` | 0.11.1 | MIT | [link](https://github.com/rapidfuzz/strsim-rs) |
| `subtle` | 2.6.1 | BSD-3-Clause | [link](https://github.com/dalek-cryptography/subtle) |
| `syn` | 2.0.117 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/syn) |
| `sync_wrapper` | 1.0.2 | Apache-2.0 | [link](https://github.com/Actyx/sync_wrapper) |
| `synstructure` | 0.13.2 | MIT | [link](https://github.com/mystor/synstructure) |
| `system-configuration` | 0.7.0 | Apache-2.0 OR MIT | [link](https://github.com/mullvad/system-configuration-rs) |
| `system-configuration-sys` | 0.6.0 | Apache-2.0 OR MIT | [link](https://github.com/mullvad/system-configuration-rs) |
| `tabled` | 0.20.0 | MIT | [link](https://github.com/zhiburt/tabled) |
| `tabled_derive` | 0.11.0 | MIT | [link](https://github.com/zhiburt/tabled) |
| `tempfile` | 3.27.0 | Apache-2.0 OR MIT | [link](https://github.com/Stebalien/tempfile) |
| `testing_table` | 0.3.0 | MIT | [link](https://github.com/zhiburt/tabled) |
| `thiserror` | 1.0.69 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/thiserror) |
| `thiserror-impl` | 1.0.69 | Apache-2.0 OR MIT | [link](https://github.com/dtolnay/thiserror) |
| `tinystr` | 0.8.3 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `tokio` | 1.51.1 | MIT | [link](https://github.com/tokio-rs/tokio) |
| `tokio-macros` | 2.7.0 | MIT | [link](https://github.com/tokio-rs/tokio) |
| `tokio-native-tls` | 0.3.1 | MIT | [link](https://github.com/tokio-rs/tls) |
| `tokio-rustls` | 0.26.4 | Apache-2.0 OR MIT | [link](https://github.com/rustls/tokio-rustls) |
| `tokio-util` | 0.7.18 | MIT | [link](https://github.com/tokio-rs/tokio) |
| `tower` | 0.5.3 | MIT | [link](https://github.com/tower-rs/tower) |
| `tower-http` | 0.6.8 | MIT | [link](https://github.com/tower-rs/tower-http) |
| `tower-layer` | 0.3.3 | MIT | [link](https://github.com/tower-rs/tower) |
| `tower-service` | 0.3.3 | MIT | [link](https://github.com/tower-rs/tower) |
| `tracing` | 0.1.44 | MIT | [link](https://github.com/tokio-rs/tracing) |
| `tracing-core` | 0.1.36 | MIT | [link](https://github.com/tokio-rs/tracing) |
| `try-lock` | 0.2.5 | MIT | [link](https://github.com/seanmonstar/try-lock) |
| `unicode-ident` | 1.0.24 | (Apache-2.0 OR MIT) AND Unicode-3.0 | [link](https://github.com/dtolnay/unicode-ident) |
| `unicode-width` | 0.2.2 | Apache-2.0 OR MIT | [link](https://github.com/unicode-rs/unicode-width) |
| `unicode-xid` | 0.2.6 | Apache-2.0 OR MIT | [link](https://github.com/unicode-rs/unicode-xid) |
| `untrusted` | 0.9.0 | ISC | [link](https://github.com/briansmith/untrusted) |
| `url` | 2.5.8 | Apache-2.0 OR MIT | [link](https://github.com/servo/rust-url) |
| `utf8_iter` | 1.0.4 | Apache-2.0 OR MIT | [link](https://github.com/hsivonen/utf8_iter) |
| `utf8parse` | 0.2.2 | Apache-2.0 OR MIT | [link](https://github.com/alacritty/vte) |
| `vcpkg` | 0.2.15 | Apache-2.0 OR MIT | [link](https://github.com/mcgoo/vcpkg-rs) |
| `want` | 0.3.1 | MIT | [link](https://github.com/seanmonstar/want) |
| `wasi` | 0.11.1+wasi-snapshot-preview1 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasi) |
| `wasip2` | 1.0.2+wasi-0.2.9 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasi-rs) |
| `wasip3` | 0.4.0+wasi-0.3.0-rc-2026-01-06 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasi-rs) |
| `wasm-bindgen` | 0.2.117 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen) |
| `wasm-bindgen-futures` | 0.4.67 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/futures) |
| `wasm-bindgen-macro` | 0.2.117 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro) |
| `wasm-bindgen-macro-support` | 0.2.117 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/macro-support) |
| `wasm-bindgen-shared` | 0.2.117 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/shared) |
| `wasm-encoder` | 0.244.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasm-encoder) |
| `wasm-metadata` | 0.244.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasm-metadata) |
| `wasm-streams` | 0.4.2 | Apache-2.0 OR MIT | [link](https://github.com/MattiasBuelens/wasm-streams/) |
| `wasmparser` | 0.244.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wasmparser) |
| `web-sys` | 0.3.94 | Apache-2.0 OR MIT | [link](https://github.com/wasm-bindgen/wasm-bindgen/tree/master/crates/web-sys) |
| `windows-link` | 0.2.1 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-registry` | 0.6.1 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-result` | 0.4.1 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-strings` | 0.5.1 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-sys` | 0.48.0 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-sys` | 0.52.0 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-sys` | 0.59.0 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-sys` | 0.61.2 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-targets` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows-targets` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_aarch64_gnullvm` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_aarch64_gnullvm` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_aarch64_msvc` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_aarch64_msvc` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_i686_gnu` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_i686_gnu` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_i686_gnullvm` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_i686_msvc` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_i686_msvc` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_gnu` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_gnu` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_gnullvm` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_gnullvm` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_msvc` | 0.48.5 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `windows_x86_64_msvc` | 0.52.6 | Apache-2.0 OR MIT | [link](https://github.com/microsoft/windows-rs) |
| `wit-bindgen` | 0.51.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wit-bindgen) |
| `wit-bindgen-core` | 0.51.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wit-bindgen) |
| `wit-bindgen-rust` | 0.51.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wit-bindgen) |
| `wit-bindgen-rust-macro` | 0.51.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wit-bindgen) |
| `wit-component` | 0.244.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wit-component) |
| `wit-parser` | 0.244.0 | Apache-2.0 OR Apache-2.0 WITH LLVM-exception OR MIT | [link](https://github.com/bytecodealliance/wasm-tools/tree/main/crates/wit-parser) |
| `writeable` | 0.6.3 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `yoke` | 0.8.2 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `yoke-derive` | 0.8.2 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zerofrom` | 0.1.7 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zerofrom-derive` | 0.1.7 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zeroize` | 1.8.2 | Apache-2.0 OR MIT | [link](https://github.com/RustCrypto/utils) |
| `zerotrie` | 0.2.4 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zerovec` | 0.11.6 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zerovec-derive` | 0.11.3 | Unicode-3.0 | [link](https://github.com/unicode-org/icu4x) |
| `zmij` | 1.0.21 | MIT | [link](https://github.com/dtolnay/zmij) |
