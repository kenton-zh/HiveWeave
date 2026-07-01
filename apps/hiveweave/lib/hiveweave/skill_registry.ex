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

  # ── External skill source (agent-skills project) ────────────
  # Skills loaded from filesystem. .compressed.md preferred over .md.
  @external_skills_dir "d:/PC_AI/Project/agent-skills/skills"
  @external_agents_dir "d:/PC_AI/Project/agent-skills/agents"

  # ── Built-in skill registry (fallback) ──────────────────────

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

  @doc "List all skills — external (agent-skills) + built-in, optionally filtered."
  def list_builtin_skills(search \\ nil) do
    external = list_external_skills(nil)
    all = external ++ @builtin_skills
    filter_skills(all, search)
  end

  @doc "Get a skill by slug — checks external (agent-skills) first, then built-in."
  def get_builtin_skill(slug) do
    case get_external_skill(slug) do
      nil -> Enum.find(@builtin_skills, fn s -> s.slug == slug end)
      external -> external
    end
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

    cond do
      # External skill (agent-skills filesystem)
      get_external_skill(slug) != nil ->
        %{description: desc} = get_external_skill(slug)
        content = case read_external_skill_file(slug) do
          {:ok, c} -> c
          :not_found -> "Instructions not available."
        end
        "## Skill: #{slug}\n\n**Description:** #{desc}\n\n**Source:** agent-skills\n\n---\n\n#{content}"

      # Built-in skill
      %{instructions: instructions, description: desc} = Enum.find(@builtin_skills, fn s -> s.slug == slug end) ->
        "## Skill: #{slug}\n\n**Description:** #{desc}\n\n**Source:** Built-in\n\n---\n\n#{instructions}"

      true ->
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
  Checks external skills (agent-skills dir) first, then built-in, then ClawHub.
  For external skills, reads .compressed.md first, falls back to .md.
  """
  def read_skill(slug, bound_skills \\ []) do
    slug = String.trim(slug)
    is_bound = slug in bound_skills
    prefix = if is_bound, do: "(Bound skill) ", else: ""

    # 1. Try external skill file (.compressed.md first, then .md)
    case read_external_skill_file(slug) do
      {:ok, content} -> "#{prefix}#{content}"
      :not_found ->
        # 2. Try built-in
        case get_builtin_skill(slug) do
          %{instructions: instructions} -> "#{prefix}#{instructions}"
          nil ->
            # 3. Try ClawHub
            case fetch_clawhub_detail(slug) do
              {:ok, detail} ->
                "#{prefix}#{detail[:skill_md] || detail[:summary] || "No instructions available."}"
              {:error, _} ->
                "Skill \"#{slug}\" not found. Use `list_available_skills` to discover available skills."
            end
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

  # ── External skill loading (agent-skills filesystem) ────────

  defp filter_skills(skills, nil), do: skills
  defp filter_skills(skills, ""), do: skills
  defp filter_skills(skills, keyword) do
    k = String.downcase(keyword)
    Enum.filter(skills, fn s ->
      String.contains?(String.downcase(s.slug || ""), k) or
      String.contains?(String.downcase(s[:description] || ""), k)
    end)
  end

  def list_external_skills(search \\ nil) do
    case File.ls(@external_skills_dir) do
      {:ok, dirs} ->
        skills = Enum.flat_map(dirs, fn dir ->
          skill_path = Path.join([@external_skills_dir, dir, "SKILL.md"])
          if File.exists?(skill_path) do
            case parse_frontmatter(skill_path) do
              {:ok, %{name: name, description: desc}} ->
                [%{slug: dir, name: name, description: desc, source: :external}]
              _ -> []
            end
          else
            []
          end
        end)
        filter_skills(skills, search)
      _ -> []
    end
  end

  def get_external_skill(slug) do
    skill_path = Path.join([@external_skills_dir, slug, "SKILL.md"])
    if File.exists?(skill_path) do
      case parse_frontmatter(skill_path) do
        {:ok, %{name: name, description: desc}} ->
          %{slug: slug, name: name, description: desc, source: :external}
        _ -> nil
      end
    else
      nil
    end
  end

  def read_external_skill_file(slug) do
    compressed = Path.join([@external_skills_dir, slug, "SKILL.compressed.md"])
    original = Path.join([@external_skills_dir, slug, "SKILL.md"])

    cond do
      File.exists?(compressed) -> {:ok, File.read!(compressed)}
      File.exists?(original) -> {:ok, File.read!(original)}
      true -> :not_found
    end
  end

  def read_external_persona(name) do
    compressed = Path.join(@external_agents_dir, "#{name}.compressed.md")
    original = Path.join(@external_agents_dir, "#{name}.md")

    cond do
      File.exists?(compressed) -> {:ok, File.read!(compressed)}
      File.exists?(original) -> {:ok, File.read!(original)}
      true -> :not_found
    end
  end

  defp parse_frontmatter(path) do
    case File.read(path) do
      {:ok, content} ->
        if String.starts_with?(content, "---") do
          parts = String.split(content, ~r/^---\s*$/m, parts: 3)
          case parts do
            [_, frontmatter, _body] ->
              name = extract_yaml_field(frontmatter, "name")
              description = extract_yaml_field(frontmatter, "description")
              {:ok, %{name: name, description: description}}
            _ -> {:error, :invalid_frontmatter}
          end
        else
          {:error, :no_frontmatter}
        end
      _ -> {:error, :read_error}
    end
  end

  defp extract_yaml_field(yaml, field) do
    pattern = ~r/^#{field}:\s*(.+)$/m
    case Regex.run(pattern, yaml) do
      [_, value] ->
        String.trim(value)
        |> String.trim("\"")
        |> String.trim("'")
        |> String.replace_prefix(">", "")
        |> String.trim()
      _ -> nil
    end
  end
end
