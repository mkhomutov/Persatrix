package server

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/mkhomutov/persatrix/internal/accounts"
)

// Auth mode values (RFC 0039 §H). `disabled` is the shipped default:
// the middleware resolves every request to the anonymous `local`
// identity and evaluates no policy, so behaviour is byte-for-byte
// pre-RFC-0039. `enabled` turns on identity resolution now (PR 3) and
// policy enforcement in Phase 2 (PR 5).
const (
	AuthModeDisabled = "disabled"
	AuthModeEnabled  = "enabled"
)

// AuthLimiterConfig sizes one of the two §B login limiters (the
// enabled-mode exposure amendment). Each limiter gets its OWN
// [security.RateLimitConfig] instance and its own key LRU — never the
// agent limiter's, whose cardinality budget is sized for agents (§B2).
type AuthLimiterConfig struct {
	CallsPerWindow int
	WindowSeconds  int
	MaxTracked     int
}

// AuthConfig is the parsed `auth:` block of config/security.yaml
// (RFC 0039 §H + the enabled-mode exposure amendment §A/§B), resolved
// to typed values with every absent field defaulted.
type AuthConfig struct {
	Mode string
	// SessionTTL is the bearer-transport session lifetime (§H, 24h).
	SessionTTL time.Duration
	// CookieSessionTTL is the cookie-transport lifetime — deliberately
	// shorter (8h): a browser session is likelier left open on an
	// unattended screen (amendment OQ #2, resolved 2026-07-29).
	CookieSessionTTL time.Duration
	// Argon carries the §C Argon2id cost read by the password
	// authenticator; stored hashes verify under their own embedded
	// params and rehash to this cost on next login.
	Argon accounts.Params
	// LoginPerSource is the load-bearing flood / Argon2id-amplification
	// defence; LoginPerUsername is the targeted-guessing defence (§B2).
	LoginPerSource   AuthLimiterConfig
	LoginPerUsername AuthLimiterConfig
	// TrustedProxies are the CIDRs whose X-Forwarded-For is believed
	// when resolving the per-source limiter key (§B3). Empty = the TCP
	// peer address is always the source.
	TrustedProxies []*net.IPNet
}

// DefaultAuthConfig returns the shipped defaults: auth disabled, 24h
// bearer / 8h cookie sessions, the §H Argon2id cost, and the
// 2026-07-29-ratified limiter caps (per-source 10/60s, per-username
// 5/60s, each on its own 1000-key LRU).
func DefaultAuthConfig() *AuthConfig {
	return &AuthConfig{
		Mode:             AuthModeDisabled,
		SessionTTL:       24 * time.Hour,
		CookieSessionTTL: 8 * time.Hour,
		Argon:            accounts.DefaultParams,
		LoginPerSource:   AuthLimiterConfig{CallsPerWindow: 10, WindowSeconds: 60, MaxTracked: 1000},
		LoginPerUsername: AuthLimiterConfig{CallsPerWindow: 5, WindowSeconds: 60, MaxTracked: 1000},
	}
}

// securityFile is the raw YAML shape of config/security.yaml. Scalars
// are pointers so a partial file overrides only what it names — the
// per-field tri-state convention the autonomous config block set.
type securityFile struct {
	Auth *authFileBlock `yaml:"auth"`
}

type authFileBlock struct {
	Mode             *string            `yaml:"mode"`
	SessionTTL       *string            `yaml:"session_ttl"`
	CookieSessionTTL *string            `yaml:"cookie_session_ttl"`
	Password         *passwordFileBlock `yaml:"password"`
	LoginThrottle    *throttleFileBlock `yaml:"login_throttle"`
	TrustedProxies   []string           `yaml:"trusted_proxies"`
}

type passwordFileBlock struct {
	Argon2MemoryKiB   *uint32 `yaml:"argon2_memory_kib"`
	Argon2Iterations  *uint32 `yaml:"argon2_iterations"`
	Argon2Parallelism *uint8  `yaml:"argon2_parallelism"`
}

type throttleFileBlock struct {
	PerSource   *limiterFileBlock `yaml:"per_source"`
	PerUsername *limiterFileBlock `yaml:"per_username"`
}

type limiterFileBlock struct {
	CallsPerWindow *int `yaml:"calls_per_window"`
	WindowSeconds  *int `yaml:"window_seconds"`
	MaxTracked     *int `yaml:"max_tracked"`
}

// LoadSecurityConfig parses config/security.yaml from path.
//
// Absent file → [DefaultAuthConfig] with no error (zero-config default:
// auth disabled, nothing enforced). A present-but-malformed or invalid
// file is returned as an error and the caller MUST fail loud (the
// orchestrator Fatals): an operator who authored `mode: enabled` with a
// typo must not silently boot an unauthenticated deployment — the
// opposite soft-degrade direction from ui.yaml, where the worst case is
// a missing panel.
func LoadSecurityConfig(path string) (*AuthConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return DefaultAuthConfig(), nil
		}
		return nil, fmt.Errorf("auth: read %s: %w", path, err)
	}

	raw := &securityFile{}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(raw); err != nil {
		if errors.Is(err, io.EOF) {
			return DefaultAuthConfig(), nil // empty / fully commented out
		}
		return nil, fmt.Errorf("auth: parse %s: %w", path, err)
	}

	cfg, err := resolveAuthConfig(raw.Auth)
	if err != nil {
		return nil, fmt.Errorf("auth: %s: %w", path, err)
	}
	return cfg, nil
}

// resolveAuthConfig folds a raw (possibly nil / partial) auth block
// onto the defaults and validates the result.
func resolveAuthConfig(b *authFileBlock) (*AuthConfig, error) {
	cfg := DefaultAuthConfig()
	if b == nil {
		return cfg, nil
	}
	if b.Mode != nil {
		cfg.Mode = *b.Mode
	}
	if cfg.Mode != AuthModeDisabled && cfg.Mode != AuthModeEnabled {
		return nil, fmt.Errorf("auth.mode must be %q or %q (got %q)", AuthModeDisabled, AuthModeEnabled, cfg.Mode)
	}
	var err error
	if cfg.SessionTTL, err = resolveTTL("auth.session_ttl", b.SessionTTL, cfg.SessionTTL); err != nil {
		return nil, err
	}
	if cfg.CookieSessionTTL, err = resolveTTL("auth.cookie_session_ttl", b.CookieSessionTTL, cfg.CookieSessionTTL); err != nil {
		return nil, err
	}
	if p := b.Password; p != nil {
		if p.Argon2MemoryKiB != nil {
			cfg.Argon.MemoryKiB = *p.Argon2MemoryKiB
		}
		if p.Argon2Iterations != nil {
			cfg.Argon.Iterations = *p.Argon2Iterations
		}
		if p.Argon2Parallelism != nil {
			cfg.Argon.Parallelism = *p.Argon2Parallelism
		}
	}
	if err := cfg.Argon.Validate(); err != nil {
		return nil, fmt.Errorf("auth.password: %w", err)
	}
	if t := b.LoginThrottle; t != nil {
		if cfg.LoginPerSource, err = resolveLimiter("auth.login_throttle.per_source", t.PerSource, cfg.LoginPerSource); err != nil {
			return nil, err
		}
		if cfg.LoginPerUsername, err = resolveLimiter("auth.login_throttle.per_username", t.PerUsername, cfg.LoginPerUsername); err != nil {
			return nil, err
		}
	}
	for _, cidr := range b.TrustedProxies {
		ipnet, err := parseProxyCIDR(cidr)
		if err != nil {
			return nil, fmt.Errorf("auth.trusted_proxies: %w", err)
		}
		cfg.TrustedProxies = append(cfg.TrustedProxies, ipnet)
	}
	return cfg, nil
}

func resolveTTL(field string, raw *string, def time.Duration) (time.Duration, error) {
	if raw == nil {
		return def, nil
	}
	d, err := time.ParseDuration(*raw)
	if err != nil {
		return 0, fmt.Errorf("%s: invalid duration %q: %w", field, *raw, err)
	}
	if d < time.Second {
		// The session store rejects sub-second TTLs (born-expired rows);
		// surface the bound at config time, not first login.
		return 0, fmt.Errorf("%s must be at least 1s (got %s)", field, d)
	}
	return d, nil
}

func resolveLimiter(field string, raw *limiterFileBlock, def AuthLimiterConfig) (AuthLimiterConfig, error) {
	out := def
	if raw == nil {
		return out, nil
	}
	if raw.CallsPerWindow != nil {
		out.CallsPerWindow = *raw.CallsPerWindow
	}
	if raw.WindowSeconds != nil {
		out.WindowSeconds = *raw.WindowSeconds
	}
	if raw.MaxTracked != nil {
		out.MaxTracked = *raw.MaxTracked
	}
	if out.CallsPerWindow <= 0 || out.WindowSeconds <= 0 || out.MaxTracked <= 0 {
		return out, fmt.Errorf("%s: calls_per_window, window_seconds and max_tracked must all be > 0", field)
	}
	return out, nil
}

// parseProxyCIDR accepts a CIDR (`10.0.0.0/8`) or a bare address
// (`10.0.0.1`, folded to a single-host network) — the common shapes an
// operator writes for a reverse proxy.
func parseProxyCIDR(s string) (*net.IPNet, error) {
	if _, ipnet, err := net.ParseCIDR(s); err == nil {
		return ipnet, nil
	}
	ip := net.ParseIP(s)
	if ip == nil {
		return nil, fmt.Errorf("%q is neither a CIDR nor an IP address", s)
	}
	bits := 32
	if ip.To4() == nil {
		bits = 128
	}
	return &net.IPNet{IP: ip, Mask: net.CIDRMask(bits, bits)}, nil
}
