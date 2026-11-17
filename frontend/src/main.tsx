import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import "@fontsource/m-plus-rounded-1c/latin-500.css";
import "@fontsource/m-plus-rounded-1c/latin-700.css";
import "@fontsource/m-plus-rounded-1c/latin-800.css";
import "@fontsource/dela-gothic-one/latin-400.css";
import "./styles.css";
import "./skin-ink.css";
import { applySkin, getSkin } from "./skin";

// Apply the saved skin before the first render (no flash of the other look).
applySkin(getSkin());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary fullPage>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
