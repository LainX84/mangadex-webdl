import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router";
import { ApiError } from "./api";
import { AuthProvider, LoginPage, useAuth } from "./auth";
import { Loading, ToastProvider } from "./components";
import Layout from "./Layout";
import DashboardPage from "./pages/Dashboard";
import LibraryPage from "./pages/Library";
import AddPage from "./pages/Add";
import MangaPage from "./pages/Manga";
import QueuePage from "./pages/Queue";
import SettingsPage from "./pages/Settings";
import ReviewPage from "./pages/Review";
import AboutPage from "./pages/About";
import ApprovalsPage from "./pages/Approvals";
import StatsPage from "./pages/Stats";

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
        staleTime: 5000,
        refetchOnWindowFocus: true,
      },
    },
  });
}

function Gate() {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <LoginPage />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="library" element={<LibraryPage />} />
        <Route path="add" element={<AddPage />} />
        <Route path="manga/:id" element={<MangaPage />} />
        <Route path="queue" element={<QueuePage />} />
        <Route path="stats" element={<StatsPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="about" element={<AboutPage />} />
        <Route path="*" element={<DashboardPage />} />
      </Route>
    </Routes>
  );
}

export default function App({ client }: { client?: QueryClient }) {
  return (
    <QueryClientProvider client={client ?? makeQueryClient()}>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <Gate />
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
