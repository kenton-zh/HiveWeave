/**
 * MCP Service — manages MCP server connections (stdio + HTTP).
 * Agents call `hiveweave__mcp_call` to invoke MCP tools on any connected server.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export interface McpServerConfig {
  name: string;
  transport: "stdio" | "http";
  // stdio
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  // http
  url?: string;
  // meta
  enabled: boolean;
}

interface McpConnection {
  config: McpServerConfig;
  client: Client;
  tools: McpTool[];
}

export interface McpTool {
  serverName: string;
  name: string;
  description: string;
  inputSchema: Record<string, any>;
}

export class McpService {
  private connections = new Map<string, McpConnection>();
  private configs = new Map<string, McpServerConfig>();

  /** Register or update a server config */
  setConfig(config: McpServerConfig): void {
    this.configs.set(config.name, config);
  }

  /** Remove a server */
  removeConfig(name: string): void {
    this.configs.delete(name);
    this.disconnect(name);
  }

  /** List all registered servers */
  listServers(): McpServerConfig[] {
    return [...this.configs.values()];
  }

  /** Connect to a server and discover its tools */
  async connect(name: string): Promise<McpTool[]> {
    const config = this.configs.get(name);
    if (!config) throw new Error(`MCP server "${name}" not configured.`);
    if (!config.enabled) throw new Error(`MCP server "${name}" is disabled.`);

    // Reuse existing connection
    const existing = this.connections.get(name);
    if (existing) return existing.tools;

    let transport;
    if (config.transport === "stdio") {
      if (!config.command) throw new Error(`stdio server "${name}" requires a command.`);
      transport = new StdioClientTransport({
        command: config.command,
        args: config.args || [],
        env: config.env,
        cwd: config.cwd,
      });
    } else {
      if (!config.url) throw new Error(`HTTP server "${name}" requires a URL.`);
      transport = new StreamableHTTPClientTransport(new URL(config.url));
    }

    const client = new Client({ name: "hiveweave", version: "1.0.0" });
    await client.connect(transport);

    const result = await client.listTools();
    const tools: McpTool[] = result.tools.map((t: any) => ({
      serverName: name,
      name: t.name,
      description: t.description || "",
      inputSchema: t.inputSchema || {},
    }));

    this.connections.set(name, { config, client, tools });
    return tools;
  }

  /** Disconnect from a server */
  async disconnect(name: string): Promise<void> {
    const conn = this.connections.get(name);
    if (conn) {
      try { await conn.client.close(); } catch { /* ignore */ }
      this.connections.delete(name);
    }
  }

  /** Call an MCP tool */
  async callTool(serverName: string, toolName: string, args: Record<string, any>): Promise<string> {
    let conn = this.connections.get(serverName);
    if (!conn) {
      // Auto-connect
      const tools = await this.connect(serverName);
      conn = this.connections.get(serverName);
      if (!conn) throw new Error(`Cannot connect to MCP server "${serverName}".`);
    }

    const result = await conn.client.callTool({ name: toolName, arguments: args });
    if ((result as any).isError) {
      const content = (result as any).content;
      const text = Array.isArray(content) ? content.map((c: any) => c.text || "").join("\n") : String(content);
      return `MCP Error: ${text}`;
    }

    const content = result.content as any[];
    if (!content || content.length === 0) return "(no output)";
    return content.map((c: any) => {
      if (c.type === "text") return c.text;
      if (c.type === "resource") return `[resource: ${c.resource?.uri}]`;
      return JSON.stringify(c);
    }).join("\n");
  }

  /** List tools from a specific server (auto-connects if needed) */
  async listTools(serverName: string): Promise<McpTool[]> {
    let conn = this.connections.get(serverName);
    if (!conn) return this.connect(serverName);
    return conn.tools;
  }

  /** List all available MCP tools across all enabled servers */
  async listAllTools(): Promise<McpTool[]> {
    const all: McpTool[] = [];
    for (const [name, config] of this.configs) {
      if (!config.enabled) continue;
      try {
        const tools = await this.listTools(name);
        all.push(...tools);
      } catch { /* server may be offline */ }
    }
    return all;
  }
}

export const mcpService = new McpService();
