export { db, createDb, ensureProjectDb, evictProjectDb, getHiveWeaveDir, allSchema, seedDefaultModel, registerAgent, lookupAgentWorkspace, unregisterProjectAgents, getProjectDbForAgent, registerProjectAgents } from "./client.js";
export type { MetaDatabase, ProjectDatabase, Database } from "./client.js";
export * from "./schema/index.js";
