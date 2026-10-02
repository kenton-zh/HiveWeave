import React from "react";
import ReactDOM from "react-dom/client";
import { MotionConfig } from "motion/react";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {/* reducedMotion="user"：motion 侧对齐系统「减弱动态效果」偏好
        （transform/位移类动画降级为瞬时，opacity 保留）——
        对应 index.css prefers-reduced-motion 块注释里
        「TSX 内 JS 驱动的动画须组件侧自查」的全局自查开关。 */}
    <MotionConfig reducedMotion="user">
      <App />
    </MotionConfig>
  </React.StrictMode>
);
