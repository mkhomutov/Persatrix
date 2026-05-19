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

	// The per-key upper bounds. They are fat-finger guards — no legitimate
	// deployment approaches them — kept in lockstep with the `maximum`
	// values in schemas/optimization.schema.json so LoadConfig and
	// `make validate` reject the same configs.
	//
	// maxTTLSeconds additionally bounds a wire narrowing: AcquireLease
	// advertises the TTL in the proto's int32 LeaseGrant.ttl_seconds, and a
	// one-day cap is far below int32's range, so that conversion cannot
	// overflow. All three also keep `seconds × time.Second` well clear of
	// the int64 time.Duration overflow an absurd value would otherwise hit.
	maxTTLSeconds            = 86400 // 1 day
	maxReaperIntervalSeconds = 3600  // 1 hour
	maxMaxActiveLeases       = 1024  // 64× the default ceiling
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
// DefaultConfig value for that field. A present key must fall within the
// inclusive bounds schemas/optimization.schema.json sets on it — at least 1,
// and at most the per-key maximum (a fat-finger guard; for ttl_seconds it
// also keeps the value within the int32 the LeaseGrant advertises on the
// wire). Out-of-range values are rejected, so the schema (`make validate`)
// and the loader agree on both ends of the range.
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
		if *w.TTLSeconds < 1 || *w.TTLSeconds > maxTTLSeconds {
			return Config{}, fmt.Errorf("validate wallet config: ttl_seconds must be in [1, %d], got %d", maxTTLSeconds, *w.TTLSeconds)
		}
		cfg.TTL = time.Duration(*w.TTLSeconds) * time.Second
	}
	if w.ReaperIntervalSeconds != nil {
		if *w.ReaperIntervalSeconds < 1 || *w.ReaperIntervalSeconds > maxReaperIntervalSeconds {
			return Config{}, fmt.Errorf("validate wallet config: reaper_interval_seconds must be in [1, %d], got %d", maxReaperIntervalSeconds, *w.ReaperIntervalSeconds)
		}
		cfg.ReaperInterval = time.Duration(*w.ReaperIntervalSeconds) * time.Second
	}
	if w.MaxActiveLeases != nil {
		if *w.MaxActiveLeases < 1 || *w.MaxActiveLeases > maxMaxActiveLeases {
			return Config{}, fmt.Errorf("validate wallet config: max_active_leases must be in [1, %d], got %d", maxMaxActiveLeases, *w.MaxActiveLeases)
		}
		cfg.MaxActiveLeases = *w.MaxActiveLeases
	}
	return cfg, nil
}
