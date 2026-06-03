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

// PanelToggle is the per-panel YAML entry. `enabled` is the operator's
// render-the-panel toggle; `create_enabled` is the per-panel structural-write
// opt-in (RFC 0048 channel-creation amendment §A) — today only the
// channel_timeline panel honours it (group-channel creation), and it ships dark
// (defaults false) so an operator must consciously opt in. Both are
// operator-authored; the companion runtime-derived flags (`available`,
// `create.available`) live on the server, never here — see [UIConfig].
type PanelToggle struct {
	Enabled bool `yaml:"enabled"`
	// CreateEnabled gates the panel's create affordance. Defined on the shared
	// toggle (mirroring the shared schema `panel` definition) but read only for
	// channel_timeline — see [Server.panelCreate]; on any other panel an authored
	// create_enabled is inert.
	CreateEnabled bool `yaml:"create_enabled"`
}

// DefaultUIConfig is the Slice-1 default applied when config/ui.yaml is absent:
// the consolidated channel-timeline conversation panel ships enabled (group
// channels + DMs over one surface — RFC 0048 chat-panel-retirement amendment, so
// the standalone chat panel is retired); memory_strip (Slice 2) and cost
// (Slice 4) ship off so they land additively with no Slice-1 rework
// (RFC 0048 §C / §D.3).
func DefaultUIConfig() *UIConfig {
	return &UIConfig{
		Panels: map[string]PanelToggle{
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
