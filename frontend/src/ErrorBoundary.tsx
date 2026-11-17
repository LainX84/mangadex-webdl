import { Component, type ErrorInfo, type ReactNode } from "react";
import errorArt from "./assets/error-art.webp";

interface Props {
  children: ReactNode;
  /** Full-screen fallback (outside the layout) instead of an inline card. */
  fullPage?: boolean;
}
interface State {
  error: Error | null;
}

/**
 * Catches render errors so a bug in one page shows a message instead of
 * unmounting the whole app and leaving a blank screen.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const box = (
      <div
        className="card"
        role="alert"
        style={{ maxWidth: 420, margin: "32px auto", textAlign: "center", overflow: "hidden", padding: 0 }}
      >
        <img
          src={errorArt}
          alt=""
          width={320}
          height={337}
          style={{ display: "block", width: "100%", maxWidth: 320, height: "auto", margin: "0 auto" }}
        />
        <div style={{ padding: "16px 20px 20px" }}>
          <h2 style={{ margin: "0 0 6px" }}>I'm so very sorry, but something went wrong.</h2>
          <p className="muted" style={{ margin: "0 0 8px" }}>This page hit an unexpected error. Your library and downloads are not affected.</p>
          <p className="muted small" style={{ margin: "0 0 16px", wordBreak: "break-word" }}>{error.message}</p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center" }}>
            {!this.props.fullPage && (
              <button type="button" className="btn" onClick={() => this.setState({ error: null })}>Try again</button>
            )}
            <button type="button" className="btn primary" onClick={() => window.location.reload()}>Reload page</button>
          </div>
        </div>
      </div>
    );
    return this.props.fullPage ? <div style={{ padding: 16 }}>{box}</div> : box;
  }
}
