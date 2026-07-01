defmodule HiveWeave.LLM.ProviderFactoryTest do
  use ExUnit.Case

  alias HiveWeave.LLM.ProviderFactory

  test "build_request creates a valid request" do
    config = %{
      base_url: "https://api.example.com/v1",
      model: "test-model",
      api_key: "test-key",
      max_output_tokens: 4096
    }
    messages = [%{role: "user", content: "hello"}]

    request = ProviderFactory.build_request(config, messages, [])

    assert request.url == "https://api.example.com/v1/chat/completions"
    assert {"Authorization", "Bearer test-key"} in request.headers
    assert request.body.model == "test-model"
    assert request.body.messages == messages
    assert request.body.stream == true
    assert request.body.max_tokens == 4096
  end

  test "build_request respects stream option" do
    config = %{base_url: "https://x", model: "m", api_key: "k", max_output_tokens: 1024}
    req = ProviderFactory.build_request(config, [], stream: false)
    assert req.body.stream == false
  end

  test "build_request respects temperature" do
    config = %{base_url: "https://x", model: "m", api_key: "k", max_output_tokens: 1024}
    req = ProviderFactory.build_request(config, [], temperature: 0.5)
    assert req.body.temperature == 0.5
  end
end
