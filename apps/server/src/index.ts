import Fastify from "fastify";
import cors from "@fastify/cors";
import { orgRoutes } from "./routes/org.js";
import { chatRoutes } from "./routes/chat.js";
import { logRoutes } from "./routes/logs.js";

const app = Fastify({ logger: true });

await app.register(cors, { origin: "http://localhost:5173" });

// Register routes
await app.register(orgRoutes, { prefix: "/api/org" });
await app.register(chatRoutes, { prefix: "/api/chat" });
await app.register(logRoutes, { prefix: "/api/logs" });

// Health check
app.get("/api/health", async () => ({ status: "ok", timestamp: Date.now() }));

const port = Number(process.env.PORT) || 3200;
await app.listen({ port, host: "0.0.0.0" });
console.log(`HiveWeave server running on http://localhost:${port}`);
