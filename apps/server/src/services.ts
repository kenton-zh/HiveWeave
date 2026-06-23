import { clearAllPendingApprovals } from "@hiveweave/core";

/**
 * On startup: resolve all stale in-memory Promise resolvers from the
 * previous server instance. The actual per-project DB cleanup
 * (marking pending permission_requests as rejected) is done in index.ts
 * by iterating all projects with their per-project DB instances.
 */
clearAllPendingApprovals();
