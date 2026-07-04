defmodule HiveWeaveWeb.Router do
  use Phoenix.Router

  scope "/", HiveWeaveWeb do
    get "/", RootController, :index
  end

  scope "/api", HiveWeaveWeb do
    pipe_through :api

    # Settings
    get "/settings", SettingsController, :index
    get "/settings/:key", SettingsController, :show
    post "/settings", SettingsController, :upsert
    put "/settings", SettingsController, :upsert

    # Projects
    get "/projects", ProjectsController, :index
    post "/projects", ProjectsController, :create
    get "/projects/:id", ProjectsController, :show
    patch "/projects/:id", ProjectsController, :update
    put "/projects/:id", ProjectsController, :update
    put "/projects/:id/workspace", ProjectsController, :update_workspace
    delete "/projects/:id", ProjectsController, :delete
    get "/projects/:id/game-time", ProjectsController, :game_time
    get "/projects/:id/goals", ProjectsController, :goals
    put "/projects/:id/goals", ProjectsController, :update_goals

    # Org / Agents
    get "/org", OrgController, :tree
    get "/org/agents", OrgController, :list_agents
    get "/org/agents/:id", OrgController, :show_agent
    get "/org/agents/:id/children", OrgController, :children
    post "/org/agents", OrgController, :create_agent
    patch "/org/agents/:id", OrgController, :update_agent
    put "/org/agents/:id", OrgController, :update_agent
    delete "/org/agents/:id", OrgController, :delete_agent
    get "/org/modules", OrgController, :list_modules

    # Chat
    post "/chat", ChatController, :send
    get "/chat/history/:agentId", ChatController, :history
    get "/chat/messages/:agentId", ExtraController, :chat_messages
    get "/chat/unread/:agentId", ChatController, :unread
    post "/chat/mark-read", ChatController, :mark_read
    get "/chat/inbox/:agentId", ChatController, :inbox
    post "/chat/inbox", ChatController, :send_inbox
    post "/chat/pause", ChatController, :pause
    post "/chat/resume", ChatController, :resume
    get "/chat/paused", ChatController, :paused
    post "/chat/reset-processing/:agentId", ChatController, :reset_processing
    get "/chat/resolved-model/:agentId", ChatController, :resolved_model
    get "/chat/todos/:agentId", ExtraController, :chat_todos
    post "/chat/todos/:agentId", ExtraController, :chat_todos_write
    get "/chat/questions", ExtraController, :chat_questions_index
    post "/chat/questions/:id/answer", ExtraController, :chat_questions_answer

    # Permissions / Approvals
    get "/permissions/rules/:agent_id", PermissionsController, :get_rules
    patch "/permissions/rules/:agent_id", PermissionsController, :update_rules
    put "/permissions/rules/:agent_id", PermissionsController, :update_rules
    get "/permissions/pending/:agent_id", PermissionsController, :get_pending
    get "/permissions/pending/project/:project_id", PermissionsController, :get_project_pending
    post "/permissions/respond", PermissionsController, :respond

    # LLM Models
    get "/llm-models", ExtraController, :llm_models_index
    post "/llm-models", ExtraController, :llm_models_create
    get "/llm-models/:id", ExtraController, :llm_model_show
    patch "/llm-models/:id", ExtraController, :llm_model_update
    put "/llm-models/:id", ExtraController, :llm_model_update
    delete "/llm-models/:id", ExtraController, :llm_model_delete
    post "/llm-models/:id/test", ExtraController, :llm_model_test

    # Agent Templates
    get "/agent-templates", ExtraController, :templates_index
    get "/agent-templates/divisions", ExtraController, :template_divisions
    get "/agent-templates/:id", ExtraController, :template_show

    # Communications
    get "/communications", ExtraController, :communications_index
    post "/communications", ExtraController, :communications_create

    # User Pings
    get "/user-pings", ExtraController, :user_pings_index
    post "/user-pings/:id/read", ExtraController, :user_ping_read

    # Project Alarms
    get "/projects/:project_id/alarms", ExtraController, :project_alarms_index
    post "/projects/:project_id/alarms", ExtraController, :project_alarms_create
    delete "/projects/:project_id/alarms/:id", ExtraController, :project_alarm_cancel

    # Work Logs
    get "/logs/:agentId", ExtraController, :work_logs_index
    get "/logs/:agentId/subordinates", ExtraController, :work_logs_subordinates

    # Debug / Monitoring
    get "/debug/agents/:agentId/traces", ExtraController, :debug_traces

    # Filesystem
    get "/fs/browse", ExtraController, :fs_browse

    # Health check
    get "/health", HealthController, :index
  end

  pipeline :api do
    plug :accepts, ["json"]
    plug HiveWeaveWeb.Plugs.ApiKeyAuth
    plug CORSPlug, origin: ["http://localhost:5173", "http://localhost:3200", "http://localhost:4000"]
  end
end

