import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/Animate UI 组件约定的类名合并工具（components.json aliases.utils）。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
