import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import Shell from "./components/Shell";
import { Dashboard, Keys, Fleet, Models, Deploy, EditModel, Catalog, Cache, Usage, Playground, Settings } from "./pages";

const qc = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
});

// SPA history routing under /ui/ (Caddy serves the app at that base).
const router = createBrowserRouter(
  [
    {
      path: "/",
      element: <Shell />,
      children: [
        { index: true, element: <Navigate to="/dashboard" replace /> },
        { path: "dashboard", element: <Dashboard /> },
        { path: "keys", element: <Keys /> },
        { path: "workers", element: <Fleet /> },
        { path: "fleet", element: <Navigate to="/workers" replace /> },
        { path: "models", element: <Models /> },
        { path: "deploy", element: <Deploy /> },
        { path: "models/:id/edit", element: <EditModel /> },
        { path: "catalog", element: <Catalog /> },
        { path: "cache", element: <Cache /> },
        { path: "usage", element: <Usage /> },
        { path: "playground", element: <Playground /> },
        { path: "settings", element: <Settings /> },
      ],
    },
  ],
  { basename: "/" },
);

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
