import { createContext, useCallback, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, api, post, setCsrfToken, setUnauthorizedHandler } from "./api";
import type { User } from "./types";

interface AuthState {
  user: User | null;
  loading: boolean;
  setupRequired: boolean;
  setUser: (u: User | null) => void;
  logout: () => Promise<void>;
}

const AuthCtx = createContext<AuthState>({
  user: null,
  loading: true,
  setupRequired: false,
  setUser: () => {},
  logout: async () => {},
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUserState] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  const qc = useQueryClient();

  const setUser = useCallback((u: User | null) => {
    setCsrfToken(u?.csrf_token ?? null);
    setUserState(u);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      qc.clear();
    });
    (async () => {
      try {
        setUser(await api<User>("/api/auth/me"));
      } catch {
        try {
          const st = await api<{ setup_required: boolean }>("/api/auth/status");
          setSetupRequired(st.setup_required);
        } catch {
          /* server unreachable: show login */
        }
      } finally {
        setLoading(false);
      }
    })();
  }, [qc, setUser]);

  const logout = useCallback(async () => {
    try {
      await post("/api/auth/logout");
    } finally {
      setUser(null);
      qc.clear();
    }
  }, [qc, setUser]);

  return <AuthCtx.Provider value={{ user, loading, setupRequired, setUser, logout }}>{children}</AuthCtx.Provider>;
}

export const useAuth = () => useContext(AuthCtx);

function Brand({ subtitle }: { subtitle: string }) {
  return (
    <div className="auth-brand">
      <img className="logo" src="/favicon.svg" alt="" />
      <h1>MangaDex WebDL</h1>
      <p className="muted small">{subtitle}</p>
    </div>
  );
}

export function LoginPage() {
  const { setUser, setupRequired } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (setupRequired) return <SetupPage />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setUser(await post<User>("/api/auth/login", { username, password }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={submit} aria-label="Sign in">
        <Brand subtitle="Sign in to manage your library" />
        {error && (
          <div className="alert bad" role="alert">
            <div className="body">{error}</div>
          </div>
        )}
        <div className="field">
          <label htmlFor="username">Username</label>
          <input id="username" className="input" autoComplete="username" autoCapitalize="none" required value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" className="input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <button className="btn primary block" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export function SetupPage() {
  const { setUser } = useAuth();
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== password2) {
      setError("Passwords do not match");
      return;
    }
    try {
      setUser(await post<User>("/api/auth/setup", { setup_token: token.trim(), username, password }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Setup failed");
    }
  }

  return (
    <div className="auth-wrap">
      <form className="card auth-card" onSubmit={submit} aria-label="First-run setup">
        <Brand subtitle="First-run setup: create the administrator" />
        <p className="small muted">
          Find the one-time setup token in the application logs (<span className="mono">docker logs mangadex-webdl</span>).
        </p>
        {error && (
          <div className="alert bad" role="alert">
            <div className="body">{error}</div>
          </div>
        )}
        <div className="field">
          <label htmlFor="token">Setup token</label>
          <input id="token" className="input mono" autoComplete="off" autoCapitalize="none" required value={token} onChange={(e) => setToken(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="su">Username</label>
          <input id="su" className="input" autoComplete="username" autoCapitalize="none" required value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="sp">Password</label>
          <input id="sp" className="input" type="password" autoComplete="new-password" minLength={12} required value={password} onChange={(e) => setPassword(e.target.value)} />
          <span className="hint">At least 12 characters.</span>
        </div>
        <div className="field">
          <label htmlFor="sp2">Repeat password</label>
          <input id="sp2" className="input" type="password" autoComplete="new-password" minLength={12} required value={password2} onChange={(e) => setPassword2(e.target.value)} />
        </div>
        <button className="btn primary block">Create administrator</button>
      </form>
    </div>
  );
}
