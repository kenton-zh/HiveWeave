defmodule HiveWeave.SkillRegistry do
  @moduledoc """
  Skill & MCP registry for HiveWeave agents.

  Ported from the TS backend's clawhub-service.ts + BINDING_REGISTRY.

  Two layers:
    1. Built-in registry — hardcoded skills & MCP servers (always available)
    2. ClawHub marketplace — remote skill registry at https://clawhub.ai/api/v1/skills

  Skills are SKILL.md-style instructions. Binding a skill means injecting its
  instructions into the agent's system prompt. The "Active Skills" section in
  the prompt shows only summaries; agents call read_skill/1 at runtime to load
  full instructions.
  """

  require Logger

  # ── Built-in skill registry ──────────────────────────────────

  @builtin_skills [
    %{
      slug: "code-review",
      name: "code-review",
      description: "Review code for quality, patterns, and potential bugs.",
      instructions: """
      # Code Review Skill

      When reviewing code:
      1. Check for correctness — does the code do what it claims?
      2. Check for readability — is it clear and maintainable?
      3. Check for performance — any obvious bottlenecks or N+1 queries?
      4. Check for security — input validation, auth checks, injection risks.
      5. Check for consistency — naming conventions, error handling patterns.
      6. Provide actionable feedback — not just "this is wrong" but "here's how to fix it".
      7. Distinguish between blockers (must fix) and suggestions (nice to have).
      """
    },
    %{
      slug: "testing",
      name: "testing",
      description: "Write and run unit tests and integration tests.",
      instructions: """
      # Testing Skill

      When writing tests:
      1. Start with the happy path — verify core functionality works.
      2. Add edge cases — empty input, boundary values, large input.
      3. Add error cases — invalid input, missing dependencies, timeout.
      4. Use descriptive test names that explain the expected behavior.
      5. Follow AAA pattern: Arrange, Act, Assert.
      6. Mock external dependencies — don't make real API calls in unit tests.
      7. Aim for high coverage on business logic, not on boilerplate.
      """
    },
    %{
      slug: "documentation",
      name: "documentation",
      description: "Generate and maintain project documentation.",
      instructions: """
      # Documentation Skill

      When writing documentation:
      1. Start with a clear summary — what does this module/function do?
      2. Document parameters, return values, and exceptions.
      3. Include usage examples that actually work.
      4. Explain WHY, not just WHAT — the reasoning behind design decisions.
      5. Keep docs near the code they describe.
      6. Update docs when code changes — stale docs are worse than no docs.
      """
    },
    %{
      slug: "debugging",
      name: "debugging",
      description: "Diagnose and fix runtime errors and performance issues.",
      instructions: """
      # Debugging Skill

      When debugging:
      1. Reproduce the issue reliably before attempting fixes.
      2. Read the error message carefully — it usually tells you what's wrong.
      3. Isolate the problem — binary search by commenting out code.
      4. Check recent changes — git log/diff to see what changed.
      5. Add logging to trace execution flow.
      6. Fix the root cause, not the symptom.
      7. Verify the fix doesn't introduce new issues.
      """
    },
    %{
      slug: "refactoring",
      name: "refactoring",
      description: "Restructure code for better maintainability without changing behavior.",
      instructions: """
      # Refactoring Skill

      When refactoring:
      1. Ensure tests exist before refactoring — they verify behavior is unchanged.
      2. Make small, incremental changes — one refactor at a time.
      3. Rename variables/functions to express intent clearly.
      4. Extract complex logic into named functions.
      5. Reduce duplication — DRY, but don't over-abstract.
      6. Keep the public API stable — internal changes only.
      7. Run tests after each change.
      """
    },
    %{
      slug: "security-audit",
      name: "security-audit",
      description: "Scan code for security vulnerabilities and best practice violations.",
      instructions: """
      # Security Audit Skill

      When auditing security:
      1. Check authentication — are all sensitive endpoints protected?
      2. Check authorization — can users access resources they shouldn't?
      3. Check input validation — SQL injection, XSS, path traversal.
      4. Check secrets management — no hardcoded passwords/API keys.
      5. Check dependencies — known CVEs in packages.
      6. Check error handling — don't leak stack traces to users.
      7. Check rate limiting — protect against brute force.
      """
    },
    %{
      slug: "deployment",
      name: "deployment",
      description: "Manage CI/CD pipelines and deployment workflows.",
      instructions: """
      # Deployment Skill

      When managing deployments:
      1. Use infrastructure-as-code — Dockerfile, docker-compose, IaC.
      2. Separate build and run stages in Dockerfiles.
      3. Use environment variables for configuration — never hardcode.
      4. Run tests in CI before deploying.
      5. Use blue-green or canary deployments for zero-downtime.
      6. Monitor after deployment — logs, metrics, alerts.
      7. Have a rollback plan — know how to revert quickly.
      """
    },
    %{
      slug: "data-analysis",
      name: "data-analysis",
      description: "Analyze data sets, generate reports and visualizations.",
      instructions: """
      # Data Analysis Skill

      When analyzing data:
      1. Understand the question — what decision will this analysis inform?
      2. Clean the data — handle missing values, outliers, duplicates.
      3. Explore with summary statistics — mean, median, distribution.
      4. Visualize patterns — use appropriate chart types.
      5. Test hypotheses — statistical significance matters.
      6. Communicate findings clearly — actionable insights, not just numbers.
      7. Document your methodology — others should be able to reproduce.
      """
    }
  ]

  # ── Built-in MCP server registry ─────────────────────────────

  @builtin_mcp_servers [
    %{name: "filesystem", description: "Read/write/search files in the workspace.", type: "local"},
    %{name: "github", description: "Interact with GitHub repositories, issues, and PRs.", type: "remote"},
    %{name: "browser", description: "Browse web pages and extract information.", type: "local"},
    %{name: "database", description: "Query and manage database records.", type: "local"},
    %{name: "slack", description: "Send and read messages in Slack channels.", type: "remote"}
  ]

  # ── Public API ───────────────────────────────────────────────

  @doc "List built-in skills, optionally filtered by search keyword."
  def list_builtin_skills(search \\ nil) do
    case search do
      nil -> @builtin_skills
      "" -> @builtin_skills
      keyword ->
        k = String.downcase(keyword)
        Enum.filter(@builtin_skills, fn s ->
          String.contains?(String.downcase(s.slug), k) or
          String.contains?(String.downcase(s.description), k)
        end)
    end
  end

  @doc "Get a built-in skill by slug."
  def get_builtin_skill(slug) do
    Enum.find(@builtin_skills, fn s -> s.slug == slug end)
  end

  @doc "List built-in MCP servers."
  def list_builtin_mcp do
    @builtin_mcp_servers
  end

  @doc """
  List all available skills — built-in + ClawHub marketplace.
  Returns a formatted string for tool output.
  """
  def list_available_skills(search \\ nil) do
    builtin = list_builtin_skills(search)

    # Try ClawHub marketplace (best-effort, fallback to built-in)
    clawhub_skills =
      case search_clawhub(search) do
        {:ok, skills} when is_list(skills) and length(skills) > 0 -> skills
        _ -> []
      end

    lines =
      case {builtin, clawhub_skills} do
        {[], []} ->
          ["No skills found. Try a different search term."]

        {_, _} ->
          builtin_lines =
            if length(builtin) > 0 do
              ["## Built-in Skills"] ++
              Enum.map(builtin, fn s -> "- **#{s.slug}**: #{s.description} [built-in]" end)
            else
              []
            end

          clawhub_lines =
            if length(clawhub_skills) > 0 do
              ["", "## ClawHub Marketplace"] ++
              Enum.map(clawhub_skills, fn s ->
                "- **#{s.slug}**: #{s[:summary] || s[:description] || "No description"}"
              end)
            else
              []
            end

          builtin_lines ++ clawhub_lines
      end

    header = "Available Skills#{if search, do: " (search: \"#{search}\")", else: ""}:\n\n"
    header <> Enum.join(lines, "\n") <>
    "\n\nTo bind a skill, use `bind_skill` with the slug as skillName."
  end

  @doc """
  Get full detail of a skill by slug — checks built-in first, then ClawHub.
  Returns formatted string for tool output.
  """
  def get_skill_detail(slug) do
    slug = String.trim(slug)

    case get_builtin_skill(slug) do
      %{instructions: instructions, description: desc} ->
        "## Skill: #{slug}\n\n**Description:** #{desc}\n\n**Source:** Built-in\n\n---\n\n#{instructions}"

      nil ->
        # Try ClawHub
        case fetch_clawhub_detail(slug) do
          {:ok, detail} ->
            "## Skill: #{slug}\n\n**Description:** #{detail[:summary] || detail[:description] || "No description"}\n\n**Source:** ClawHub Marketplace\n\n---\n\n#{detail[:skill_md] || "No instructions available."}"

          {:error, _reason} ->
            "Skill \"#{slug}\" not found in built-in registry or ClawHub. Use `list_available_skills` to search for available skills."
        end
    end
  end

  @doc """
  Read a skill's full instructions — used at runtime by agents.
  Checks agent's bound skills first, then any skill by slug.
  """
  def read_skill(slug, bound_skills \\ []) do
    slug = String.trim(slug)

    # If agent has bound skills, check if this slug is bound
    is_bound = slug in bound_skills
    prefix = if is_bound, do: "(Bound skill) ", else: ""

    case get_builtin_skill(slug) do
      %{instructions: instructions} ->
        "#{prefix}#{instructions}"

      nil ->
        case fetch_clawhub_detail(slug) do
          {:ok, detail} ->
            "#{prefix}#{detail[:skill_md] || detail[:summary] || "No instructions available."}"

          {:error, _} ->
            "Skill \"#{slug}\" not found. Use `list_available_skills` to discover available skills."
        end
    end
  end

  @doc "List available MCP servers — built-in + configured."
  def list_available_mcp do
    lines = Enum.map(@builtin_mcp_servers, fn s ->
      "- **#{s.name}**: #{s.description} [#{s.type}]"
    end)

    if lines == [] do
      "No MCP servers currently registered in the system."
    else
      "Available MCP Servers:\n\n" <> Enum.join(lines, "\n") <>
      "\n\nTo bind an MCP server to an agent, use `bind_mcp` with the server name."
    end
  end

  @doc """
  Build the "Active Skills" section for an agent's system prompt.
  Shows only summaries to save context. Agents call read_skill at runtime for full instructions.
  """
  def build_active_skills_section(bound_skills_json) when is_binary(bound_skills_json) do
    case parse_json_list(bound_skills_json) do
      [] -> ""
      slugs ->
        lines = Enum.map(slugs, fn slug ->
          case get_builtin_skill(slug) do
            %{description: desc} -> "- **#{slug}**: #{desc}"
            nil -> "- **#{slug}**: (custom skill — use read_skill to load instructions)"
          end
        end)

        """
        ## Active Skills
        The following skills are bound to you. Each shows only a summary here.
        When a task matches a skill, use `read_skill("#{hd(slugs)}")` (or the relevant slug) to load its full instructions before proceeding.

        #{Enum.join(lines, "\n")}
        """
    end
  end

  def build_active_skills_section(_), do: ""

  # ── ClawHub HTTP client (best-effort, graceful fallback) ─────

  defp search_clawhub(search) do
    url = "https://clawhub.ai/api/v1/skills"
    params = if search, do: [search: search, limit: 10], else: [limit: 10]

    case Req.get(url, params: params, receive_timeout: 5_000) do
      {:ok, %{status: 200, body: body}} when is_map(body) ->
        skills = Map.get(body, "items", []) |> Enum.map(fn s ->
          %{
            slug: s["slug"],
            summary: s["summary"],
            description: s["description"],
            displayName: s["displayName"]
          }
        end)
        {:ok, skills}

      _ ->
        {:error, :clawhub_unavailable}
    end
  rescue
    e ->
      Logger.debug("[SkillRegistry] ClawHub search failed: #{inspect(e)}")
      {:error, :clawhub_unavailable}
  end

  defp fetch_clawhub_detail(slug) do
    url = "https://clawhub.ai/api/v1/skills/#{slug}"

    case Req.get(url, receive_timeout: 5_000) do
      {:ok, %{status: 200, body: body}} when is_map(body) ->
        {:ok, %{
          slug: body["slug"],
          summary: body["summary"],
          description: body["description"],
          skill_md: body["skillMd"] || body["skill_md"]
        }}

      _ ->
        {:error, :not_found}
    end
  rescue
    e ->
      Logger.debug("[SkillRegistry] ClawHub detail fetch failed: #{inspect(e)}")
      {:error, :clawhub_unavailable}
  end

  # ── Helpers ──────────────────────────────────────────────────

  defp parse_json_list(json) when is_binary(json) do
    case Jason.decode(json) do
      {:ok, list} when is_list(list) -> list
      _ -> []
    end
  end

  defp parse_json_list(_), do: []
end
