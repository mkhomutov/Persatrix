// Package bridges is a placeholder for planned channel bridges, which will
// connect a Persatrix channel to an outside service such as Slack, Discord,
// Telegram, email or a generic webhook. Nothing is implemented yet: the
// TODOs below are the plan, sketched in docs/persatrix-extension-spec.md
// §E5.4. RFC 0011 built internal channels and deferred bridges to a later
// RFC; ROADMAP.md has the target release.
package bridges

// TODO: Implement BridgeManager (lifecycle, routing, approval)
// TODO: Implement EmailBridge (SMTP/IMAP)
// TODO: Implement SlackBridge (Bot API)
// TODO: Implement DiscordBridge (Bot API)
// TODO: Implement TelegramBridge (Bot API)
// TODO: Implement WebhookBridge (generic HTTP)
// TODO: Implement BridgeSecurity (content filter, PII detection, rate limiting)
