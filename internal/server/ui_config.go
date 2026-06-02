package server

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"

	"gopkg.in/yaml.v3"
)

// UIConfig is the parsed shape of config/ui.yaml (RFC 0048 §C) — the
// feature-toggle file that decides which web-console panels render. It carries
// only the operator-authored `enabled` toggle per panel; the companion
// `available` flag is *runtime-derived* by the server from whether the backing
// subsystem is wired and is never read from YAML (the schema's
// additionalProperties:false rejects an authored `available:`). See
// [Server.panelAvailable].
type UIConfig struct {
	Panels map[string]PanelToggle `yaml:"panels"`
}

// PanelToggle is the per-panel YAML entry. Only `enabled` is authored; see
// [UIConfig] for why `available` lives on the server, not here.
type PanelToggle struct {
	Enabled bool `yaml:"enabled"`
}

// DefaultUIConfig is the Slice-1 default applied when config/ui.yaml is absent:
// the chat and channel-timeline hero panels ship enabled; memory_strip (Slice 2)
// and cost (Slice 4) ship off so they land additively with no Slice-1 rework
// (RFC 0048 §C / §D.3).
func DefaultUIConfig() *UIConfig {
	return &UIConfig{
		Panels: map[string]PanelToggle{
			"chat":             {Enabled: true},
			"channel_timeline": {Enabled: true},
			"memory_strip":     {Enabled: false},
			"cost":             {Enabled: false},
		},
	}
}

// PanelEnabled reports whether the named panel's `enabled` toggle is on. An
// unknown panel is reported disabled (forward-compat: an older binary serving a
// newer bundle simply does not render a panel it has no toggle for).
func (c *UIConfig) PanelEnabled(name string) bool {
	if c == nil {
		return false
	}
	return c.Panels[name].Enabled
}

// LoadUIConfig parses config/ui.yaml from path.
//
// Absent file → [DefaultUIConfig] with no error: a flag-on binary with no
// ui.yaml is the expected zero-config case and renders the Slice-1 defaults.
// This differs from channels.LoadConfig, which returns the not-exist error for
// its caller to classify — here the defaults-on-absent decision lives in the
// loader because the console always has a sensible default panel set, whereas
// an absent channels.yaml means "no channels at all". A present-but-malformed
// file is an operator bug returned as an error so the caller can log loudly and
// soft-degrade; that loud-on-malformed posture is the part that matches
// channels.yaml. KnownFields is enabled so a stray key (e.g. an authored
// `available:`) surfaces as a parse error rather than loading silently.
func LoadUIConfig(path string) (*UIConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return DefaultUIConfig(), nil
		}
		return nil, fmt.Errorf("ui: read %s: %w", path, err)
	}

	cfg := &UIConfig{}
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(cfg); err != nil {
		// io.EOF means the file is empty / fully commented out — treat as "no
		// overrides" and fall back to defaults, matching channels.yaml. Any
		// other decode error is an operator bug surfaced loudly.
		if errors.Is(err, io.EOF) {
			return DefaultUIConfig(), nil
		}
		return nil, fmt.Errorf("ui: parse %s: %w", path, err)
	}

	// An empty/partial file leaves Panels nil or partial; backfill any panel the
	// operator did not name with its Slice-1 default so the console always sees
	// the full known-panel set.
	if cfg.Panels == nil {
		cfg.Panels = map[string]PanelToggle{}
	}
	for name, def := range DefaultUIConfig().Panels {
		if _, ok := cfg.Panels[name]; !ok {
			cfg.Panels[name] = def
		}
	}
	return cfg, nil
}
