package security

// Tests for agent tokens' time, revocation and forgetting (RFC 0009 Phases
// 3–4 plan, PR 5). The helpers are in agent_token_test.go.

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestAgentTokenIssuer_RefusesAnExpiredToken(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")

	clk.pass(AgentTokenTTL - time.Nanosecond)
	_, err := issuer.Check(token)
	require.NoError(t, err, "a token works until the last moment of its 24 hours")

	clk.pass(time.Nanosecond)
	_, err = issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired)

	fresh := issueToken(t, issuer, "ember-owl")
	_, err = issuer.Check(fresh)
	assert.NoError(t, err, "an agent whose token expired gets a working one by registering again")
}

// A token stops at 24 hours of the issuer's running time however the time
// of day is stepped, so no time sync lengthens a token's life or brings an
// expired one back (PR 5 review F-10).
func TestAgentTokenIssuer_ATokenStopsAt24RunningHoursWhateverTheClockSays(t *testing.T) {
	t.Run("the clock steps back", func(t *testing.T) {
		clk := newTokenClock(tokenClockStart)
		issuer := newTestIssuer(clk)
		token := issueToken(t, issuer, "ember-owl")
		clk.pass(AgentTokenTTL - time.Hour)
		clk.stepWall(-2 * time.Hour)
		_, err := issuer.Check(token)
		require.NoError(t, err)

		clk.pass(time.Hour)
		_, err = issuer.Check(token)
		assert.ErrorIs(t, err, ErrAgentTokenExpired, "the clock reads two hours short of the expiry, but 24 hours have run")
	})
	t.Run("the clock jumps ahead, and is corrected", func(t *testing.T) {
		clk := newTokenClock(tokenClockStart)
		issuer := newTestIssuer(clk)
		clk.stepWall(30 * 24 * time.Hour)
		ahead := issueToken(t, issuer, "ember-owl")
		clk.stepWall(-30 * 24 * time.Hour)
		after, claims, err := issuer.Issue("tide-wren")
		require.NoError(t, err)
		assert.Equal(t, tokenClockStart, claims.IssuedAt, "a token issued after the correction carries the corrected date")

		clk.pass(AgentTokenTTL)
		for _, token := range []string{ahead, after} {
			_, err := issuer.Check(token)
			assert.ErrorIs(t, err, ErrAgentTokenExpired)
		}
	})
	t.Run("an expired token, and the clock steps back", func(t *testing.T) {
		clk := newTokenClock(tokenClockStart)
		issuer := newTestIssuer(clk)
		token := issueToken(t, issuer, "ember-owl")
		clk.pass(AgentTokenTTL)
		_, err := issuer.Check(token)
		require.ErrorIs(t, err, ErrAgentTokenExpired)

		clk.stepWall(-2 * time.Hour)
		_, err = issuer.Check(token)
		assert.ErrorIs(t, err, ErrAgentTokenExpired, "an expired token came back when the clock went back")
	})
}

// Time the machine sleeps counts: the running clock stops while it sleeps,
// but the time of day moves on, and a token stops at its expiry by that
// clock too (PR 5 review F-14).
func TestAgentTokenIssuer_TimeTheMachineSleepsCounts(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")
	clk.pass(time.Hour)
	clk.stepWall(7 * 24 * time.Hour) // asleep for a week

	_, err := issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired)
}

// A machine without a clock battery can read 1970 until it syncs. A token
// issued at that first instant works; once the clock syncs, a token carries
// the date the clock reads, and the step forward ends the tokens issued
// before it, whose agents register again (PR 5 review F-15).
func TestAgentTokenIssuer_DatesFollowAClockThatSyncsAfterStart(t *testing.T) {
	epoch := time.Unix(0, 0).UTC()
	clk := newTokenClock(epoch)
	issuer := newTestIssuer(clk)
	early, issued, err := issuer.Issue("ember-owl")
	require.NoError(t, err)
	require.Zero(t, issued.IssuedAt.UnixNano())
	_, err = issuer.Check(early)
	require.NoError(t, err)

	clk.pass(time.Minute)
	clk.stepWall(tokenClockStart.Sub(epoch)) // the first sync
	_, synced, err := issuer.Issue("tide-wren")
	require.NoError(t, err)
	assert.Equal(t, tokenClockStart.Add(time.Minute), synced.IssuedAt)
	_, err = issuer.Check(early)
	assert.ErrorIs(t, err, ErrAgentTokenExpired)
}

func TestAgentTokenIssuer_RevokeRefusesEveryTokenTheAgentHolds(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	first := issueToken(t, issuer, "ember-owl")
	clk.pass(time.Minute)
	second := issueToken(t, issuer, "ember-owl")
	other := issueToken(t, issuer, "tide-wren")

	issuer.Revoke("ember-owl")

	for _, token := range []string{first, second} {
		_, err := issuer.Check(token)
		assert.ErrorIs(t, err, ErrAgentTokenRevoked)
	}
	got, err := issuer.Check(other)
	require.NoError(t, err, "revoking one agent's tokens leaves another's working")
	assert.Equal(t, "tide-wren", got.AgentID)
}

// Registering again shuts out every earlier token the agent holds, so a
// copy of one, leaked or stolen, stops working once the agent has a new one
// (PR 5 review F-12).
func TestAgentTokenIssuer_ANewTokenRetiresTheAgentsEarlierOnes(t *testing.T) {
	issuer := newTestIssuer(newTokenClock(tokenClockStart))
	copied := issueToken(t, issuer, "ember-owl")
	other := issueToken(t, issuer, "tide-wren")
	fresh := issueToken(t, issuer, "ember-owl")

	_, err := issuer.Check(copied)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "the agent's earlier token works beside its new one")
	for _, token := range []string{fresh, other} {
		_, err := issuer.Check(token)
		assert.NoError(t, err)
	}
}

func TestAgentTokenIssuer_ATokenIssuedAfterARevocationWorks(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	old := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")

	// The clocks read what they read at the revocation, as when a
	// deregistered agent registers again at once.
	sameMoment := issueToken(t, issuer, "ember-owl")
	_, err := issuer.Check(sameMoment)
	assert.NoError(t, err, "a token issued after a revocation is not caught by it")

	// Revocation goes by serial number, so the time of day stepping back
	// changes nothing.
	clk.stepWall(-time.Hour)
	issuer.Revoke("ember-owl")
	_, err = issuer.Check(sameMoment)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "a revocation after the step still catches every token issued before it")
	afterStep := issueToken(t, issuer, "ember-owl")
	_, err = issuer.Check(afterStep)
	assert.NoError(t, err, "a token issued after the step's revocation works")

	_, err = issuer.Check(old)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked)
}

// A revoked token reads as revoked after its 24 hours too, for as long as
// the issuer holds the agent, since an agent told its token expired
// registers again and one told it was revoked does not (PR 5 review F-13).
// Once the issuer forgets the agent, the token reads as expired.
func TestAgentTokenIssuer_ARevokedTokenReadsRevokedPastItsExpiry(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")
	clk.pass(AgentTokenTTL)

	_, err := issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked)

	issueUntilForgetting(t, issuer, "filler")
	require.NotContains(t, issuer.agents, "ember-owl")
	_, err = issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired)
}

// issueUntilForgetting issues tokens to new agents, named from prefix, until
// the next Issue forgets the agents whose tokens have all expired, and then
// makes that Issue.
func issueUntilForgetting(t *testing.T, issuer *AgentTokenIssuer, prefix string) {
	t.Helper()
	for i := 0; len(issuer.agents) < issuer.sweepAt; i++ {
		require.Less(t, i, 4*agentTokenSweepFloor, "the map never reached the size that sets Issue forgetting")
		issueToken(t, issuer, fmt.Sprintf("%s-%d", prefix, i))
	}
	issueToken(t, issuer, prefix+"-last")
}

func TestAgentTokenIssuer_ForgetsAnAgentOnceAllItsTokensHaveExpired(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	old := issueToken(t, issuer, "ember-owl")
	for range 3 {
		issuer.Revoke("ember-owl")
	}
	issuer.Revoke("never-issued")
	issuer.Revoke("Not An Agent")
	assert.Len(t, issuer.agents, 1, "one entry per agent holding a token; revoking again, or revoking an agent with none, keeps nothing more")

	clk.pass(AgentTokenTTL)
	issueUntilForgetting(t, issuer, "later")
	assert.NotContains(t, issuer.agents, "ember-owl", "an agent whose tokens have all expired is forgotten")
	assert.LessOrEqual(t, len(issuer.agents), 2*agentTokenSweepFloor)
	_, err := issuer.Check(old)
	assert.ErrorIs(t, err, ErrAgentTokenExpired, "a forgotten agent's token is still refused")
}

// Issue forgets in passes that wait for the map to double since the last
// one, so it does not walk every agent on each call once there are many
// (PR 5 review F-17).
func TestAgentTokenIssuer_ForgetsOnlyOnceTheMapHasDoubled(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	// Past the floor, so a pass runs once and finds every token working.
	live := agentTokenSweepFloor + agentTokenSweepFloor/2
	for i := range live {
		issueToken(t, issuer, fmt.Sprintf("agent-%d", i))
	}
	clk.pass(AgentTokenTTL)

	issueToken(t, issuer, "next")
	assert.Len(t, issuer.agents, live+1, "every token has expired, but the map has not doubled since the last pass, so Issue forgot nothing")

	issueUntilForgetting(t, issuer, "filler")
	assert.NotContains(t, issuer.agents, "agent-0")
	assert.Contains(t, issuer.agents, "next", "a token issued after the others expired still works")
	assert.Less(t, len(issuer.agents), live, "once the map has doubled, Issue forgets the expired")
}

func TestAgentTokenIssuer_KeepsARevocationWhileTheAgentsLatestTokenWorks(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")
	clk.pass(23 * time.Hour)
	later := issueToken(t, issuer, "ember-owl")
	issuer.Revoke("ember-owl")

	clk.pass(2 * time.Hour) // past the first token's 24 hours, not the later one's
	issueUntilForgetting(t, issuer, "filler")
	_, err := issuer.Check(later)
	assert.ErrorIs(t, err, ErrAgentTokenRevoked, "the agent, and its revocation, are kept while its latest token still works")
}

// A revoked token stays refused even when the time of day goes back after
// the issuer forgot the agent (PR 5 review F-1).
func TestAgentTokenIssuer_ARevokedTokenStaysRefusedWhenTheClockGoesBack(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	issuer := newTestIssuer(clk)
	token := issueToken(t, issuer, "ember-owl")
	clk.pass(time.Minute)
	issuer.Revoke("ember-owl")

	clk.pass(AgentTokenTTL + time.Second)
	issueUntilForgetting(t, issuer, "filler")
	require.NotContains(t, issuer.agents, "ember-owl", "the issuer has forgotten the agent and its revocation")

	clk.stepWall(-2 * time.Minute) // to before the token expired
	_, err := issuer.Check(token)
	assert.ErrorIs(t, err, ErrAgentTokenExpired, "a revoked token came back when the clock went back")
}

// Check reads its time and the agent's revocation together, under its lock,
// so nothing can drop the revocation between the two (PR 5 review F-1).
func TestAgentTokenIssuer_CheckReadsItsTimeUnderItsLock(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	var issuer *AgentTokenIssuer
	checking, unlockedReads := false, 0
	issuer = newAgentTokenIssuer(newTestKey(), func() (int64, time.Duration) {
		if checking && issuer.mu.TryLock() {
			issuer.mu.Unlock()
			unlockedReads++
		}
		return clk.read()
	})
	token := issueToken(t, issuer, "ember-owl")

	checking = true
	_, err := issuer.Check(token)
	checking = false
	require.NoError(t, err)
	assert.Zero(t, unlockedReads, "Check read the clock while a revocation could be dropped")
}

// Every read of the clock moves it on, so tokens expire while the goroutines
// run and Issue forgets agents while the others check theirs (PR 5 review
// F-18).
func TestAgentTokenIssuer_IsSafeForConcurrentUse(t *testing.T) {
	clk := newTokenClock(tokenClockStart)
	clk.tick = AgentTokenTTL / 64
	issuer := newTestIssuer(clk)
	const goroutines, rounds = 8, 50
	var wg sync.WaitGroup
	for i := range goroutines {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range rounds {
				id := fmt.Sprintf("agent-%d-%d", i, j)
				token, _, err := issuer.Issue(id)
				if err != nil {
					t.Error(err)
					return
				}
				// The clock may have run past the token's 24 hours
				// since it was issued.
				if _, err := issuer.Check(token); err != nil && !errors.Is(err, ErrAgentTokenExpired) {
					t.Errorf("%s: a fresh token was refused: %v", id, err)
				}
				issuer.Revoke(id)
				if _, err := issuer.Check(token); !errors.Is(err, ErrAgentTokenRevoked) && !errors.Is(err, ErrAgentTokenExpired) {
					t.Errorf("%s: a revoked token was not refused as revoked or expired: %v", id, err)
				}
			}
		}()
	}
	wg.Wait()
	assert.Less(t, len(issuer.agents), goroutines*rounds, "no agent was forgotten while the others ran")
}
