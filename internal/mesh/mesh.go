// Package mesh is a placeholder for planned multi-machine networking: a
// registry of machines (nodes), agent addresses of the form
// agent_id@node_id, and message routing between nodes. Nothing is
// implemented yet: the TODOs below are the plan, sketched in
// docs/persatrix-extension-spec.md §E6. ROADMAP.md tracks the status.
//
// Agents that run as separate network services can already message each
// other today, through the orchestrator: each agent registers its network
// address, and the orchestrator delivers messages to it.
package mesh

// TODO: Implement NodeRegistry (register, discover, health check)
// TODO: Implement AgentAddressing (agent_id@node_id resolution)
// TODO: Implement MessageRouter (hub-and-spoke, latency-aware)
// TODO: Implement PartitionHandler (queue messages for offline nodes)
// TODO: Implement AgentMigrator (state transfer between nodes)
