// Package mesh is a placeholder for planned multi-machine networking, so
// that agents running on different machines (nodes) can find and message
// each other. Nothing is implemented yet: the TODOs below are the plan,
// sketched in docs/persatrix-extension-spec.md §E6. No RFC covers it yet;
// ROADMAP.md has the target release.
package mesh

// TODO: Implement NodeRegistry (register, discover, health check)
// TODO: Implement AgentAddressing (agent_id@node_id resolution)
// TODO: Implement MessageRouter (hub-and-spoke, latency-aware)
// TODO: Implement PartitionHandler (queue messages for offline nodes)
// TODO: Implement AgentMigrator (state transfer between nodes)
