module github.com/orchestr8/orchestr8

go 1.23

require (
	google.golang.org/grpc v1.68.0
	google.golang.org/protobuf v1.36.0
	gopkg.in/yaml.v3 v3.0.1
	go.opentelemetry.io/otel v1.32.0
	go.opentelemetry.io/otel/trace v1.32.0
	go.opentelemetry.io/otel/metric v1.32.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.32.0
	go.opentelemetry.io/otel/sdk v1.32.0
	go.uber.org/zap v1.27.0
)
