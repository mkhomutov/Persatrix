package wallet

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"gopkg.in/yaml.v3"
)

// RFC 0023 — LLM Call Leasing: lease-lifecycle defaults. Both the TTL and
// the reaper interval are config-tunable, not constants — operators size
// them per deployment via the `wallet:` block of config/optimization.yaml.
const (
	// defaultTTLSeconds is the default lease time-to-live: 2× the 30 s
	// default per-call timeout, the cap from RFC 0023 Open Question §2.
	defaultTTLSeconds = 60
	// defaultReaperIntervalSeconds is the default reaper scan cadence.
	defaultReaperIntervalSeconds = 5
	// defaultMaxActiveLeases is the default per-agent concurrency ceiling
	// — a DoS ceiling (RFC 0023 Security Considerations), not a budget.
	defaultMaxActiveLeases = 16
)

// Config holds the WalletService lease-lifecycle tuning loaded from the
// `wallet:` block of config/optimization.yaml.
type Config struct {
	// TTL is how long a lease may stay unsettled before the reaper settles
	// it at its granted (worst-case) amount.
	TTL time.Duration
	// ReaperInterval is how often the reaper scans for expired leases.
	ReaperInterval time.Duration
	// MaxActiveLeases caps the number of concurrently-held (unsettled)
	// leases a single agent may hold — a DoS ceiling.
	MaxActiveLeases int
}

// DefaultConfig returns the RFC 0023 default lease-lifecycle tuning, used
// when config/optimization.yaml carries no `wallet:` block (or omits a key).
func DefaultConfig() Config {
	return Config{
		TTL:             defaultTTLSeconds * time.Second,
		ReaperInterval:  defaultReaperIntervalSeconds * time.Second,
		MaxActiveLeases: defaultMaxActiveLeases,
	}
}

// rawOptimizationFile mirrors the top-level structure of optimization.yaml
// for parsing the `wallet:` block only. The `cost:` block is parsed
// separately by internal/cost — each package owns its own slice of the
// shared file.
type rawOptimizationFile struct {
	Wallet rawWalletSection `yaml:"wallet"`
}

// rawWalletSection mirrors the `wallet:` block. The fields are *int, not
// int, so an omitted key (nil) is distinguishable from an explicit `0`:
// the former falls back to the default, the latter is rejected. A plain
// int cannot tell the two apart — both unmarshal to 0.
type rawWalletSection struct {
	TTLSeconds            *int `yaml:"ttl_seconds"`
	ReaperIntervalSeconds *int `yaml:"reaper_interval_seconds"`
	MaxActiveLeases       *int `yaml:"max_active_leases"`
}

// LoadConfig reads optimization.yaml from configDir and parses the optional
// `wallet:` block. An absent block — or an absent key — falls back to the
// DefaultConfig value for that field. A present key must be a positive
// integer; zero and negative values are rejected, matching the `minimum: 1`
// bound on every wallet key in schemas/optimization.schema.json so the
// schema (`make validate`) and the loader agree.
func LoadConfig(configDir string) (Config, error) {
	path := filepath.Join(configDir, "optimization.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read optimization config: %w", err)
	}

	var raw rawOptimizationFile
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return Config{}, fmt.Errorf("parse optimization config: %w", err)
	}

	cfg := DefaultConfig()
	w := raw.Wallet
	if w.TTLSeconds != nil {
		if *w.TTLSeconds < 1 {
			return Config{}, fmt.Errorf("validate wallet config: ttl_seconds must be >= 1, got %d", *w.TTLSeconds)
		}
		cfg.TTL = time.Duration(*w.TTLSeconds) * time.Second
	}
	if w.ReaperIntervalSeconds != nil {
		if *w.ReaperIntervalSeconds < 1 {
			return Config{}, fmt.Errorf("validate wallet config: reaper_interval_seconds must be >= 1, got %d", *w.ReaperIntervalSeconds)
		}
		cfg.ReaperInterval = time.Duration(*w.ReaperIntervalSeconds) * time.Second
	}
	if w.MaxActiveLeases != nil {
		if *w.MaxActiveLeases < 1 {
			return Config{}, fmt.Errorf("validate wallet config: max_active_leases must be >= 1, got %d", *w.MaxActiveLeases)
		}
		cfg.MaxActiveLeases = *w.MaxActiveLeases
	}
	return cfg, nil
}
