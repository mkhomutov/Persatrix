package server

// auth_config_allowed_hosts_test.go: the `auth.allowed_hosts` list of
// config/security.yaml (ISSUE-0212), the names a request's Host may carry.

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestDefaultAllowedHosts(t *testing.T) {
	want := []string{"localhost", "127.0.0.1", "[::1]"}
	assert.Equal(t, want, DefaultAllowedHosts())
	assert.Equal(t, want, DefaultAuthConfig().AllowedHosts)

	// Each call hands out its own list: a caller that appends to one must
	// not change the default for the next.
	first := DefaultAllowedHosts()
	first[0] = "changed"
	assert.Equal(t, want, DefaultAllowedHosts())
}

func TestLoadSecurityConfig_AllowedHosts(t *testing.T) {
	tests := []struct {
		name    string
		yaml    string
		want    []string
		wantErr string
	}{
		{
			name: "the key is left out",
			yaml: "auth:\n  mode: disabled\n",
			want: []string{"localhost", "127.0.0.1", "[::1]"},
		},
		{
			name: "the key has no value",
			yaml: "auth:\n  allowed_hosts:\n",
			want: []string{"localhost", "127.0.0.1", "[::1]"},
		},
		{
			name: "a list replaces the default",
			yaml: "auth:\n  allowed_hosts: [persatrix.example, orchestrator]\n",
			want: []string{"persatrix.example", "orchestrator"},
		},
		{
			name: "spellings of one name are made one",
			yaml: "auth:\n  allowed_hosts: [LocalHost, localhost, \"[0:0:0:0:0:0:0:1]\", \"::1\", \"[::1]\", 127.0.0.1]\n",
			want: []string{"localhost", "[::1]", "127.0.0.1"},
		},
		{
			name: "a name may end with a dot and hold an underscore",
			yaml: "auth:\n  allowed_hosts: [persatrix.example., my_service]\n",
			want: []string{"persatrix.example.", "my_service"},
		},
		{
			name:    "an empty list",
			yaml:    "auth:\n  allowed_hosts: []\n",
			wantErr: "auth.allowed_hosts must name at least one host; leave the key out for the default (localhost, 127.0.0.1, [::1])",
		},
		{
			name:    "a name with a port",
			yaml:    "auth:\n  allowed_hosts: [\"localhost:8080\"]\n",
			wantErr: `auth.allowed_hosts: "localhost:8080" is not a host name or an IP address on its own (no scheme, port, path or wildcard)`,
		},
		{
			name:    "an IPv4 address with a port",
			yaml:    "auth:\n  allowed_hosts: [\"127.0.0.1:8080\"]\n",
			wantErr: `auth.allowed_hosts: "127.0.0.1:8080" is not a host name or an IP address on its own (no scheme, port, path or wildcard)`,
		},
		{
			name:    "an address with a port",
			yaml:    "auth:\n  allowed_hosts: [\"[::1]:8080\"]\n",
			wantErr: `auth.allowed_hosts: "[::1]:8080" is not a host name`,
		},
		{
			name:    "a URL",
			yaml:    "auth:\n  allowed_hosts: [\"http://localhost\"]\n",
			wantErr: `auth.allowed_hosts: "http://localhost" is not a host name`,
		},
		{
			name:    "a path",
			yaml:    "auth:\n  allowed_hosts: [localhost/ui]\n",
			wantErr: `auth.allowed_hosts: "localhost/ui" is not a host name`,
		},
		{
			name:    "a wildcard",
			yaml:    "auth:\n  allowed_hosts: [\"*.example.com\"]\n",
			wantErr: `auth.allowed_hosts: "*.example.com" is not a host name`,
		},
		{
			name:    "a name that starts with a dot",
			yaml:    "auth:\n  allowed_hosts: [.example.com]\n",
			wantErr: `auth.allowed_hosts: ".example.com" is not a host name`,
		},
		{
			name:    "an empty name",
			yaml:    "auth:\n  allowed_hosts: [localhost, \"\"]\n",
			wantErr: `auth.allowed_hosts: "" is not a host name`,
		},
		{
			name:    "a name with a space",
			yaml:    "auth:\n  allowed_hosts: [\"local host\"]\n",
			wantErr: `auth.allowed_hosts: "local host" is not a host name`,
		},
		{
			name:    "an address with a zone",
			yaml:    "auth:\n  allowed_hosts: [\"fe80::1%eth0\"]\n",
			wantErr: `auth.allowed_hosts: "fe80::1%eth0" is not a host name`,
		},
		{
			name:    "an IPv4 address in brackets",
			yaml:    "auth:\n  allowed_hosts: [\"[127.0.0.1]\"]\n",
			wantErr: `auth.allowed_hosts: "[127.0.0.1]" is not a host name`,
		},
		{
			// YAML does not read an unquoted [::1] as a string, in either
			// list style; the start is refused with a parse error.
			name:    "an IPv6 address without quotes, flow style",
			yaml:    "auth:\n  allowed_hosts: [[::1]]\n",
			wantErr: "auth: parse",
		},
		{
			name:    "an IPv6 address without quotes, block style",
			yaml:    "auth:\n  allowed_hosts:\n    - localhost\n    - [::1]\n",
			wantErr: "auth: parse",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg, err := LoadSecurityConfig(writeSecurityYAML(t, tt.yaml))
			if tt.wantErr != "" {
				require.Error(t, err)
				assert.Contains(t, err.Error(), tt.wantErr)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, tt.want, cfg.AllowedHosts)
		})
	}
}

// TestAllowedHostEntriesMatchTheHostsTheyName: whatever the loader keeps for
// an entry is what the check works out from a request's Host, with a port and
// without one. If the two spellings drifted apart, a listed name would be
// refused.
func TestAllowedHostEntriesMatchTheHostsTheyName(t *testing.T) {
	for entry, hosts := range map[string][]string{
		"localhost":          {"localhost", "localhost:8080", "LOCALHOST:8080"},
		"Persatrix.Example":  {"persatrix.example", "persatrix.example:443"},
		"persatrix.example.": {"persatrix.example.", "persatrix.example.:443"},
		"my_service":         {"my_service:8080"},
		"127.0.0.1":          {"127.0.0.1", "127.0.0.1:8080"},
		"[::1]":              {"[::1]", "[::1]:8080", "[0:0:0:0:0:0:0:1]:8080"},
		"::1":                {"[::1]", "[::1]:8080"},
		"2001:DB8::a":        {"[2001:db8::a]:8080", "[2001:0db8:0:0:0:0:0:a]"},
		"10.0.0.7":           {"10.0.0.7:8080"},
	} {
		kept, err := NormalizeAllowedHosts([]string{entry})
		require.NoError(t, err, entry)
		require.Len(t, kept, 1, entry)
		for _, host := range hosts {
			assert.Equal(t, kept[0], canonicalHost(host), "entry %q, Host %q", entry, host)
		}
	}
}
