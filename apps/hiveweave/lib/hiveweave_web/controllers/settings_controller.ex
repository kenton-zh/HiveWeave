defmodule HiveWeaveWeb.SettingsController do
  use Phoenix.Controller

  alias HiveWeave.Schema.GlobalSetting

  plug :accepts, ["json"]

  def index(conn, _params) do
    settings = HiveWeave.Repo.Meta.all(GlobalSetting) |> Enum.map(&serialize_setting/1)
    json(conn, %{settings: settings})
  end

  def show(conn, %{"key" => key}) do
    case HiveWeave.Repo.Meta.get_by(GlobalSetting, key: key) do
      nil ->
        conn
        |> put_status(404)
        |> json(%{error: "Not found"})
      setting ->
        json(conn, %{setting: serialize_setting(setting)})
    end
  end

  def upsert(conn, %{"key" => key, "value" => value}) do
    attrs = %{
      key: key,
      value: value,
      updated_at: System.system_time(:millisecond)
    }

    case HiveWeave.Repo.Meta.get_by(GlobalSetting, key: key) do
      nil ->
        %GlobalSetting{}
        |> GlobalSetting.changeset(attrs)
        |> HiveWeave.Repo.Meta.insert()
        |> case do
          {:ok, setting} -> json(conn, %{setting: serialize_setting(setting)})
          {:error, _} = _err -> json(conn, %{error: "Failed to upsert setting"}) |> Plug.Conn.put_status(500)
        end
      setting ->
        setting
        |> GlobalSetting.changeset(attrs)
        |> HiveWeave.Repo.Meta.update()
        |> case do
          {:ok, s} -> json(conn, %{setting: serialize_setting(s)})
          {:error, _} = _err -> json(conn, %{error: "Failed to upsert setting"}) |> Plug.Conn.put_status(500)
        end
    end
  end

  defp serialize_setting(nil), do: nil
  defp serialize_setting(setting) do
    %{key: setting.key, value: setting.value, updated_at: setting.updated_at}
  end
end

