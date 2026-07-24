package metrics

// metrics_config.go — the env-driven startup Config half of the package:
// the OTEL_* parsing and endpoint-normalisation helpers Init consumes. Split
// out of metrics.go when the ISSUE-0109 cap-utilization instrument pushed
// that file past the 500-line review cap (the same sibling-file precedent as
// channel_instruments.go); metrics.go keeps the package doc, the Instruments
// inventory, and Init.

import (
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/mkhomutov/persatrix/internal/observability"
)

const (
	defaultExportInterval = 60 * time.Second
	defaultExportTimeout  = 10 * time.Second
)

// Config defines metrics startup settings.
type Config struct {
	ServiceName    string
	Environment    string
	OTLPEndpoint   string
	ExportInterval time.Duration
	ExportTimeout  time.Duration
	InsecureOTLP   bool
}

// NewConfigFromEnv builds metrics config from OTEL_* environment variables.
// Kept symmetrical with observability.NewConfigFromEnv so both initialisers
// behave the same under “OTEL_EXPORTER_OTLP_ENDPOINT“ etc.
func NewConfigFromEnv(environment string) Config {
	cfg := Config{
		ServiceName:    envOrDefault("OTEL_SERVICE_NAME", observability.DefaultServiceName),
		Environment:    environment,
		OTLPEndpoint:   envOrDefault("OTEL_EXPORTER_OTLP_ENDPOINT", observability.DefaultOTLPEndpoint),
		ExportInterval: defaultExportInterval,
		ExportTimeout:  defaultExportTimeout,
	}
	if s := os.Getenv("OTEL_METRIC_EXPORT_INTERVAL"); s != "" {
		if d, err := time.ParseDuration(s); err == nil && d > 0 {
			cfg.ExportInterval = d
		}
	}
	if s := os.Getenv("OTEL_METRIC_EXPORT_TIMEOUT"); s != "" {
		if d, err := time.ParseDuration(s); err == nil && d > 0 {
			cfg.ExportTimeout = d
		}
	}
	if s := os.Getenv("OTEL_EXPORTER_OTLP_INSECURE"); s != "" {
		if b, err := strconv.ParseBool(s); err == nil {
			cfg.InsecureOTLP = b
		}
	}
	if strings.HasPrefix(cfg.OTLPEndpoint, "http://") {
		cfg.InsecureOTLP = true
	}
	return cfg
}

// isLoopbackEndpoint returns true when the endpoint host is localhost or a
// loopback IP literal.  Used by Init to suppress the cleartext-transport
// warning for the documented dev default.  Conservative: anything we cannot
// classify (unparseable, non-loopback) is treated as remote so the warning
// fires.
func isLoopbackEndpoint(endpoint string) bool {
	host := trimScheme(endpoint)
	// Strip any trailing path before splitting host:port.
	if i := strings.IndexByte(host, '/'); i >= 0 {
		host = host[:i]
	}
	if i := strings.LastIndexByte(host, ':'); i >= 0 {
		host = host[:i]
	}
	host = strings.TrimSpace(host)
	switch host {
	case "localhost", "127.0.0.1", "::1", "[::1]":
		return true
	}
	return false
}

// trimScheme strips the URL scheme from an endpoint because otlpmetrichttp
// expects a host:port (it adds the /v1/metrics path itself).  Matches the
// tracing package's approach of accepting a full URL from the env var and
// normalising here rather than forcing operators to think about it.
//
// PR-170 N4: also strips a trailing “/v1/metrics“ (and any leftover slash)
// so an operator who sets “OTEL_EXPORTER_OTLP_ENDPOINT“ to a fully
// path-qualified URL (a common mistake when copying the value used for the
// HTTP traces exporter, which DOES want the full URL) does not end up with
// the otlpmetrichttp client posting to “/v1/metrics/v1/metrics“.  Mirrors
// the equivalent normalisation in agents/observability/metrics.py so both
// runtimes accept the same env-var spelling.
func trimScheme(endpoint string) string {
	trimmed := endpoint
	switch {
	case strings.HasPrefix(trimmed, "http://"):
		trimmed = strings.TrimPrefix(trimmed, "http://")
	case strings.HasPrefix(trimmed, "https://"):
		trimmed = strings.TrimPrefix(trimmed, "https://")
	}
	trimmed = strings.TrimRight(trimmed, "/")
	trimmed = strings.TrimSuffix(trimmed, "/v1/metrics")
	return strings.TrimRight(trimmed, "/")
}

func envOrDefault(key, fallback string) string {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	return v
}
