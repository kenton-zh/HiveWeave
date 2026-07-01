defmodule HiveWeaveWeb.ErrorView do
  def render("404.json", _assigns) do
    %{error: "Not found"}
  end

  def render("500.json", _assigns) do
    %{error: "Internal server error"}
  end

  def render(template, _assigns) do
    %{error: template}
  end
end
