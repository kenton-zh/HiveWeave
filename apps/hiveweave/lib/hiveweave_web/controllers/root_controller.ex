defmodule HiveWeaveWeb.RootController do
  use Phoenix.Controller

  def index(conn, _params) do
    html(conn, """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <title>HiveWeave v1.5 Backend (Elixir/Phoenix)</title>
      <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #e6edf3; padding: 2rem; max-width: 900px; margin: 0 auto; line-height: 1.6; }
        h1 { color: #7c6cf0; margin-bottom: 0.5rem; font-size: 1.8rem; }
        .subtitle { color: #8b949e; margin-bottom: 2rem; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.5rem; margin-bottom: 1rem; }
        .card h2 { color: #2dd4bf; font-size: 1.1rem; margin-bottom: 1rem; }
        .endpoint { font-family: 'JetBrains Mono', monospace; background: #0d1117; padding: 0.5rem 0.75rem; border-radius: 4px; margin: 0.25rem 0; font-size: 0.85rem; }
        .method { display: inline-block; min-width: 50px; padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.7rem; font-weight: 600; text-align: center; margin-right: 0.5rem; }
        .get { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb44; }
        .post { background: #23863622; color: #3fb950; border: 1px solid #23863644; }
        .patch { background: #9e6a0322; color: #d29922; border: 1px solid #9e6a0344; }
        .delete { background: #da363322; color: #f85149; border: 1px solid #da363344; }
        .put { background: #8957e522; color: #d2a8ff; border: 1px solid #8957e544; }
        .ws { background: #f59e0b22; color: #f59e0b; border: 1px solid #f59e0b44; }
        .footer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #30363d; color: #8b949e; font-size: 0.85rem; }
        a { color: #2dd4bf; text-decoration: none; }
        a:hover { text-decoration: underline; }
      </style>
    </head>
    <body>
      <h1>🜲 HiveWeave v1.5 Backend</h1>
      <p class="subtitle">Elixir/Phoenix + BEAM supervision tree. Running on port 4000.</p>

      <div class="card">
        <h2>WebSocket</h2>
        <div class="endpoint"><span class="method ws">WS</span> /socket/websocket</div>
        <p style="margin-top: 0.5rem; color: #8b949e; font-size: 0.85rem;">Channels: <code>lobby:status</code>, <code>project:&lt;id&gt;</code>, <code>agent:&lt;id&gt;</code></p>
      </div>

      <div class="card">
        <h2>Health</h2>
        <div class="endpoint"><span class="method get">GET</span> <a href="/api/health">/api/health</a></div>
      </div>

      <div class="card">
        <h2>Projects</h2>
        <div class="endpoint"><span class="method get">GET</span> <a href="/api/projects">/api/projects</a></div>
        <div class="endpoint"><span class="method post">POST</span> /api/projects</div>
        <div class="endpoint"><span class="method get">GET</span> /api/projects/&lt;id&gt;</div>
        <div class="endpoint"><span class="method patch">PATCH</span> /api/projects/&lt;id&gt;</div>
        <div class="endpoint"><span class="method delete">DELETE</span> /api/projects/&lt;id&gt;</div>
        <div class="endpoint"><span class="method get">GET</span> /api/projects/&lt;id&gt;/game-time</div>
        <div class="endpoint"><span class="method get">GET</span> /api/projects/&lt;id&gt;/goals</div>
        <div class="endpoint"><span class="method put">PUT</span> /api/projects/&lt;id&gt;/goals</div>
      </div>

      <div class="card">
        <h2>Org / Agents</h2>
        <div class="endpoint"><span class="method get">GET</span> <a href="/api/org">/api/org</a></div>
        <div class="endpoint"><span class="method get">GET</span> <a href="/api/org/agents">/api/org/agents</a></div>
        <div class="endpoint"><span class="method post">POST</span> /api/org/agents</div>
        <div class="endpoint"><span class="method get">GET</span> /api/org/agents/&lt;id&gt;</div>
        <div class="endpoint"><span class="method patch">PATCH</span> /api/org/agents/&lt;id&gt;</div>
        <div class="endpoint"><span class="method delete">DELETE</span> /api/org/agents/&lt;id&gt;</div>
      </div>

      <div class="card">
        <h2>Settings</h2>
        <div class="endpoint"><span class="method get">GET</span> <a href="/api/settings">/api/settings</a></div>
        <div class="endpoint"><span class="method get">GET</span> /api/settings/&lt;key&gt;</div>
        <div class="endpoint"><span class="method post">POST</span> /api/settings</div>
      </div>

      <div class="card">
        <h2>Chat (HTTP trigger; stream via WebSocket)</h2>
        <div class="endpoint"><span class="method post">POST</span> /api/chat</div>
        <div class="endpoint"><span class="method get">GET</span> /api/chat/history/&lt;agentId&gt;</div>
        <div class="endpoint"><span class="method post">POST</span> /api/chat/mark-read</div>
        <div class="endpoint"><span class="method get">GET</span> /api/chat/inbox/&lt;agentId&gt;</div>
        <div class="endpoint"><span class="method post">POST</span> /api/chat/inbox</div>
        <div class="endpoint"><span class="method get">GET</span> /api/chat/paused</div>
        <div class="endpoint"><span class="method post">POST</span> /api/chat/pause</div>
        <div class="endpoint"><span class="method post">POST</span> /api/chat/resume</div>
      </div>

      <div class="footer">
        Backend: <code>hiveweave v0.2.0</code> on Elixir 1.17 + OTP 26 · Migration from Fastify/TypeScript in progress
      </div>
    </body>
    </html>
    """)
  end
end
