package server

// request_origin.go: the two checks of where a request came from (ISSUE-0212).
//
// The REST API used to trust that it was bound to the loopback address. A web
// browser on the same machine is inside that boundary, and a page it has open
// can reach the API in two ways:
//
//   - DNS rebinding. The page's own host name is pointed at 127.0.0.1 after
//     the page has loaded. The browser then treats the page and the
//     orchestrator as one origin, and the page reads and writes as it likes.
//     Such a request still carries the page's name in its Host header, which
//     a browser does not let a page change. So a Host that is not one of the
//     names the operator expects is refused.
//   - A cross-site write. A page on another site submits a form, or sends a
//     request script may send without asking, to a route that changes
//     something. The browser marks such a request: with Sec-Fetch-Site when
//     it counts the address as secure (HTTPS, localhost, a loopback
//     address), and with Origin at any address. So a write the browser
//     marks as coming from another origin is refused.
//
// Both run for every caller, whether sign-in (auth.mode) is on or off. Neither
// stops a client that is not a browser: such a client writes any header it
// likes. What keeps those out is the bind address and, when it is on, sign-in.

import (
	"fmt"
	"net/http"
	"net/netip"
	"regexp"
	"strings"
	"unicode/utf8"

	"go.uber.org/zap"
)

// allowedHostsSetting is how a refusal and the logs name the list, so that an
// operator who reaches the orchestrator under another name knows what to add.
const allowedHostsSetting = "auth.allowed_hosts in config/security.yaml"

// healthzPath is the one route the Host check leaves out. A liveness probe
// calls it with an address as Host (a Kubernetes probe sends the pod's own
// address), and a refused probe restarts the process for ever. The route says
// only that an orchestrator is running and which parts it runs without.
const healthzPath = "/healthz"

// crossOriginRefusal is what a refused cross-origin write is told.
const crossOriginRefusal = "cross-origin write refused: the browser reports that this request comes from another origin"

// shownHostBytes caps how much of a refused Host, Origin or Sec-Fetch-Site
// value is repeated in a response or a log line. The client chose the value.
const shownHostBytes = 64

// DefaultAllowedHosts is the list used when config/security.yaml sets none:
// the three names a browser or a client on the same machine reaches a
// loopback bind by. Each call returns a new slice.
func DefaultAllowedHosts() []string {
	return []string{"localhost", "127.0.0.1", "[::1]"}
}

// WithAllowedHosts turns both checks on and gives the names a request's Host
// may carry (ISSUE-0212). The names are made canonical here, so a caller may
// pass them as an operator writes them. One that is not a name or an address
// is left out, and if none is left every request but a health probe is
// refused: NormalizeAllowedHosts is where a caller learns of a bad name first.
//
// The option fails closed. Given no names at all, nil or an empty list, it
// still turns the checks on and so refuses everything but a health probe: a
// caller that builds its list wrongly cannot switch them off. Only a server
// built without the option runs neither check. The orchestrator always
// passes it (cmd/orchestrator, initAuth); bare servers in tests do not.
func WithAllowedHosts(names []string) ServerOption {
	return func(s *Server) {
		allowed := make(map[string]struct{}, len(names))
		for _, name := range names {
			if entry, err := allowedHostEntry(name); err == nil {
				allowed[entry] = struct{}{}
			}
		}
		s.allowedHosts = allowed
	}
}

// originMiddleware runs the two checks in front of everything but the access
// log: the API, the console's files, the sign-in routes and the event streams.
// It sits outside authMiddleware, so a refused request is told why it was
// refused whether or not it also lacked a session.
func (s *Server) originMiddleware(next http.Handler) http.Handler {
	if s.allowedHosts == nil {
		return next
	}
	// The zero value trusts no other origin and exempts no route.
	var crossOrigin http.CrossOriginProtection
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != healthzPath {
			name := canonicalHost(r.Host)
			if _, ok := s.allowedHosts[name]; !ok {
				shown := shorten(name)
				s.logger.Warn("request refused: its Host is not an expected name ("+allowedHostsSetting+")",
					zap.String("host", shown),
					zap.String("method", r.Method),
					zap.String("path", r.URL.Path),
				)
				writeError(w, "FORBIDDEN",
					fmt.Sprintf("host name %q is not expected: add it to %s", shown, allowedHostsSetting),
					http.StatusForbidden)
				return
			}
		}
		// Reads pass: a page on another site cannot read the answer, and
		// the Host check above is what stops a rebound page. Among writes,
		// Sec-Fetch-Site decides when the browser sent it (same-origin and
		// none pass). Without it an Origin must name this request's own
		// Host, port included: that is every browser write to a plain-HTTP
		// address that is not localhost or loopback. A request with neither
		// header is not from a browser.
		if err := crossOrigin.Check(r); err != nil {
			s.logger.Warn("request refused: a write that the browser marks as coming from another origin",
				zap.String("method", r.Method),
				zap.String("path", r.URL.Path),
				zap.String("origin", shorten(r.Header.Get("Origin"))),
				zap.String("sec_fetch_site", shorten(r.Header.Get("Sec-Fetch-Site"))),
			)
			writeError(w, "FORBIDDEN", crossOriginRefusal, http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// canonicalHost returns the name a Host value addresses, in the one spelling
// the allow-list is kept in: without its port, in lower case, and an IPv6
// address in its shortest form inside brackets.
//
// The port is dropped because it says nothing about who is asking: a rebound
// page keeps its own name on any port, and an operator may publish the
// orchestrator on another port without touching the list.
//
// A value that is neither a name nor an address, with or without a port,
// comes back as it came, and so matches nothing on the list.
func canonicalHost(hostport string) string {
	host := hostport
	switch {
	case strings.HasPrefix(hostport, "["):
		end := strings.IndexByte(hostport, ']')
		if end < 0 || !isPortSuffix(hostport[end+1:]) {
			return hostport
		}
		addr, err := netip.ParseAddr(hostport[1:end])
		if err != nil || !addr.Is6() || addr.Zone() != "" {
			return hostport
		}
		return "[" + addr.String() + "]"
	case strings.Count(hostport, ":") == 1:
		colon := strings.IndexByte(hostport, ':')
		if !isPortSuffix(hostport[colon:]) {
			return hostport
		}
		host = hostport[:colon]
	}
	if addr, err := netip.ParseAddr(host); err == nil && addr.Zone() == "" {
		if addr.Is6() {
			return "[" + addr.String() + "]"
		}
		return addr.String()
	}
	return strings.ToLower(host)
}

// isPortSuffix reports whether s is what may follow the name in a Host value:
// nothing, or a colon and up to five digits.
func isPortSuffix(s string) bool {
	if s == "" {
		return true
	}
	if s[0] != ':' || len(s) > 6 {
		return false
	}
	for _, c := range s[1:] {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// hostNameRE is a host name as the list accepts it, after lower-casing:
// letters, digits, dots, hyphens and underscores (a compose service may be
// named with one), starting with a letter or a digit, with one optional dot at
// the end. No port, scheme, path or wildcard fits it.
var hostNameRE = regexp.MustCompile(`^[a-z0-9]([a-z0-9._-]*[a-z0-9])?\.?$`)

// NormalizeAllowedHosts checks a list of expected names as an operator wrote
// them and returns each once, in the spelling canonicalHost gives a request's
// Host. An entry is a host name or an IP address on its own; an IPv6 address
// may be written with or without its brackets.
func NormalizeAllowedHosts(names []string) ([]string, error) {
	out := make([]string, 0, len(names))
	seen := make(map[string]struct{}, len(names))
	for _, name := range names {
		entry, err := allowedHostEntry(name)
		if err != nil {
			return nil, err
		}
		if _, dup := seen[entry]; dup {
			continue
		}
		seen[entry] = struct{}{}
		out = append(out, entry)
	}
	return out, nil
}

func allowedHostEntry(name string) (string, error) {
	bad := fmt.Errorf("%q is not a host name or an IP address on its own (no scheme, port, path or wildcard)", name)
	if strings.HasPrefix(name, "[") {
		if !strings.HasSuffix(name, "]") {
			return "", bad
		}
		addr, err := netip.ParseAddr(name[1 : len(name)-1])
		if err != nil || !addr.Is6() || addr.Zone() != "" {
			return "", bad
		}
		return "[" + addr.String() + "]", nil
	}
	if addr, err := netip.ParseAddr(name); err == nil {
		if addr.Zone() != "" {
			return "", bad
		}
		return canonicalHost(name), nil
	}
	lower := strings.ToLower(name)
	if !hostNameRE.MatchString(lower) {
		return "", bad
	}
	return lower, nil
}

// shorten cuts a value the client chose to shownHostBytes, on a character
// boundary, and marks the cut.
func shorten(s string) string {
	if len(s) <= shownHostBytes {
		return s
	}
	cut := shownHostBytes
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}
