import { useState, useEffect } from "react";
import { getProjectGameTime } from "../api";

interface Props {
  projectId: string | null;
}

type GameTimeResponse = {
  formatted: string;
};

export default function ProjectTimeBadge({ projectId }: Props) {
  const [formatted, setFormatted] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setFormatted(null);
      return;
    }

    let cancelled = false;

    const poll = async () => {
      try {
        const data = (await getProjectGameTime(projectId)) as GameTimeResponse;
        if (!cancelled && data.formatted) {
          setFormatted(data.formatted);
        }
      } catch {
        // ignore transient poll errors
      }
    };

    void poll();
    const intervalId = window.setInterval(poll, 1000);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [projectId]);

  if (!projectId) {
    return null;
  }

  return (
    <div className="px-2.5 py-1 rounded-md bg-surface border border-surface-border text-xs text-gray-400 whitespace-nowrap shrink-0">
      项目时间 {formatted ?? "—"}
    </div>
  );
}
