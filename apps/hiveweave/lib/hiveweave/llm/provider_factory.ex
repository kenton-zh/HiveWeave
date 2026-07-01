defmodule HiveWeave.LLM.ProviderFactory do
  @moduledoc """
  Provider factory for LLM services.

  Maps provider config to actual HTTP client.
  """

  @doc """
  Create a request configuration for a given provider.
  """
  def build_request(provider_config, messages, opts \\ []) do
    %{
      url: build_url(provider_config),
      headers: build_headers(provider_config),
      body: build_body(provider_config, messages, opts)
    }
  end

  defp build_url(%{base_url: base_url, model: model}) do
    "#{base_url}/chat/completions"
  end

  defp build_headers(%{api_key: api_key}) do
    [
      {"Content-Type", "application/json"},
      {"Authorization", "Bearer #{api_key}"}
    ]
  end

  defp build_body(provider_config, messages, opts) do
    %{
      model: provider_config[:model],
      messages: messages,
      stream: Keyword.get(opts, :stream, true),
      temperature: Keyword.get(opts, :temperature, 0.7),
      max_tokens: provider_config[:max_output_tokens] || 8_192
    }
  end
end
